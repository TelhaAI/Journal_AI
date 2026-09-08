from __future__ import annotations

from fastapi import APIRouter, HTTPException
from sqlalchemy import select
from sqlalchemy.orm import Session

from ..auth import ADMIN, DB, USER
from ..models import PromptVersion
from ..prompts import activate_version, create_version
from ..schemas import PromptVersionIn, PromptVersionOut

router = APIRouter(tags=["prompt-versions"])


@router.get("/prompt-versions", response_model=list[PromptVersionOut])
def list_versions(db: Session = DB, _: str = USER):
    rows = db.scalars(select(PromptVersion).order_by(PromptVersion.created_at)).all()
    return [PromptVersionOut(id=r.id, label=r.label, is_active=r.is_active, notes=r.notes, created_at=r.created_at)
            for r in rows]


@router.get("/prompt-versions/{version_id}", response_model=PromptVersionOut, dependencies=[ADMIN])
def get_version(version_id: str, db: Session = DB):
    r = db.get(PromptVersion, version_id)
    if r is None:
        raise HTTPException(status_code=404, detail="not found")
    return r


@router.post("/prompt-versions", response_model=PromptVersionOut, status_code=201, dependencies=[ADMIN])
def post_version(payload: PromptVersionIn, db: Session = DB):
    if db.scalars(select(PromptVersion).where(PromptVersion.label == payload.label)).first():
        raise HTTPException(status_code=409, detail="label exists; versions are immutable — pick a new label")
    return create_version(db, payload.label, payload.body, payload.addenda, payload.notes, payload.activate)


@router.post("/prompt-versions/{version_id}/activate", response_model=PromptVersionOut, dependencies=[ADMIN])
def activate(version_id: str, db: Session = DB):
    try:
        return activate_version(db, version_id)
    except KeyError:
        raise HTTPException(status_code=404, detail="not found")
