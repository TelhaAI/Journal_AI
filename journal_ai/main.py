from __future__ import annotations

from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.responses import RedirectResponse
from fastapi.staticfiles import StaticFiles

from .config import get_settings
from .db import init_db, session_scope
from .gates.provenance import ProvenanceGate
from .llm import LLMProvider, get_provider
from .lookback.service import LookbackService
from .orchestration.engine import TurnEngine
from .prompts import sync_prompts_from_disk
from .routers import ALL


def create_app(provider: LLMProvider | None = None, *, init: bool = True) -> FastAPI:
    s = get_settings()

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        if init:
            init_db()
            with session_scope() as db:
                sync_prompts_from_disk(db, s.prompts_dir)
        yield

    app = FastAPI(title="Journal AI backend", version="0.1.0", lifespan=lifespan)
    p = provider or get_provider()
    app.state.provider = p
    app.state.engine = TurnEngine(p)
    app.state.lookback = LookbackService(p, ProvenanceGate(min_entries_for_pattern=s.lookback_min_entries_for_pattern))
    for r in ALL:
        app.include_router(r)
    if s.frontend_dir.is_dir():
        app.mount("/app", StaticFiles(directory=str(s.frontend_dir), html=True), name="frontend")

        @app.get("/", include_in_schema=False)
        def root():
            return RedirectResponse("/app/")
    return app


app = create_app()
