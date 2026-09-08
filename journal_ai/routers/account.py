"""Export, export-then-delete, UI-driven events, health."""
from __future__ import annotations

from fastapi import APIRouter, Request
from fastapi.responses import PlainTextResponse
from sqlalchemy import delete, func, select
from sqlalchemy.orm import Session

from ..auth import DB, USER
from ..config import get_settings
from ..db import allow_entry_deletion
from ..export import export_json, export_markdown
from ..gates.provenance import GATE_VERSION
from ..models import USER_TABLES, Event, Entry, JournalSession
from ..prompts import active_version

router = APIRouter(tags=["account"])


@router.get("/export")
def export(db: Session = DB, user_id: str = USER, format: str = "json", include_ai: bool = False):
    if format == "md":
        return PlainTextResponse(export_markdown(db, user_id, include_ai), media_type="text/markdown; charset=utf-8")
    return export_json(db, user_id, include_ai)


@router.delete("/me")
def delete_me(db: Session = DB, user_id: str = USER):
    """Export-then-delete. Returns the full export (entries + AI turns) and removes every row for the user."""
    payload = export_json(db, user_id, include_ai=True)
    with allow_entry_deletion(db):
        for model in USER_TABLES:
            col = model.id if model.__tablename__ == "users" else model.user_id
            db.execute(delete(model).where(col == user_id))
    db.flush()
    return {"deleted": True, "export": payload}


@router.post("/events/preservation-declined", status_code=204)
def preservation_declined(db: Session = DB, user_id: str = USER, session_id: str | None = None):
    db.add(Event(user_id=user_id, session_id=session_id, type="preservation_declined", payload={}))
    db.flush()


@router.get("/me/observables")
def observables(db: Session = DB, user_id: str = USER):
    """Section 10 observables, computed from the DB without log reading."""
    entries = db.scalar(select(func.count(Entry.id)).where(Entry.user_id == user_id)) or 0
    talk_entries = db.scalar(select(func.count(Entry.id)).where(Entry.user_id == user_id, Entry.source == "talk")) or 0
    sessions = db.scalars(select(JournalSession).where(JournalSession.user_id == user_id)
                          .order_by(JournalSession.started_at)).all()
    days = sorted({s.started_at.date().isoformat() for s in sessions})
    ev_counts = dict(db.execute(select(Event.type, func.count(Event.id)).where(Event.user_id == user_id)
                                .group_by(Event.type)).all())
    return {
        "wrote_second_entry": entries >= 2,
        "entries": entries,
        "voluntarily_asked_to_engage": talk_entries > 0,
        "talk_entries": talk_entries,
        "sessions": len(sessions),
        "distinct_days": len(days),
        "returned_unprompted": len(days) >= 2,
        "substantive_sessions": sum(1 for s in sessions if s.substantive),
        "events": ev_counts,
    }


@router.get("/health")
def health(request: Request, db: Session = DB):
    s = get_settings()
    try:
        pv = active_version(db).label
    except RuntimeError:
        pv = None
    return {"ok": True, "prompt_version": pv, "llm_provider": s.llm_provider, "model": s.llm_model,
            "gate_version": GATE_VERSION, "encryption_at_rest": bool(s.encryption_key),
            "database": s.database_url.split("://")[0],
            "secrets": getattr(s, "secrets_loaded", {})}  # labels only, never values
