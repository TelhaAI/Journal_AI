# Frontend contract (plan §4)

Send this to the frontend team. Everything below is what the backend depends on.

## 1. `intent` on every submission

`POST /entries` body:

```json
{
  "body": "…the user's words…",
  "created_at_local": "2026-09-08T21:14:00",
  "tz": "America/Chicago",
  "intent": "write" | "talk" | "you_decide",
  "mode": null | "listen" | "reflect" | "think" | "challenge" | "want" | "decide" | "act" | "space",
  "volume_id": null,
  "session_id": null,
  "supersedes_entry_id": null
}
```

- Default the UI to **"Keep writing"** (`intent: "write"`) with a lightweight "Want me in this one?"
  affordance that sets `intent: "talk"` (optionally with a `mode`). The model is never asked to guess
  whether an entry is writing or a request.
- `you_decide` is its own intent; the backend records the model's choice as `mode_set_by: "model"`.
- To edit an entry, send a new entry with `supersedes_entry_id`. The old row is kept; `GET /entries`
  hides superseded rows unless `include_superseded=true`.

Response:

```json
{ "entry": {…}, "session_id": "…", "mode": "write", "path": "write_silent", "ai_turn": null }
```

`path` ∈ `write_silent | write | ack | talk | safety`. **`ai_turn` may be `null`** (item 5 below).

## 2. `created_at_local` and `tz` are required

Wall-clock time in the user's zone plus the IANA zone name. Offsets in the timestamp are stripped;
the backend stores the wall clock. "Fall 2026" and "Then: … / Now: …" are computed from local time.

## 3. Talk Back turns stream over SSE

`POST /sessions/{id}/turns` with `{ "message": "…", "mode": null, "intent": "talk" | "you_decide" }`
returns `text/event-stream`:

```
event: meta    data: {"session_id":"…","entry_id":"…","mode":"think","path":"talk"}
event: token   data: {"t":"I read "}
event: token   data: {"t":"that."}
event: done    data: {"ai_turn":{…}|null,"mode":"think","path":"talk"}
event: error   data: {"detail":"…"}
```

Append `?stream=false` for a plain JSON response. Every message sent here is stored as an entry
(`source: "talk"`) — the user's words are the record.

Mode changes from UI controls: `PATCH /sessions/{id}/mode` `{ "mode": "reflect" }`.
Closing a session explicitly: `POST /sessions/{id}/close` (otherwise 30 min of inactivity).

## 4. Empty responses are normal

When `intent = "write"` and the entry is short, the backend returns **no AI turn** (`ai_turn: null`,
`path: "write_silent"`). Over SSE this is `meta` → `done` with `ai_turn: null`. The UI must not show
a spinner waiting for text.

## 5. Look Back

`POST /lookback` `{ "volume_ids": […], "from": "…", "to": "…" }` → 

```json
{
  "report_id": "…",
  "outcome": "served" | "partial" | "fallback",
  "observations": [
    {
      "kind": "observation" | "possible_pattern" | "interpretation",
      "text": "You've used 'allowed' six times across five entries.",
      "entry_ids": ["…"],
      "quotes": ["allowed to want this"],
      "claims": [{"type": "count", "term": "allowed", "asserted": 6, "recomputed": {…}}],
      "tier": "quoted" | "recomputed",
      "label": "5 entries · verified"
    }
  ],
  "message": "closing line, or the fallback message",
  "based_on": {"entry_count": 9, "range": {"start": "2026-03-02", "end": "2026-08-02"}, "entry_ids": […]},
  "evidence_table": {…}
}
```

Render each observation with its `kind`, its `label`/`tier` marker, and the cited entries as links
(`entry_ids`). Never hide the evidence. `tier` is the strongest level the evidence earned:
`quoted` = every quote is verbatim in a cited entry; `recomputed` = additionally every count /
absence / then-now claim was independently recalculated from the entries and matched. Nothing
below `quoted` is ever returned. Show "based on N entries" from `based_on.entry_count`.

`GET /lookback/{id}/receipt` returns the full re-executable receipt; `POST /lookback/{id}/replay`
re-runs the gate and reports whether the verdicts reproduce.

## 6. Other endpoints

| Method | Path | Notes |
|---|---|---|
| GET | `/entries?volume_id&from&to` | full original entries |
| GET/POST/PATCH | `/volumes`, `/volumes/{id}` | list / open / rename / close (`{"close": true}`) |
| GET | `/export?format=md\|json&include_ai=false` | entries only by default; AI turns as a separate section when asked |
| DELETE | `/me` | export-then-delete; response carries the export |
| POST | `/events/preservation-declined` | when the user dismisses the preservation note |
| GET | `/me/observables` | Section 10 observables from the DB |
| GET | `/prompt-versions` | labels + active flag |
| GET | `/health` | provider, model, prompt version, `encryption_at_rest` |

All endpoints require `X-User-Id`. A user can never read another user's rows by ID (404).
