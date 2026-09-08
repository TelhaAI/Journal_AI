"""U6 citation gate, U7 frequency cross-check, U8 evidence extraction, U17 tier assignment."""
from datetime import datetime

from journal_ai.gates.provenance import ProvenanceGate, Tier, log_bytes
from journal_ai.lookback.evidence import EntryView, build_evidence_table, content_hash, count_occurrences


def E(i, body, month, day=1):
    return EntryView(id=f"e{i}", body=body, created_at_local=datetime(2026, month, day, 21, 0))


# A planted corpus: "allowed" 6 times across 4 entries; "the promotion" in months 3-4 only, gone later;
# hedges early, certainty late; "should" 2 times; noise (recipe) once.
CORPUS = [
    E(1, "I don't know if I'm allowed to want this. The promotion talk again at work.", 3, 2),
    E(2, "Maybe I should call my sister. I'm not sure the promotion is even real.", 3, 9),
    E(3, "Am I allowed to say no? I feel like I'm not allowed. The promotion came up again.", 4, 1),
    E(4, "Tried a new recipe tonight, lentils. My sister called about the house.", 4, 12),
    E(5, "I should stop saying allowed. Nobody has to give me permission.", 5, 3),
    E(6, "My sister's house again. I know I won't go back there for the holidays.", 6, 7),
    E(7, "I'm sure now. I won't take the job. I'm allowed to change my mind.", 7, 1),
    E(8, "Quiet day. Sister texted about the house. I'm done being asked.", 7, 20),
    E(9, "I know what I want. Not allowed isn't a thing anymore.", 8, 2),
]


def test_u8_recurring_word_counts_exact():
    t = build_evidence_table(CORPUS)
    rw = {r["term"]: r for r in t["recurring_words"]}
    assert rw["allowed"]["count"] == 6
    assert rw["allowed"]["entry_ids"] == ["e1", "e3", "e5", "e7", "e9"]
    assert rw["should"]["count"] == 2
    assert count_occurrences(CORPUS, "allowed") == (6, ["e1", "e3", "e5", "e7", "e9"])


def test_u8_absence_fires_for_disappearing_term_only():
    t = build_evidence_table(CORPUS)
    terms = {a["term"] for a in t["absences"]}
    assert "promotion" in terms
    assert "allowed" not in terms and "sister" not in terms
    promo = next(a for a in t["absences"] if a["term"] == "promotion")
    assert promo["last_entry_id"] == "e3" and promo["last_third_count"] == 0


def test_u8_certainty_markers_per_month():
    t = build_evidence_table(CORPUS)
    c = t["certainty"]
    assert c["2026-03"]["hedge"] >= 2 and c["2026-03"]["certain"] == 0
    assert c["2026-07"]["certain"] >= 2


def test_u8_then_now_pairs_and_subjects():
    t = build_evidence_table(CORPUS)
    tn = {x["term"]: x for x in t["then_now"]}
    assert tn["allowed"]["then"]["entry_id"] == "e1" and tn["allowed"]["now"]["entry_id"] == "e9"
    subjects = {s["term"] for s in t["recurring_subjects"]}
    assert "sister" in subjects or "sister house" in subjects
    assert "recipe" not in subjects and "lentils" not in subjects


def test_evidence_table_deterministic():
    a, b = build_evidence_table(CORPUS), build_evidence_table(list(reversed(CORPUS)))
    assert a == b


# --------------------------------------------------------------------------- gate
gate = ProvenanceGate(min_entries_for_pattern=3)


def obs(**kw):
    base = {"kind": "observation", "text": "You wrote about being allowed.", "entry_ids": ["e1", "e3", "e5"],
            "quotes": ["allowed to want this"], "claims": []}
    base.update(kw)
    return base


def test_u6_valid_citation_passes():
    r = gate.run({"observations": [obs()]}, CORPUS)
    assert len(r.served) == 1 and r.served[0]["tier"] == "quoted"


def test_u6_nonexistent_entry_id_drops():
    r = gate.run({"observations": [obs(entry_ids=["e1", "zzz"])]}, CORPUS)
    assert r.served == [] and r.dropped[0]["reason"] == "not_quoted"


def test_u6_quote_not_in_entry_drops():
    r = gate.run({"observations": [obs(quotes=["allowed to want everything"])]}, CORPUS)
    assert r.served == []


def test_u6_whitespace_and_curly_normalized_quote_passes():
    r = gate.run({"observations": [obs(quotes=["I  don’t know if\n I'm allowed"])]}, CORPUS)
    assert len(r.served) == 1


def test_u6_out_of_range_entry_drops():
    in_range = CORPUS[:4]  # e5 is outside the requested range
    r = gate.run({"observations": [obs(entry_ids=["e1", "e5"])]}, in_range)
    assert r.served == []


def test_u7_frequency_cross_check():
    several_but_once = obs(text="You mention lentils several times.", entry_ids=["e4"], quotes=["lentils"],
                           claims=[{"type": "count", "term": "lentils", "asserted": 1}])
    r = gate.run({"observations": [several_but_once]}, CORPUS)
    assert r.served == [] and r.dropped[0]["reason"] == "frequency_unsupported"
    four = obs(text="You've written 'allowed' several times.", entry_ids=["e1", "e3", "e5", "e7"],
               claims=[{"type": "count", "term": "allowed", "asserted": 6}])
    r = gate.run({"observations": [four]}, CORPUS)
    assert len(r.served) == 1 and r.served[0]["tier"] == "recomputed"


def test_u17_named_only_is_dropped():
    r = gate.run({"observations": [obs(quotes=[], claims=[{"type": "count", "term": "allowed", "asserted": 6}])]}, CORPUS)
    assert r.served == []
    assert r.log[0]["tier"] == Tier.NAMED.label and r.log[0]["reason"] == "not_quoted"


def test_u17_claim_off_at_small_n_is_stripped_tier_quoted():
    o = obs(text="You wrote should twice.", entry_ids=["e2", "e5"], quotes=["I should call my sister"],
            claims=[{"type": "count", "term": "should", "asserted": 4}])  # actual 2, n<=3 tolerance 0
    r = gate.run({"observations": [o]}, CORPUS)
    assert len(r.served) == 1
    s = r.served[0]
    assert s["tier"] == "quoted" and s["claims"] == []
    assert r.log[0]["claims_stripped"][0]["term"] == "should"


def test_u17_all_checks_pass_is_recomputed_with_tolerance():
    o = obs(text="You used 'allowed' six times across five entries.", entry_ids=["e1", "e3", "e5", "e7", "e9"],
            claims=[{"type": "count", "term": "allowed", "asserted": 5},  # actual 6, n>3 → ±1 ok
                    {"type": "then_now", "term": "allowed", "asserted": ["e1", "e9"]}])
    r = gate.run({"observations": [o]}, CORPUS)
    assert r.served[0]["tier"] == "recomputed"
    assert r.served[0]["label"].endswith("verified")


def test_u17_absence_claim():
    ok = obs(text="The promotion stops appearing after April.", entry_ids=["e1", "e3"],
             quotes=["The promotion talk again"], claims=[{"type": "absence", "term": "promotion", "asserted": True}])
    bad = obs(text="Your sister stops appearing.", entry_ids=["e2", "e6"], quotes=["My sister's house again"],
              claims=[{"type": "absence", "term": "sister", "asserted": True}])
    r = gate.run({"observations": [ok, bad]}, CORPUS)
    assert r.served[0]["tier"] == "recomputed"
    assert r.served[1]["tier"] == "quoted" and r.served[1]["claims"] == []


def test_u17_kind_downgraded_and_hedged_below_three_entries():
    o = obs(kind="possible_pattern", text="You keep circling permission.", entry_ids=["e1", "e3"],
            quotes=["allowed to want this", "Am I allowed to say no?"])
    r = gate.run({"observations": [o]}, CORPUS)
    s = r.served[0]
    assert s["kind"] == "observation"
    assert "caught my attention, not a pattern yet" in s["text"]
    three = obs(kind="possible_pattern", text="Permission comes up a lot.", entry_ids=["e1", "e3", "e5"],
                claims=[{"type": "count", "term": "allowed", "asserted": 6}])
    r = gate.run({"observations": [three]}, CORPUS)
    assert r.served[0]["kind"] == "possible_pattern"


def test_growth_lexicon_is_fail_closed():
    o = obs(text="Look how far you've come on this journey.")
    r = gate.run({"observations": [o]}, CORPUS)
    assert r.served == [] and r.dropped[0]["reason"] == "growth_lexicon"


def test_no_repair_dropped_observation_is_gone():
    r = gate.run({"observations": [obs(quotes=["not in there"]), obs()]}, CORPUS)
    assert len(r.served) == 1 and len(r.dropped) == 1
    assert r.drop_fraction == 0.5


def test_gate_is_deterministic_and_serializable():
    raw = {"observations": [obs(), obs(quotes=["nope"]), obs(kind="interpretation", entry_ids=["e1"])]}
    assert log_bytes(gate.run(raw, CORPUS).log) == log_bytes(gate.run(raw, list(reversed(CORPUS))).log)


def test_content_hash_changes_with_any_edit():
    h1 = content_hash(CORPUS)
    edited = CORPUS[:-1] + [E(9, CORPUS[-1].body + " ", 8, 2)]
    assert h1 != content_hash(edited)
    assert h1 == content_hash(list(reversed(CORPUS)))
