"""Mode state machine (§5.1), insight-landing detection (§5.3), write-mode policy (§5.5).
Pure functions over the session row so they are unit-testable without an LLM."""
from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime, timezone

from ..models import MODES, JournalSession

TALK_MODES = {m for m in MODES if m not in ("write", "you_decide")}

INSIGHT_TRIGGERS = [
    r"^\s*oh\.", r"^\s*wait\.", r"^\s*actually\b", r"\bi think what i['’]m really saying\b", r"\bi hadn['’]t\b",
    r"\bi don['’]t actually want\b", r"^\s*huh\.", r"\bi just realized\b", r"\bthat['’]s not it\b",
]
_INSIGHT_RE = [re.compile(p, re.IGNORECASE) for p in INSIGHT_TRIGGERS]


def detect_insight(text: str) -> bool:
    return any(rx.search(text) for rx in _INSIGHT_RE)


@dataclass
class ModeDecision:
    mode: str                # mode to generate under
    first_talk: bool         # no mode had been set this session before this turn
    forbid_menu: bool        # style gate must reject any choice menu
    awaiting_model_choice: bool  # you_decide: parse the model's <mode> tag afterwards


def apply_intent(session: JournalSession, intent: str, mode: str | None, now: datetime | None = None) -> ModeDecision:
    """Mutates session.mode/mode_set_* per §5.1 and returns what the generator needs to know."""
    now = now or datetime.now(timezone.utc)
    if mode is not None and mode not in MODES:
        raise ValueError(f"unknown mode {mode!r}")
    first_talk = not bool(session.talked)

    if intent == "write":
        # (b) write intent resets to write; the talk-mode memory is cleared
        session.mode = "write"
        session.mode_set_at = now
        session.mode_set_by = "user"
        return ModeDecision("write", first_talk, True, False)

    if intent == "you_decide" or mode == "you_decide":
        session.mode = "you_decide"
        session.mode_set_at = now
        session.mode_set_by = "user"
        return ModeDecision("you_decide", first_talk, True, True)

    # intent == "talk"
    if mode is not None and mode != "write":
        session.mode = mode
        session.mode_set_at = now
        session.mode_set_by = "user"
        return ModeDecision(mode, first_talk, not first_talk, False)
    if session.mode in TALK_MODES:
        # (a) persists across turns; the menu must not be re-offered (Test I)
        return ModeDecision(session.mode, False, True, False)
    session.mode = "open"
    session.mode_set_at = now
    session.mode_set_by = "user"
    return ModeDecision("open", first_talk, not first_talk, False)


def set_mode_explicit(session: JournalSession, mode: str, now: datetime | None = None) -> None:
    if mode not in MODES:
        raise ValueError(f"unknown mode {mode!r}")
    session.mode = mode
    session.mode_set_at = now or datetime.now(timezone.utc)
    session.mode_set_by = "user"


_MODE_TAG_RE = re.compile(r"^\s*<mode>\s*([a-z_]+)\s*</mode>\s*", re.IGNORECASE)


def extract_model_mode(text: str) -> tuple[str, str | None]:
    """For you_decide: strip the leading <mode>NAME</mode> tag; return (clean_text, mode|None)."""
    m = _MODE_TAG_RE.match(text)
    if not m:
        return text, None
    mode = m.group(1).lower()
    if mode not in TALK_MODES:
        mode = None
    return text[m.end():], mode


def record_model_choice(session: JournalSession, mode: str | None, now: datetime | None = None) -> str:
    """After a you_decide turn. Falls back to 'reflect' if the model didn't tag — a judgment was still made."""
    chosen = mode or "reflect"
    session.mode = chosen
    session.mode_set_at = now or datetime.now(timezone.utc)
    session.mode_set_by = "model"
    return chosen


@dataclass
class WritePolicy:
    respond: bool
    ack: bool = False          # minimal acknowledgment after N silent entries
    max_sentences: int = 2
    max_questions: int = 0


def write_policy(word_count: int, body: str, silent_streak: int, *, min_words: int = 30,
                 streak_before_ack: int = 3) -> WritePolicy:
    ends_with_question = body.rstrip().endswith("?")
    if word_count < min_words and not ends_with_question:
        # silent_streak counts entries already left silent; this one makes streak+1
        if silent_streak + 1 >= streak_before_ack:
            return WritePolicy(respond=True, ack=True, max_sentences=1, max_questions=0)
        return WritePolicy(respond=False)
    return WritePolicy(respond=True, max_sentences=2, max_questions=0 if not ends_with_question else 1)
