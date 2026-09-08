"""Layer 3 — Look Back synthetic-corpus test (plan §6.3).

    python -m evals.run_layer3 --corpora 20 [--dry-run] [--out evals/reports]

For each generated corpus: load its entries into a fresh database, run Look Back through the real
service (evidence → model → provenance gate → receipt), then score the served observations against
the planted ground truth. Reports precision on noise (TOO SMART) and recall on planted features
(NOT ENOUGH EDGE), plus the deterministic invariants that must hold 100 %.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import re
import sys
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from journal_ai import db as dbmod  # noqa: E402
from journal_ai.config import get_settings  # noqa: E402
from journal_ai.llm import ScriptedProvider, get_provider  # noqa: E402
from journal_ai.lookback.service import LookbackService, replay_receipt  # noqa: E402
from journal_ai.models import Entry, User, Volume  # noqa: E402
from journal_ai.prompts import sync_prompts_from_disk  # noqa: E402

from .corpus_generator import Corpus, generate_corpus, noise_terms  # noqa: E402
from .harness import wilson_interval  # noqa: E402

GROWTH_RE = re.compile(r"\b(growth|journey|progress|healing|transformation|breakthrough|how far you['’]ve come)\b", re.I)
ABSENCE_RE = re.compile(r"\b(used to|barely|no longer|stopped|hasn['’]t come up|disappear|dropped out|went quiet)\b", re.I)
HEDGE_RE = re.compile(r"(not enough|caught my attention|twice|not a pattern yet|only once)", re.I)
IMPERATIVE_LIST_RE = re.compile(r"^\s*(?:[-*•]|\d+[.)])\s+[A-Z][a-z]+\b", re.M)


def load_corpus(db, corpus: Corpus, user_id: str) -> None:
    db.add(User(id=user_id))
    vol = Volume(user_id=user_id, title="Journal — Test")
    db.add(vol)
    db.flush()
    for e in corpus.entries:
        db.add(Entry(id=e["id"], user_id=user_id, volume_id=vol.id, body=e["body"], word_count=len(e["body"].split()),
                     created_at_utc=e["created_at_local"].replace(tzinfo=timezone.utc),
                     created_at_local=e["created_at_local"], tz="America/Chicago", source="write"))
    db.flush()


def score(corpus: Corpus, report, receipt, evidence: dict, replay_ok: bool) -> dict:
    obs = report.observations
    texts = " ".join(o["text"] for o in obs)
    quotes = " ".join(q for o in obs for q in o["quotes"])
    all_text = texts + " " + quotes
    t = corpus.truth
    claim_tiers_ok = all(o["tier"] == "recomputed" for o in obs if o.get("claims"))
    return {
        "seed": corpus.seed,
        "outcome": receipt.outcome,
        "n_obs": len(obs),
        "all_served_quoted_or_better": all(o["tier"] in ("quoted", "recomputed") for o in obs),
        "claims_are_recomputed": claim_tiers_ok,
        "receipt_written": receipt.id is not None,
        "replay_reproduces": replay_ok,
        "obs_count_in_range": 2 <= len(obs) <= 6,
        "recurring_word_surfaced": re.search(r"(?<![a-z])" + re.escape(t.recurring_word) + r"(?![a-z])", all_text, re.I) is not None,
        "disappearing_as_absence": any(re.search(re.escape(t.disappearing.split()[-1]), o["text"], re.I)
                                       and ABSENCE_RE.search(o["text"]) for o in obs),
        "no_noise": not any(re.search(r"\b" + re.escape(n) + r"\b", texts, re.I) for n in noise_terms()),
        "small_n_hedged": all(o["kind"] == "observation" and HEDGE_RE.search(o["text"])
                              for o in obs if len(set(o["entry_ids"])) < 3),
        "no_growth_lexicon": not GROWTH_RE.search(texts + " " + (report.message or "")),
        "certainty_then_now": any(t.certainty_then_id in o["entry_ids"] and t.certainty_now_id in o["entry_ids"]
                                  for o in obs),
        "contradiction_recall": any(a in o["entry_ids"] and b in o["entry_ids"] for o in obs
                                    for a, b in t.contradiction_pairs),
        "closes_without_action_items": not IMPERATIVE_LIST_RE.search(report.message or ""),
    }


THRESHOLDS = {  # assertion -> minimum fraction of corpora
    "all_served_quoted_or_better": 1.0, "claims_are_recomputed": 1.0, "receipt_written": 1.0, "replay_reproduces": 1.0,
    "obs_count_in_range": 1.0, "recurring_word_surfaced": 0.90, "disappearing_as_absence": 0.70, "no_noise": 1.0,
    "small_n_hedged": 1.0, "no_growth_lexicon": 1.0, "certainty_then_now": 0.70, "closes_without_action_items": 1.0,
}


def dry_run_provider() -> ScriptedProvider:
    """Builds a plausible, evidence-table-driven answer offline, so the Layer 3 pipeline can be exercised."""

    def script(system, messages):
        user = messages[-1].content
        ev = json.loads(user.split("EVIDENCE TABLE (computed from the record):\n", 1)[1].split("\n\n", 1)[0])
        entries = {}
        for block in user.split("ENTRIES:\n", 1)[1].split("\n\n"):
            m = re.match(r"\[entry (\S+) — (\S+)\]\n(.*)", block, re.S)
            if m:
                entries[m.group(1)] = m.group(3)
        obs = []
        if ev["recurring_words"]:
            rw = ev["recurring_words"][0]
            ids = rw["entry_ids"]
            q = next((s for s in re.split(r"(?<=[.!?])\s+", entries[ids[0]]) if re.search(r"(?<![a-z])" + rw["term"].split()[0], s, re.I)), None)
            obs.append({"kind": "possible_pattern" if len(ids) >= 3 else "observation",
                        "text": f"The word '{rw['term']}' shows up {rw['count']} times across {len(ids)} entries.",
                        "entry_ids": ids, "quotes": [q] if q else [],
                        "claims": [{"type": "count", "term": rw["term"], "asserted": rw["count"]}]})
        if ev["absences"]:
            ab = ev["absences"][0]
            first_ids = [e for e in entries if re.search(r"(?<![a-z])" + re.escape(ab["term"]), entries[e], re.I)][:2]
            q = next((s for s in re.split(r"(?<=[.!?])\s+", entries[first_ids[0]]) if re.search(re.escape(ab["term"]), s, re.I)), None)
            obs.append({"kind": "observation",
                        "text": f"'{ab['term']}' used to come up early on and no longer appears after {ab['last_date']}. It caught my attention, not a pattern yet.",
                        "entry_ids": first_ids, "quotes": [q] if q else [],
                        "claims": [{"type": "absence", "term": ab["term"], "asserted": True}]})
        c = ev["certainty"]
        months = sorted(c)
        if months and c[months[0]]["hedge_entry_ids"] and c[months[-1]]["certain_entry_ids"]:
            a = next((e for e in c[months[0]]["hedge_entry_ids"] if "could ever leave" in entries.get(e, "")), None)
            b = next((e for e in c[months[-1]]["certain_entry_ids"] if "won't stay" in entries.get(e, "")), None)
            if a and b:
                obs.append({"kind": "observation",
                            "text": f"Then ({months[0]}): 'I don't know if I could ever leave.' Now ({months[-1]}): 'I know I won't stay past spring.' That caught my attention, not a pattern yet.",
                            "entry_ids": [a, b],
                            "quotes": ["I don't know if I could ever leave.", "I know I won't stay past spring."], "claims": []})
        return json.dumps({"observations": obs, "closing": "That's what your own words point to."})

    return ScriptedProvider(script=script)


async def main_async(args) -> int:
    provider = dry_run_provider() if args.dry_run else get_provider()
    s = get_settings()
    rows = []
    for seed in range(args.corpora):
        corpus = generate_corpus(1000 + seed)
        eng = dbmod.reset_engine("sqlite://")
        dbmod.init_db(eng)
        with dbmod.session_scope() as db:
            sync_prompts_from_disk(db, s.prompts_dir)
            user = f"corpus-{seed}"
            load_corpus(db, corpus, user)
            svc = LookbackService(provider)
            out = await svc.run(db, user)
            db.flush()
            replay_ok = replay_receipt(db, out.receipt)
            rows.append(score(corpus, out.report, out.receipt, out.evidence, replay_ok))
            print(f"corpus {seed}: outcome={rows[-1]['outcome']} obs={rows[-1]['n_obs']} "
                  f"word={rows[-1]['recurring_word_surfaced']} absence={rows[-1]['disappearing_as_absence']} "
                  f"noise_ok={rows[-1]['no_noise']}")
    n = len(rows)
    summary = {}
    ok = True
    for key, thr in THRESHOLDS.items():
        passed = sum(1 for r in rows if r[key])
        lo, hi = wilson_interval(n - passed, n)
        met = passed / n >= thr
        ok &= met
        summary[key] = {"passed": passed, "n": n, "rate": round(passed / n, 3), "threshold": thr, "met": met,
                        "fail_ci95": [round(lo, 3), round(hi, 3)]}
    noise_fp = sum(1 for r in rows if not r["no_noise"])
    summary["_precision_on_noise"] = 1 - noise_fp / n
    summary["_recall_recurring_word"] = sum(r["recurring_word_surfaced"] for r in rows) / n
    summary["_recall_contradiction"] = sum(r["contradiction_recall"] for r in rows) / n
    out_dir = Path(args.out)
    out_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    label = "dry-run" if args.dry_run else s.llm_model
    path = out_dir / f"layer3_{label}_{stamp}.json"
    path.write_text(json.dumps({"model": label, "corpora": n, "summary": summary, "rows": rows}, indent=2), encoding="utf-8")
    print("\n== Layer 3 report ==")
    for k, v in summary.items():
        if k.startswith("_"):
            print(f"{k[1:]}: {v:.2f}")
        else:
            print(f"{k}: {v['passed']}/{n} ({v['rate']:.2f}) threshold {v['threshold']} → {'ok' if v['met'] else 'MISS'}")
    print(f"all thresholds met: {ok}\nreport: {path}")
    return 0 if ok else 1


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--corpora", type=int, default=20)
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--out", default=str(ROOT / "evals" / "reports"))
    args = ap.parse_args()
    if args.dry_run:
        os.environ.setdefault("JOURNAL_LLM_PROVIDER", "scripted")
    raise SystemExit(asyncio.run(main_async(args)))


if __name__ == "__main__":
    main()
