"""Session and volume lifecycle: open/close, substantive flag, preservation trigger (§5.4),
volume rollover (§5.4)."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from ..config import get_settings
from ..models import Entry, Event, JournalSession, User, Volume


def _now(now: datetime | None) -> datetime:
    return now or datetime.now(timezone.utc)


def ensure_user(db: Session, user_id: str) -> User:
    u = db.get(User, user_id)
    if u is None:
        u = User(id=user_id)
        db.add(u)
        db.flush()
    return u


def suggest_volume_title(local: datetime) -> str:
    m = local.month
    season = "Winter" if m in (12, 1, 2) else "Spring" if m in (3, 4, 5) else "Summer" if m in (6, 7, 8) else "Fall"
    year = local.year + 1 if m == 12 else local.year
    return f"Journal — {season} {year}"


def open_volume(db: Session, user_id: str, title: str | None, local: datetime, now: datetime | None = None) -> Volume:
    v = Volume(user_id=user_id, title=title or suggest_volume_title(local), opened_at=_now(now))
    db.add(v)
    db.flush()
    return v


def current_volume(db: Session, user_id: str, local: datetime, now: datetime | None = None) -> Volume:
    v = db.scalars(select(Volume).where(Volume.user_id == user_id, Volume.closed_at.is_(None))
                   .order_by(Volume.opened_at.desc())).first()
    return v or open_volume(db, user_id, None, local, now)


def close_volume(db: Session, volume: Volume, now: datetime | None = None) -> None:
    volume.closed_at = _now(now)
    db.flush()


def _substantive_closed_sessions(db: Session, user_id: str) -> int:
    return db.scalar(select(func.count(JournalSession.id)).where(
        JournalSession.user_id == user_id, JournalSession.closed_at.is_not(None), JournalSession.substantive.is_(True)
    )) or 0


def _has_event(db: Session, user_id: str, *types: str) -> bool:
    return db.scalar(select(func.count(Event.id)).where(Event.user_id == user_id, Event.type.in_(types))) > 0


def preservation_due(db: Session, user_id: str) -> bool:
    s = get_settings()
    if _has_event(db, user_id, "preservation_offered", "preservation_declined"):
        return False
    return _substantive_closed_sessions(db, user_id) >= s.preservation_sessions_threshold


def close_session(db: Session, session: JournalSession, now: datetime | None = None) -> None:
    """Inactivity or explicit close. Preservation is *evaluated* here but only *offered* at the next open."""
    if session.closed_at is not None:
        return
    session.closed_at = _now(now)
    db.add(Event(user_id=session.user_id, session_id=session.id, type="session_closed",
                 payload={"substantive": session.substantive, "turns": session.turn_count}))
    db.flush()


def get_or_open_session(db: Session, user_id: str, volume: Volume, now: datetime | None = None,
                        session_id: str | None = None) -> JournalSession:
    now = _now(now)
    s = get_settings()
    if session_id:
        sess = db.get(JournalSession, session_id)
        if sess is None or sess.user_id != user_id:
            raise KeyError(session_id)
        if sess.closed_at is None:
            sess.last_activity_at = now
            return sess
    # close any stale open sessions for the user
    open_sessions = db.scalars(select(JournalSession).where(
        JournalSession.user_id == user_id, JournalSession.closed_at.is_(None))).all()
    for os_ in open_sessions:
        if now - _aware(os_.last_activity_at) <= timedelta(minutes=s.session_inactivity_minutes) and os_.volume_id == volume.id:
            os_.last_activity_at = now
            return os_
        close_session(db, os_, now)
    sess = JournalSession(user_id=user_id, volume_id=volume.id, started_at=now, last_activity_at=now)
    sess.preservation_pending = preservation_due(db, user_id)
    sess.rollover_pending = bool(volume.rollover_suggested) and not _has_event(db, user_id, "volume_rollover_served")
    db.add(sess)
    db.flush()
    return sess


def _aware(dt: datetime) -> datetime:
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def mark_substantive(db: Session, session: JournalSession) -> None:
    s = get_settings()
    if session.substantive:
        return
    big_entry = db.scalar(select(func.count(Entry.id)).where(
        Entry.session_id == session.id, Entry.word_count >= s.substantive_entry_words)) or 0
    if big_entry >= 1 or session.turn_count >= s.substantive_turns:
        session.substantive = True


def record_entry_in_volume(db: Session, volume: Volume, entry: Entry, session: JournalSession | None) -> bool:
    """Update counters; return True if a rollover suggestion fired on this entry (once per volume)."""
    s = get_settings()
    volume.entry_count = (volume.entry_count or 0) + 1
    volume.token_estimate = (volume.token_estimate or 0) + max(1, len(entry.body) // 4)
    if not volume.rollover_suggested and (volume.entry_count >= s.volume_rollover_entries
                                          or volume.token_estimate >= s.volume_rollover_tokens):
        volume.rollover_suggested = True
        db.add(Event(user_id=volume.user_id, session_id=session.id if session else None,
                     type="volume_rollover_suggested", payload={"volume_id": volume.id,
                                                                "entry_count": volume.entry_count}))
        if session is not None:
            session.rollover_pending = True
        return True
    return False
