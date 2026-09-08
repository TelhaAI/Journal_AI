# Journal_AI — backend for *Journal for People Who Don't Journal*

A deliberately thin backend that implements `docs/Journal_Backend_Plan_of_Action.md`:
the user's words are the record, the AI is a gated intelligence layer around it, and
every claim the journal makes about the record is provenance-typed, independently
recomputed, fail-closed, and leaves a re-executable receipt.

Independent of DataBridge_AI. FastAPI + SQLAlchemy; SQLite for dev/CI, Postgres for prod.

## What is here

| Plan section | Code | Tests |
|---|---|---|
| §1 invariants I1/I2 (append-only entries, byte-exact export) | `journal_ai/db.py` (DB triggers), `journal_ai/export.py` | U1, U2 |
| §2.3 Gate 1 — style gate | `journal_ai/gates/style.py` | U3–U5 |
| §2.3 Gate 2 — provenance-typed verification (`named`/`quoted`/`recomputed`, fail-closed, no repair, kind/tier coupling, regeneration budget) | `journal_ai/gates/provenance.py` | U6, U7, U17, U20 |
| §2.3 Gate 3 — safety gate | `journal_ai/gates/safety.py` | U14 |
| §2.4 deterministic evidence extraction | `journal_ai/lookback/evidence.py` | U8 |
| §0.5 receipts + replay | `journal_ai/lookback/service.py` | U18, U19 |
| §3 data model | `journal_ai/models.py` | — |
| §4 API surface (SSE turns, export, delete, prompt versions) | `journal_ai/routers/` | U16 |
| §5 orchestration (mode state machine, prompt assembly, insight hold, write policy, preservation, rollover) | `journal_ai/orchestration/` | U9–U11, U15 |
| §6.2 behavioral fixtures A–M | `evals/fixtures/behavioral.yaml`, `evals/run_layer2.py` | run against a model |
| §6.3 synthetic corpus test | `evals/corpus_generator.py`, `evals/run_layer3.py` | run against a model |
| §6.4 Wilson intervals, clustering, non-regression | `evals/harness.py` | `tests/test_eval_harness.py` |
| §2.1 prompt versions outside code | `prompts/V1.0.md`, `prompts/V1.0.addenda.json`, `journal_ai/prompts.py` | U12 |

## Quick start

```bash
python -m venv .venv && . .venv/bin/activate        # Windows: .venv\Scripts\activate
pip install -e ".[dev,anthropic,openai]"
cp .env.example .env                                 # set JOURNAL_LLM_PROVIDER / API key
pytest -q                                            # Layer 1: deterministic, no network
uvicorn journal_ai.main:app --reload                 # http://127.0.0.1:8000/docs
```

Every request carries `X-User-Id` (V1 auth: the frontend supplies a stable id). Admin routes take
`X-Admin-Token`.

```bash
curl -s -X POST localhost:8000/entries -H 'X-User-Id: demo' -H 'content-type: application/json' \
  -d '{"body":"Work was annoying today. I don'\''t really feel like getting into it.",
       "created_at_local":"2026-09-08T21:14:00","tz":"America/Chicago","intent":"write"}'
```

### Providers

`JOURNAL_LLM_PROVIDER` = `anthropic` | `openai` | `scripted` | `silent`. The `scripted` provider is
what tests and `--dry-run` evals use; it never calls a network. Model names are config
(`JOURNAL_LLM_MODEL`, optional `JOURNAL_SAFETY_MODEL`, `JOURNAL_LOOKBACK_MODEL`), so a Section 12
"prompt regression vs. model change" question is answerable from `ai_turns.model` and
`ai_turns.prompt_version_id`.

### Postgres

```bash
pip install -e ".[postgres]"
export JOURNAL_DATABASE_URL=postgresql+psycopg://journal:journal@localhost:5432/journal
python -m scripts.init_db        # creates tables, installs immutability triggers, syncs prompts
```

### Encryption at rest

Set `JOURNAL_ENCRYPTION_KEY` (a Fernet key: `python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"`).
`entries.body` and `ai_turns.body` are then Fernet-encrypted in the database; export still returns
the exact original. `/health` reports `encryption_at_rest` so the tester data statement stays true (I7).

## Running the evals

Layer 1 (`pytest`) is deterministic and runs in CI on every change. Layers 2 and 3 need a model
and are run on every prompt-version or model change:

```bash
python -m evals.run_layer2 --n 10 --judge            # fixtures A–M, Wilson CIs, clusters, freeze decision
python -m evals.run_layer2 --n 10 --n30 30 --fixtures A,B,F,H   # N=30 on Section-11-linked fixtures
python -m evals.run_layer3 --corpora 20              # planted-feature corpora, precision/recall, receipt replay
python -m evals.run_layer2 --dry-run                 # exercises the harness offline with scripted replies
```

Reports land in `evals/reports/` as JSON. Attach the Layer 2 report to any prompt-version PR;
`evals.harness.non_regression_ok` is the activation rule (a fixture's new failure-rate lower bound
may not exceed the previous version's upper bound).

## Frontend contract (plan §4)

See `docs/frontend_contract.md`: `intent` on every submission, `created_at_local` + `tz`, SSE event
shape, empty-response handling, and how to render Look Back observations with their `tier` label.

## Prompt change control (plan §2.1, Section 12)

`prompts/<LABEL>.md` is the master instruction file; `prompts/<LABEL>.addenda.json` holds the per-mode
and situational addenda. On startup any new label on disk is inserted into `prompt_versions`
(never modifying an existing row). Activation is explicit: `POST /prompt-versions/{id}/activate`
(admin). `prompts/V1.0.md` is reconstructed from the plan's quoted spec language — **replace its
body with Section 3 of the V1.0 Build & Test Package verbatim before freezing V1.0.**

## Layout

```
journal_ai/
  config.py          settings (thresholds, provider, prompts dir)
  db.py              engine, EncryptedText, immutability triggers, export-then-delete guard
  models.py          §3 tables
  prompts.py         prompt_versions sync/activate
  llm/               provider-agnostic adapter (anthropic, openai, scripted, silent)
  gates/             style.py, safety.py, provenance.py (GATE_VERSION)
  lookback/          evidence.py (deterministic table), service.py (run, receipts, replay)
  orchestration/     modes.py, sessions.py, engine.py
  routers/           entries, sessions (SSE), lookback, volumes, account, prompt_versions
  export.py          md/json export
prompts/             V1.0.md, V1.0.addenda.json
evals/               harness.py, run_layer2.py, run_layer3.py, corpus_generator.py, fixtures/
tests/               Layer 1 (U1–U20) + harness tests
docs/                plan of action, frontend contract, data statement
```
