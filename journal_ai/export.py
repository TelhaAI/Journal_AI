"""Export (I2): original entries byte-for-byte. AI turns only when asked, never interleaved."""
from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.orm import Session

from .models import AITurn, Entry, Volume


def export_json(db: Session, user_id: str, include_ai: bool = False) -> dict:
    volumes = db.scalars(select(Volume).where(Volume.user_id == user_id).order_by(Volume.opened_at)).all()
    entries = db.scalars(select(Entry).where(Entry.user_id == user_id)
                         .order_by(Entry.created_at_local, Entry.created_at_utc, Entry.id)).all()
    out = {
        "user_id": user_id,
        "volumes": [{"id": v.id, "title": v.title, "opened_at": v.opened_at.isoformat(),
                     "closed_at": v.closed_at.isoformat() if v.closed_at else None} for v in volumes],
        "entries": [{"id": e.id, "volume_id": e.volume_id, "body": e.body, "created_at_local": e.created_at_local.isoformat(),
                     "created_at_utc": e.created_at_utc.isoformat(), "tz": e.tz, "source": e.source,
                     "supersedes_entry_id": e.supersedes_entry_id} for e in entries],
    }
    if include_ai:
        turns = db.scalars(select(AITurn).where(AITurn.user_id == user_id).order_by(AITurn.created_at)).all()
        out["ai_turns"] = [{"id": t.id, "in_reply_to_entry_id": t.in_reply_to_entry_id, "body": t.body,
                            "created_at": t.created_at.isoformat(), "prompt_version_id": t.prompt_version_id,
                            "model": t.model} for t in turns]
    return out


def export_markdown(db: Session, user_id: str, include_ai: bool = False) -> str:
    data = export_json(db, user_id, include_ai)
    titles = {v["id"]: v["title"] for v in data["volumes"]}
    lines = ["# Journal", ""]
    current = None
    for e in data["entries"]:
        if e["volume_id"] != current:
            current = e["volume_id"]
            lines += [f"## {titles.get(current, current)}", ""]
        stamp = e["created_at_local"].replace("T", " ")
        note = f" (revises {e['supersedes_entry_id']})" if e["supersedes_entry_id"] else ""
        lines += [f"### {stamp} ({e['tz']}){note}", "", e["body"], ""]
    if include_ai and data.get("ai_turns"):
        lines += ["---", "", "## Responses from the journal (kept separately; not part of the record)", ""]
        for t in data["ai_turns"]:
            lines += [f"### {t['created_at'].replace('T', ' ')} — reply to entry {t['in_reply_to_entry_id']}", "",
                      t["body"], ""]
    return "\n".join(lines)
