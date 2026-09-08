"""Look Back evidence extraction (plan §2.4). Deterministic, pre-LLM, and the
independent-recompute oracle the provenance gate checks model claims against.

Everything here is a pure function of the entry set: same entries in, same table
out, so receipts replay byte-for-byte.
"""
from __future__ import annotations

import hashlib
import json
import re
from collections import Counter, defaultdict
from dataclasses import dataclass
from datetime import datetime

# --------------------------------------------------------------------------- inputs


@dataclass(frozen=True)
class EntryView:
    id: str
    body: str
    created_at_local: datetime

    @property
    def month(self) -> str:
        return self.created_at_local.strftime("%Y-%m")

    @property
    def date(self) -> str:
        return self.created_at_local.strftime("%Y-%m-%d")


def content_hash(entries: list[EntryView]) -> str:
    """SHA-256 over sorted (entry_id, body) pairs. Any edit/supersede changes it (U19)."""
    h = hashlib.sha256()
    for e in sorted(entries, key=lambda e: e.id):
        h.update(e.id.encode("utf-8"))
        h.update(b"\x00")
        h.update(e.body.encode("utf-8"))
        h.update(b"\x00")
    return h.hexdigest()


# --------------------------------------------------------------------------- lexicon
# Spec's recurring-word list, with a light "lemmatizer": each term maps to a regex that
# accepts its inflections and contractions.
RECURRING_WORDS: dict[str, str] = {
    "should": r"should(?:n['’]t| not)?",
    "supposed to": r"supposed\s+to",
    "have to": r"(?:have|has|had|having)\s+to",
    "can't": r"(?:can['’]t|cannot|can\s+not|couldn['’]t|could\s+not)",
    "allowed": r"allow(?:ed|s|ing)?",
    "responsible": r"responsib(?:le|ility|ilities)",
    "selfish": r"selfish(?:ness|ly)?",
    "successful": r"success(?:ful|fully)?",
    "enough": r"enough",
    "freedom": r"free(?:dom)?",
    "safe": r"safe(?:ty|ly)?",
    "waste": r"wast(?:e|ed|ing|es)",
    "too late": r"too\s+late",
}

HEDGE_MARKERS = [
    r"i don['’]t know if", r"i don['’]t know whether", r"\bmaybe\b", r"\bi might\b", r"i['’]m not sure",
    r"\bperhaps\b", r"\bi guess\b", r"\bi wonder if\b", r"\bpossibly\b", r"\bi could\b",
]
CERTAINTY_MARKERS = [
    r"\bi know\b", r"i['’]m sure", r"\bi won['’]t\b", r"\bi will\b", r"\bdefinitely\b", r"\bi['’]m certain\b",
    r"\bi['’]m done\b", r"\bnever again\b", r"\bi have decided\b", r"\bi['’]ve decided\b",
]
POSTPONE_PHRASES = [
    "i should probably", "i'll deal with", "at some point", "eventually", "one of these days", "later",
    "when things calm down", "i'll figure it out", "i'll get to it", "not right now", "some day", "someday",
    "next week", "next month", "after the holidays",
]

STOPWORDS = set(
    """a an the and or but if then than so of to in on at by for from with without about into over under again
    is are was were be been being am do does did doing have has had having will would should could can may might
    must shall not no nor never i me my mine myself we us our ours you your yours he him his she her hers it its
    they them their theirs this that these those there here what which who whom whose when where why how all any
    both each few more most other some such only own same too very just also really actually kind sort thing things
    lot lots day days today tonight tomorrow yesterday week weeks month months year years time times still even
    like get got getting go going went gone came come make made makes want wanted wants feel felt feels feeling
    think thought thinks know knew knows say said says see saw seen way ways one two three back much many well
    because while though although yet ever always sometimes often maybe need needs needed keep keeps kept let
    put take took taken tell told trying try tried didn don doesn isn aren wasn weren won wouldn couldn shouldn
    can't don't didn't doesn't isn't aren't wasn't weren't won't wouldn't couldn't shouldn't i'm i've i'll i'd
    it's that's there's he's she's we're they're you're something anything nothing everything someone anyone
    everyone nobody around through before after up down out off away again once bit little big new old good bad
    long short first last next right left sure enough""".split()
)

_TOKEN_RE = re.compile(r"[a-z]+(?:['’][a-z]+)?")


def normalize_ws(s: str) -> str:
    return re.sub(r"\s+", " ", s).strip()


def _term_regex(term: str) -> re.Pattern:
    if term in RECURRING_WORDS:
        pat = RECURRING_WORDS[term]
    else:
        words = [re.escape(w) for w in normalize_ws(term.lower()).split(" ")]
        pat = r"\s+".join(words)
        pat = pat.replace("\\'", "['’]")
    return re.compile(r"(?<![a-z])" + pat + r"(?![a-z])", re.IGNORECASE)


def count_occurrences(entries: list[EntryView], term: str) -> tuple[int, list[str]]:
    """Independent recompute: total occurrences and the ids of entries containing the term."""
    rx = _term_regex(term)
    total, ids = 0, []
    for e in entries:
        n = len(rx.findall(e.body))
        if n:
            total += n
            ids.append(e.id)
    return total, ids


def _sorted(entries: list[EntryView]) -> list[EntryView]:
    return sorted(entries, key=lambda e: (e.created_at_local, e.id))


def last_third_count(entries: list[EntryView], term: str) -> tuple[int, int]:
    """(occurrences in first half, occurrences in last third) by chronological position."""
    es = _sorted(entries)
    n = len(es)
    rx = _term_regex(term)
    first_half = sum(len(rx.findall(e.body)) for e in es[: max(1, n // 2)])
    last_third = sum(len(rx.findall(e.body)) for e in es[n - max(1, n // 3):]) if n else 0
    return first_half, last_third


def first_last(entries: list[EntryView], term: str) -> tuple[EntryView | None, EntryView | None]:
    rx = _term_regex(term)
    hits = [e for e in _sorted(entries) if rx.search(e.body)]
    if not hits:
        return None, None
    return hits[0], hits[-1]


def sentence_containing(body: str, term: str) -> str | None:
    rx = _term_regex(term)
    for sent in re.split(r"(?<=[.!?])\s+", body):
        if rx.search(sent):
            return normalize_ws(sent)
    return None


# --------------------------------------------------------------------------- table


def build_evidence_table(entries: list[EntryView], *, min_subject_entries: int = 3, max_subjects: int = 15) -> dict:
    es = _sorted(entries)
    n = len(es)
    months = sorted({e.month for e in es})
    table: dict = {
        "entry_count": n,
        "range": {"start": es[0].date if es else None, "end": es[-1].date if es else None},
        "months": months,
        "entries": [{"id": e.id, "date": e.date, "month": e.month, "words": len(e.body.split())} for e in es],
    }

    # recurring words (spec list)
    rw = []
    for term in RECURRING_WORDS:
        total, ids = count_occurrences(es, term)
        if total >= 2:
            by_month = Counter(e.month for e in es if _term_regex(term).search(e.body))
            rw.append({"term": term, "count": total, "entry_ids": ids, "entries": len(ids),
                       "by_month": dict(sorted(by_month.items()))})
    rw.sort(key=lambda r: (-r["count"], r["term"]))
    table["recurring_words"] = rw

    # recurring subjects: uni/bi-grams over content words, present in >= min_subject_entries entries
    gram_entries: dict[str, set[str]] = defaultdict(set)
    gram_counts: Counter = Counter()
    for e in es:
        toks = [t.replace("’", "'") for t in _TOKEN_RE.findall(e.body.lower())]
        toks = [t[:-2] if t.endswith("'s") else t for t in toks]
        content = [(i, t) for i, t in enumerate(toks) if t not in STOPWORDS and len(t) > 2]
        seen = set()
        for i, t in content:
            seen.add(t)
            if i + 1 < len(toks) and toks[i + 1] not in STOPWORDS and len(toks[i + 1]) > 2:
                seen.add(f"{t} {toks[i + 1]}")
        for g in seen:
            gram_entries[g].add(e.id)
        gram_counts.update(seen)
    subjects = []
    for g, ids in gram_entries.items():
        if len(ids) >= min_subject_entries and g not in RECURRING_WORDS:
            subjects.append({"term": g, "entries": len(ids), "entry_ids": sorted(ids, key=lambda i: _index(es, i)),
                             "by_month": dict(sorted(Counter(e.month for e in es if e.id in ids).items()))})
    # prefer bigrams over their component unigrams when both qualify with the same support
    bigram_parts = {p for s in subjects if " " in s["term"] for p in s["term"].split()}
    subjects = [s for s in subjects if not (" " not in s["term"] and s["term"] in bigram_parts
                                            and any(s["term"] in b["term"] and b["entries"] >= s["entries"] - 1
                                                    for b in subjects if " " in b["term"]))]
    subjects.sort(key=lambda s: (-s["entries"], s["term"]))
    table["recurring_subjects"] = subjects[:max_subjects]

    # absence: frequent early, gone in the last third
    absences = []
    candidates = [r["term"] for r in rw] + [s["term"] for s in subjects]
    for term in candidates:
        fh, lt = last_third_count(es, term)
        if fh >= 3 and lt == 0 and n >= 6:
            _, last = first_last(es, term)
            absences.append({"term": term, "first_half_count": fh, "last_third_count": 0,
                             "last_entry_id": last.id if last else None, "last_date": last.date if last else None})
    absences.sort(key=lambda a: (-a["first_half_count"], a["term"]))
    table["absences"] = absences

    # certainty markers per month
    cert: dict[str, dict] = {}
    for m in months:
        cert[m] = {"hedge": 0, "certain": 0, "hedge_entry_ids": [], "certain_entry_ids": []}
    for e in es:
        h = sum(len(re.findall(p, e.body, re.IGNORECASE)) for p in HEDGE_MARKERS)
        c = sum(len(re.findall(p, e.body, re.IGNORECASE)) for p in CERTAINTY_MARKERS)
        cert[e.month]["hedge"] += h
        cert[e.month]["certain"] += c
        if h:
            cert[e.month]["hedge_entry_ids"].append(e.id)
        if c:
            cert[e.month]["certain_entry_ids"].append(e.id)
    table["certainty"] = cert

    # then vs now
    tn = []
    for r in rw + subjects[:8]:
        term = r["term"]
        first, last = first_last(es, term)
        if first and last and first.id != last.id:
            tn.append({"term": term,
                       "then": {"entry_id": first.id, "date": first.date, "quote": sentence_containing(first.body, term)},
                       "now": {"entry_id": last.id, "date": last.date, "quote": sentence_containing(last.body, term)}})
    table["then_now"] = tn

    # postponements
    post = []
    for phrase in POSTPONE_PHRASES:
        total, ids = count_occurrences(es, phrase)
        if total >= 2:
            post.append({"phrase": phrase, "count": total, "entry_ids": ids})
    post.sort(key=lambda p: (-p["count"], p["phrase"]))
    table["postponements"] = post
    return table


def _index(es: list[EntryView], entry_id: str) -> int:
    for i, e in enumerate(es):
        if e.id == entry_id:
            return i
    return len(es)


def evidence_json(table: dict) -> str:
    return json.dumps(table, sort_keys=True, separators=(",", ":"))
