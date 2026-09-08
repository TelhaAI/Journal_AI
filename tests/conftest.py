from __future__ import annotations

import os
from datetime import datetime

import pytest
from fastapi.testclient import TestClient

os.environ.setdefault("JOURNAL_LLM_PROVIDER", "scripted")
os.environ.setdefault("JOURNAL_ADMIN_TOKEN", "test-admin")
os.environ.setdefault("JOURNAL_ENCRYPTION_KEY", "")  # plaintext by default; test_encryption sets a key

from journal_ai import db as dbmod  # noqa: E402
from journal_ai.config import get_settings  # noqa: E402
from journal_ai.llm import ScriptedProvider, set_provider  # noqa: E402
from journal_ai.main import create_app  # noqa: E402
from journal_ai.prompts import sync_prompts_from_disk  # noqa: E402

LOCAL = datetime(2026, 9, 8, 21, 0, 0)


@pytest.fixture
def settings():
    s = get_settings()
    snapshot = s.model_dump()
    yield s
    for k, v in snapshot.items():
        setattr(s, k, v)


@pytest.fixture
def engine(settings):
    eng = dbmod.reset_engine("sqlite://")
    dbmod.init_db(eng)
    with dbmod.session_scope() as db:
        sync_prompts_from_disk(db, settings.prompts_dir)
    return eng


@pytest.fixture
def db(engine):
    s = dbmod.get_sessionmaker()()
    yield s
    s.close()


@pytest.fixture
def provider():
    p = ScriptedProvider()
    set_provider(p)
    yield p
    set_provider(None)


@pytest.fixture
def app(engine, provider):
    return create_app(provider, init=False)


@pytest.fixture
def client(app):
    with TestClient(app) as c:
        c.headers.update({"X-User-Id": "user-a"})
        yield c


def entry_payload(body: str, intent: str = "write", mode: str | None = None, **kw) -> dict:
    d = {"body": body, "created_at_local": LOCAL.isoformat(), "tz": "America/Chicago", "intent": intent}
    if mode:
        d["mode"] = mode
    d.update(kw)
    return d
