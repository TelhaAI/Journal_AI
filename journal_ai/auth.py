"""V1 auth: the frontend supplies a stable user id. Replace `current_user` with the real
scheme when the frontend team picks one; nothing else in the backend needs to change."""
from __future__ import annotations

from typing import Iterator

from fastapi import Depends, Header, HTTPException
from sqlalchemy.orm import Session

from .config import get_settings
from .db import get_sessionmaker


def get_db() -> Iterator[Session]:
    db = get_sessionmaker()()
    try:
        yield db
        db.commit()
    except Exception:
        db.rollback()
        raise
    finally:
        db.close()


def current_user(x_user_id: str | None = Header(default=None)) -> str:
    if not x_user_id or len(x_user_id) > 64:
        raise HTTPException(status_code=401, detail="X-User-Id header required")
    return x_user_id


def require_admin(x_admin_token: str | None = Header(default=None)) -> None:
    expected = get_settings().admin_token
    if not expected or x_admin_token != expected:
        raise HTTPException(status_code=403, detail="admin token required")


DB = Depends(get_db)
USER = Depends(current_user)
ADMIN = Depends(require_admin)
