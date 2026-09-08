"""Look Back orchestration: evidence → LLM → provenance gate → receipt (plan §2.3, §2.4, §3)."""
from __future__ import annotations

import json
import re
from dataclasses import dataclass
from datetime import datetime

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..config import get_settings
from ..gates.provenance import GATE_VERSION, ProvenanceGate, log_bytes
from ..llm import LLMProvider, Message
from ..models import Entry, Event, LookbackReceipt, LookbackReport, PromptVersion, Volume
from ..prompts import active_version
from .evidence import EntryView, build_evidence_table, content_hash

FALLBACK_MESSAGE = "I looked, and I can't point to enough in your own words to say anything I'd stand behind yet."
INSUFFICIENT_MESSAGE = "There isn't enough writing here yet for me to look back over. Keep going; I'll be able to point to more once there's more."

_JSON_RE = re.compile(r"\{.*\}", re.DOTALL)


def parse_model_json(text: str) -> dict:
    text = text.strip()
    if text.startswith("```"):
        text = re.sub(r"^```(?:json)?\s*|\s*```$", "", text, flags=re.IGNORECASE)
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        m = _JSON_RE.search(text)
        if m:
            try:
                return json.loads(m.group(0))
            except json.JSONDecodeError:
                pass
    return {"observations": [], "closing": "", "_parse_error": True}


@dataclass
class LookbackOutcome:
    report: LookbackReport
    receipt: LookbackReceipt
    evidence: dict


def select_entries(session: Session, user_id: str, volume_ids: list[str] | None, start: datetime | None,
                   end: datetime | None) -> list[Entry]:
    q = select(Entry).where(Entry.user_id == user_id)
    if volume_ids:
        q = q.where(Entry.volume_id.in_(volume_ids))
    if start:
        q = q.where(Entry.created_at_local >= start)
    if end:
        q = q.where(Entry.created_at_local <= end)
    rows = session.scalars(q.order_by(Entry.created_at_local, Entry.created_at_utc, Entry.id)).all()
    # superseded rows are excluded: the record is the latest version of each entry
    superseded = {r.supersedes_entry_id for r in rows if r.supersedes_entry_id}
    return [r for r in rows if r.id not in superseded]


def to_views(rows: list[Entry]) -> list[EntryView]:
    return [EntryView(id=r.id, body=r.body, created_at_local=r.created_at_local) for r in rows]


def _entries_block(views: list[EntryView], budget_tokens: int) -> tuple[str, list[EntryView]]:
    """Raw entries, dated, newest-last. If over budget, keep the most recent and say so (I2: never summarize)."""
    kept: list[EntryView] = []
    used = 0
    for v in reversed(views):
        cost = len(v.body) // 4 + 20
        if used + cost > budget_tokens and kept:
            break
        kept.append(v)
        used += cost
    kept.reverse()
    lines = [f"[entry {v.id} — {v.date}]\n{v.body}" for v in kept]
    return "\n\n".join(lines), kept


class LookbackService:
    def __init__(self, provider: LLMProvider, gate: ProvenanceGate | None = None):
        s = get_settings()
        self.provider = provider
        self.gate = gate or ProvenanceGate(min_entries_for_pattern=s.lookback_min_entries_for_pattern)
        self.settings = s

    async def run(self, session: Session, user_id: str, *, volume_ids: list[str] | None = None,
                  start: datetime | None = None, end: datetime | None = None) -> LookbackOutcome:
        s = self.settings
        pv: PromptVersion = active_version(session)
        model = s.lookback_model or s.llm_model
        rows = select_entries(session, user_id, volume_ids, start, end)
        views = to_views(rows)
        evidence = build_evidence_table(views)
        vol_ids = sorted({r.volume_id for r in rows}) if not volume_ids else volume_ids

        report = LookbackReport(user_id=user_id, volume_ids=vol_ids, range_start=start, range_end=end,
                                prompt_version_id=pv.id, model=model)
        receipt = LookbackReceipt(user_id=user_id, input_entry_ids=[v.id for v in views],
                                  input_content_hash=content_hash(views), evidence_table=evidence,
                                  prompt_version_id=pv.id, model=model, gate_version=GATE_VERSION)

        # Test K: not enough history. No model call; fail-closed by construction.
        if len(views) < 3:
            report.observations, report.message = [], INSUFFICIENT_MESSAGE
            receipt.raw_model_output, receipt.verification_log, receipt.dropped = [], [], []
            receipt.outcome = "fallback"
            return self._persist(session, report, receipt, evidence, "insufficient_history")

        system_base = [pv.body, pv.addenda.get("lookback_system", "")]
        entries_text, kept = _entries_block(views, s.lookback_context_token_budget)
        range_note = ""
        if len(kept) < len(views):
            range_note = (f"NOTE: only the most recent {len(kept)} of {len(views)} entries are shown in full. "
                          "Cite only entries you can see.")
        user_msg = (f"EVIDENCE TABLE (computed from the record):\n{json.dumps(evidence, sort_keys=True)}\n\n"
                    f"{range_note}\nENTRIES:\n{entries_text}\n\nProduce the Look Back JSON now.")

        raw_outputs: list = []
        gate_results = []
        messages = [Message("user", user_msg)]
        gen = await self.provider.generate(system_base, messages, model=model, max_tokens=1500, temperature=0.4)
        raw = parse_model_json(gen.text)
        raw_outputs.append(raw)
        gr = self.gate.run(raw, views)
        gate_results.append(gr)
        regenerated = False

        if gr.total == 0 or gr.drop_fraction > s.lookback_drop_threshold:
            regenerated = True
            dropped_names = "; ".join(d.get("text", d.get("reason", ""))[:120] for d in gr.dropped) or "all"
            regen_addendum = pv.addenda.get("lookback_regenerate", "").replace("{dropped}", dropped_names)
            gen2 = await self.provider.generate(system_base + [regen_addendum], messages, model=model,
                                                max_tokens=1500, temperature=0.4)
            raw2 = parse_model_json(gen2.text)
            raw_outputs.append(raw2)
            gr = self.gate.run(raw2, views)
            gate_results.append(gr)

        # outcome per §2.3 rule 5 — never a third attempt
        if gr.total == 0 or gr.drop_fraction > s.lookback_drop_threshold:
            if regenerated and gr.served and gr.total and gr.drop_fraction <= 0.75:
                outcome, observations, message = "partial", gr.served, None
            else:
                outcome, observations, message = "fallback", [], FALLBACK_MESSAGE
        else:
            outcome, observations = ("served" if not gr.dropped else "partial"), gr.served
            message = None
        closing = ""
        if outcome != "fallback":
            closing = str(raw_outputs[-1].get("closing", "") or "") if isinstance(raw_outputs[-1], dict) else ""
            if re.search(r"\b(growth|journey|progress|healing|transformation|breakthrough)\b", closing, re.I):
                closing = ""

        report.observations = observations
        report.message = message or (closing or None)
        receipt.raw_model_output = raw_outputs
        receipt.verification_log = [g.log for g in gate_results]
        receipt.dropped = [g.dropped for g in gate_results]
        receipt.regenerated = regenerated
        receipt.outcome = outcome
        for g in gate_results:
            for d in g.dropped:
                session.add(Event(user_id=user_id, type="citation_dropped", payload={"reason": d.get("reason")}))
        return self._persist(session, report, receipt, evidence, outcome)

    @staticmethod
    def _persist(session: Session, report: LookbackReport, receipt: LookbackReceipt, evidence: dict,
                 outcome: str) -> LookbackOutcome:
        session.add(report)
        session.flush()
        receipt.report_id = report.id
        session.add(receipt)
        if outcome in ("fallback", "insufficient_history"):
            session.add(Event(user_id=report.user_id, type="lookback_fallback", payload={"reason": outcome}))
        session.flush()
        return LookbackOutcome(report=report, receipt=receipt, evidence=evidence)


# --------------------------------------------------------------------------- replay (U18, U19)


class RecordChangedError(RuntimeError):
    pass


def replay_receipt(session: Session, receipt: LookbackReceipt, *, gate: ProvenanceGate | None = None) -> bool:
    """Rerun the gate from the receipt's inputs. Returns True iff verification_log is byte-identical.

    Raises RecordChangedError if the entry set no longer hashes to input_content_hash — replay
    against a changed record is refused rather than silently recomputed (U19).
    """
    if receipt.gate_version != GATE_VERSION:
        raise RuntimeError(f"receipt gate_version {receipt.gate_version} != running {GATE_VERSION}; "
                           "check out that gate version to replay")
    rows = session.scalars(select(Entry).where(Entry.id.in_(receipt.input_entry_ids))).all()
    by_id = {r.id: r for r in rows}
    views = [EntryView(id=i, body=by_id[i].body, created_at_local=by_id[i].created_at_local)
             for i in receipt.input_entry_ids if i in by_id]
    if len(views) != len(receipt.input_entry_ids) or content_hash(views) != receipt.input_content_hash:
        raise RecordChangedError("record changed since this receipt was written")
    gate = gate or ProvenanceGate(min_entries_for_pattern=get_settings().lookback_min_entries_for_pattern)
    logs = [gate.run(raw, views).log for raw in receipt.raw_model_output]
    return log_bytes(logs) == log_bytes(receipt.verification_log)


def volume_entry_ids(session: Session, volume: Volume) -> list[str]:
    return [e.id for e in select_entries(session, volume.user_id, [volume.id], None, None)]
