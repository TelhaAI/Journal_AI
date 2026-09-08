"""Gate 2 — provenance-typed verification gate (plan §0.5, §2.3). Look Back only.

Pure function of (raw model output, entry set, settings). Given the same inputs and
the same GATE_VERSION it must reproduce the same verification log byte-for-byte —
that is what makes receipts re-executable (U18). Bump GATE_VERSION whenever any
check here changes.
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from enum import IntEnum

from ..lookback.evidence import EntryView, count_occurrences, first_last, last_third_count, normalize_ws

GATE_VERSION = "pg-1.0.1"

HEDGE_PHRASE = "caught my attention, not a pattern yet"
HEDGE_PATTERNS = [r"caught my attention", r"not a pattern yet", r"not enough", r"\btwice\b", r"only once",
                  r"just once", r"too early to (?:say|call)"]
GROWTH_LEXICON = ["growth", "journey", "progress", "healing", "transformation", "breakthrough",
                  "how far you've come", "how far you have come", "you've grown", "you have grown", "evolved"]
FREQUENCY_WORDS = [r"\bseveral\b", r"\bmany times\b", r"\boften\b", r"\bfrequently\b", r"\brepeatedly\b",
                   r"\bkeep(?:s)? (?:coming|showing|saying|writing|returning)\b", r"\bagain and again\b",
                   r"\bover and over\b", r"\bconstantly\b", r"\balways\b", r"\ba lot\b", r"\bevery entry\b"]
_NUM_WORDS = {"three": 3, "four": 4, "five": 5, "six": 6, "seven": 7, "eight": 8, "nine": 9, "ten": 10}


class Tier(IntEnum):
    NONE = 0
    NAMED = 1
    QUOTED = 2
    RECOMPUTED = 3

    @property
    def label(self) -> str:
        return self.name.lower()


def _norm_quote(s: str) -> str:
    s = s.replace("’", "'").replace("‘", "'").replace("“", '"').replace("”", '"')
    s = normalize_ws(s)
    return s.strip(" \"'.…")


def _text_frequency_claim(text: str) -> int | None:
    """If the prose asserts frequency, return the minimum count it implies (None if it doesn't)."""
    low = text.lower()
    m = re.search(r"\b(\d+|" + "|".join(_NUM_WORDS) + r")\s+(?:times|entries|separate entries|different entries)\b", low)
    if m:
        w = m.group(1)
        return int(w) if w.isdigit() else _NUM_WORDS[w]
    for p in FREQUENCY_WORDS:
        if re.search(p, low):
            return 3
    return None


@dataclass
class GateResult:
    served: list[dict] = field(default_factory=list)
    dropped: list[dict] = field(default_factory=list)
    log: list[dict] = field(default_factory=list)
    total: int = 0

    @property
    def drop_fraction(self) -> float:
        return (len(self.dropped) / self.total) if self.total else 0.0


class ProvenanceGate:
    def __init__(self, *, min_entries_for_pattern: int = 3):
        self.min_entries_for_pattern = min_entries_for_pattern

    # ------------------------------------------------------------------ entry
    def run(self, raw: dict | list, entries: list[EntryView]) -> GateResult:
        observations = raw.get("observations", []) if isinstance(raw, dict) else raw
        if not isinstance(observations, list):
            observations = []
        by_id = {e.id: e for e in entries}
        result = GateResult(total=len(observations))
        for i, obs in enumerate(observations):
            if not isinstance(obs, dict):
                rec = {"index": i, "verdict": "dropped", "reason": "malformed", "tier": Tier.NONE.label, "checks": []}
                result.log.append(rec)
                result.dropped.append({"index": i, "reason": "malformed", "observation": obs})
                continue
            rec = self._verify(i, obs, entries, by_id)
            result.log.append(rec)
            if rec["verdict"] == "dropped":
                result.dropped.append({"index": i, "reason": rec["reason"], "text": str(obs.get("text", ""))})
            else:
                result.served.append(rec["served"])
        return result

    # ------------------------------------------------------------------ one observation
    def _verify(self, i: int, obs: dict, entries: list[EntryView], by_id: dict[str, EntryView]) -> dict:
        checks: list[dict] = []
        text = str(obs.get("text", "") or "")
        kind_in = str(obs.get("kind", "observation") or "observation")
        if kind_in not in ("observation", "possible_pattern", "interpretation"):
            kind_in = "observation"  # unknown kinds are treated as the weakest claim
        entry_ids = [str(x) for x in (obs.get("entry_ids") or [])]
        quotes = [str(q) for q in (obs.get("quotes") or []) if str(q).strip()]
        claims = [c for c in (obs.get("claims") or []) if isinstance(c, dict)]
        tier = Tier.NONE

        def check(name: str, passed: bool, detail=None):
            checks.append({"name": name, "pass": bool(passed), "detail": detail})
            return passed

        # ---- lint that is fail-closed regardless of tier
        growth = [g for g in GROWTH_LEXICON if re.search(r"\b" + re.escape(g) + r"\b", text, re.IGNORECASE)]
        if not check("no_growth_lexicon", not growth, growth):
            return self._drop(i, kind_in, tier, checks, "growth_lexicon")

        # ---- named: every referenced term appears in range; something must anchor the observation
        terms = [str(c.get("term", "")) for c in claims if c.get("term")]
        missing = [t for t in terms if count_occurrences(entries, t)[0] == 0]
        anchored = bool(terms or quotes)
        check("named_anchored", anchored, {"terms": terms, "quotes": len(quotes)})
        check("named_terms_present", not missing, missing)
        if not anchored or missing:
            return self._drop(i, kind_in, tier, checks, "not_named")
        tier = Tier.NAMED

        # ---- quoted: cited ids exist in range, every quote is a ws-normalized substring of a cited entry
        unknown = [eid for eid in entry_ids if eid not in by_id]
        check("cited_ids_in_range", bool(entry_ids) and not unknown, {"cited": entry_ids, "unknown": unknown})
        cited_bodies = [_norm_quote(by_id[eid].body) for eid in entry_ids if eid in by_id]
        bad_quotes = [q for q in quotes if not any(_norm_quote(q) in b for b in cited_bodies)]
        check("quotes_verbatim", bool(quotes) and not bad_quotes, {"bad": bad_quotes})
        if not entry_ids or unknown or not quotes or bad_quotes:
            return self._drop(i, kind_in, tier, checks, "not_quoted")
        tier = Tier.QUOTED

        # ---- frequency cross-check on the prose (U7)
        implied = _text_frequency_claim(text)
        if implied is not None:
            actual = 0
            if terms:
                actual = max(count_occurrences(entries, t)[0] for t in terms)
            else:
                actual = len(set(entry_ids))
            if not check("prose_frequency", actual >= implied, {"implied_min": implied, "actual": actual}):
                return self._drop(i, kind_in, tier, checks, "frequency_unsupported")

        # ---- recomputed: every structured claim agrees with the deterministic recompute
        stripped: list[dict] = []
        kept_claims: list[dict] = []
        for c in claims:
            ok, detail = self._verify_claim(c, entries)
            check(f"claim_{c.get('type', '?')}", ok, detail)
            (kept_claims if ok else stripped).append({**c, "recomputed": detail})
        if claims and not stripped:
            tier = Tier.RECOMPUTED
        elif claims and stripped and not kept_claims:
            tier = Tier.QUOTED  # all claims failed: still quoted, but nothing verified beyond the quote

        # ---- kind/tier coupling
        distinct = len(set(entry_ids))
        kind_out = kind_in
        hedge_added = False
        if kind_in in ("possible_pattern", "interpretation") and distinct < self.min_entries_for_pattern:
            kind_out = "observation"
        if distinct < self.min_entries_for_pattern:
            if not any(re.search(p, text, re.IGNORECASE) for p in HEDGE_PATTERNS):
                text = text.rstrip() + f" This {HEDGE_PHRASE}."
                hedge_added = True
        check("kind_tier_coupling", True, {"kind_in": kind_in, "kind_out": kind_out, "distinct_entries": distinct,
                                           "hedge_added": hedge_added})

        served = {
            "kind": kind_out,
            "text": text,
            "entry_ids": entry_ids,
            "quotes": quotes,
            "claims": kept_claims,
            "tier": tier.label,
            "verdict": "served",
            "label": self._label(tier, distinct),
        }
        return {"index": i, "verdict": "served", "reason": None, "tier": tier.label, "kind_in": kind_in,
                "kind_out": kind_out, "claims_stripped": stripped, "checks": checks, "served": served}

    @staticmethod
    def _drop(i: int, kind_in: str, tier: Tier, checks: list[dict], reason: str) -> dict:
        return {"index": i, "verdict": "dropped", "reason": reason, "tier": tier.label, "kind_in": kind_in,
                "kind_out": None, "claims_stripped": [], "checks": checks}

    @staticmethod
    def _label(tier: Tier, distinct: int) -> str:
        if tier == Tier.RECOMPUTED:
            return f"{distinct} {'entry' if distinct == 1 else 'entries'} · verified"
        return f"{distinct} {'entry' if distinct == 1 else 'entries'} · quoted"

    # ------------------------------------------------------------------ claims
    def _verify_claim(self, c: dict, entries: list[EntryView]) -> tuple[bool, dict]:
        ctype = c.get("type")
        term = str(c.get("term", "") or "")
        asserted = c.get("asserted")
        if ctype == "count":
            try:
                n = int(asserted)
            except (TypeError, ValueError):
                return False, {"error": "asserted not an integer"}
            actual, ids = count_occurrences(entries, term)
            tol = 0 if n <= 3 else 1
            return abs(actual - n) <= tol, {"asserted": n, "actual": actual, "tolerance": tol, "entry_ids": ids}
        if ctype == "absence":
            fh, lt = last_third_count(entries, term)
            return (lt == 0 and fh > 0), {"first_half": fh, "last_third": lt}
        if ctype in ("first_last", "then_now"):
            first, last = first_last(entries, term)
            if first is None:
                return False, {"error": "term absent"}
            expected = [first.id, last.id]
            got = [str(x) for x in asserted] if isinstance(asserted, (list, tuple)) else None
            return got == expected, {"expected": expected, "asserted": got}
        return False, {"error": f"unknown claim type {ctype!r}"}


def log_bytes(log: list[dict]) -> bytes:
    """Canonical serialization used by receipt replay comparisons."""
    return json.dumps(log, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
