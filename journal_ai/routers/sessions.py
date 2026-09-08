"""Talk Back turns (SSE) and mode control.

SSE event shape (agree with the frontend, plan §4 item 4):
  event: meta    data: {"session_id", "entry_id", "mode", "path"}
  event: token   data: {"t": "..."}         (zero or more)
  event: done    data: {"ai_turn": {...} | null, "mode": "...", "path": "..."}
  event: error   data: {"detail": "..."}
When path is write_silent, the stream is meta → done with ai_turn null (contract item 5).
"""
from __future__ import annotations

import json
from datetime import datetime, timezone

from fastapi import APIRouter, HTTPException, Request
from sqlalchemy.orm import Session
from sse_starlette.sse import EventSourceResponse

from ..auth import DB, USER, get_sessionmaker
from ..models import Event, JournalSession
from ..orchestration.modes import set_mode_explicit
from ..orchestration.sessions import close_session
from ..schemas import AITurnOut, ModeIn, SessionOut, SubmitOut, TurnIn, EntryOut

router = APIRouter(tags=["sessions"])


def _own_session(db: Session, user_id: str, session_id: str) -> JournalSession:
    s = db.get(JournalSession, session_id)
    if s is None or s.user_id != user_id:
        raise HTTPException(status_code=404, detail="session not found")
    return s


def _chunks(text: str, size: int = 12):
    for i in range(0, len(text), size):
        yield text[i: i + size]


@router.post("/sessions/{session_id}/turns")
async def post_turn(session_id: str, payload: TurnIn, request: Request, db: Session = DB, user_id: str = USER,
                    stream: bool = True):
    sess = _own_session(db, user_id, session_id)
    if sess.closed_at is not None:
        raise HTTPException(status_code=409, detail="session is closed; submit a new entry to open one")
    engine = request.app.state.engine
    local = payload.created_at_local.replace(tzinfo=None) if payload.created_at_local else \
        datetime.now(timezone.utc).replace(tzinfo=None)
    tz = payload.tz or "UTC"

    if not stream:
        result = await engine.submit(db, user_id, payload.message, created_at_local=local, tz=tz,
                                     intent=payload.intent, mode=payload.mode, volume_id=sess.volume_id,
                                     session_id=sess.id)
        return SubmitOut(entry=EntryOut.model_validate(result.entry), session_id=sess.id, mode=result.mode,
                         path=result.path, ai_turn=AITurnOut.model_validate(result.ai_turn) if result.ai_turn else None)

    # Streaming path: do the work in its own DB session so the generator owns commit/close.
    db.commit()

    async def gen():
        sdb = get_sessionmaker()()
        try:
            result = await engine.submit(sdb, user_id, payload.message, created_at_local=local, tz=tz,
                                         intent=payload.intent, mode=payload.mode, volume_id=sess.volume_id,
                                         session_id=sess.id)
            sdb.commit()
            yield {"event": "meta", "data": json.dumps({"session_id": sess.id, "entry_id": result.entry.id,
                                                        "mode": result.mode, "path": result.path})}
            turn = result.ai_turn
            if turn is not None and turn.body:
                for c in _chunks(turn.body):
                    yield {"event": "token", "data": json.dumps({"t": c})}
            yield {"event": "done", "data": json.dumps({
                "ai_turn": AITurnOut.model_validate(turn).model_dump(mode="json") if turn else None,
                "mode": result.mode, "path": result.path})}
        except Exception as e:  # noqa: BLE001
            sdb.rollback()
            yield {"event": "error", "data": json.dumps({"detail": str(e)})}
        finally:
            sdb.close()

    return EventSourceResponse(gen())


@router.get("/sessions/{session_id}", response_model=SessionOut)
def get_session(session_id: str, db: Session = DB, user_id: str = USER):
    return _own_session(db, user_id, session_id)


@router.patch("/sessions/{session_id}/mode", response_model=SessionOut)
def patch_mode(session_id: str, payload: ModeIn, db: Session = DB, user_id: str = USER):
    sess = _own_session(db, user_id, session_id)
    set_mode_explicit(sess, payload.mode)
    db.add(Event(user_id=user_id, session_id=sess.id, type="mode_changed", payload={"mode": payload.mode, "by": "user"}))
    db.flush()
    return sess


@router.post("/sessions/{session_id}/close", response_model=SessionOut)
def close(session_id: str, db: Session = DB, user_id: str = USER):
    sess = _own_session(db, user_id, session_id)
    close_session(db, sess)
    return sess
