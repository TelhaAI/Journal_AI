"""Gate 1 — style gate (plan §2.3). Post-generation lint on every TALK response.

This is lint, not verification: it catches the mechanically checkable parts of
RESPONSE STYLE and leaves judgment to the behavioral evals.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field

BANNED_PHRASES = [
    "lean into",
    "trust the process",
    "healing journey",
    "step into your power",
    "live your best life",
    "you deserve",
    "life is too short",
    "prioritize yourself",
    "silver lining",
    "self-care",
    "safe space",
    "hold space",
    "do the work",
    "doing the work",
    "how far you've come",
    "how far you have come",
]

# Praise-of-vulnerability lexicon: these words are banned outright when addressed to the
# person's act of sharing/writing. "healthy" and "important" are only banned when applied
# to journaling/writing in the same sentence.
PRAISE_WORDS = ["brave", "bravery", "courageous", "courage", "healing", "powerful", "vulnerable", "vulnerability"]
JOURNALING_WORDS = ["journal", "journaling", "writing", "write", "wrote", "sharing", "share", "opening up"]
CONDITIONAL_PRAISE = ["healthy", "important"]

_WORD_RE_CACHE: dict[str, re.Pattern] = {}
_JOURNAL_ACT = r"(?:journal\w*|writ(?:e|ing|ten)(?:\s+(?:this|it|that|things)\s+(?:down|out))?|shar(?:e|ing)|open(?:ing)?\s+up|put(?:ting)?\s+(?:this|it)\s+(?:down|into\s+words))"
_CONDITIONAL_RE = {
    w: re.compile(
        rf"\b{_JOURNAL_ACT}\b[^.!?]{{0,40}}\b(?:is|was|are|feels|seems|can\s+be|so|really|very)\s+(?:a\s+|an\s+)?{w}\b"
        rf"|\b{w}\b[^.!?]{{0,25}}\b(?:to|that\s+you|for\s+you\s+to)\s+{_JOURNAL_ACT}\b",
        re.IGNORECASE)
    for w in CONDITIONAL_PRAISE
}


def _phrase_re(phrase: str) -> re.Pattern:
    if phrase not in _WORD_RE_CACHE:
        # tolerate any whitespace/punctuation between words and curly apostrophes
        parts = [re.escape(p).replace("'", "['’]") for p in phrase.split()]
        _WORD_RE_CACHE[phrase] = re.compile(r"\b" + r"[\s\-,]+".join(parts) + r"\b", re.IGNORECASE)
    return _WORD_RE_CACHE[phrase]


_QUOTE_SPAN_RE = re.compile(r"\"[^\"\n]{1,400}\"|“[^”\n]{1,400}”|‘[^’\n]{1,400}’")
_SENT_SPLIT_RE = re.compile(r"(?<=[.!?])[\"”’)]?\s+(?=[A-Z\"“(])")
_BULLET_RE = re.compile(r"^\s*(?:[-*•–]|\d+[.)]|[a-zA-Z][.)])\s+\S", re.MULTILINE)
_INLINE_LIST_RE = re.compile(
    r"((?:[A-Za-z][^,.;:?!\n]{1,60}),\s*)+(?:or|and)\s+[A-Za-z][^.;:?!\n]{1,60}", re.IGNORECASE
)
_OR_PAIR_RE = re.compile(
    r"\b(?:would you (?:like|prefer|rather)|do you want|want me to|should I|I can|I could|we could)\b[^.?!\n]*?\bor\b",
    re.IGNORECASE,
)
_NUMBER_RE = re.compile(r"\b\d+\.\d+\b")


def strip_quotes(text: str) -> str:
    """Remove quoted spans so question marks and phrases inside user-quoted text aren't counted."""
    return _QUOTE_SPAN_RE.sub(" ", text)


def count_questions(text: str) -> int:
    body = strip_quotes(text)
    return len(re.findall(r"\?+", body))


def count_sentences(text: str) -> int:
    body = _NUMBER_RE.sub("0", text.strip())
    if not body:
        return 0
    parts = [p for p in _SENT_SPLIT_RE.split(body) if p.strip()]
    # count any bullet lines as sentences too
    return max(len(parts), 1)


def count_menu_options(text: str) -> int:
    """Best-effort count of options offered to the user. 0 means no menu detected."""
    bullets = _BULLET_RE.findall(text)
    if len(bullets) >= 2:
        return len(bullets)
    best = 0
    for m in _INLINE_LIST_RE.finditer(text):
        span = m.group(0)
        items = re.split(r",\s*(?:or\s+|and\s+)?|\s+(?:or|and)\s+", span)
        items = [i for i in items if i.strip()]
        if len(items) >= 2 and _looks_like_offer(text, m.start()):
            best = max(best, len(items))
    if best == 0 and _OR_PAIR_RE.search(text):
        best = 2
    return best


def _looks_like_offer(text: str, pos: int) -> bool:
    window = text[max(0, pos - 120): pos + 10].lower()
    cues = ("i can", "i could", "we could", "would you", "do you want", "want me", "should i", "let me know",
            "you can", "options", "either", "pick", "choose", "up to you", "or i can")
    return any(c in window for c in cues) or "?" in text[pos: pos + 200]


def find_banned_phrases(text: str) -> list[str]:
    found = [p for p in BANNED_PHRASES if _phrase_re(p).search(text)]
    for w in PRAISE_WORDS:
        if _phrase_re(w).search(text):
            found.append(w)
    for w in CONDITIONAL_PRAISE:
        if _CONDITIONAL_RE[w].search(text):
            found.append(f"{w} (applied to journaling)")
    return found


@dataclass
class StyleResult:
    ok: bool
    violations: list[dict] = field(default_factory=list)
    question_count: int = 0
    sentence_count: int = 0
    option_count: int = 0
    banned: list[str] = field(default_factory=list)

    def tightening_instruction(self) -> str:
        parts = []
        for v in self.violations:
            parts.append(v["fix"])
        return "Rewrite your reply. " + " ".join(parts)


class StyleGate:
    def __init__(self, *, max_questions: int = 1, hard_sentence_cap: int = 8, max_menu_options: int = 3):
        self.max_questions = max_questions
        self.hard_sentence_cap = hard_sentence_cap
        self.max_menu_options = max_menu_options

    def check(self, text: str, *, max_questions: int | None = None, hard_sentence_cap: int | None = None,
              forbid_menu: bool = False, max_menu_options: int | None = None) -> StyleResult:
        mq = self.max_questions if max_questions is None else max_questions
        cap = self.hard_sentence_cap if hard_sentence_cap is None else hard_sentence_cap
        mo = self.max_menu_options if max_menu_options is None else max_menu_options

        res = StyleResult(ok=True)
        res.banned = find_banned_phrases(text)
        res.question_count = count_questions(text)
        res.sentence_count = count_sentences(text)
        res.option_count = count_menu_options(text)

        if res.banned:
            res.violations.append({"type": "banned_phrase", "detail": res.banned,
                                   "fix": f"Do not use these words or phrases: {', '.join(res.banned)}."})
        if res.question_count > mq:
            res.violations.append({"type": "question_count", "detail": res.question_count,
                                   "fix": f"Ask at most {mq} question." if mq else "Ask no questions at all."})
        if res.sentence_count > cap:
            res.violations.append({"type": "sentence_cap", "detail": res.sentence_count,
                                   "fix": f"Use at most {cap} sentences; fewer is better."})
        if forbid_menu and res.option_count >= 2:
            res.violations.append({"type": "menu_forbidden", "detail": res.option_count,
                                   "fix": "Do not offer options or ask the person to choose. Decide, and respond."})
        elif res.option_count > mo:
            res.violations.append({"type": "menu_count", "detail": res.option_count,
                                   "fix": f"Offer at most {mo} options, in one sentence, or none."})
        res.ok = not res.violations
        return res
