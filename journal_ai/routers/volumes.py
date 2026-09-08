from __future__ import annotations

from datetime import datetime, timezone

from fastapi import APIRouter, HTTPException
from sqlalchemy import select
from sqlalchemy.orm import Session

from ..auth import DB, USER
from ..models import Volume
from ..orchestration.sessions import close_volume, ensure_user, open_volume
from ..schemas import VolumeIn, VolumeOut, VolumePatch

router = APIRouter(tags=["volumes"])


@router.get("/volumes", response_model=list[VolumeOut])
def list_volumes(db: Session = DB, user_id: str = USER):
    return db.scalars(select(Volume).where(Volume.user_id == user_id).order_by(Volume.opened_at)).all()


@router.post("/volumes", response_model=VolumeOut, status_code=201)
def create_volume(payload: VolumeIn, db: Session = DB, user_id: str = USER):
    ensure_user(db, user_id)
    return open_volume(db, user_id, payload.title, datetime.now(timezone.utc))


@router.patch("/volumes/{volume_id}", response_model=VolumeOut)
def patch_volume(volume_id: str, payload: VolumePatch, db: Session = DB, user_id: str = USER):
    v = db.get(Volume, volume_id)
    if v is None or v.user_id != user_id:
        raise HTTPException(status_code=404, detail="not found")
    if payload.title is not None:
        v.title = payload.title
    if payload.close and v.closed_at is None:
        close_volume(db, v)
    db.flush()
    return v
