# Deploying for testers

The app is one container: FastAPI serves the API and the UI (`/app/`). State is a SQLite file on a
persistent disk (fine for a tester cohort) or Postgres. Secrets come from environment variables.

## What you need before deploying

| Env var | What | How |
|---|---|---|
| `ANTHROPIC_API_KEY` | the Claude key (contents of `secrets/claude_api_key/*.txt`) | paste into the host's secret store — never commit |
| `JOURNAL_TESTER_PASSCODE` | a shared code you give testers; the app asks for it once per browser | pick any phrase, e.g. `lostandfound-fall26` |
| `JOURNAL_ENCRYPTION_KEY` | encrypts entry text at rest | any long random string (the app derives a Fernet key) |
| `JOURNAL_ADMIN_TOKEN` | needed only for `POST /prompt-versions` | any long random string |

Optional: `JOURNAL_LLM_MODEL` (default `claude-sonnet-4-5`), `JOURNAL_SAFETY_MODEL` (e.g. `claude-haiku-4-5` for the
model-based safety screen on top of the lexical one), `JOURNAL_STYLE_HARD_SENTENCE_CAP` (default 8).

## Option A — Render (recommended: one click from the GitHub repo)

1. Push the repo to GitHub (see README).
2. In Render: **New + → Blueprint**, pick `TelhaAI/Journal_AI`. `render.yaml` defines the web service,
   a 1 GB disk at `/data`, and the env vars. Disks require the Starter plan (~$7/mo); the free plan has
   no persistent disk, so entries would vanish on every deploy.
3. When prompted, paste `ANTHROPIC_API_KEY` and `JOURNAL_TESTER_PASSCODE` (marked `sync: false`).
4. Deploy. Your URL is `https://journal-ai-<hash>.onrender.com/` (redirects to `/app/`). Check
   `https://…/health` shows `"llm_provider":"anthropic"` and `"encryption_at_rest":true`.

## Option B — Fly.io (CLI)

```bash
fly launch --copy-config --no-deploy          # uses fly.toml; pick an app name if journal-ai is taken
fly volumes create journal_data --size 1 --region ord
fly secrets set ANTHROPIC_API_KEY=... JOURNAL_TESTER_PASSCODE=... JOURNAL_ENCRYPTION_KEY=... JOURNAL_ADMIN_TOKEN=...
fly deploy
```

## Option C — any Docker host (Railway, a VPS, Azure Container Apps…)

```bash
docker build -t journal-ai .
docker run -d -p 8000:8000 -v journal_data:/data \
  -e ANTHROPIC_API_KEY=... -e JOURNAL_TESTER_PASSCODE=... -e JOURNAL_ENCRYPTION_KEY=... -e JOURNAL_ADMIN_TOKEN=... \
  journal-ai
```

For Postgres instead of SQLite: set `JOURNAL_DATABASE_URL=postgresql+psycopg://user:pass@host:5432/db`
(the `psycopg` driver is in the image). Tables and immutability triggers are created on first start.

## Inviting testers

Send each tester a link with their own handle so their journal follows them across devices:

```
https://<your-host>/app/?u=amy          (+ the tester code, separately)
```

Without `?u=` the browser gets a random id, which is also fine for a single device. Testers can
export their journal at any time (`/export?format=md` with their `X-User-Id`, or ask you), and
`DELETE /me` removes everything. Share `docs/tester_data_statement.md` with them, filled in for your host.

## What to watch

- `GET /me/observables` per tester (with their `X-User-Id` + tester code) gives the Section 10 signals:
  second entry, asked to engage, returned on another day.
- Cost: the `ai_turns` table records tokens per turn; a median Talk Back turn over ~800 output tokens
  means verbosity drift.
- Prompt changes: edit `prompts/`, open a PR — CI runs Layers 2/3 with `ANTHROPIC_API_KEY` set as a
  repository secret — then activate the new version with `POST /prompt-versions/{id}/activate`.
