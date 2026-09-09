"""Secrets folder loader.

Layout (gitignored):

    secrets/
      claude_api_key/<key-name>.txt     -> ANTHROPIC_API_KEY   (the file *name* is the key's label)
      openai_api_key/<key-name>.txt     -> OPENAI_API_KEY
      admin_token/<name>.txt            -> JOURNAL_ADMIN_TOKEN
      encryption_key/<name>.txt         -> JOURNAL_ENCRYPTION_KEY

Each folder holds one file whose stem is the human name of the key (e.g. `temp-key-test-journal`)
and whose content is the secret. Environment variables that are already set win; the loader never
prints a value, only the label it loaded.
"""
from __future__ import annotations

import os
from pathlib import Path

FOLDER_TO_ENV = {
    "claude_api_key": "ANTHROPIC_API_KEY",
    "anthropic_api_key": "ANTHROPIC_API_KEY",
    "openai_api_key": "OPENAI_API_KEY",
    "admin_token": "JOURNAL_ADMIN_TOKEN",
    "encryption_key": "JOURNAL_ENCRYPTION_KEY",
    "tester_passcode": "JOURNAL_TESTER_PASSCODE",
}


def load_secrets(secrets_dir: Path) -> dict[str, str]:
    """Populate os.environ from the secrets folder. Returns {env_var: key_label} for what was loaded."""
    loaded: dict[str, str] = {}
    if not secrets_dir.is_dir():
        return loaded
    for folder, env in FOLDER_TO_ENV.items():
        d = secrets_dir / folder
        if not d.is_dir() or os.environ.get(env):
            continue
        files = sorted(p for p in d.iterdir() if p.is_file() and not p.name.startswith("."))
        if not files:
            continue
        value = files[0].read_text(encoding="utf-8").strip()
        if value:
            os.environ[env] = value
            loaded[env] = files[0].stem
    return loaded
