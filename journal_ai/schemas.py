from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, Field, field_validator

from .models import MODES

Intent = Literal["write", "talk", "you_decide"]


class EntryIn(BaseModel):
    body: str = Field(min_length=1, max_length=50_000)
    created_at_local: datetime
    tz: str = Field(min_length=1, max_length=64)
    intent: Intent = "write"
    mode: str | None = None
    volume_id: str | None = None
    session_id: str | None = None
    supersedes_entry_id: str | None = None

    @field_validator("mode")
    @classmethod
    def _mode(cls, v):
        if v is not None and v not in MODES:
            raise ValueError(f"mode must be one of {MODES}")
        return v

    @field_validator("created_at_local")
    @classmethod
    def _naive(cls, v: datetime):
        # wall-clock time in `tz`; strip any offset the client attached
        return v.replace(tzinfo=None)


class EntryOut(BaseModel):
    id: str
    volume_id: str
    body: str
    word_count: int
    created_at_utc: datetime
    created_at_local: datetime
    tz: str
    source: str
    supersedes_entry_id: str | None
    session_id: str | None

    model_config = {"from_attributes": True}


class AITurnOut(BaseModel):
    id: str
    body: str
    mode_at_generation: str
    path: str
    prompt_version_id: str
    model: str
    regenerated: bool
    created_at: datetime

    model_config = {"from_attributes": True}


class SubmitOut(BaseModel):
    entry: EntryOut
    session_id: str
    mode: str
    path: str
    ai_turn: AITurnOut | None


class TurnIn(BaseModel):
    message: str = Field(min_length=1, max_length=50_000)
    mode: str | None = None
    intent: Literal["talk", "you_decide"] = "talk"
    created_at_local: datetime | None = None
    tz: str | None = None

    @field_validator("mode")
    @classmethod
    def _mode(cls, v):
        if v is not None and v not in MODES:
            raise ValueError(f"mode must be one of {MODES}")
        return v


class RespondIn(BaseModel):
    mode: str | None = None
    intent: Literal["talk", "you_decide"] = "talk"

    @field_validator("mode")
    @classmethod
    def _mode(cls, v):
        if v is not None and v not in MODES:
            raise ValueError(f"mode must be one of {MODES}")
        return v


class ModeIn(BaseModel):
    mode: str

    @field_validator("mode")
    @classmethod
    def _mode(cls, v):
        if v not in MODES:
            raise ValueError(f"mode must be one of {MODES}")
        return v


class SessionOut(BaseModel):
    id: str
    volume_id: str
    mode: str
    mode_set_by: str | None
    started_at: datetime
    last_activity_at: datetime
    closed_at: datetime | None
    substantive: bool
    turn_count: int

    model_config = {"from_attributes": True}


class LookbackIn(BaseModel):
    volume_ids: list[str] | None = None
    from_: datetime | None = Field(default=None, alias="from")
    to: datetime | None = None

    model_config = {"populate_by_name": True}


class VolumeIn(BaseModel):
    title: str | None = Field(default=None, max_length=200)


class VolumePatch(BaseModel):
    title: str | None = Field(default=None, max_length=200)
    close: bool = False


class VolumeOut(BaseModel):
    id: str
    title: str
    opened_at: datetime
    closed_at: datetime | None
    entry_count: int
    token_estimate: int
    rollover_suggested: bool

    model_config = {"from_attributes": True}


class PromptVersionIn(BaseModel):
    label: str = Field(min_length=1, max_length=32)
    body: str = Field(min_length=1)
    addenda: dict = Field(default_factory=dict)
    notes: str | None = None
    activate: bool = False


class PromptVersionOut(BaseModel):
    id: str
    label: str
    is_active: bool
    notes: str | None
    created_at: datetime
    body: str | None = None
    addenda: dict | None = None

    model_config = {"from_attributes": True}
