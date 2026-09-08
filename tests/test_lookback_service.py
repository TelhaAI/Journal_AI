"""U18 receipt replay, U19 content hash / record-changed refusal, U20 fallback path, Test K, and the
end-to-end Look Back route with a scripted model."""
import json
from datetime import datetime

import pytest
from sqlalchemy import select

from journal_ai.gates.provenance import GATE_VERSION
from journal_ai.lookback.service import FALLBACK_MESSAGE, INSUFFICIENT_MESSAGE, RecordChangedError, replay_receipt
from journal_ai.models import Event, LookbackReceipt

BODIES = [
    (3, 2, "I don't know if I'm allowed to want this. The promotion talk again at work."),
    (3, 9, "Maybe I should call my sister. I'm not sure the promotion is even real."),
    (4, 1, "Am I allowed to say no? I feel like I'm not allowed. The promotion came up again."),
    (4, 12, "Tried a new recipe tonight, lentils. My sister called about the house."),
    (5, 3, "I should stop saying allowed. Nobody has to give me permission."),
    (6, 7, "My sister's house again. I know I won't go back there for the holidays."),
    (7, 1, "I'm sure now. I won't take the job. I'm allowed to change my mind."),
    (7, 20, "Quiet day. Sister texted about the house. I'm done being asked."),
    (8, 2, "I know what I want. Not allowed isn't a thing anymore."),
]


def seed(client, n=None):
    ids = []
    for m, d, body in BODIES[: n or len(BODIES)]:
        r = client.post("/entries", json={"body": body, "created_at_local": datetime(2026, m, d, 21).isoformat(),
                                          "tz": "America/Chicago", "intent": "write"})
        ids.append(r.json()["entry"]["id"])
    return ids


def good_json(ids):
    return json.dumps({"observations": [
        {"kind": "possible_pattern", "text": "You used 'allowed' six times across five entries.",
         "entry_ids": [ids[0], ids[2], ids[4], ids[6], ids[8]], "quotes": ["allowed to want this"],
         "claims": [{"type": "count", "term": "allowed", "asserted": 6},
                    {"type": "then_now", "term": "allowed", "asserted": [ids[0], ids[8]]}]},
        {"kind": "observation", "text": "The promotion stops appearing after April.",
         "entry_ids": [ids[0], ids[2]], "quotes": ["The promotion came up again"],
         "claims": [{"type": "absence", "term": "promotion", "asserted": True}]},
    ], "closing": "That's what I can point to."})


def bad_json(ids):
    return json.dumps({"observations": [
        {"kind": "interpretation", "text": "You've grown so much on this journey.", "entry_ids": [ids[0]],
         "quotes": ["allowed to want this"], "claims": []},
        {"kind": "observation", "text": "You keep mentioning your father.", "entry_ids": [ids[1]],
         "quotes": ["my father"], "claims": []},
        {"kind": "observation", "text": "Something about lentils.", "entry_ids": ["nope"], "quotes": ["lentils"],
         "claims": []},
    ], "closing": "Keep growing!"})


def test_lookback_served_with_receipt(client, provider, db):
    ids = seed(client)
    provider.push(good_json(ids))
    r = client.post("/lookback", json={})
    assert r.status_code == 200, r.text
    out = r.json()
    assert out["outcome"] == "served" and len(out["observations"]) == 2
    tiers = {o["tier"] for o in out["observations"]}
    assert tiers == {"recomputed"}
    assert out["observations"][0]["kind"] == "possible_pattern"
    assert out["observations"][1]["kind"] == "observation" and "caught my attention" in out["observations"][1]["text"]
    assert out["based_on"]["entry_count"] == 9 and out["gate_version"] == GATE_VERSION
    assert out["message"] == "That's what I can point to."
    rc = client.get(f"/lookback/{out['report_id']}/receipt").json()
    assert rc["input_entry_ids"] == ids and len(rc["input_content_hash"]) == 64
    assert rc["raw_model_output"][0]["observations"][0]["kind"] == "possible_pattern"
    assert rc["verification_log"][0][0]["verdict"] == "served"
    # the model saw the evidence table and the raw entries, never a summary
    user_msg = provider.calls[-1]["messages"][0][1]
    assert "EVIDENCE TABLE" in user_msg and BODIES[3][2] in user_msg


def test_u20_fallback_after_two_bad_attempts(client, provider, db):
    ids = seed(client)
    provider.push(bad_json(ids), bad_json(ids))
    out = client.post("/lookback", json={}).json()
    assert out["outcome"] == "fallback" and out["observations"] == []
    assert out["message"] == FALLBACK_MESSAGE
    assert out["regenerated"] is True
    lb_calls = [c for c in provider.calls if "EVIDENCE TABLE" in c["messages"][0][1]]
    assert len(lb_calls) == 2  # never a third attempt
    assert any("unsupported by the record" in b for b in lb_calls[1]["system"])
    rc = client.get(f"/lookback/{out['report_id']}/receipt").json()
    assert len(rc["raw_model_output"]) == 2 and rc["outcome"] == "fallback"
    assert db.scalars(select(Event).where(Event.type == "lookback_fallback")).first() is not None
    assert db.scalars(select(Event).where(Event.type == "citation_dropped")).all()


def test_regenerate_once_then_serve(client, provider, db):
    ids = seed(client)
    provider.push(bad_json(ids), good_json(ids))
    out = client.post("/lookback", json={}).json()
    assert out["outcome"] == "served" and out["regenerated"] is True and len(out["observations"]) == 2


def test_partial_when_some_drop_under_threshold(client, provider, db):
    ids = seed(client)
    g = json.loads(good_json(ids))
    g["observations"].append({"kind": "observation", "text": "Lentils.", "entry_ids": ["zzz"], "quotes": ["lentils"]})
    provider.push(json.dumps(g))
    out = client.post("/lookback", json={}).json()
    assert out["outcome"] == "partial" and len(out["observations"]) == 2


def test_k_not_enough_history(client, provider):
    seed(client, 2)
    out = client.post("/lookback", json={}).json()
    assert out["outcome"] == "fallback" and out["observations"] == [] and out["message"] == INSUFFICIENT_MESSAGE
    assert not [c for c in provider.calls if "EVIDENCE TABLE" in c["messages"][0][1]]  # no model call at all
    assert client.get(f"/lookback/{out['report_id']}/receipt").status_code == 200


def test_u18_receipt_replay_reproduces(client, provider, db):
    ids = seed(client)
    for i in range(50):
        provider.push(good_json(ids) if i % 3 else bad_json(ids), good_json(ids))
        client.post("/lookback", json={})
    receipts = db.scalars(select(LookbackReceipt)).all()
    assert len(receipts) == 50
    for rc in receipts:
        assert replay_receipt(db, rc) is True
    assert all(client.post(f"/lookback/{rc.report_id}/replay").json()["reproduced"] for rc in receipts[:5])


def test_u19_record_change_refuses_replay(client, provider, db):
    ids = seed(client)
    provider.push(good_json(ids))
    report_id = client.post("/lookback", json={}).json()["report_id"]
    rc = db.scalars(select(LookbackReceipt)).one()
    h1 = rc.input_content_hash
    # supersede one entry in the input set
    client.post("/entries", json={"body": BODIES[0][2] + " (edited)", "created_at_local": "2026-03-02T21:00:00",
                                  "tz": "America/Chicago", "supersedes_entry_id": ids[0]})
    # a new run sees a different record → different hash
    provider.push(good_json(ids))
    client.post("/lookback", json={})
    db.expire_all()
    hashes = {r.input_content_hash for r in db.scalars(select(LookbackReceipt)).all()}
    assert len(hashes) == 2 and h1 in hashes
    # the original receipt still replays (its entry rows are untouched — superseding never edits in place)
    assert replay_receipt(db, rc) is True
    # but if the record it points to changed underneath it, replay is refused
    rc.input_content_hash = "0" * 64
    with pytest.raises(RecordChangedError):
        replay_receipt(db, rc)
    r = client.post(f"/lookback/{report_id}/replay")
    assert r.status_code == 409 and "record changed" in r.json()["detail"]


def test_lookback_range_and_volume_filters(client, provider):
    seed(client)
    provider.push(json.dumps({"observations": [], "closing": ""}), json.dumps({"observations": [], "closing": ""}))
    out = client.post("/lookback", json={"from": "2026-03-01T00:00:00", "to": "2026-04-30T23:59:59"}).json()
    assert out["based_on"]["entry_count"] == 4
    assert out["outcome"] == "fallback"  # model returned nothing: fail closed
