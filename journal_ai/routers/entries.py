from __future__ import annotations

from datetime import datetime

from fastapi import APIRouter, HTTPException, Query, Request
from sqlalchemy import select
from sqlalchemy.orm import Session

from ..auth import DB, USER
from ..models import Entry
from ..schemas import AITurnOut, EntryIn, EntryOut, SubmitOut

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
