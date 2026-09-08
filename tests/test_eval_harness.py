"""§6.4 harness statistics and the §6.3 corpus generator's ground truth are themselves tested."""
from evals.corpus_generator import generate_corpus
from evals.harness import RunRecord, cluster_failures, decision_at, non_regression_ok, summarize, wilson_interval
from journal_ai.lookback.evidence import EntryView, build_evidence_table, count_occurrences


def test_wilson_matches_plan_table():
    lo, hi = wilson_interval(0, 10)
    assert lo == 0.0 and 0.27 < hi < 0.29
    lo, hi = wilson_interval(1, 10)
    assert 0.0 < lo < 0.02 and 0.39 < hi < 0.41
    lo, hi = wilson_interval(2, 10)
    assert 0.05 < lo < 0.07 and 0.50 < hi < 0.52
    lo, _ = wilson_interval(3, 10)
    assert lo >= 0.10
    _, hi = wilson_interval(0, 30)
    assert 0.11 < hi < 0.12


def test_decisions():
    assert decision_at(0, 10).startswith("freeze-eligible")
    assert "cannot tell" in decision_at(1, 10)
    assert "cluster" in decision_at(2, 10)
    assert "do not freeze" in decision_at(3, 10)


def _rec(fx, resp, fired, mode="think"):
    return RunRecord(fx, "V1.0", "m", 0, resp, {"q": "q" not in fired, "menu": "menu" not in fired}, mode)


def test_clustering_groups_repeatable_behavior():
    recs = [_rec("A", "What happened at work today?", ["q"]) for _ in range(3)]
    recs += [_rec("B", "Would you like me to listen or reflect?", ["menu"]), _rec("C", "Fine.", [])]
    clusters = cluster_failures(recs)
    assert clusters[0]["fixture"] == "A" and clusters[0]["size"] == 3 and clusters[0]["repeatable"]
    assert all(not c["repeatable"] for c in clusters[1:])
    rep = summarize(recs)
    assert rep["A"]["failures"] == 3 and rep["C"]["failures"] == 0 and "q" in rep["A"]["falsifiers_fired"]


def test_non_regression_rule():
    prev = {"A": {"wilson_lo": 0.0, "wilson_hi": 0.28}}
    good = {"A": {"wilson_lo": 0.0, "wilson_hi": 0.28}}
    bad = {"A": {"wilson_lo": 0.35, "wilson_hi": 0.9}}
    assert non_regression_ok(prev, good) == (True, [])
    ok, problems = non_regression_ok(prev, bad)
    assert not ok and "A:" in problems[0]


def test_corpus_ground_truth_is_true():
    for seed in range(5):
        c = generate_corpus(seed)
        views = [EntryView(e["id"], e["body"], e["created_at_local"]) for e in c.entries]
        t = c.truth
        count, ids = count_occurrences(views, t.recurring_word)
        assert count >= 7 and set(t.recurring_word_entry_ids) <= set(ids)
        table = build_evidence_table(views)
        assert any(a["term"] == t.disappearing.split()[-1] or t.disappearing.split()[-1] in a["term"]
                   for a in table["absences"]), (seed, table["absences"])
        assert c.entries[0]["created_at_local"] < c.entries[-1]["created_at_local"]
        assert len({m for m in table["months"]}) >= 4
        assert generate_corpus(seed).entries == c.entries  # deterministic per seed
