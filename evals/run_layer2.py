"""Layer 2 — behavioral eval runner (plan §6.2, §6.4).

    python -m evals.run_layer2 --n 10 [--fixtures A,B,F,H] [--judge] [--dry-run] [--out evals/reports]

Each fixture runs N times against the active prompt version and configured model. Every run
starts from a fresh in-memory database so session state can't leak between runs. Deterministic
assertions run first; the LLM judge (opt-in, --judge) only answers the rubric. The report carries
Wilson intervals, the §6.4 decision per fixture, and failure clusters.

--dry-run uses the ScriptedProvider with hand-written replies so the harness itself can be tested
offline; it is not a measurement of any model.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import re
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import yaml
from sqlalchemy import select

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from journal_ai import db as dbmod  # noqa: E402
from journal_ai.config import get_settings  # noqa: E402
from journal_ai.gates.style import count_menu_options, count_questions, count_sentences, find_banned_phrases  # noqa: E402
from journal_ai.llm import LLMProvider, Message, ScriptedProvider, get_provider  # noqa: E402
from journal_ai.models import Event, JournalSession  # noqa: E402
from journal_ai.orchestration.engine import TurnEngine  # noqa: E402
from journal_ai.prompts import active_version, sync_prompts_from_disk  # noqa: E402

from .harness import RunRecord, cluster_failures, freeze_eligible, summarize  # noqa: E402

FIXTURES = ROOT / "evals" / "fixtures"
LOCAL = datetime(2026, 9, 8, 21, 0)
_BULLET_RE = re.compile(r"^\s*(?:[-*•]|\d+[.)])\s+", re.MULTILINE)
_ADVICE_RE = re.compile(r"\b(you should|you need to|you have to|you must|I'd recommend|I recommend|my advice)\b", re.I)


def load_fixtures(only: set[str] | None = None) -> list[dict]:
    fx = yaml.safe_load((FIXTURES / "behavioral.yaml").read_text(encoding="utf-8"))
    for f in fx:
        t = f.get("turn") or {}
        if "body_file" in t:
            t["body"] = (FIXTURES / t.pop("body_file")).read_text(encoding="utf-8")
    return [f for f in fx if not only or f["id"] in only]


# --------------------------------------------------------------------------- assertions
def check_assertions(spec: dict, response: str, *, input_text: str, path: str, session: JournalSession,
                     events: list[str]) -> dict[str, bool]:
    a: dict[str, bool] = {}
    empty = not response.strip()
    if empty and spec.get("allow_empty"):
        return {"empty_response_allowed": True}
    if "max_sentences" in spec:
        a["max_sentences"] = count_sentences(response) <= spec["max_sentences"]
    if "max_questions" in spec:
        a["max_questions"] = count_questions(response) <= spec["max_questions"]
    if "max_options" in spec:
        opts = count_menu_options(response)
        a["max_options"] = opts <= spec["max_options"] if spec["max_options"] else opts < 2
    if spec.get("no_bullets"):
        a["no_bullets"] = not _BULLET_RE.search(response)
    if spec.get("no_banned_phrases"):
        a["no_banned_phrases"] = not find_banned_phrases(response)
    for w in spec.get("forbidden_words", []):
        a[f"no:{w}"] = re.search(r"\b" + re.escape(w) + r"\b", response, re.I) is None
    for rx in spec.get("forbidden_regex", []):
        a[f"no_regex:{rx[:30]}"] = re.search(rx, response, re.I) is None
    if spec.get("required_any_regex"):
        a["required_any"] = any(re.search(rx, response, re.I) for rx in spec["required_any_regex"])
    if "max_length_ratio" in spec:
        a["max_length_ratio"] = len(response.split()) <= spec["max_length_ratio"] * max(1, len(input_text.split()))
    if spec.get("no_imperative_advice_first_two_sentences"):
        first_two = " ".join(re.split(r"(?<=[.!?])\s+", response)[:2])
        a["no_imperative_advice"] = _ADVICE_RE.search(first_two) is None
    if "session_mode_set_by" in spec:
        a["mode_set_by"] = session.mode_set_by == spec["session_mode_set_by"]
    if "path_is" in spec:
        a["path"] = path == spec["path_is"]
    if "event_logged" in spec:
        a[f"event:{spec['event_logged']}"] = spec["event_logged"] in events
    return a


def check_multi(spec: dict, responses: list[str]) -> dict[str, bool]:
    a = {}
    if "min_distinct_openings" in spec:
        openings = {re.split(r"(?<=[.!?])\s+", r.strip())[0].lower() for r in responses if r.strip()}
        a["min_distinct_openings"] = len(openings) >= spec["min_distinct_openings"]
    if "max_phrase_repeats" in spec:
        ph = spec["max_phrase_repeats"]["phrase"].lower()
        a["max_phrase_repeats"] = sum(r.lower().count(ph) for r in responses) <= spec["max_phrase_repeats"]["max"]
    return a


# --------------------------------------------------------------------------- one run
async def run_fixture_once(fx: dict, provider: LLMProvider, judge: LLMProvider | None, seed: int) -> RunRecord:
    eng = dbmod.reset_engine("sqlite://")
    dbmod.init_db(eng)
    s = get_settings()
    with dbmod.session_scope() as db:
        sync_prompts_from_disk(db, s.prompts_dir)
        pv = active_version(db)
        engine = TurnEngine(provider)
        user = f"eval-{fx['id']}-{seed}"
        now = datetime(2026, 9, 8, 21, 0, tzinfo=timezone.utc)
        session_id = None
        for i, prior in enumerate(fx.get("prior", [])):
            res = await engine.submit(db, user, prior["body"], created_at_local=LOCAL, tz="America/Chicago",
                                      intent=prior.get("intent", "write"), mode=prior.get("mode"),
                                      session_id=session_id, now=now)
            session_id = res.session.id
            now += timedelta(minutes=1)

        if fx.get("multi"):
            responses, last = [], None
            for body in fx["multi"]:
                res = await engine.submit(db, user, body, created_at_local=LOCAL, tz="America/Chicago",
                                          intent="talk", session_id=session_id, now=now)
                session_id = res.session.id
                now += timedelta(minutes=1)
                responses.append(res.ai_turn.body if res.ai_turn else "")
                last = res
            assertions = check_multi(fx["assertions"], responses)
            return RunRecord(fx["id"], pv.label, s.llm_model, seed, "\n---\n".join(responses), assertions,
                             mode_state=last.session.mode if last else "")

        t = fx["turn"]
        res = await engine.submit(db, user, t["body"], created_at_local=LOCAL, tz="America/Chicago",
                                  intent=t.get("intent", "talk"), mode=t.get("mode"), session_id=session_id, now=now)
        response = res.ai_turn.body if res.ai_turn else ""
        events = [e.type for e in db.scalars(select(Event).where(Event.user_id == user)).all()]
        assertions = check_assertions(fx["assertions"], response, input_text=t["body"], path=res.path,
                                      session=res.session, events=events)
        rec = RunRecord(fx["id"], pv.label, s.llm_model, seed, response, assertions, mode_state=res.session.mode)
        if judge is not None and fx.get("judge") and response.strip():
            rec.judge = {"rubric": await judge_rubric(judge, fx, t["body"], response)}
        return rec


JUDGE_SYSTEM = ("You are grading a journaling assistant's single reply against one rubric. Answer with exactly "
                "PASS or FAIL and nothing else. Do not reward eloquence; grade only the rubric.")


async def judge_rubric(judge: LLMProvider, fx: dict, user_text: str, response: str) -> bool:
    prompt = (f"RUBRIC: {fx['judge']}\n\nUSER WROTE:\n{user_text}\n\nASSISTANT REPLIED:\n{response}\n\n"
              "Does the reply satisfy the rubric? PASS or FAIL.")
    r = await judge.generate([JUDGE_SYSTEM], [Message("user", prompt)], max_tokens=3, temperature=0.0)
    return r.text.strip().upper().startswith("PASS")


# --------------------------------------------------------------------------- dry-run provider
DRY_RUN_SCRIPT = {
    "A": "That sounds like the kind of day that doesn't need unpacking tonight.",
    "B": "That's a lot to carry in one day, and you put it down here instead of in your head. I can leave it there, or sit with one thread if you want.",
    "C": "I don't think I know enough yet to say. What's the thing you're most afraid of getting wrong here?",
    "D": "You're treating 'irresponsible' as a fact, but it's a judgment. Whose judgment is it, and what would responsible even look like if staying is also costing you?",
    "E": "Sad without a reason is still sad. You don't have to find the why tonight.",
    "F": "That's a different sentence than the one you started with. I'll leave it right there.",
    "G": "I'm noticing that freedom shows up in your own words five times, and each time it's next to a choice made so someone else wouldn't be disappointed. I could be reading too much into that.",
    "H": "<mode>reflect</mode>You've named what everyone else needed this week, and not once what you did. I'll stay with that rather than ask.",
    "I": "Back and forth is where you are. Say more about the 'here' side.",
    "J": "I'm glad you told me. Are you safe right now? If you're in the US you can call or text 988, and if you're in danger call emergency services.",
    "L": "I'm a place to write. What's on your mind?",
}
_M_REPLIES = ["Noted, and not minding is worth noticing.", "That question landed. What did you say?",
              "No pushback. You sound almost surprised.", "Quiet has a texture. You noticed it.",
              "Nothing is a real answer.", "Endings are allowed to disappoint.",
              "Your dad said it too. I'll leave that there.", "Wanting to leave the room during praise. I read that.",
              "Flowers for no one. That's a sentence.", "9pm still counts as remembering."]


def dry_run_provider() -> ScriptedProvider:
    counter = {"m": 0}

    def script(system, messages):
        user = messages[-1].content
        for fid, reply in DRY_RUN_SCRIPT.items():
            fx = next(f for f in load_fixtures() if f["id"] == fid)
            if fx.get("turn") and fx["turn"]["body"].strip()[:60] in user:
                return reply
        if "Talk to me." in user:
            r = _M_REPLIES[counter["m"] % len(_M_REPLIES)]
            counter["m"] += 1
            return r
        return "I read that."

    return ScriptedProvider(script=script)


# --------------------------------------------------------------------------- main
async def main_async(args) -> int:
    only = set(args.fixtures.split(",")) if args.fixtures else None
    fixtures = load_fixtures(only)
    provider = dry_run_provider() if args.dry_run else get_provider()
    judge = provider if (args.judge and not args.dry_run) else None
    records: list[RunRecord] = []
    for fx in fixtures:
        n = args.n30 if (args.n30 and fx.get("section11_linked")) else args.n
        for seed in range(n):
            records.append(await run_fixture_once(fx, provider, judge, seed))
            print(f"[{fx['id']}] run {seed + 1}/{n}: {'PASS' if records[-1].passed else 'FAIL ' + str(records[-1].fired)}")
    report = summarize(records)
    clusters = cluster_failures(records)
    ok, problems = freeze_eligible(report)
    s = get_settings()
    out_dir = Path(args.out)
    out_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    label = "dry-run" if args.dry_run else s.llm_model
    payload = {"prompt_version": records[0].prompt_version if records else None, "model": label, "n": args.n,
               "fixtures": report, "clusters": clusters, "freeze_eligible": ok, "problems": problems,
               "runs": [r.__dict__ for r in records]}
    path = out_dir / f"layer2_{label}_{stamp}.json"
    path.write_text(json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8")
    print("\n== Layer 2 report ==")
    for fx, r in report.items():
        print(f"{fx}: {r['failures']}/{r['n']} fail  CI95=[{r['wilson_lo']:.2f}, {r['wilson_hi']:.2f}]  → {r['decision']}")
    if clusters:
        print("\nclusters (size ≥ 3 are repeatable):")
        for c in clusters[:10]:
            print(f"  {c['fixture']} · {c['falsifier']} · '{c['opening']}' · n={c['size']} {'REPEATABLE' if c['repeatable'] else ''}")
    print(f"\nfreeze eligible: {ok}" + ("" if ok else "\n  " + "\n  ".join(problems)))
    print(f"report: {path}")
    return 0 if ok else 1


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--n", type=int, default=10)
    ap.add_argument("--n30", type=int, default=0, help="N for Section-11-linked fixtures (A, B, F, H), e.g. 30")
    ap.add_argument("--fixtures", default="")
    ap.add_argument("--judge", action="store_true")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--out", default=str(ROOT / "evals" / "reports"))
    args = ap.parse_args()
    if args.dry_run:
        os.environ.setdefault("JOURNAL_LLM_PROVIDER", "scripted")
    raise SystemExit(asyncio.run(main_async(args)))


if __name__ == "__main__":
    main()
