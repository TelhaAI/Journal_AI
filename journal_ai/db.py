"""Engine, session factory, and the DB-level guards that enforce invariant I1.

Entries are append-only: UPDATE and DELETE on `entries` are refused by triggers.
The only exception is the export-then-delete path (`DELETE /me`), which flips a
per-connection guard flag for the duration of one transaction.
"""
from __future__ import annotations

import base64
import hashlib
from contextlib import contextmanager
from typing import Iterator

from sqlalchemy import Text, TypeDecorator, create_engine, event, text
from sqlalchemy.engine import Connection, Engine
from sqlalchemy.orm import DeclarativeBase, Session, sessionmaker

from .config import get_settings


class Base(DeclarativeBase):
    pass


# --------------------------------------------------------------------------- encryption
class EncryptedText(TypeDecorator):
    """Fernet-encrypts a text column when JOURNAL_ENCRYPTION_KEY is set.

    Stored values are prefixed with `enc:` so plaintext rows written before a key
    was configured still read back. Export returns the decrypted original,
    byte-for-byte (I2).
    """

    impl = Text
    cache_ok = True

    def _fernet(self):
        key = get_settings().encryption_key
        if not key:
            return None
        from cryptography.fernet import Fernet

        if len(key) != 44:  # derive a proper key from an arbitrary passphrase
            key = base64.urlsafe_b64encode(hashlib.sha256(key.encode()).digest()).decode()
        return Fernet(key)

    def process_bind_param(self, value, dialect):
        if value is None:
            return None
        f = self._fernet()
        if f is None:
            return value
        return "enc:" + f.encrypt(value.encode("utf-8")).decode("ascii")

    def process_result_value(self, value, dialect):
        if value is None or not value.startswith("enc:"):
            return value
        f = self._fernet()
        if f is None:
            raise RuntimeError("Encrypted row read without JOURNAL_ENCRYPTION_KEY configured")
        return f.decrypt(value[4:].encode("ascii")).decode("utf-8")


# --------------------------------------------------------------------------- engine
_engine: Engine | None = None
_SessionLocal: sessionmaker | None = None


def make_engine(url: str | None = None) -> Engine:
    url = url or get_settings().database_url
    kwargs = {}
    if url.startswith("sqlite"):
        kwargs["connect_args"] = {"check_same_thread": False}
        from sqlalchemy.pool import StaticPool

        if ":memory:" in url or url.endswith("sqlite://"):
            kwargs["poolclass"] = StaticPool
    engine = create_engine(url, future=True, **kwargs)
    if url.startswith("sqlite"):

        @event.listens_for(engine, "connect")
        def _sqlite_pragmas(dbapi_conn, _):
            cur = dbapi_conn.cursor()
            cur.execute("PRAGMA foreign_keys=ON")
            cur.close()

    return engine


def get_engine() -> Engine:
    global _engine
    if _engine is None:
        _engine = make_engine()
    return _engine


def get_sessionmaker() -> sessionmaker:
    global _SessionLocal
    if _SessionLocal is None:
        _SessionLocal = sessionmaker(bind=get_engine(), expire_on_commit=False, future=True)
    return _SessionLocal


def reset_engine(url: str | None = None) -> Engine:
    """Used by tests to point the app at a fresh database."""
    global _engine, _SessionLocal
    _engine = make_engine(url)
    _SessionLocal = sessionmaker(bind=_engine, expire_on_commit=False, future=True)
    return _engine


@contextmanager
def session_scope() -> Iterator[Session]:
    s = get_sessionmaker()()
    try:
        yield s
        s.commit()
    except Exception:
        s.rollback()
        raise
    finally:
        s.close()


# --------------------------------------------------------------------------- immutability guards
SQLITE_GUARDS = [
    "CREATE TABLE IF NOT EXISTS _guard (id INTEGER PRIMARY KEY CHECK (id = 1), allow_delete INTEGER NOT NULL DEFAULT 0)",
    "INSERT OR IGNORE INTO _guard (id, allow_delete) VALUES (1, 0)",
    """CREATE TRIGGER IF NOT EXISTS entries_no_update BEFORE UPDATE ON entries
       BEGIN SELECT RAISE(ABORT, 'entries are immutable (I1): supersede instead of updating'); END""",
    """CREATE TRIGGER IF NOT EXISTS entries_no_delete BEFORE DELETE ON entries
       WHEN (SELECT allow_delete FROM _guard WHERE id = 1) = 0
       BEGIN SELECT RAISE(ABORT, 'entries are immutable (I1): deletion only via export-then-delete'); END""",
    """CREATE TRIGGER IF NOT EXISTS ai_turns_no_update BEFORE UPDATE ON ai_turns
       BEGIN SELECT RAISE(ABORT, 'ai_turns are immutable'); END""",
    """CREATE TRIGGER IF NOT EXISTS prompt_versions_no_body_update BEFORE UPDATE OF body, label ON prompt_versions
       BEGIN SELECT RAISE(ABORT, 'prompt versions are immutable (I6): create a new version'); END""",
]

POSTGRES_GUARDS = [
    """CREATE OR REPLACE FUNCTION journal_refuse_entry_change() RETURNS trigger AS $$
       BEGIN
         IF TG_OP = 'DELETE' AND current_setting('journal.allow_delete', true) = '1' THEN RETURN OLD; END IF;
         RAISE EXCEPTION 'entries are immutable (I1)';
       END $$ LANGUAGE plpgsql""",
    "DROP TRIGGER IF EXISTS entries_immutable ON entries",
    """CREATE TRIGGER entries_immutable BEFORE UPDATE OR DELETE ON entries
       FOR EACH ROW EXECUTE FUNCTION journal_refuse_entry_change()""",
    """CREATE OR REPLACE FUNCTION journal_refuse_update() RETURNS trigger AS $$
       BEGIN RAISE EXCEPTION 'row is immutable'; END $$ LANGUAGE plpgsql""",
    "DROP TRIGGER IF EXISTS ai_turns_immutable ON ai_turns",
    "CREATE TRIGGER ai_turns_immutable BEFORE UPDATE ON ai_turns FOR EACH ROW EXECUTE FUNCTION journal_refuse_update()",
    "DROP TRIGGER IF EXISTS prompt_versions_immutable ON prompt_versions",
    """CREATE TRIGGER prompt_versions_immutable BEFORE UPDATE OF body, label ON prompt_versions
       FOR EACH ROW EXECUTE FUNCTION journal_refuse_update()""",
]


def install_guards(conn: Connection) -> None:
    stmts = SQLITE_GUARDS if conn.dialect.name == "sqlite" else POSTGRES_GUARDS
    for stmt in stmts:
        conn.execute(text(stmt))


@contextmanager
def allow_entry_deletion(session: Session) -> Iterator[None]:
    """Scope in which DELETE on entries is permitted (export-then-delete only)."""
    dialect = session.get_bind().dialect.name
    if dialect == "sqlite":
        session.execute(text("UPDATE _guard SET allow_delete = 1 WHERE id = 1"))
        try:
            yield
        finally:
            session.execute(text("UPDATE _guard SET allow_delete = 0 WHERE id = 1"))
    else:
        session.execute(text("SELECT set_config('journal.allow_delete', '1', true)"))
        yield  # transaction-local setting; cleared on commit/rollback


def init_db(engine: Engine | None = None) -> None:
    from . import models  # noqa: F401  (register tables)

    engine = engine or get_engine()
    Base.metadata.create_all(engine)
    with engine.begin() as conn:
        install_guards(conn)
