FROM python:3.11-slim
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 PIP_NO_CACHE_DIR=1
WORKDIR /app
COPY pyproject.toml README.md ./
COPY journal_ai ./journal_ai
COPY prompts ./prompts
COPY frontend ./frontend
COPY scripts ./scripts
COPY evals ./evals
RUN pip install -e ".[anthropic,openai,postgres]"
# Data lives on a mounted volume at /data (SQLite) or in Postgres via JOURNAL_DATABASE_URL.
ENV JOURNAL_DATABASE_URL=sqlite:////data/journal.db JOURNAL_SECRETS_DIR=/secrets
RUN mkdir -p /data /secrets
EXPOSE 8000
HEALTHCHECK CMD python -c "import urllib.request;urllib.request.urlopen('http://127.0.0.1:8000/health')" || exit 1
CMD ["sh", "-c", "python -m scripts.init_db && uvicorn journal_ai.main:app --host 0.0.0.0 --port ${PORT:-8000}"]
