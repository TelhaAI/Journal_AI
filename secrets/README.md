# secrets/ (gitignored)

One folder per secret; one file per folder. **The file name is the key's label** (shown in
`/health` so you can tell which key a deployment is using); the file content is the secret.

```
secrets/
  claude_api_key/temp-key-test-journal.txt   -> ANTHROPIC_API_KEY
  openai_api_key/<label>.txt                 -> OPENAI_API_KEY
  admin_token/<label>.txt                    -> JOURNAL_ADMIN_TOKEN
  encryption_key/<label>.txt                 -> JOURNAL_ENCRYPTION_KEY
```

Environment variables that are already set take precedence. Nothing in this folder except this
README is committed.
