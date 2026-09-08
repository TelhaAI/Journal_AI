"""Prompt version storage (I6, Section 12 change control).

Git-tracked `prompts/<LABEL>.md` (+ `<LABEL>.addenda.json`) is the human source of
truth; `prompt_versions` is the runtime truth. `sync_prompts_from_disk` inserts any
label not yet in the DB and never modifies an existing row.
"""
from __future__ import annotations

import json
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.orm import Session

from .config import get_settings
from .models import PromptVersion


def load_prompt_files(prompts_dir: Path | None = None) -> dict[str, tuple[str, dict]]:
    prompts_dir = prompts_dir or get_settings().prompts_dir
    out: dict[str, tuple[str, dict]] = {}
    for md in sorted(prompts_dir.glob("*.md")):
        label = md.stem
        addenda_path = md.with_name(f"{label}.addenda.json")
        addenda = json.loads(addenda_path.read_text(encoding="utf-8")) if addenda_path.exists() else {}
        out[label] = (md.read_text(encoding="utf-8"), addenda)
    return out


def sync_prompts_from_disk(session: Session, prompts_dir: Path | None = None, activate_latest: bool = True) -> None:
    files = load_prompt_files(prompts_dir)
    existing = {pv.label for pv in session.scalars(select(PromptVersion)).all()}
    created = []
    for label, (body, addenda) in files.items():
        if label in existing:
            continue
        pv = PromptVersion(label=label, body=body, addenda=addenda, notes="synced from disk")
        session.add(pv)
        created.append(pv)
    session.flush()
    if activate_latest and created and not session.scalars(select(PromptVersion).where(PromptVersion.is_active)).first():
        created[-1].is_active = True
    session.flush()


def create_version(session: Session, label: str, body: str, addenda: dict | None = None, notes: str | None = None,
                   activate: bool = False) -> PromptVersion:
    pv = PromptVersion(label=label, body=body, addenda=addenda or {}, notes=notes)
    session.add(pv)
    session.flush()
    if activate:
        activate_version(session, pv.id)
    return pv


def activate_version(session: Session, version_id: str) -> PromptVersion:
    target = session.get(PromptVersion, version_id)
    if target is None:
        raise KeyError(version_id)
    for pv in session.scalars(select(PromptVersion).where(PromptVersion.is_active)).all():
        pv.is_active = False
    target.is_active = True
    session.flush()
    return target


def active_version(session: Session) -> PromptVersion:
    pv = session.scalars(select(PromptVersion).where(PromptVersion.is_active)).first()
    if pv is None:
        raise RuntimeError("No active prompt version. Run sync_prompts_from_disk or POST /prompt-versions.")
    return pv
