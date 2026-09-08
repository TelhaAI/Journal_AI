"""Eval harness (plan §6.4): falsification framing, Wilson intervals, failure clustering,
non-regression check. Pure functions here; runners in run_layer2.py / run_layer3.py."""
from __future__ import annotations

import math
import re
from collections import defaultdict
from dataclasses import dataclass, field

# --------------------------------------------------------------------------- statistics


def wilson_interval(failures: int, n: int, z: float = 1.96) -> tuple[float, float]:
    """95 % Wilson score interval for a failure rate."""
    if n == 0:
        return 0.0, 1.0
    p = failures / n
    denom = 1 + z * z / n
    centre = (p + z * z / (2 * n)) / denom
    half = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n)) / denom
    return max(0.0, centre - half), min(1.0, centre + half)


def decision_at(failures: int, n: int) -> str:
    """Plan §6.4 table."""
    lo, hi = wilson_interval(failures, n)
    if failures == 0:
        return "freeze-eligible; run N=30 on Section-11-linked fixtures" if n < 30 else "freeze-eligible"
    if failures == 1 and n <= 10:
        return "isolated or repeatable — cannot tell; run N=30"
    if lo >= 0.10:  # 3/10 → lower bound ≈ 0.108, the plan's '≥ 11 %' row
        return "repeatable; do not freeze"
    return "treat as repeatable; cluster before editing"


# --------------------------------------------------------------------------- run records


@dataclass
class RunRecord:
    fixture: str
    prompt_version: str
    model: str
    seed: int
    response: str
    assertions: dict[str, bool]  # falsifier name -> passed
    mode_state: str = ""
    judge: dict[str, bool] = field(default_factory=dict)

    @property
    def passed(self) -> bool:
        return all(self.assertions.values())

    @property
    def fired(self) -> list[str]:
        return [k for k, v in self.assertions.items() if not v]


def opening_ngram(text: str, n: int = 3) -> str:
    words = re.findall(r"[a-z']+", text.lower())
    return " ".join(words[:n])


def cluster_failures(records: list[RunRecord], min_size: int = 3) -> list[dict]:
    """Group failed runs by (fixture, falsifier, opening n-gram, mode state). Clusters of >= min_size
    are repeatable behaviors mapping to one Section 3 instruction; singletons are noise."""
    groups: dict[tuple, list[RunRecord]] = defaultdict(list)
    for r in records:
        for f in r.fired:
            groups[(r.fixture, f, opening_ngram(r.response), r.mode_state)].append(r)
    out = []
    for key, rs in groups.items():
        out.append({"fixture": key[0], "falsifier": key[1], "opening": key[2], "mode_state": key[3],
                    "size": len(rs), "repeatable": len(rs) >= min_size,
                    "examples": [r.response[:200] for r in rs[:3]]})
    out.sort(key=lambda c: (-c["size"], c["fixture"], c["falsifier"]))
    return out


def summarize(records: list[RunRecord]) -> dict[str, dict]:
    by_fixture: dict[str, list[RunRecord]] = defaultdict(list)
    for r in records:
        by_fixture[r.fixture].append(r)
    report = {}
    for fx, rs in sorted(by_fixture.items()):
        n = len(rs)
        fails = sum(1 for r in rs if not r.passed)
        lo, hi = wilson_interval(fails, n)
        judge_n = sum(1 for r in rs if r.judge)
        judge_pass = sum(1 for r in rs if r.judge and all(r.judge.values()))
        report[fx] = {"n": n, "failures": fails, "fail_rate": fails / n if n else None,
                      "wilson_lo": round(lo, 3), "wilson_hi": round(hi, 3), "decision": decision_at(fails, n),
                      "judge_pass_rate": (judge_pass / judge_n) if judge_n else None,
                      "falsifiers_fired": sorted({f for r in rs for f in r.fired})}
    return report


def non_regression_ok(previous: dict[str, dict], candidate: dict[str, dict]) -> tuple[bool, list[str]]:
    """A new prompt version may not be activated if any fixture's failure-rate lower bound is above the
    previous version's upper bound."""
    problems = []
    for fx, cand in candidate.items():
        prev = previous.get(fx)
        if prev and cand["wilson_lo"] > prev["wilson_hi"]:
            problems.append(f"{fx}: candidate lower bound {cand['wilson_lo']} > previous upper bound {prev['wilson_hi']}")
    return not problems, problems


def freeze_eligible(report: dict[str, dict], deterministic_min: float = 0.90, judge_min: float = 0.80) -> tuple[bool, list[str]]:
    problems = []
    for fx, r in report.items():
        if r["n"] and (1 - r["fail_rate"]) < deterministic_min:
            problems.append(f"{fx}: deterministic pass rate {1 - r['fail_rate']:.2f} < {deterministic_min}")
        if r["judge_pass_rate"] is not None and r["judge_pass_rate"] < judge_min:
            problems.append(f"{fx}: judge pass rate {r['judge_pass_rate']:.2f} < {judge_min}")
    return not problems, problems
