"""Data model (plan §3). Arrays are stored as JSON lists so the same schema
runs on SQLite (dev/CI) and Postgres (prod)."""
from __future__ import annotations

import uuid
from datetime import datetime, timezone

from sqlalchemy import JSON, Boolean, DateTime, ForeignKey, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column, relationship

from .db import Base, EncryptedText

MODES = ("write", "open", "listen", "reflect", "think", "challenge", "want", "decide", "act", "you_decide", "space")
INTENTS = ("write", "talk", "you_decide")
SOURCES = ("write", "talk")
OUTCOMES = ("served", "partial", "fallback")


def new_id() -> str:
    return uuid.uuid4().hex


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


class User(Base):
    __tablename__ = "users"
    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class Volume(Base):
    __tablename__ = "volumes"
    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=new_id)
    user_id: Mapped[str] = mapped_column(ForeignKey("users.id"), index=True)
    title: Mapped[str] = mapped_column(String(200))
    opened_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    closed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    entry_count: Mapped[int] = mapped_column(Integer, default=0)
    token_estimate: Mapped[int] = mapped_column(Integer, default=0)
    rollover_suggested: Mapped[bool] = mapped_column(Boolean, default=False)


class Entry(Base):
    """The record. Never updated, never deleted by the system (I1)."""

    __tablename__ = "entries"
    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=new_id)
    user_id: Mapped[str] = mapped_column(ForeignKey("users.id"), index=True)
    volume_id: Mapped[str] = mapped_column(ForeignKey("volumes.id"), index=True)
    body: Mapped[str] = mapped_column(EncryptedText)
    word_count: Mapped[int] = mapped_column(Integer)
    created_at_utc: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    created_at_local: Mapped[datetime] = mapped_column(DateTime(timezone=False))  # naive wall-clock
    tz: Mapped[str] = mapped_column(String(64))
    source: Mapped[str] = mapped_column(String(8))  # write | talk
    supersedes_entry_id: Mapped[str | None] = mapped_column(String(32), nullable=True)
    session_id: Mapped[str | None] = mapped_column(String(32), nullable=True, index=True)


class JournalSession(Base):
    __tablename__ = "sessions"
    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=new_id)
    user_id: Mapped[str] = mapped_column(ForeignKey("users.id"), index=True)
    volume_id: Mapped[str] = mapped_column(ForeignKey("volumes.id"))
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    last_activity_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    closed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    local_date: Mapped[str | None] = mapped_column(String(10), nullable=True)  # YYYY-MM-DD in the user's tz
    mode: Mapped[str] = mapped_column(String(16), default="write")
    mode_set_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    mode_set_by: Mapped[str | None] = mapped_column(String(8), nullable=True)  # user | model
    hold: Mapped[bool] = mapped_column(Boolean, default=False)  # one-turn insight-landing hold
    talked: Mapped[bool] = mapped_column(Boolean, default=False)  # a Talk Back turn has happened this session
    substantive: Mapped[bool] = mapped_column(Boolean, default=False)
    turn_count: Mapped[int] = mapped_column(Integer, default=0)
    silent_write_streak: Mapped[int] = mapped_column(Integer, default=0)
    preservation_pending: Mapped[bool] = mapped_column(Boolean, default=False)
    rollover_pending: Mapped[bool] = mapped_column(Boolean, default=False)


class AITurn(Base):
    __tablename__ = "ai_turns"
    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=new_id)
    session_id: Mapped[str] = mapped_column(ForeignKey("sessions.id"), index=True)
    user_id: Mapped[str] = mapped_column(ForeignKey("users.id"), index=True)
    in_reply_to_entry_id: Mapped[str | None] = mapped_column(String(32), nullable=True)
    body: Mapped[str] = mapped_column(EncryptedText)
    prompt_version_id: Mapped[str] = mapped_column(String(32))
    model: Mapped[str] = mapped_column(String(80))
    mode_at_generation: Mapped[str] = mapped_column(String(16))
    input_tokens: Mapped[int] = mapped_column(Integer, default=0)
    output_tokens: Mapped[int] = mapped_column(Integer, default=0)
    latency_ms: Mapped[int] = mapped_column(Integer, default=0)
    style_violations: Mapped[list] = mapped_column(JSON, default=list)
    regenerated: Mapped[bool] = mapped_column(Boolean, default=False)
    path: Mapped[str] = mapped_column(String(16), default="talk")  # talk | write | safety | ack
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class LookbackReport(Base):
    __tablename__ = "lookback_reports"
    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=new_id)
    user_id: Mapped[str] = mapped_column(ForeignKey("users.id"), index=True)
    volume_ids: Mapped[list] = mapped_column(JSON, default=list)
    range_start: Mapped[datetime | None] = mapped_column(DateTime(timezone=False), nullable=True)
    range_end: Mapped[datetime | None] = mapped_column(DateTime(timezone=False), nullable=True)
    observations: Mapped[list] = mapped_column(JSON, default=list)
    message: Mapped[str | None] = mapped_column(Text, nullable=True)
    prompt_version_id: Mapped[str] = mapped_column(String(32))
    model: Mapped[str] = mapped_column(String(80))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    receipt: Mapped["LookbackReceipt"] = relationship(back_populates="report", uselist=False)


class LookbackReceipt(Base):
    """Re-executable: input_entry_ids + raw_model_output + gate_version must reproduce verification_log."""

    __tablename__ = "lookback_receipts"
    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=new_id)
    report_id: Mapped[str] = mapped_column(ForeignKey("lookback_reports.id"), index=True)
    user_id: Mapped[str] = mapped_column(ForeignKey("users.id"), index=True)
    input_entry_ids: Mapped[list] = mapped_column(JSON, default=list)
    input_content_hash: Mapped[str] = mapped_column(String(64))
    evidence_table: Mapped[dict] = mapped_column(JSON, default=dict)
    raw_model_output: Mapped[list] = mapped_column(JSON, default=list)  # one item per attempt, pre-gate
    verification_log: Mapped[list] = mapped_column(JSON, default=list)
    dropped: Mapped[list] = mapped_column(JSON, default=list)
    regenerated: Mapped[bool] = mapped_column(Boolean, default=False)
    outcome: Mapped[str] = mapped_column(String(16))
    prompt_version_id: Mapped[str] = mapped_column(String(32))
    model: Mapped[str] = mapped_column(String(80))
    gate_version: Mapped[str] = mapped_column(String(16))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    report: Mapped[LookbackReport] = relationship(back_populates="receipt")


class PromptVersion(Base):
    __tablename__ = "prompt_versions"
    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=new_id)
    label: Mapped[str] = mapped_column(String(32), unique=True)
    body: Mapped[str] = mapped_column(Text)
    addenda: Mapped[dict] = mapped_column(JSON, default=dict)  # mode/write/you_decide/insight/... addenda
    notes: Mapped[str | None] = mapped_column(Text, nullable=True)
    is_active: Mapped[bool] = mapped_column(Boolean, default=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class Event(Base):
    """Section 10 instrumentation. Types: preservation_offered / preservation_declined /
    volume_rollover_suggested / safety_triggered / style_violation / citation_dropped /
    insight_signal / lookback_fallback / session_closed / entry_created / mode_changed."""

    __tablename__ = "events"
    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=new_id)
    user_id: Mapped[str] = mapped_column(ForeignKey("users.id"), index=True)
    session_id: Mapped[str | None] = mapped_column(String(32), nullable=True, index=True)
    type: Mapped[str] = mapped_column(String(48), index=True)
    payload: Mapped[dict] = mapped_column(JSON, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


USER_TABLES = [Event, LookbackReceipt, LookbackReport, AITurn, Entry, JournalSession, Volume, User]
