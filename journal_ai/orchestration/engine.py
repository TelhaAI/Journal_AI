"""Turn engine: the router in plan §2.2. One entry point per API route.

Design note on streaming: Gate 1 is post-generation, so the engine generates the
full response, runs the gate (and the single permitted regeneration), and only then
hands the final text to the SSE layer, which emits it in token-sized chunks. True
token-by-token streaming of an ungated draft would let a violating response reach
the screen before the gate could act; V1 accepts the small first-token latency cost.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..config import get_settings
from ..gates import SafetyGate, StyleGate
from ..llm import LLMProvider, Message
from ..models import AITurn, Entry, Event, JournalSession, PromptVersion, Volume
from ..prompts import active_version
from . import modes as M
from . import sessions as S


@dataclass
class TurnResult:
    entry: Entry | None
    session: JournalSession
    ai_turn: AITurn | None
    path: str  # write_silent | write | ack | talk | safety
    mode: str
    system_blocks: list[str] = field(default_factory=list)  # for tests / debugging; not returned to clients


class TurnEngine:
    def __init__(self, provider: LLMProvider, style_gate: StyleGate | None = None,
                 safety_gate: SafetyGate | None = None):
        s = get_settings()
        self.provider = provider
        self.style_gate = style_gate or StyleGate(max_questions=s.style_max_questions,
                                                  hard_sentence_cap=s.style_hard_sentence_cap,
                                                  max_menu_options=s.style_max_menu_options)
        self.safety_gate = safety_gate or SafetyGate(
            classifier=provider if s.safety_model else None, classifier_model=s.safety_model)
        self.settings = s

    # ------------------------------------------------------------------ public
    async def submit(self, db: Session, user_id: str, body: str, *, created_at_local: datetime, tz: str,
                     intent: str, mode: str | None = None, volume_id: str | None = None,
                     session_id: str | None = None, supersedes_entry_id: str | None = None,
                     now: datetime | None = None) -> TurnResult:
        """POST /entries and POST /sessions/{id}/turns both land here; the difference is `intent`
        (every submission is an entry — the user's words are the record)."""
        now = now or datetime.now(timezone.utc)
        self._now = now
        S.ensure_user(db, user_id)
        if volume_id:
            volume = db.get(Volume, volume_id)
            if volume is None or volume.user_id != user_id:
                raise KeyError(volume_id)
        else:
            volume = S.current_volume(db, user_id, created_at_local, now)
        session = S.get_or_open_session(db, user_id, volume, now, session_id)

        if supersedes_entry_id:
            old = db.get(Entry, supersedes_entry_id)
            if old is None or old.user_id != user_id:
                raise KeyError(supersedes_entry_id)

        entry = Entry(user_id=user_id, volume_id=volume.id, body=body, word_count=len(body.split()),
                      created_at_utc=now, created_at_local=created_at_local, tz=tz,
                      source="write" if intent == "write" else "talk", session_id=session.id,
                      supersedes_entry_id=supersedes_entry_id)
        db.add(entry)
        db.flush()
        S.record_entry_in_volume(db, volume, entry, session)
        session.turn_count += 1
        session.last_activity_at = now
        S.mark_substantive(db, session)
        db.add(Event(user_id=user_id, session_id=session.id, type="entry_created",
                     payload={"entry_id": entry.id, "intent": intent, "words": entry.word_count}))

        # Gate 3 — safety, before anything else
        verdict = await self.safety_gate.check(body)
        if verdict.risk:
            return await self._safety_path(db, user_id, session, entry, verdict.source, now)

        decision = M.apply_intent(session, intent, mode, now)
        if intent == "write":
            return await self._write_path(db, user_id, volume, session, entry, decision, now)
        return await self._talk_path(db, user_id, volume, session, entry, decision, now)

    # ------------------------------------------------------------------ paths
    async def _safety_path(self, db, user_id, session, entry, source, now) -> TurnResult:
        pv = active_version(db)
        system = [pv.addenda.get("safety", "The person may be at immediate risk. Respond with care and point them "
                                            "to immediate help.")]
        gen = await self.provider.generate(system, [Message("user", entry.body)], model=self.settings.llm_model,
                                           max_tokens=400, temperature=0.3)
        turn = self._store_turn(db, user_id, session, entry, gen, pv, "safety", "safety", [], False)
        db.add(Event(user_id=user_id, session_id=session.id, type="safety_triggered",
                     payload={"source": source, "entry_id": entry.id}))
        return TurnResult(entry, session, turn, "safety", "safety", system)

    async def _write_path(self, db, user_id, volume, session, entry, decision, now) -> TurnResult:
        s = self.settings
        policy = M.write_policy(entry.word_count, entry.body, session.silent_write_streak,
                                min_words=s.write_min_words_for_response,
                                streak_before_ack=s.write_silent_streak_before_ack)
        if not policy.respond:
            session.silent_write_streak += 1
            return TurnResult(entry, session, None, "write_silent", "write")
        session.silent_write_streak = 0
        pv = active_version(db)
        addendum_key = "write_ack" if policy.ack else None
        system = self._assemble(db, pv, volume, session, "write", decision, extra_key=addendum_key)
        messages = self._history(db, session)
        turn, blocks = await self._generate_gated(db, user_id, session, entry, pv, system, messages,
                                                  mode="write", path="ack" if policy.ack else "write",
                                                  max_questions=policy.max_questions,
                                                  sentence_cap=policy.max_sentences, forbid_menu=True)
        return TurnResult(entry, session, turn, "ack" if policy.ack else "write", "write", blocks)

    async def _talk_path(self, db, user_id, volume, session, entry, decision: M.ModeDecision, now) -> TurnResult:
        pv = active_version(db)
        session.silent_write_streak = 0
        hold = M.detect_insight(entry.body)
        if hold:
            session.hold = True
            db.add(Event(user_id=user_id, session_id=session.id, type="insight_signal",
                         payload={"entry_id": entry.id}))
        system = self._assemble(db, pv, volume, session, decision.mode, decision, hold=session.hold)
        messages = self._history(db, session)
        max_q = 0 if session.hold else self.settings.style_max_questions
        cap = 3 if session.hold else self.settings.style_hard_sentence_cap
        turn, blocks = await self._generate_gated(db, user_id, session, entry, pv, system, messages,
                                                  mode=decision.mode, path="talk", max_questions=max_q,
                                                  sentence_cap=cap, forbid_menu=decision.forbid_menu,
                                                  awaiting_model_choice=decision.awaiting_model_choice)
        session.hold = False  # one-turn flag
        session.talked = True
        return TurnResult(entry, session, turn, "talk", session.mode, blocks)

    # ------------------------------------------------------------------ prompt assembly (§5.2)
    def _assemble(self, db: Session, pv: PromptVersion, volume: Volume, session: JournalSession, mode: str,
                  decision: M.ModeDecision, *, hold: bool = False, extra_key: str | None = None) -> list[str]:
        add = pv.addenda or {}
        blocks = [pv.body]
        mode_add = (add.get("modes") or {}).get(mode)
        if mode_add:
            blocks.append(mode_add)
        if extra_key and add.get(extra_key):
            blocks.append(add[extra_key])
        if decision.first_talk and mode not in ("write", "you_decide") and add.get("first_talk"):
            blocks.append(add["first_talk"])
        blocks.append(self._context_block(db, volume, session))
        if hold and add.get("insight_hold"):
            blocks.append(add["insight_hold"])
        if session.preservation_pending and add.get("preservation"):
            blocks.append(add["preservation"])
        if session.rollover_pending and add.get("rollover"):
            blocks.append(add["rollover"])
        return blocks

    def _context_block(self, db: Session, volume: Volume, session: JournalSession) -> str:
        n = self.settings.recent_entries_in_context
        recent = db.scalars(select(Entry).where(Entry.volume_id == volume.id, Entry.session_id != session.id)
                            .order_by(Entry.created_at_local.desc()).limit(n)).all()
        recent = list(reversed(recent))
        lines = [f"CONTEXT\nVolume: {volume.title}"]
        if recent:
            lines.append(f"Earlier entries in this volume (most recent {len(recent)}, full text, dated — the "
                         f"record, not a summary):")
            for e in recent:
                lines.append(f"[{e.created_at_local.strftime('%Y-%m-%d')}] {e.body}")
        else:
            lines.append("No earlier entries in this volume.")
        return "\n".join(lines)

    @staticmethod
    def _history(db: Session, session: JournalSession) -> list[Message]:
        entries = db.scalars(select(Entry).where(Entry.session_id == session.id)).all()
        turns = db.scalars(select(AITurn).where(AITurn.session_id == session.id)).all()
        items = [(S._aware(e.created_at_utc), 0, "user", e.body) for e in entries] + \
                [(S._aware(t.created_at), 1, "assistant", t.body) for t in turns if t.body]
        items.sort(key=lambda x: (x[0], x[1]))
        msgs: list[Message] = []
        for _, _, role, content in items:
            if msgs and msgs[-1].role == role:  # merge consecutive same-role turns (silent write entries)
                msgs[-1] = Message(role, msgs[-1].content + "\n\n" + content)
            else:
                msgs.append(Message(role, content))
        if not msgs or msgs[-1].role != "user":
            msgs.append(Message("user", "(continue)"))
        return msgs

    # ------------------------------------------------------------------ generation + Gate 1
    async def _generate_gated(self, db, user_id, session, entry, pv, system, messages, *, mode, path,
                              max_questions, sentence_cap, forbid_menu, awaiting_model_choice=False):
        model = self.settings.llm_model
        gen = await self.provider.generate(system, messages, model=model, max_tokens=500)
        text, chosen = (M.extract_model_mode(gen.text) if awaiting_model_choice else (gen.text, None))
        res = self.style_gate.check(text, max_questions=max_questions, hard_sentence_cap=sentence_cap,
                                    forbid_menu=forbid_menu)
        violations = []
        regenerated = False
        if not res.ok and text.strip():
            violations.append({"attempt": 1, "violations": res.violations})
            regenerated = True
            gen2 = await self.provider.generate(system + [res.tightening_instruction()], messages, model=model,
                                                max_tokens=500)
            text2, chosen2 = (M.extract_model_mode(gen2.text) if awaiting_model_choice else (gen2.text, None))
            res2 = self.style_gate.check(text2, max_questions=max_questions, hard_sentence_cap=sentence_cap,
                                         forbid_menu=forbid_menu)
            if not res2.ok:
                violations.append({"attempt": 2, "violations": res2.violations})
                db.add(Event(user_id=user_id, session_id=session.id, type="style_violation",
                             payload={"violations": res2.violations, "mode": mode}))
            gen, text, chosen = gen2, text2, (chosen2 or chosen)
        if awaiting_model_choice:
            final_mode = M.record_model_choice(session, chosen)
            db.add(Event(user_id=user_id, session_id=session.id, type="mode_changed",
                         payload={"mode": final_mode, "by": "model"}))
            mode = final_mode
        if session.preservation_pending:
            session.preservation_pending = False
            db.add(Event(user_id=user_id, session_id=session.id, type="preservation_offered", payload={}))
        if session.rollover_pending:
            session.rollover_pending = False
            db.add(Event(user_id=user_id, session_id=session.id, type="volume_rollover_served", payload={}))
        gen.text = text
        turn = self._store_turn(db, user_id, session, entry, gen, pv, mode, path, violations, regenerated)
        return turn, system

    def _store_turn(self, db, user_id, session, entry, gen, pv, mode, path, violations, regenerated) -> AITurn:
        turn = AITurn(created_at=getattr(self, "_now", None) or datetime.now(timezone.utc), session_id=session.id, user_id=user_id, in_reply_to_entry_id=entry.id if entry else None,
                      body=gen.text.strip(), prompt_version_id=pv.id, model=gen.model, mode_at_generation=mode,
                      input_tokens=gen.input_tokens, output_tokens=gen.output_tokens, latency_ms=gen.latency_ms,
                      style_violations=violations, regenerated=regenerated, path=path)
        db.add(turn)
        db.flush()
        return turn
