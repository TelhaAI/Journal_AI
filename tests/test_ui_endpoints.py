"""Endpoints added for the wired UI: /transcripts, /entries/{id}/respond, secrets loader, static app."""
from pathlib import Path

from journal_ai.secrets import load_secrets

from .conftest import entry_payload


def test_respond_to_existing_entry_and_transcript(client, provider, db):
    e = client.post("/entries", json=entry_payload("Work was annoying today. Dan brought up Portland again.")).json()
    assert e["ai_turn"] is None
    provider.push("You changed the subject. Whose subject was it?")
    r = client.post(f"/entries/{e['entry']['id']}/respond", json={"mode": "challenge"})
    assert r.status_code == 200, r.text
    out = r.json()
    assert out["mode"] == "challenge" and out["ai_turn"]["body"].startswith("You changed")
    assert out["entry"]["id"] == e["entry"]["id"]  # no new entry was created
    assert len(client.get("/entries").json()) == 1
    provider.push("Everyone, or two or three people?")
    t = client.post(f"/sessions/{out['session_id']}/turns", params={"stream": "false"},
                    json={"message": "The conclusion, probably.", "created_at_local": "2026-09-08T21:05:00", "tz": "America/Chicago"})
    assert t.status_code == 200
    pages = client.get("/transcripts").json()
    assert len(pages) == 1
    items = pages[0]["items"]
    assert [i["who"] for i in items] == ["you", "journal", "you", "journal"]
    assert items[1]["mode"] == "challenge" and pages[0]["talked"] is True
    # you_decide through respond records the model's choice
    provider.push("<mode>listen</mode>I read it.")
    r = client.post(f"/entries/{e['entry']['id']}/respond", json={"intent": "you_decide"})
    assert r.json()["mode"] == "listen"
    assert client.post("/entries/nope/respond", json={"mode": "reflect"}).status_code == 404


def test_pages_split_by_local_date(client, provider):
    client.post("/entries", json={"body": "March entry", "created_at_local": "2026-03-03T21:00:00", "tz": "UTC"})
    client.post("/entries", json={"body": "April entry", "created_at_local": "2026-04-03T21:00:00", "tz": "UTC"})
    pages = client.get("/transcripts").json()
    assert [p["date"] for p in pages] == ["2026-04-03", "2026-03-03"]


def test_secrets_loader_uses_filename_as_label(tmp_path, monkeypatch):
    d = tmp_path / "claude_api_key"
    d.mkdir()
    (d / "my-test-key.txt").write_text("sk-ant-not-a-real-key\n")
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    loaded = load_secrets(tmp_path)
    assert loaded == {"ANTHROPIC_API_KEY": "my-test-key"}
    import os
    assert os.environ["ANTHROPIC_API_KEY"] == "sk-ant-not-a-real-key"
    monkeypatch.setenv("ANTHROPIC_API_KEY", "already-set")
    assert load_secrets(tmp_path) == {}  # env wins; nothing overwritten
    assert load_secrets(Path("/nonexistent")) == {}


def test_frontend_served(client):
    r = client.get("/app/")
    assert r.status_code == 200 and "<x-dc>" in r.text
    assert client.get("/health").json()["secrets"] is not None
