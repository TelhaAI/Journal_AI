"""U9 mode state machine, U10 preservation trigger, U11 volume rollover, U14 safety gate, U15 write policy,
plus insight hold (§5.3) and prompt assembly (§5.2)."""
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import select

from journal_ai.gates.safety import SafetyGate
from journal_ai.models import Event, JournalSession, Volume
from journal_ai.orchestration.modes import apply_intent, detect_insight, extract_model_mode, write_policy
from journal_ai.orchestration.sessions import get_or_open_session

from .conftest import LOCAL, entry_payload


def _post(client, body, **kw):
    r = client.post("/entries", json=entry_payload(body, **kw))
    assert r.status_code == 200, r.text
    return r.json()


LONG = "This is a long enough entry that the write policy will respond to it, if I keep going a little more. " * 3


# --------------------------------------------------------------------------- U9
def test_u9_transitions_pure():
    s = JournalSession(mode="write")
    d = apply_intent(s, "talk", "think")
    assert s.mode == "think" and s.mode_set_by == "user" and d.first_talk and not d.forbid_menu
    d = apply_intent(s, "talk", None)
    assert s.mode == "think" and d.mode == "think" and d.forbid_menu  # persists; no menu re-offer
    d = apply_intent(s, "write", None)
    assert s.mode == "write" and d.mode == "write"
    d = apply_intent(s, "you_decide", None)
    assert d.awaiting_model_choice and d.forbid_menu and s.mode == "you_decide"
    with pytest.raises(ValueError):
        apply_intent(s, "talk", "hypnosis")


def test_u9_you_decide_model_choice_persists(client, provider, db):
    provider.push("<mode>challenge</mode>You say you hate it and also that leaving is irresponsible. Both can't be the whole story.")
    provider.push("Staying with that contradiction.")
    out = _post(client, "I don't know what I need from you. You decide. " + LONG, intent="you_decide")
    assert out["mode"] == "challenge"
    assert not out["ai_turn"]["body"].startswith("<mode>")
    sess = db.get(JournalSession, out["session_id"])
    assert sess.mode == "challenge" and sess.mode_set_by == "model"
    out2 = _post(client, "Go on. " + LONG, intent="talk", session_id=out["session_id"])
    assert out2["mode"] == "challenge"
    db.expire_all()
    assert db.get(JournalSession, out["session_id"]).mode_set_by == "model"


def test_u9_you_decide_menu_rejected_and_regenerated(client, provider, db):
    provider.push("I could listen, reflect, or challenge you — which would you like?")
    provider.push("<mode>reflect</mode>I'll reflect. You keep saying you don't know what you need, and then you keep writing anyway.")
    out = _post(client, "You decide. " + LONG, intent="you_decide")
    assert out["ai_turn"]["regenerated"] is True
    assert "which would you like" not in out["ai_turn"]["body"]
    assert db.get(JournalSession, out["session_id"]).mode == "reflect"


def test_u9_write_intent_resets_and_hold_clears_after_one_turn(client, provider, db):
    provider.push("Room.")
    provider.push("Next.")
    out = _post(client, "Oh. I don't actually want a different job. " + LONG, intent="talk", mode="think")
    sess = db.get(JournalSession, out["session_id"])
    assert sess.hold is False  # cleared after the one turn
    assert db.scalars(select(Event).where(Event.type == "insight_signal")).first() is not None
    _post(client, "short", intent="write", session_id=out["session_id"])
    db.expire_all()
    assert db.get(JournalSession, out["session_id"]).mode == "write"


def test_extract_model_mode():
    assert extract_model_mode("<mode>listen</mode>\nI read it.") == ("I read it.", "listen")
    assert extract_model_mode("<mode>banana</mode>x") == ("x", None)
    assert extract_model_mode("plain") == ("plain", None)


# --------------------------------------------------------------------------- §5.3
@pytest.mark.parametrize("text,expected", [
    ("Oh. I don't actually want a different job.", True),
    ("Wait. That's not it.", True),
    ("I think what I'm really saying is that I'm tired.", True),
    ("Work was annoying today.", False),
    ("Actually the meeting went fine.", True),
])
def test_insight_detection(text, expected):
    assert detect_insight(text) is expected


def test_insight_hold_forces_zero_questions(client, provider):
    provider.push("Something landed there. Do you want to say more?")  # violates: question during hold
    provider.push("Something landed there. I'll leave it with you.")
    out = _post(client, "Oh. I don't actually want a different job. I want my job to matter less to me.",
                intent="talk", mode="think")
    body = out["ai_turn"]["body"]
    assert "?" not in body and out["ai_turn"]["regenerated"]
    assert any("Do not ask a question this turn" in b for b in provider.calls[0]["system"])


# --------------------------------------------------------------------------- U10
def _run_substantive_session(client, provider, db, i):
    provider.push("Here.")
    out = _post(client, f"Session {i}. " + LONG, intent="talk")
    client.post(f"/sessions/{out['session_id']}/close")
    return out["session_id"]


def test_u10_preservation_fires_at_third_close_once_only(client, provider, db):
    _run_substantive_session(client, provider, db, 0)
    _run_substantive_session(client, provider, db, 1)
    provider.push("Here.")
    third = _post(client, "third " + LONG, intent="talk")  # 2 closed so far: nothing yet
    assert not any("exported in full" in b for b in provider.calls[-1]["system"])
    provider.push("Here.")
    _post(client, "still third " + LONG, intent="talk", session_id=third["session_id"])
    assert not any("exported in full" in b for b in provider.calls[-1]["system"])  # never mid-session
    client.post(f"/sessions/{third['session_id']}/close")  # 3rd substantive close
    provider.push("Here.")
    out = _post(client, "after " + LONG, intent="talk")  # next open carries the addendum
    assert any("exported in full" in b for b in provider.calls[-1]["system"])
    assert len(db.scalars(select(Event).where(Event.type == "preservation_offered")).all()) == 1
    provider.push("Here.")
    _post(client, "again " + LONG, intent="talk", session_id=out["session_id"])
    assert not any("exported in full" in b for b in provider.calls[-1]["system"])
    client.post(f"/sessions/{out['session_id']}/close")
    provider.push("Here.")
    _post(client, "later " + LONG, intent="talk")  # once only
    assert not any("exported in full" in b for b in provider.calls[-1]["system"])
    assert len(db.scalars(select(Event).where(Event.type == "preservation_offered")).all()) == 1


def test_u10_not_before_threshold_and_declined_suppresses(client, provider, db):
    _run_substantive_session(client, provider, db, 0)
    provider.push("Here.")
    _post(client, "two " + LONG, intent="talk")
    assert not any("exported in full" in b for b in provider.calls[-1]["system"])
    client.post("/events/preservation-declined")
    for i in range(4):
        _run_substantive_session(client, provider, db, i)
    provider.push("Here.")
    _post(client, "after decline " + LONG, intent="talk")
    assert not any("exported in full" in b for b in provider.calls[-1]["system"])


def test_u10_non_substantive_sessions_do_not_count(client, provider, db):
    for i in range(5):
        out = _post(client, "tiny")  # < 50 words, 1 turn → not substantive
        client.post(f"/sessions/{out['session_id']}/close")
    provider.push("Here.")
    _post(client, "x " + LONG, intent="talk")
    assert not any("exported in full" in b for b in provider.calls[-1]["system"])


def test_session_inactivity_closes_and_reopens(db, settings):
    from journal_ai.orchestration.sessions import ensure_user, open_volume

    ensure_user(db, "u")
    v = open_volume(db, "u", None, LOCAL)
    t0 = datetime(2026, 9, 8, 21, 0, tzinfo=timezone.utc)
    s1 = get_or_open_session(db, "u", v, t0)
    s2 = get_or_open_session(db, "u", v, t0 + timedelta(minutes=5))
    assert s1.id == s2.id
    s3 = get_or_open_session(db, "u", v, t0 + timedelta(minutes=settings.session_inactivity_minutes + 6))
    assert s3.id != s1.id and s1.closed_at is not None


# --------------------------------------------------------------------------- U11
def test_u11_rollover_fires_once_and_user_close_resets(client, provider, db, settings):
    settings.volume_rollover_entries = 5
    for i in range(4):
        _post(client, f"entry {i}")
    assert db.scalars(select(Event).where(Event.type == "volume_rollover_suggested")).all() == []
    out = _post(client, "entry 4")
    db.expire_all()
    vol = db.get(Volume, out["entry"]["volume_id"])
    assert vol.rollover_suggested is True
    assert len(db.scalars(select(Event).where(Event.type == "volume_rollover_suggested")).all()) == 1
    provider.push("Here.")
    _post(client, "talk " + LONG, intent="talk")  # next natural stop: the addendum rides along once
    assert any("close this chapter" in b for b in provider.calls[-1]["system"])
    _post(client, "entry 5")
    _post(client, "entry 6")
    assert len(db.scalars(select(Event).where(Event.type == "volume_rollover_suggested")).all()) == 1
    provider.push("Here.")
    _post(client, "talk again " + LONG, intent="talk")
    assert not any("close this chapter" in b for b in provider.calls[-1]["system"])
    # user closes the volume → a fresh volume, no suggestion pending
    r = client.patch(f"/volumes/{vol.id}", json={"close": True})
    assert r.json()["closed_at"] is not None
    out = _post(client, "fresh")
    assert out["entry"]["volume_id"] != vol.id
    assert db.get(Volume, out["entry"]["volume_id"]).rollover_suggested is False


# --------------------------------------------------------------------------- U14
CRISIS = [
    "I don't want to be alive anymore. I've been thinking about how to end it all.",
    "I want to kill myself.",
    "I'm going to hurt myself tonight.",
]
NOT_CRISIS = [
    "I feel really sad tonight. I don't even know why.",
    "Work was annoying today. I don't really feel like getting into it.",
    "I'm exhausted and I hate my job but leaving now would be irresponsible.",
    "My grandmother died last year and I still miss her.",
    "I'm not suicidal, I'm just tired of everything.",
]


@pytest.mark.parametrize("text", CRISIS)
def test_u14_crisis_routes_to_safety_path(text, client, provider, db):
    provider.push("I'm glad you told me. Are you safe right now? If you're in the US you can call or text 988.")
    out = _post(client, text, intent="talk", mode="think")
    assert out["path"] == "safety" and out["mode"] == "safety"
    sys_blocks = provider.calls[-1]["system"]
    assert len(sys_blocks) == 1 and "immediate risk" in sys_blocks[0]  # journaling prompt bypassed
    assert db.scalars(select(Event).where(Event.type == "safety_triggered")).first() is not None


@pytest.mark.parametrize("text", NOT_CRISIS)
def test_u14_difficult_but_not_crisis_passes(text):
    assert SafetyGate.lexical(text).risk is False


@pytest.mark.parametrize("text", CRISIS)
def test_u14_lexical(text):
    assert SafetyGate.lexical(text).risk is True


# --------------------------------------------------------------------------- U15
def test_u15_write_policy_pure():
    assert write_policy(10, "short", 0).respond is False
    assert write_policy(10, "short", 1).respond is False
    p = write_policy(10, "short", 2)
    assert p.respond and p.ack and p.max_sentences == 1 and p.max_questions == 0
    p = write_policy(40, "x " * 40, 0)
    assert p.respond and not p.ack and p.max_sentences == 2 and p.max_questions == 0
    p = write_policy(5, "Am I wrong about this?", 0)
    assert p.respond and p.max_questions == 1


def test_u15_write_mode_through_api(client, provider):
    assert _post(client, "Work was annoying today.")["ai_turn"] is None
    assert _post(client, "Still is.")["ai_turn"] is None
    provider.push("I'm here, and I read all three.")
    out = _post(client, "Third short one.")
    assert out["path"] == "ack" and out["ai_turn"] is not None
    assert any("several entries without hearing" in b for b in provider.calls[-1]["system"])
    provider.push("I read that. It sounds like a heavy day, and you don't have to get into it.")
    out = _post(client, LONG)
    assert out["path"] == "write"
    assert out["ai_turn"]["body"].count(".") <= 2 and "?" not in out["ai_turn"]["body"]
    # a long reply is regenerated with a tightening instruction, not truncated
    provider.push("One. Two. Three. Four.")
    provider.push("One. Two.")
    out = _post(client, LONG + " again")
    assert out["ai_turn"]["regenerated"] and out["ai_turn"]["body"] == "One. Two."
    assert any("at most 2 sentences" in b for b in provider.calls[-1]["system"])


# --------------------------------------------------------------------------- §5.2
def test_prompt_assembly_order_and_context(client, provider):
    earlier = _post(client, "Earlier entry about the sister and the house.")
    client.post(f"/sessions/{earlier['session_id']}/close")  # earlier session → shows up as volume context
    provider.push("Ok.")
    out = _post(client, "Now talk. " + LONG, intent="talk", mode="reflect")
    blocks = provider.calls[-1]["system"]
    assert blocks[0].startswith("You are a journal")             # master instructions verbatim, first
    assert blocks[1].startswith("Mode: REFLECT")                   # mode addendum
    assert any(b.startswith("CONTEXT") and "sister and the house" in b for b in blocks)  # full text, not summary
    assert any("first time in this session" in b for b in blocks)  # first_talk once
    provider.push("Ok.")
    _post(client, "More. " + LONG, intent="talk", session_id=out["session_id"])
    assert not any("first time in this session" in b for b in provider.calls[-1]["system"])
    # session history is passed as messages, user's words verbatim
    msgs = provider.calls[-1]["messages"]
    assert msgs[0][0] == "user" and "Now talk." in msgs[0][1] and msgs[-1][0] == "user"
