"""Runtime configuration. Everything behavioral that the spec says should be
tunable (thresholds, model choice) lives here, not in code paths."""
from __future__ import annotations

from functools import lru_cache
from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict

ROOT = Path(__file__).resolve().parent.parent


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="JOURNAL_", env_file=".env", extra="ignore")

    # --- storage ---------------------------------------------------------
    database_url: str = f"sqlite:///{ROOT / 'journal.db'}"
    # Fernet key (urlsafe base64, 32 bytes). If unset, bodies are stored in plaintext
    # and /health reports encryption_at_rest=false so the tester data statement stays true (I7).
    encryption_key: str | None = None

    # --- LLM -------------------------------------------------------------
    # "anthropic" | "openai" | "scripted" (tests) | "silent" (never produces a turn)
    llm_provider: str = "scripted"
    llm_model: str = "claude-sonnet-4-5"
    safety_model: str | None = None  # small model for the safety classifier; None => lexical only
    lookback_model: str | None = None  # defaults to llm_model

    # --- prompts ---------------------------------------------------------
    prompts_dir: Path = ROOT / "prompts"

    # --- gates -----------------------------------------------------------
    style_hard_sentence_cap: int = 8
    style_max_questions: int = 1
    style_max_menu_options: int = 3
    lookback_drop_threshold: float = 0.40
    lookback_min_entries_for_pattern: int = 3
    lookback_context_token_budget: int = 60_000

    # --- orchestration ---------------------------------------------------
    write_min_words_for_response: int = 30
    write_silent_streak_before_ack: int = 3
    substantive_entry_words: int = 50
    substantive_turns: int = 3
    preservation_sessions_threshold: int = 3
    volume_rollover_entries: int = 40
    volume_rollover_tokens: int = 60_000
    session_inactivity_minutes: int = 30
    recent_entries_in_context: int = 10

    # --- auth ------------------------------------------------------------
    # V1: the frontend supplies a stable user id in X-User-Id. Swap the dependency in auth.py
    # when the frontend team picks a real auth scheme. Admin endpoints need X-Admin-Token.
    admin_token: str | None = None


@lru_cache
def get_settings() -> Settings:
    return Settings()
