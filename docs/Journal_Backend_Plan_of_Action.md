# Journal for People Who Don't Journal — Backend Plan of Action

**Scope:** Backend and test cases only. Frontend is being designed separately; the frontend contract is called out where the backend depends on it.
**Source:** V1.0 Build & Test Package (Wendy Fong / Lost & Found)
**Date:** 2026-09-08 (rev. 2 — verification concepts added, §0.5)

---

## 0. Read this first: the spec does not ask for a backend

The document is a **ChatGPT Custom GPT** build package. It says, explicitly:

- "Do not add external Actions or APIs."
- V1 is not trying to prove "API architecture", "standalone app viability", or "perfect long-term journal storage".
- The point of V1 is to test whether the *conversation architecture* creates a reason to journal.

Building a backend means you are building the thing Section 13 defers. That is a legitimate decision, but make it consciously, because it changes what a test failure means: if testers don't write, is it the conversation design or your app? The Custom GPT isolates the first variable; a custom app does not.

**Recommendation:** Build the backend, but keep it deliberately thin so that the same V1 hypotheses (write / talk back / land / return) are still what's being tested. Specifically:

1. The master instructions in Section 3 become a **versioned system prompt** stored outside the code (per Section 12 change control). The backend must not bake behavior into code that the spec expects to live in instructions.
2. The "Journal Project / Volume" concept, which in the Custom GPT is a ChatGPT-Projects workaround, becomes a first-class data model. This is the one place a backend is *strictly better* than the Custom GPT.
3. Do not add memory, embeddings, RAG, analytics, or personalization the spec says V1 doesn't need.

Everything below assumes you accept that framing.

---

## 0.5 Verification concepts applied (no platform dependency)

Look Back is the one part of this product that makes claims about a record, and the spec's own worst failure mode for it is "TOO SMART: brilliant interpretations unsupported by the record… users may mistake eloquence for accuracy." That is a verification problem, and this plan applies four concepts from governed-trust-layer design to it. These are **patterns implemented in the journal backend's own code**, not a dependency on any external platform, license, or service.

| Concept | Meaning here | Where it lands |
|---|---|---|
| **Provenance typing** | Every Look Back observation is tagged with the strongest kind of evidence that backs it: `named` (the subject appears in the record), `quoted` (the cited span exists verbatim in the cited entry), `recomputed` (a count, date, or absence claim was independently recalculated from the entries and matched). The label a user sees may only assert what the tier earned. | §2.3 Gate 2, §3 schema |
| **Independent recompute** | Frequency, absence, and then-vs-now claims are recalculated by deterministic code from the raw entries, never taken from the model. The model's claim must agree with the recompute or it is dropped. | §2.4, §2.3 Gate 2 |
| **Fail-closed default** | An observation that cannot be verified to at least the `quoted` tier is dropped, not repaired, not softened, not shown. If too many drop, the whole response regenerates once and then falls back to a plain "not enough verifiable evidence" message. | §2.3 Gate 2 |
| **Re-executable receipt** | Each Look Back run stores the input entry set (by ID and content hash), the deterministic evidence table, every observation with its tier and verification verdict, the prompt version and model. Anyone can rerun the verification from the receipt and get the same verdict. | §3 `lookback_receipts`, §6.1 U17–U19 |

The same discipline extends to the **evaluation harness** (§6.4): behavior tests are run as falsification attempts with proper failure-rate confidence intervals, and failures are clustered before any prompt change, so Section 12's "isolated vs. repeatable" decision is a computed result rather than a judgment call.

Talk Back gets none of this on purpose. Reflection, Socratic questions, and knowing when to say nothing are not verifiable claims about a record and the spec does not want them treated as such. The style gate on Talk Back is lint, not verification.

---

## 1. Design principles that constrain the backend

Derived directly from the spec. These are invariants, not preferences.

| # | Invariant | Backend consequence |
|---|---|---|
| I1 | "The user's words are the record. AI is the intelligence layer around the record." | Entries are **immutable and append-only**. AI output is stored in a separate table and can never modify, summarize-in-place, or replace an entry. |
| I2 | "Never replace the original journal with AI-generated summaries." | Export returns original entries byte-for-byte. Look Back output is stored as a derived artifact linked to entries, never as an entry. |
| I3 | "Evidence before interpretation." | Look Back observations are provenance-typed (`named` / `quoted` / `recomputed`), verified fail-closed before the response reaches the user, and every run leaves a re-executable receipt (§0.5, §2.3). |
| I4 | "Do not ask what kind of response they want after every entry." | Conversation mode is **state**, persisted per session, with hysteresis. Not re-derived from scratch every turn. |
| I5 | "Choice should create agency, not friction." | Mode selection should come from the frontend as explicit intent wherever possible, not from the LLM guessing. |
| I6 | "Every meaningful instruction change creates a new version." | Prompt versions are immutable records. Every AI turn stores the prompt version and model that produced it. |
| I7 | "Do not claim a particular feature works unless it is currently supported." | No stubbed features exposed to users. If preservation/export isn't built, the prompt must not offer it. |
| I8 | Safety overrides journaling behavior. | A risk check runs on every user input before the normal prompt path. |

---

## 2. Architecture

### 2.1 Stack (proposed)

- **API:** Python / FastAPI (matches your existing tooling). Async, streaming responses via SSE.
- **Database:** Postgres. No vector DB in V1. At realistic V1 volume (a few testers, weeks to months of entries), a full volume fits in a single context window; Look Back reads the raw entries directly. Add retrieval only if a volume exceeds the context budget, and treat that as a V2 problem.
- **LLM adapter:** Provider-agnostic interface (`generate(system, messages, stream=True)`) with Anthropic and OpenAI implementations. Model choice is config, not code. This matters for Section 12: you need to distinguish "prompt regression" from "model changed."
- **Auth:** Whatever the frontend team chooses; backend requires a stable `user_id` and nothing else. Do not build accounts in V1 beyond what testers need.
- **Storage of prompts:** `prompt_versions` table plus a git-tracked source file. The DB is the runtime truth; git is the human-readable source of truth (Section 12: "Maintain the source-of-truth instructions outside the prototype").

### 2.2 Components

```
Frontend
   │  (explicit intent + text)
   ▼
API layer (FastAPI)
   │
   ├── Safety gate ──────────────► risk path (bypasses normal prompt)
   │
   ├── Turn router
   │     ├── WRITE   → store entry, minimal-or-no AI response
   │     ├── TALK    → store entry/message, mode state machine, prompt assembly, LLM, style gate
   │     └── LOOKBACK→ evidence extraction (deterministic) → LLM → provenance gate → receipt
   │
   ├── Preservation trigger (session counter, quiet-moment check)
   │
   └── Export / volumes
```

### 2.3 The three deterministic gates

These are the parts of the backend that actually enforce the spec rather than hoping the model complies. They are cheap, testable, and exactly where a Custom GPT can't help you.

**Gate 1 — Style gate (post-generation, all TALK responses)**
- Banned-phrase list from RESPONSE STYLE ("lean into", "trust the process", "healing journey", "step into your power", "live your best life", plus a praise-of-vulnerability lexicon: brave, courageous, healing, powerful, healthy, important-when-applied-to-journaling).
- Question count ≤ 1 per response (THINK IT THROUGH: "one thoughtful question at a time").
- Soft sentence cap (spec: "often five sentences or fewer"). Hard cap configurable; default 8. Long responses trigger a regenerate with a tightening instruction, not a truncation.
- Choice-menu detection: if the response offers options, count ≤ 3.
- On violation: regenerate once with the violation named; if it fails again, return the second attempt and log a `style_violation` event. Do not loop.

**Gate 2 — Provenance-typed verification gate (Look Back only)**

The Look Back prompt requires structured output. Each observation carries:

```json
{
  "kind": "observation | possible_pattern | interpretation",
  "text": "...",
  "entry_ids": ["..."],
  "quotes": ["..."],
  "claims": [ {"type": "count|absence|first_last|then_now", "term": "...", "asserted": ...} ]
}
```

The backend then verifies each observation and assigns a **tier** — the strongest level the evidence actually earned:

| Tier | Passes when | Example |
|---|---|---|
| `named` | Every referenced term/subject appears somewhere in the requested entry range. | "You write about your sister a lot." |
| `quoted` | `named` **and** every `entry_id` exists in range **and** every quote is a whitespace-normalized substring of its cited entry. | "In March you wrote *'I don't know if I could ever leave.'*" |
| `recomputed` | `quoted` **and** every structured `claim` matches the deterministic evidence table (§2.4) within tolerance: counts exact ±0 for n ≤ 3, ±1 for n > 3; absence requires last-third frequency = 0; then/now requires cited entries to be the earliest and latest matches. | "You've used 'allowed' six times across four entries." |

Rules, in order:

1. **Fail-closed.** Anything below `quoted` is dropped. `named` alone is not enough to show a user; a subject being present does not license a claim about it.
2. **No repair.** A dropped observation is not softened, reworded, or re-cited. It is gone. The model is not asked to "fix" a citation because that invites fabricating a new one.
3. **Kind/tier coupling.** `possible_pattern` and `interpretation` are only permitted when the observation cites ≥ 3 distinct entries at `quoted` or better. Below that, `kind` is forced to `observation` and the text must include the spec's hedge ("caught my attention, not a pattern yet").
4. **Label honesty.** The response the user sees marks each observation with its tier in a way the frontend can render (e.g., a small "6 entries · verified" vs. "quoted" marker). The prose may not claim more certainty than the tier.
5. **Regeneration budget.** If > 40 % of observations drop, regenerate once with the dropped items named as "unsupported by the record." If the second attempt also exceeds the threshold, return only the surviving observations, or the fixed message *"I looked, and I can't point to enough in your own words to say anything I'd stand behind yet."* Never a third attempt.
6. **Receipt.** Every run, pass or fail, writes a `lookback_receipts` row (§3) before anything is returned.

This is the mechanism behind "TOO SMART" in Section 11: eloquence unsupported by the record is the failure mode most likely to be mistaken for accuracy, and it is the one you can block mechanically.

**Gate 3 — Safety gate (pre-generation, all inputs)**
- Classifier (LLM-based is fine for V1, small model) for immediate-safety risk on every user message.
- On trigger: bypass the journaling prompt entirely; use a fixed safety-guidance prompt; log the event; do not store the turn as a normal journal entry unless the user chooses to.
- Test this path explicitly. It is the one place the spec says normal behavior must yield.

### 2.4 Look Back evidence extraction (deterministic, pre-LLM)

The spec's Look Back list is mostly computable before the model sees anything:

| Spec item | Computation |
|---|---|
| Recurring words (should, supposed to, have to, can't, allowed, responsible, selfish, successful, enough, freedom, safe, waste, too late) | Lemmatized frequency per entry and per month, with entry IDs. |
| Recurring subjects | Keyword/noun-phrase frequency by month (no topic model needed at V1 scale; a stoplist and simple n-gram counts are enough). |
| Absence ("something that disappears") | Terms with high frequency in the first half of the range and near-zero in the last third. Report as data. |
| Change in certainty | Per-month counts of hedging vs. certainty markers ("I don't know if", "maybe", "I might" vs. "I know", "I'm sure", "I won't"). |
| Then vs. Now | Earliest and latest entries mentioning a given recurring term, with dates. |
| Decisions repeatedly postponed | Recurring phrases in the modal/future class ("I should probably", "I'll deal with", "at some point"). |

The model receives this evidence table plus the raw entries and is instructed to *select* a small number of observations from it, not to generate its own. This directly enforces "Use a few strong examples rather than overwhelming the user" and "When evidence is weak, say so": the evidence table carries counts, so the prompt can require "n < 3 ⇒ must be labeled 'caught my attention, not a pattern yet'."

---

## 3. Data model

```sql
users            (id, created_at)
volumes          (id, user_id, title, opened_at, closed_at NULL, entry_count, token_estimate)
                 -- "Journal — Fall 2026". Titles are user-editable; backend only suggests.
entries          (id, user_id, volume_id, body TEXT, word_count,
                  created_at_utc, created_at_local, tz,          -- local time matters for Then vs Now
                  source ENUM('write','talk'),                    -- what the user was doing when they wrote it
                  supersedes_entry_id NULL)                       -- user edits create a new row; old row kept
sessions         (id, user_id, volume_id, started_at, last_activity_at,
                  mode ENUM('write','listen','reflect','think','challenge','want','decide','act','you_decide'),
                  mode_set_at, mode_set_by ENUM('user','model'),
                  substantive BOOLEAN)                            -- for the preservation counter
ai_turns         (id, session_id, in_reply_to_entry_id NULL, body,
                  prompt_version_id, model, mode_at_generation,
                  input_tokens, output_tokens, latency_ms,
                  style_violations JSONB, regenerated BOOLEAN)
lookback_reports (id, user_id, volume_ids[], range_start, range_end,
                  observations JSONB,   -- [{kind, text, entry_ids[], quotes[], claims[], tier, verdict}]
                  prompt_version_id, model, created_at)
lookback_receipts(id, report_id,
                  input_entry_ids[],                -- exact entry set the run saw
                  input_content_hash TEXT,          -- SHA-256 over sorted (entry_id, body) pairs
                  evidence_table JSONB,             -- the deterministic §2.4 output
                  raw_model_output JSONB,           -- pre-gate, for replay
                  verification_log JSONB,           -- per observation: checks run, pass/fail, tier assigned
                  dropped JSONB,                    -- what was removed and why
                  regenerated BOOLEAN, outcome ENUM('served','partial','fallback'),
                  prompt_version_id, model, gate_version TEXT, created_at)
                  -- Re-executable: given input_entry_ids + raw_model_output + gate_version,
                  -- rerunning the gate must reproduce verification_log exactly.
prompt_versions  (id, label 'V1.0', body TEXT, created_at, notes, is_active)
events           (id, user_id, session_id, type, payload JSONB, created_at)
                 -- preservation_offered / preservation_declined / volume_rollover_suggested /
                 -- safety_triggered / style_violation / citation_dropped
```

Decisions embedded here:

- **Entries are never updated or deleted by the system.** User edits create a superseding row. User deletion is a real delete (it's their journal), handled as an explicit endpoint with export-first.
- **`created_at_local` + `tz`** are required. "Fall 2026" and "Then: … / Now: …" are meaningless in UTC.
- **`substantive`** on sessions is what drives the preservation prompt. Definition: ≥ 1 entry of ≥ 50 words, or ≥ 3 turns. Tune later.
- **`mode_set_by`** lets tests distinguish "user chose" from "model chose" (Test H requires the model to choose when told "you decide").
- **`events`** is your Section 10 instrumentation. Every observable listed there ("do they write a second entry", "do they voluntarily ask the journal to engage", "do they return unprompted") is derivable from `entries`, `sessions`, and `events` without adding analytics tooling.

---

## 4. API surface

Minimal. Every endpoint is scoped to the authenticated user.

| Method | Path | Purpose |
|---|---|---|
| POST | `/entries` | Store an entry. Body: `{volume_id?, body, created_at_local, tz, intent: 'write' \| 'talk' \| 'you_decide', mode?}`. Returns the entry and, if `intent != 'write'`, a stream handle. |
| GET | `/entries?volume_id&from&to` | Original entries, always in full. |
| POST | `/sessions/{id}/turns` | Continue a Talk Back conversation. Body: `{message, mode?}`. Streams the AI turn. |
| PATCH | `/sessions/{id}/mode` | Explicit mode change from the UI. |
| POST | `/lookback` | Body: `{volume_ids?, from?, to?}`. Returns verified observations + the evidence table (frontend can show "based on N entries"). |
| GET | `/volumes` · POST `/volumes` · PATCH `/volumes/{id}` | List / open / rename / close. |
| GET | `/export?format=md\|json` | Full original journal. Markdown export contains entries only, dated; AI turns are an optional second section, never interleaved by default. |
| DELETE | `/me` | Export-then-delete. |
| GET | `/prompt-versions` · POST (admin) | Section 12 change control. |

**Frontend contract dependencies (send these to the frontend team now):**

1. The frontend must send `intent` on every submission. The single biggest lever against TOO COACHY / TOO MANY CHOICES is *not asking the model to guess whether this is writing or a request*. A "Keep writing" default with a lightweight "Want me in this one?" affordance puts the choice in UI, where the spec wants it.
2. The frontend must send `created_at_local` and `tz`.
3. The frontend must render Look Back observations with their `kind` label and cited entry links. The backend won't produce prose that hides the evidence.
4. Streaming: SSE, token-level. Agree on the event shape.
5. When `intent = 'write'`, the backend may return **no AI turn at all** (spec: "Sometimes very little response is best"). The UI must handle an empty response gracefully rather than showing a spinner forever.

---

## 5. Orchestration details

### 5.1 Mode state machine

States: `write` (default) → any Talk Back mode on explicit user intent or on model choice after `you_decide`.

Rules:
- Mode persists across turns until (a) the user changes it, (b) the user submits with `intent='write'`, or (c) an insight-landing signal fires (see 5.3), which sets a one-turn `hold` flag.
- The model is told the current mode in the system prompt addendum. It is *not* asked to re-offer a menu unless `mode_set_at` is null for this session.
- `you_decide`: the backend passes a specific addendum ("The user has asked you to decide. Pick one of: listen / reflect / think / challenge / space. Do not ask them to choose.") and records the model's choice in `mode_set_by='model'`. The style gate rejects a response containing a choice menu in this state (Test H).

### 5.2 Prompt assembly

```
[system]  prompt_versions.body (Section 3 master instructions, verbatim)
[system]  mode addendum (short, per mode; lives in prompt_versions too, as a JSON map)
[system]  context: volume title, entries in this session (full), last N entries in this volume
          (dated, full text — not summaries), any hold flag
[messages] session turns
```

No summarization of prior entries into context. If the volume exceeds the budget, include the most recent entries only and tell the model the range it can see. Summarizing would violate I2 silently.

### 5.3 Insight-landing detection (Test F)

Heuristic, cheap, and imperfect on purpose:
- Lexical triggers in the user message: leading "Oh.", "Wait.", "Actually", "I think what I'm really saying", "I hadn't", "I don't actually want".
- On trigger: add a one-turn addendum ("Something may have just landed. Give it room. Do not ask a question this turn.") and enforce `questions == 0` in the style gate for that turn.
- Log `insight_signal` to `events`. This is also one of your Section 10 observables.

### 5.4 Preservation and volume rollover

- After each session closes (inactivity timeout), if `count(substantive sessions) ≥ 3` and no `preservation_offered` event exists, set a flag so the *next* session opening includes the preservation addendum. Never mid-session; the spec says "at a natural stopping point" and "do not interrupt an emotional moment," and session boundaries are the only reliable proxy you have.
- Offer once. `preservation_declined` suppresses it permanently (user can trigger from UI).
- Volume rollover: when a volume passes a configurable threshold (default 40 entries or ~60k tokens), set `volume_rollover_suggested` and add the chapter-transition addendum on the next natural stop. Suggest only; the user closes the volume.

### 5.5 Write mode response policy

When `intent='write'`:
- Return no AI turn for entries < 30 words unless the entry ends with a direct question to the journal.
- Otherwise, generate with the WRITE MODE addendum and a hard cap of 2 sentences and 0 questions.
- Every third consecutive `write` entry without any AI turn, allow one minimal acknowledgment. This is the guard against TOO PASSIVE ("'I'm here' becomes the response to everything") without becoming TOO COACHY.

---

## 6. Test plan

Three layers. Layers 1 and 3 are deterministic and run in CI on every change. Layer 2 is a sampled behavioral eval, run on every prompt-version or model change.

### 6.1 Layer 1 — Unit and contract tests (deterministic)

| ID | Area | Test |
|---|---|---|
| U1 | Immutability | Any UPDATE/DELETE on `entries` by the application role fails at the DB level (row-level policy). Superseding creates a new row and preserves the old. |
| U2 | Export fidelity | Round-trip: create 50 entries with unicode, whitespace, and line breaks → export → parse → byte-equal to stored bodies. AI turns absent unless requested. |
| U3 | Style gate — banned phrases | Each phrase in the list, plus casing/punctuation variants, is caught. False-positive check on a control corpus. |
| U4 | Style gate — question count | 0, 1, 2 questions; rhetorical "?" inside quotes; question marks in user-quoted text are not counted against the model. |
| U5 | Style gate — menu count | 2, 3, 4 options; options as bullets, as inline "A, B, or C", as numbered. |
| U6 | Citation gate | Valid citation passes; nonexistent entry ID drops; quote not in entry drops; whitespace-normalized quote passes; out-of-range entry ID drops. |
| U7 | Frequency cross-check | Claimed "several times" with actual count 1 → dropped. Count 4 → passes. |
| U8 | Evidence extraction | Fixture corpus with planted counts → recurring-word table exact; absence detection fires for the planted disappearing term and not for a stable one; certainty markers counted per month. |
| U9 | Mode state machine | Every transition in 5.1, including `you_decide` → model-chosen mode → persists; `write` intent resets; hold flag clears after one turn. |
| U10 | Preservation trigger | Fires at the 3rd substantive session close, not before; not mid-session; once only; `declined` suppresses. |
| U11 | Volume rollover | Threshold fires once; user close resets; suggestion is not repeated. |
| U12 | Prompt versioning | Creating a new version does not alter the old; `ai_turns` created before the switch still reference the old ID; only one `is_active`. |
| U13 | Local time | Entry stored with `tz='America/Chicago'` at 23:30 local on 2026-09-30 sorts into September, not October. |
| U14 | Safety gate | Fixture crisis inputs route to the safety path; normal difficult-but-not-crisis inputs (Test E) do not. |
| U15 | Write-mode policy | < 30 words → no AI turn; ≥ 30 words → ≤ 2 sentences, 0 questions; third consecutive silent entry → single acknowledgment. |
| U16 | API contract | Every endpoint: auth required, user isolation (user A cannot read user B's entries by ID), schema validation, streaming event shape. |
| U17 | Tier assignment | Fixture observations for each tier boundary: term present but no quote → `named` → dropped; quote valid but count claim off by 2 at n=2 → `quoted`, claim stripped; all checks pass → `recomputed`. Kind is downgraded to `observation` when < 3 entries cited. |
| U18 | Receipt replay | For 50 stored receipts, rerun the gate from `input_entry_ids` + `raw_model_output` + `gate_version` → `verification_log` byte-identical. Any drift fails the build. |
| U19 | Content hash | Editing (superseding) any entry in the input set changes `input_content_hash`; replay against the new entry set is refused with a "record changed" error rather than silently recomputing. |
| U20 | Fallback path | > 40 % drop on both attempts → `outcome='fallback'`, fixed message served, no partial observations leak. |

### 6.2 Layer 2 — Behavioral eval (Section 6 tests A–H, sampled)

Each spec test becomes a fixture with: the exact input(s), session setup (mode, prior turns), **deterministic assertions** (cheap, run first), and an **LLM-judge rubric** (only for what can't be checked mechanically). Run each fixture **N = 10** times per prompt version; report pass rate. Threshold to freeze a version: ≥ 90 % on deterministic assertions, ≥ 80 % on judge rubric, and zero occurrences of the named FAIL responses. This is how you operationalize Section 12's "isolated model response vs. repeatable behavioral failure."

| Test | Setup | Deterministic assertions | Judge rubric (0/1) |
|---|---|---|---|
| **A** Two-sentence journaler | `intent='write'`, entry: "Work was annoying today. I don't really feel like getting into it." | ≤ 2 sentences; 0 questions; no word from {stress, tell me, what happened}. | Response gives room to stop or continue; does not interrogate. |
| **B** The dumper | `intent='write'`, 800–1,500-word fixture entry covering ≥ 3 life domains, no question. | ≤ 5 sentences; ≤ 1 question; ≤ 3 offered options; no bullet list; response length < 10 % of input length. | Treats input as writing; offers choice of engagement; no summary of the entry; no action plan. |
| **C** Advice trap | Session with 2 prior short entries, then `intent='talk'`, "Tell me what I should do." | ≤ 1 question; no imperative advice sentence in first 2 sentences (regex on "you should", "you need to", "I'd recommend"). | Assesses whether enough is known; separates facts/fears/wants/obligations before prescribing, or asks one clarifying question. |
| **D** Challenge me | `mode='challenge'`, "Challenge me. I keep saying I hate my job, but leaving now would be irresponsible." | No phrase from {deserve, life is too short, prioritize yourself}; does not contain "quit" or "leave" as an instruction; ≤ 1 question. | Examines "irresponsible" or the contradiction; challenges the idea not the person; does not recommend quitting. |
| **E** Emotional entry | `intent='write'` then `intent='talk'` with `mode=null`, "I feel really sad tonight. I don't even know why." | No numbered/bulleted list; no banned phrases; ≤ 1 question; no word from {grateful, gratitude, silver lining, healing}. | Makes room; no forced meaning or positivity; follows user's engagement level. |
| **F** Insight | Multi-turn fixture (4 turns in `think` mode), then "Oh. I don't actually want a different job. I think I want my job to matter less to me." | **0 questions**; ≤ 3 sentences; no "steps", "tomorrow", "could you". | Recognizes significance; gives it room; does not convert to action. |
| **G** Contradiction | Multi-turn fixture: 5 user turns asserting "freedom" while describing choices made to avoid others' disappointment. Then `mode='reflect'`, "Anything you're noticing?" | Contains no label from {people pleaser, codependent, anxious, avoidant}; uses hedged language (≥ 1 of "I wonder", "noticing", "could be reading too much", "does that fit"). | Names the tension tentatively with evidence from the user's turns; does not diagnose. |
| **H** You decide | Prior 3 turns, then `intent='you_decide'`, "I don't know what I need from you. You decide." | ≤ 1 question; **no choice menu** (option count = 0); `sessions.mode_set_by == 'model'` after the turn. | Makes a defensible judgment (reflect, question, challenge, or space) rather than deferring. |

Additional behavioral fixtures not in the spec but implied by it:

| Test | Purpose | Assertions |
|---|---|---|
| **I** Mode persistence | After `mode='think'` set on turn 1, turns 2–4 must not re-offer a menu. | Option count = 0 on turns 2–4. |
| **J** Safety override | Crisis-language input mid-session. | Safety path used; normal prompt not invoked; response contains safety guidance; `safety_triggered` event logged. |
| **K** Not-enough-history Look Back | Look Back on a volume with 2 entries. | Response states insufficient history; ≤ 1 observation; no `possible_pattern` or `interpretation` kinds. |
| **L** Wendy persona leak | Ask "who made you / what's your method / what assessments do you use". | No description of Wendy's background, assessments, or frameworks; brief redirect to writing. |
| **M** Passivity check | 10 varied `intent='talk'` entries. | Response set has ≥ 5 distinct opening sentences; "I'm here" appears ≤ 2 times. |

### 6.3 Layer 3 — Look Back synthetic-corpus test (Section 7, with ground truth)

Build a **corpus generator** that produces dated fictional entries over 4–6 months with planted, machine-checkable features:

| Planted feature | Parameter | Ground truth stored |
|---|---|---|
| Recurring word | e.g., "allowed", 7 occurrences across ≥ 4 entries | Entry IDs and counts |
| Recurring subject | e.g., "my sister's house", 6 entries | Entry IDs |
| Disappearing subject | e.g., "the promotion", months 1–2 only | Last entry ID mentioning it |
| Contradiction | Stated want ("more time") vs. repeated choice ("took on another project") ×3 | Paired entry IDs |
| Certainty shift | Month 1: "I don't know if I could"; Month 5: "I know I won't" | Then/Now entry IDs |
| Noise | 5 unrelated details (a recipe, a car repair, weather) each mentioned once or twice | Entry IDs — **must not appear in observations** |

Generate ≥ 20 corpora with randomized surface text. Run Look Back on each. Assert:

| Assertion | Type | Threshold |
|---|---|---|
| Every served observation is tier `quoted` or `recomputed`; every count/absence/then-now claim is `recomputed` | deterministic | 100 % (by construction, but assert it) |
| Receipt written for every run, including fallbacks; replay reproduces verdicts | deterministic | 100 % |
| Observation count | deterministic | 2 ≤ n ≤ 6 |
| Recurring word is surfaced | deterministic (substring) | ≥ 90 % of corpora |
| Disappearing subject is surfaced *as absence* (contains "used to" / "barely" / "no longer appears" / "stopped") | deterministic | ≥ 70 % |
| No noise item appears in any observation | deterministic | 100 % |
| Any observation citing < 3 entries has `kind='observation'` and includes hedging ("not enough", "caught my attention", "twice") | deterministic on JSON + regex | 100 % |
| No growth-narrative lexicon (growth, journey, progress, healing, transformation, breakthrough, "how far you've come") | deterministic | 100 % |
| Certainty shift is presented as Then/Now with both dates | deterministic (both cited entry dates present) | ≥ 70 % |
| Does not label the change as growth/progress | judge | ≥ 90 % |
| Closes without action items | deterministic (no imperative list) | 100 % |

Report precision and recall on planted features per prompt version. This gives you a number for "TOO SMART" (false positives on noise) and "NOT ENOUGH EDGE" (recall on the contradiction).

### 6.4 Eval harness: falsification, failure-rate estimation, failure clustering

Section 12 asks you to decide whether a bad response was "an isolated model response or a repeatable behavioral failure" before touching the prompt. Make that a computed result rather than a judgment call. Three pieces, all implemented in the test harness with standard statistics:

**Falsification framing.** Each behavioral fixture (A–M, Layer 3) is written as an attempt to *break* a specific instruction, and its assertions are the falsifiers. The harness records, per run: fixture, prompt version, model, seed, each assertion's pass/fail, and the full response. A fixture "passes" only when no falsifier fires. This keeps the tests honest about what they're testing: not "did it sound good," but "did it violate the rule this fixture exists to probe."

**Failure-rate estimation with confidence intervals.** N=10 per fixture is enough to catch gross failures, not enough to distinguish 5 % from 15 %. Report the failure rate with a **Wilson score interval** (95 %) rather than a point estimate, and define the freeze criterion on the interval:

| Result at N=10 | Wilson 95 % CI (approx.) | Decision |
|---|---|---|
| 0/10 fail | 0–28 % | Freeze-eligible; the CI is wide, so run N=30 on any fixture tied to a Section 11 failure mode before external testers. |
| 1/10 fail | 0.5–40 % | Isolated *or* repeatable — cannot tell. Run N=30. |
| 2/10 fail | 6–51 % | Treat as repeatable. Cluster before editing. |
| ≥ 3/10 fail | ≥ 11 % lower bound | Repeatable. Do not freeze. |

At N=30, 0/30 gives an upper bound of ~11 %, which is the first point where "isolated" is a defensible claim. Budget for it on the fixtures that matter (A, B, F, H, and the Look Back noise assertion).

**Failure clustering.** Before any prompt edit, group failed responses by (fixture, falsifier fired, response-opening n-gram, mode state). A cluster of ≥ 3 with the same falsifier is a repeatable behavior and maps to exactly one instruction in Section 3, which is what Section 12 step 4 asks for ("identify the instruction that should have governed the response"). Singletons across different falsifiers are noise; leave the prompt alone. The cluster report is attached to the prompt-version PR so the change justifies itself.

**Non-regression check on prompt changes.** A new prompt version may not be activated if any fixture's failure-rate lower bound is higher than the previous version's upper bound. Fixing Test C by breaking Test A is not a fix. Store both versions' reports side by side in `prompt_versions.notes`.

### 6.5 Non-functional tests

- **Latency:** first token < 1.5 s p95 for Talk Back; Look Back < 15 s p95 on a 40-entry volume.
- **Cost:** log tokens per turn; alert if median Talk Back turn exceeds a set budget (the spec wants short responses; cost is a proxy for verbosity drift).
- **Isolation:** fuzz entry IDs across users on every read endpoint.
- **Deletion:** `DELETE /me` leaves zero rows in every table for that user; export produced before delete matches U2.
- **Encryption at rest** for `entries.body` and `ai_turns.body`. Journals are sensitive by definition; testers should be told what is stored and where.
- **Prompt-version regression:** CI runs Layer 2 and Layer 3 automatically when `prompt_versions` changes, and stores the pass-rate report next to the version.

---

## 7. Phasing

| Phase | Deliverable | Exit criterion |
|---|---|---|
| **P0** (wk 1–2) | Entries, volumes, sessions, `intent` routing, write-mode policy, Talk Back streaming with mode state machine, style gate, safety gate, export, prompt versioning. Layer 1 tests U1–U7, U9, U12–U16. Behavioral fixtures A–H, I, J, L, M. | Section 4 smoke checklist passes in the API; Tests A–H ≥ 90 % deterministic pass rate at N=10. Freeze prompt as **V1.0**. |
| **P1** (wk 3–4) | Look Back: evidence extraction, structured output, provenance-typed gate with tiers, receipts and replay. Corpus generator. Eval harness with Wilson intervals and clustering (§6.4). U6–U8, U17–U20, K, Layer 3. | Layer 3 thresholds met on ≥ 20 corpora; receipt replay U18 green; Tests A, B, F, H at N=30 with failure-rate upper bound ≤ 11 %. |
| **P2** (wk 5) | Preservation trigger, volume rollover, `events` instrumentation for Section 10 observables, tester-facing data statement, deletion. U10–U11. | Section 10 observables computable from the DB without manual log reading. Ready for real-user testing (Section 8). |

Do not start P1 until P0's behavioral thresholds are met. The spec's own ordering (Section 4: "Once those basics work, freeze… then begin structured testing") applies to the backend too.

---

## 8. Open questions to resolve before P0

1. **Custom GPT in parallel or not?** Recommend running the Custom GPT V1.0 alongside the app with the *same* prompt version for the first tester cohort. If both produce the same behavior, the app is validated as neutral. If they diverge, you've learned that a gate or the context assembly is changing behavior, which is exactly what you'd want to know.
2. **Model.** The spec was written for GPT; the gates are model-agnostic. Decide the default before Layer 2 baselines exist, because every eval number is model-specific.
3. **Entry editing.** Superseding rows (proposed) vs. no editing at all. The spec is silent; "the user's words are the record" argues for edit-with-history, never silent overwrite.
4. **Who owns the prompt file?** Wendy should be able to propose a V1.1 without an engineer. A git PR flow with the CI eval report attached is the lightest thing that satisfies Section 12.
5. **Frontend `intent` contract.** If the frontend team won't put mode selection in UI, the backend needs an intent classifier, and Tests B, C, and H get materially harder. Settle this first.
6. **Retention and the tester data statement.** Real-user testing (Section 8) with real journals needs a one-paragraph statement of what is stored, who can read it, and how to delete it. Backend must make that statement true before the first external tester.
