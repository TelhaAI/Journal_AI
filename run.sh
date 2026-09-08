#!/usr/bin/env bash
set -e
[ -d .venv ] || python3 -m venv .venv
. .venv/bin/activate
pip install -q -e ".[dev,anthropic,openai]"
python -m scripts.init_db
exec uvicorn journal_ai.main:app --reload --port 8000
