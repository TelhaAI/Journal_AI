"""Gate 3 — safety gate (plan §2.3). Runs on every user input before the normal prompt path.

Two layers: a lexical screen (always on, deterministic, testable) and an optional
small-model classifier (JOURNAL_SAFETY_MODEL) for wording the screen misses. Either
firing routes the turn to the safety path. Difficult-but-not-crisis writing (Test E)
must pass through untouched, so the lexical list is about expressed intent to
harm oneself or others, not about sadness.
"""
from __future__ import annotations

import re
from dataclasses import dataclass

from ..llm import LLMProvider, Message

_INTENT_PATTERNS = [
    r"\b(kill|end|take)\s+(my\s*self|my\s+own\s+life|my\s+life)\b",
    r"\bend\s+it\s+all\b",
    r"\b(want|wanted|wanting|going|plan(?:ning)?|ready)\s+to\s+(die|be\s+dead|not\s+(?:be\s+)?(?:alive|here|exist|wake\s+up))\b",
    r"\bsuicid(e|al)\b",
    r"\b(don'?t|do\s+not)\s+want\s+to\s+(be\s+alive|live|wake\s+up|exist|be\s+here)\s*(anymore|any\s+more|tomorrow)?\b",
    r"\bbetter\s+off\s+(dead|without\s+me)\b",
    r"\bno\s+reason\s+to\s+(live|keep\s+going|go\s+on)\b",
    r"\b(hurt|harm|cut)\s+my\s*self\b",
    r"\bself[\s-]?harm\b",
    r"\bnot\s+worth\s+living\b",
    r"\b(overdose|od)\s+(on|tonight|myself)\b",
    r"\b(kill|hurt)\s+(him|her|them|someone|everyone)\b",
    r"\bwrote\s+(a\s+)?(suicide\s+)?note\b",
]
_INTENT_RE = [re.compile(p, re.IGNORECASE) for p in _INTENT_PATTERNS]
_NEGATION_RE = re.compile(r"\b(not|never|n't|no)\s+(?:\w+\s+){0,2}(suicidal|thinking about|going to)\b", re.IGNORECASE)

CLASSIFIER_SYSTEM = (
    "You are a safety triage classifier for a journaling app. Read the user's message and answer with exactly one "
    "word: RISK if the person expresses intent, plan, or desire to seriously harm themselves or someone else, or "
    "describes being in immediate danger; otherwise SAFE. Sadness, anger, grief, hopeless-sounding but non-lethal "
    "language, and discussion of someone else's past crisis are SAFE."
)


@dataclass
class SafetyVerdict:
    risk: bool
    source: str  # "lexical" | "classifier" | "none"
    matched: str | None = None


class SafetyGate:
    def __init__(self, classifier: LLMProvider | None = None, classifier_model: str | None = None):
        self.classifier = classifier
        self.classifier_model = classifier_model

    @staticmethod
    def lexical(text: str) -> SafetyVerdict:
        for rx in _INTENT_RE:
            m = rx.search(text)
            if m:
                # crude negation handling: "I'm not suicidal" should not fire
                window = text[max(0, m.start() - 25): m.end()]
                if _NEGATION_RE.search(window) and "anymore" not in window.lower():
                    continue
                return SafetyVerdict(True, "lexical", m.group(0))
        return SafetyVerdict(False, "none")

    async def check(self, text: str) -> SafetyVerdict:
        v = self.lexical(text)
        if v.risk or self.classifier is None:
            return v
        try:
            r = await self.classifier.generate([CLASSIFIER_SYSTEM], [Message("user", text)],
                                               model=self.classifier_model, max_tokens=3, temperature=0.0)
            if r.text.strip().upper().startswith("RISK"):
                return SafetyVerdict(True, "classifier", None)
        except Exception:  # classifier outage must never block journaling; the lexical screen still ran
            pass
        return SafetyVerdict(False, "none")
