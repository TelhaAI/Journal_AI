"""Runtime configuration. Everything behavioral that the spec says should be
tunable (thresholds, model choice) lives here, not in code paths."""
from __future__ import annotations

from functools import lru_cache
from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict

ROOT = Path(__file__).resolve().parent.parent


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="JOURNAL_", env_file=".env", extra="allow")

    # --- storage ---------------------------------------------------------
    database_url: str = f"sqlite:///{ROOT / 'journal.db'}"
    # Fernet key (urlsafe base64, 32 bytes). If unset, bodies are stored in plaintext
    # and /health reports encryption_at_rest=false so the tester data statement stays true (I7).
    encryption_key: str | None = None

    # --- LLM -------------------------------------------------------------
    # "anthropic" | "openai" | "scripted" (tests) | "silent" (never produces a turn) | "auto"
    # auto => anthropic if ANTHROPIC_API_KEY (env or secrets/) is present, else openai if OPENAI_API_KEY, else scripted
    llm_provider: str = "auto"
    llm_model: str = "claude-sonnet-4-5"
    safety_model: str | None = None  # small model for the safety classifier; None => lexical only
    lookback_model: str | None = None  # defaults to llm_model

    # --- prompts / secrets / frontend ------------------------------------
    prompts_dir: Path = ROOT / "prompts"
    secrets_dir: Path = ROOT / "secrets"      # see journal_ai/secrets.py
    frontend_dir: Path = ROOT / "frontend"    # served at /app when present

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
    # Shared passcode for invited testers (hosted deployments). When set, every API call must carry
    # X-Tester-Code; the app asks for it once and remembers it in the browser. Empty = open.
    tester_passcode: str | None = None


@lru_cache
def get_settings() -> Settings:
    import os

    from .secrets import load_secrets

    s = Settings()
    s.secrets_loaded = load_secrets(s.secrets_dir)  # type: ignore[attr-defined]
    if s.llm_provider == "auto":
        if os.environ.get("ANTHROPIC_API_KEY"):
            s.llm_provider = "anthropic"
        elif os.environ.get("OPENAI_API_KEY"):
            s.llm_provider = "openai"
            if s.llm_model.startswith("claude"):
                s.llm_model = "gpt-4o"
        else:
            s.llm_provider = "scripted"
    if s.admin_token is None and os.environ.get("JOURNAL_ADMIN_TOKEN"):
        s.admin_token = os.environ["JOURNAL_ADMIN_TOKEN"]
    if s.tester_passcode is None and os.environ.get("JOURNAL_TESTER_PASSCODE"):
        s.tester_passcode = os.environ["JOURNAL_TESTER_PASSCODE"]
    if s.encryption_key is None and os.environ.get("JOURNAL_ENCRYPTION_KEY"):
        s.encryption_key = os.environ["JOURNAL_ENCRYPTION_KEY"]
    return s
