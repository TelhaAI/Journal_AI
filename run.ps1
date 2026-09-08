# Windows quick start: creates a venv, installs, and serves the app at http://127.0.0.1:8000/app/
if (-not (Test-Path .venv)) { python -m venv .venv }
. .venv\Scripts\Activate.ps1
pip install -q -e ".[dev,anthropic,openai]"
python -m scripts.init_db
uvicorn journal_ai.main:app --reload --port 8000
