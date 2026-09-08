from __future__ import annotations

from datetime import datetime

from fastapi import APIRouter, HTTPException, Query, Request
from sqlalchemy import select
from sqlalchemy.orm import Session

from ..auth import DB, USER
from ..models import Entry
from ..models import AITurn, JournalSession
from ..schemas import AITurnOut, EntryIn, EntryOut, RespondIn, SubmitOut

router = APIRouter(tags=["entries"])


@router.post("/entries", response_model=SubmitOut)
async def create_entry(payload: EntryIn, request: Request, db: Session = DB, user_id: str = USER):
    engine = request.app.state.engine
    try:
        result = await engine.submit(
            db, user_id, payload.body, created_at_local=payload.created_at_local, tz=payload.tz,
            intent=payload.intent, mode=payload.mode, volume_id=payload.volume_id,
            session_id=payload.session_id, supersedes_entry_id=payload.supersedes_entry_id,
        )
    except KeyError as e:
        raise HTTPException(status_code=404, detail=f"not found: {e}")
    except ValueError as e:
        raise HTTPException(status_code=422, detail=str(e))
    return SubmitOut(entry=EntryOut.model_validate(result.entry), session_id=result.session.id,
                     mode=result.mode, path=result.path,
                     ai_turn=AITurnOut.model_validate(result.ai_turn) if result.ai_turn else None)


@router.get("/entries", response_model=list[EntryOut])
def list_entries(db: Session = DB, user_id: str = USER, volume_id: str | None = None,
                 from_: datetime | None = Query(default=None, alias="from"), to: datetime | None = None,
                 include_superseded: bool = False):
    q = select(Entry).where(Entry.user_id == user_id)
    if volume_id:
        q = q.where(Entry.volume_id == volume_id)
    if from_:
        q = q.where(Entry.created_at_local >= from_.replace(tzinfo=None))
    if to:
        q = q.where(Entry.created_at_local <= to.replace(tzinfo=None))
    rows = db.scalars(q.order_by(Entry.created_at_local, Entry.created_at_utc, Entry.id)).all()
    if not include_superseded:
        superseded = {r.supersedes_entry_id for r in rows if r.supersedes_entry_id}
        rows = [r for r in rows if r.id not in superseded]
    return rows


@router.get("/entries/{entry_id}", response_model=EntryOut)
def get_entry(entry_id: str, db: Session = DB, user_id: str = USER):
    e = db.get(Entry, entry_id)
    if e is None or e.user_id != user_id:
        raise HTTPException(status_code=404, detail="not found")
    return e


@router.post("/entries/{entry_id}/respond", response_model=SubmitOut)
async def respond_to_entry(entry_id: str, payload: RespondIn, request: Request, db: Session = DB, user_id: str = USER):
    """Ask the journal to talk back on an entry that is already saved (no new entry is created)."""
    e = db.get(Entry, entry_id)
    if e is None or e.user_id != user_id:
        raise HTTPException(status_code=404, detail="not found")
    try:
        result = await request.app.state.engine.respond_to_entry(db, user_id, e, mode=payload.mode, intent=payload.intent)
    except ValueError as err:
        raise HTTPException(status_code=422, detail=str(err))
    return SubmitOut(entry=EntryOut.model_validate(e), session_id=result.session.id, mode=result.mode,
                     path=result.path, ai_turn=AITurnOut.model_validate(result.ai_turn) if result.ai_turn else None)


@router.get("/transcripts")
def transcripts(db: Session = DB, user_id: str = USER, volume_id: str | None = None):
    """Pages for the UI: one item per session, with the user's entries and the journal's replies in order.
    Superseded entries are replaced by their latest version. Nothing is summarized."""
    q = select(Entry).where(Entry.user_id == user_id)
    if volume_id:
        q = q.where(Entry.volume_id == volume_id)
    entries = db.scalars(q.order_by(Entry.created_at_utc, Entry.id)).all()
    superseded = {r.supersedes_entry_id for r in entries if r.supersedes_entry_id}
    turns = db.scalars(select(AITurn).where(AITurn.user_id == user_id).order_by(AITurn.created_at)).all()
    sessions = {s.id: s for s in db.scalars(select(JournalSession).where(JournalSession.user_id == user_id)).all()}
    by_session: dict[str, dict] = {}
    for e in entries:
        page = by_session.setdefault(e.session_id or e.id, {"session_id": e.session_id, "volume_id": e.volume_id,
                                                          "date": e.created_at_local.date().isoformat(),
                                                          "created_at_local": e.created_at_local.isoformat(),
                                                          "tz": e.tz, "items": []})
        if e.id in superseded:
            continue
        page["items"].append({"who": "you", "entry_id": e.id, "text": e.body, "source": e.source,
                              "supersedes_entry_id": e.supersedes_entry_id, "at": e.created_at_utc.isoformat()})
    for t in turns:
        page = by_session.get(t.session_id)
        if page is None or not t.body:
            continue
        page["items"].append({"who": "journal", "turn_id": t.id, "text": t.body, "mode": t.mode_at_generation,
                              "path": t.path, "in_reply_to_entry_id": t.in_reply_to_entry_id,
                              "at": t.created_at.isoformat()})
    out = []
    for sid, page in by_session.items():
        page["items"].sort(key=lambda i: i["at"])
        sess = sessions.get(sid)
        page["mode"] = sess.mode if sess else None
        page["closed"] = bool(sess and sess.closed_at)
        page["talked"] = any(i["who"] == "journal" for i in page["items"])
        out.append(page)
    out.sort(key=lambda p: p["created_at_local"], reverse=True)
    return out
