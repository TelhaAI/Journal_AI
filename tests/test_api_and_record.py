"""U1 immutability, U2 export fidelity, U12 prompt versioning, U13 local time, U16 API contract,
plus SSE event shape and export-then-delete."""
import json

import pytest
from sqlalchemy import select, text
from sqlalchemy.exc import IntegrityError, OperationalError

from journal_ai.models import AITurn, Entry, Event, LookbackReceipt, PromptVersion, User
from journal_ai.prompts import activate_version, create_version

from .conftest import entry_payload


def _post(client, body, **kw):
    r = client.post("/entries", json=entry_payload(body, **kw))
    assert r.status_code == 200, r.text
    return r.json()


# --------------------------------------------------------------------------- U1
def test_u1_entries_cannot_be_updated_or_deleted(client, db):
    out = _post(client, "The first thing I wrote.")
    with pytest.raises((IntegrityError, OperationalError)):
        db.execute(text("UPDATE entries SET body='x' WHERE id=:id"), {"id": out["entry"]["id"]})
        db.commit()
    db.rollback()
    with pytest.raises((IntegrityError, OperationalError)):
        db.execute(text("DELETE FROM entries WHERE id=:id"), {"id": out["entry"]["id"]})
        db.commit()
    db.rollback()
    assert db.get(Entry, out["entry"]["id"]).body == "The first thing I wrote."


def test_u1_supersede_creates_new_row_keeps_old(client, db):
    old = _post(client, "Draft one.")["entry"]
    new = _post(client, "Draft one, corrected.", supersedes_entry_id=old["id"])["entry"]
    assert new["supersedes_entry_id"] == old["id"]
    assert db.get(Entry, old["id"]).body == "Draft one."
    listed = client.get("/entries").json()
    assert [e["id"] for e in listed] == [new["id"]]
    with_old = client.get("/entries", params={"include_superseded": "true"}).json()
    assert len(with_old) == 2


def test_u1_ai_turns_and_prompt_bodies_immutable(client, db, provider):
    provider.push("I read that.")
    _post(client, "A" + " word" * 40, intent="talk")
    turn = db.scalars(select(AITurn)).first()
    with pytest.raises((IntegrityError, OperationalError)):
        db.execute(text("UPDATE ai_turns SET body='x' WHERE id=:id"), {"id": turn.id})
        db.commit()
    db.rollback()
    pv = db.scalars(select(PromptVersion)).first()
    with pytest.raises((IntegrityError, OperationalError)):
        db.execute(text("UPDATE prompt_versions SET body='x' WHERE id=:id"), {"id": pv.id})
        db.commit()
    db.rollback()


# --------------------------------------------------------------------------- U2
def test_u2_export_round_trip_byte_equal(client, provider):
    bodies = []
    for i in range(50):
        b = f"Entry {i} — üñíçødé ☃ 日本語\n\n  leading spaces, trailing tab\t\r\nline {i}\n" + "x " * (i % 7)
        bodies.append(b)
        _post(client, b)
    provider.push("Here.")
    _post(client, "Talk to me about this one please, it is long enough to warrant a response " * 2, intent="talk")
    exp = client.get("/export").json()
    assert "ai_turns" not in exp
    got = [e["body"] for e in exp["entries"]]
    assert got[:50] == bodies
    md = client.get("/export", params={"format": "md"}).text
    for b in bodies:
        assert b in md
    assert "Responses from the journal" not in md
    md_ai = client.get("/export", params={"format": "md", "include_ai": "true"}).text
    assert "Responses from the journal" in md_ai and md_ai.index("Responses from the journal") > md_ai.rindex(bodies[-1])


def test_u2_encryption_at_rest_roundtrips(settings, engine, provider, db):
    from cryptography.fernet import Fernet
    from journal_ai.main import create_app
    from fastapi.testclient import TestClient

    settings.encryption_key = Fernet.generate_key().decode()
    with TestClient(create_app(provider, init=False)) as c:
        c.headers.update({"X-User-Id": "enc-user"})
        body = "Secret ☃ entry"
        eid = c.post("/entries", json=entry_payload(body)).json()["entry"]["id"]
        assert c.get("/export").json()["entries"][0]["body"] == body
    raw = db.execute(text("SELECT body FROM entries WHERE id=:id"), {"id": eid}).scalar()
    assert raw.startswith("enc:") and body not in raw
    assert c.get("/health").json()["encryption_at_rest"] is True


# --------------------------------------------------------------------------- U12
def test_u12_prompt_versioning(client, db, provider):
    provider.push("Yes.")
    _post(client, "Long enough entry to get a reply from the journal, I would think, yes indeed it is " * 2,
          intent="talk")
    old = db.scalars(select(PromptVersion).where(PromptVersion.is_active)).one()
    old_body = old.body
    r = client.post("/prompt-versions", headers={"X-Admin-Token": "test-admin"},
                    json={"label": "V1.1", "body": "new body", "activate": True})
    assert r.status_code == 201
    db.expire_all()
    actives = db.scalars(select(PromptVersion).where(PromptVersion.is_active)).all()
    assert [a.label for a in actives] == ["V1.1"]
    assert db.get(PromptVersion, old.id).body == old_body
    assert db.scalars(select(AITurn)).first().prompt_version_id == old.id
    # duplicate labels refused; admin required
    assert client.post("/prompt-versions", headers={"X-Admin-Token": "test-admin"},
                       json={"label": "V1.1", "body": "x"}).status_code == 409
    assert client.post("/prompt-versions", json={"label": "V1.2", "body": "x"}).status_code == 403
    assert client.get("/prompt-versions").status_code == 200


def test_u12_only_one_active(db):
    a = create_version(db, "T1", "a", activate=True)
    create_version(db, "T2", "b", activate=True)
    activate_version(db, a.id)
    actives = db.scalars(select(PromptVersion).where(PromptVersion.is_active)).all()
    assert [x.id for x in actives] == [a.id]


# --------------------------------------------------------------------------- U13
def test_u13_local_time_sorts_into_local_month(client):
    late_sept = "2026-09-30T23:30:00"  # 04:30 UTC on Oct 1
    e = client.post("/entries", json={"body": "late", "created_at_local": late_sept, "tz": "America/Chicago",
                                       "intent": "write"}).json()["entry"]
    assert e["created_at_local"].startswith("2026-09-30T23:30")
    sept = client.get("/entries", params={"from": "2026-09-01T00:00:00", "to": "2026-09-30T23:59:59"}).json()
    octo = client.get("/entries", params={"from": "2026-10-01T00:00:00", "to": "2026-10-31T23:59:59"}).json()
    assert [x["id"] for x in sept] == [e["id"]] and octo == []
    # offsets attached by the client are stripped: wall-clock is what's stored
    e2 = client.post("/entries", json={"body": "offset", "created_at_local": "2026-09-30T23:30:00-05:00",
                                        "tz": "America/Chicago"}).json()["entry"]
    assert e2["created_at_local"].startswith("2026-09-30T23:30")


# --------------------------------------------------------------------------- U16
def test_u16_auth_required(app):
    from fastapi.testclient import TestClient

    with TestClient(app) as c:
        assert c.get("/entries").status_code == 401
        assert c.post("/entries", json=entry_payload("x")).status_code == 401


def test_u16_user_isolation(client, app, provider):
    a_entry = _post(client, "A's private words.")["entry"]
    a_session = _post(client, "more")["session_id"]
    from fastapi.testclient import TestClient

    with TestClient(app) as b:
        b.headers.update({"X-User-Id": "user-b"})
        assert b.get(f"/entries/{a_entry['id']}").status_code == 404
        assert b.get("/entries").json() == []
        assert b.get(f"/sessions/{a_session}").status_code == 404
        assert b.patch(f"/sessions/{a_session}/mode", json={"mode": "think"}).status_code == 404
        assert b.post(f"/sessions/{a_session}/turns", json={"message": "hi"}, params={"stream": "false"}).status_code == 404
        assert b.post("/entries", json=entry_payload("x", volume_id=a_entry["volume_id"])).status_code == 404
        assert b.post("/entries", json=entry_payload("x", session_id=a_session)).status_code == 404
        assert b.post("/entries", json=entry_payload("x", supersedes_entry_id=a_entry["id"])).status_code == 404
        assert b.get("/export").json()["entries"] == []
        assert b.get("/lookback").json() == []


def test_u16_schema_validation(client):
    assert client.post("/entries", json={"body": "x"}).status_code == 422
    assert client.post("/entries", json=entry_payload("x", mode="nonsense")).status_code == 422
    assert client.post("/entries", json=entry_payload("x", intent="guess")).status_code == 422
    assert client.post("/entries", json=entry_payload("")).status_code == 422


def test_u16_sse_event_shape(client, provider):
    provider.push("I read that. It sounds like a long day.")
    sid = _post(client, "Opening entry, short.")["session_id"]
    with client.stream("POST", f"/sessions/{sid}/turns",
                       json={"message": "Talk to me about today, it was a long one and I want to think.",
                             "mode": "think"}) as r:
        assert r.status_code == 200
        assert r.headers["content-type"].startswith("text/event-stream")
        raw = "".join(r.iter_text())
    events = []
    for block in raw.replace("\r\n", "\n").strip().split("\n\n"):
        ev, data = None, None
        for line in block.splitlines():
            if line.startswith("event:"):
                ev = line[6:].strip()
            elif line.startswith("data:"):
                data = json.loads(line[5:].strip())
        if ev:
            events.append((ev, data))
    kinds = [e for e, _ in events]
    assert kinds[0] == "meta" and kinds[-1] == "done" and "token" in kinds
    assert events[0][1]["session_id"] == sid and events[0][1]["mode"] == "think"
    assert "".join(d["t"] for e, d in events if e == "token") == events[-1][1]["ai_turn"]["body"]


def test_u16_sse_silent_write_ends_cleanly(client):
    sid = _post(client, "short one")["session_id"]
    # a write-intent through /entries with the same session: no AI turn, no spinner forever
    out = _post(client, "another short one", session_id=sid)
    assert out["ai_turn"] is None and out["path"] == "write_silent"


# --------------------------------------------------------------------------- delete
def test_delete_me_export_then_zero_rows(client, db, provider):
    provider.push("Here.")
    for i in range(3):
        _post(client, f"entry {i} " * (20 if i == 2 else 2), intent="talk" if i == 2 else "write")
    r = client.delete("/me")
    assert r.status_code == 200
    exp = r.json()["export"]
    assert len(exp["entries"]) == 3 and len(exp["ai_turns"]) == 1
    for model in (Entry, AITurn, Event, User, LookbackReceipt):
        col = model.id if model is User else model.user_id
        assert db.scalars(select(model).where(col == "user-a")).all() == []
    assert client.get("/entries").json() == []
