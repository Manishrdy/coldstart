# Coldstart

Coldstart polls public job-board data, filters it down to roles actually
worth your time, scores each surviving posting against the right one of
your 4 resumes using an LLM, and delivers a curated daily email digest plus
a full CSV audit trail. No manual job-board trawling.

- **Why it's built this way:** [`scope.md`](scope.md) — design rationale
  and every real-world finding that shaped it.
- **How it's built, module by module:** [`DEVELOPMENT_PLAN.md`](DEVELOPMENT_PLAN.md)
  — the build spec, including the reasoning behind every deviation made
  from the original plan while building.

This README is the *operator's* doc: how to set it up, run it, and keep it
running.

---

## How it works, in one pass

```
manifest poll → per changed ATS slice:
  download + verify → title filter → location filter → eligibility filter
  → dedupe → route to one of 4 resumes → LLM score → persist
→ CSV export (all outcomes, every band)
→ daily digest email (strong / consider / uncertain sections + footer stats)
```

Every job that survives title+location+eligibility filtering ends up in
SQLite with exactly one terminal status: `scored`, `excluded`, or `failed`.
Nothing is silently dropped — filter/eligibility/routing decisions, LLM
failures, and email failures are all logged to an `errors` table you can
query directly (see [Operations](#operations) below).

---

## Requirements

- Python 3.12+
- [`uv`](https://docs.astral.sh/uv/) for dependency management
- Either:
  - **Dev mode (free, local):** [Ollama](https://ollama.com) running
    locally with a small model pulled (e.g. `ollama pull qwen2.5:3b`), or
  - **Prod mode (real cost):** an API key for at least one of DeepSeek,
    Kimi, Mistral, Grok, OpenAI, Anthropic, or Gemini
- A Gmail account with 2FA enabled, to generate an
  [App Password](https://myaccount.google.com/apppasswords) for sending
  the digest (Gmail SMTP over STARTTLS — not OAuth)
- 4 resumes (PDF or DOCX) covering: general SWE, AI/Agentic Engineer,
  Forward Deployed Engineer (general), Forward Deployed Engineer (AI)

---

## Quick start

```bash
# 1. Install dependencies
uv sync

# 2. Configure
cp .env.example .env
# edit .env — at minimum: EXPERIENCE_YEARS, SMTP_APP_PASSWORD, and either
# OLLAMA_MODEL (dev mode) or a provider API key (prod mode). See
# "Configuration" below for what every variable does.

# 3. Add your resumes
cp ~/path/to/*.pdf config/resumes/
# see config/resumes/README.md for the filename convention that resolves
# each resume to the right slot

# 4. Initialize the database
uv run python scripts/init_db.py

# 5. Run a poll — this ingests your resumes on first run, then fetches,
#    filters, and scores any new jobs
uv run python scripts/run_poll.py

# 6. Send today's digest
uv run python scripts/run_digest.py
```

`run_poll` prints a one-line summary (fetched/filtered/scored/failed/
excluded counts and the CSV path) and exits `0` on success. `run_digest`
prints `Digest sent.` or a failure message and exits `1` if the send
failed (check the `errors` table — the CSV is never lost even if email
fails).

---

## Configuration

Everything is read from `.env` (see [`.env.example`](.env.example) for the
full, current list with inline comments). The sections below explain the
*why* behind each group; `.env.example` is the source of truth for exact
variable names and defaults.

### Experience

`EXPERIENCE_YEARS` is the one required value with no safe default — a
direct number (e.g. `3.5`), not a start date, since a career break would
make date-derived experience silently overcount. It's shared across all 4
resumes.

### LLM / providers

`LLM_MODE` is the switch, and there is **no cross-provider fallback** —
exactly one provider is used per run:

- `LLM_MODE=dev` → **always** Ollama, using `OLLAMA_MODEL` directly
  (`OLLAMA_BASE_URL` for where it's running) — free, no key, good for
  testing the pipeline end-to-end before spending anything real.
- `LLM_MODE=prod` → **exactly** the one provider named by `LLM_PROVIDER`
  (one of `deepseek`/`kimi`/`gemini`/`mistral`/`openai`/`anthropic`/`grok`).
  That provider's API key must be set (`DEEPSEEK_API_KEY`,
  `ANTHROPIC_API_KEY`, etc. — only the one matching `LLM_PROVIDER` needs
  a real value). On a rate limit or outage, `MAX_RETRIES_PER_PROVIDER`
  controls how many times that *same* provider is retried with backoff
  before the job is marked `failed` — there's no second provider it falls
  over to.
- Each provider also has its own optional **model override**
  (`ANTHROPIC_MODEL`, `DEEPSEEK_MODEL`, etc.) — leave blank to use the
  built-in default (see `DEFAULT_MODELS` in
  `src/coldstart/scoring/providers.py`), or set it to pick a specific
  model, e.g. `ANTHROPIC_MODEL=claude-haiku-4-5-20251001` for a
  cheaper/faster run or `claude-opus-5` for a stronger one. Picking a model
  with no entry in that file's `PRICING` dict still works — cost tracking
  for it just reports $0 with a warning in the logs until you add real
  pricing numbers there.
- `BATCH_SIZE` and `LLM_REQUEST_TIMEOUT_SECONDS` rarely need changing;
  batching multiple jobs per LLM call is a known rough edge — leave
  `BATCH_SIZE=1` unless you've verified it against your provider.

### Budget

`DAILY_TOKEN_SPEND_CEILING_USD` (default `3.0`) is a hard circuit breaker,
checked before every LLM call. Once today's spend (local time, in
`TIMEZONE`) reaches the ceiling, `run_poll` raises and halts the *entire*
run immediately — not just the current job or slice. A warning is logged
once per day at 80% of the ceiling. Spend is tracked in the `spend_log`
table regardless of whether you ever hit the ceiling.

### Scoring thresholds

`SCORE_THRESHOLD_STRONG` (default `70`) and `SCORE_THRESHOLD_CONSIDER`
(default `60`) control which digest section a scored job lands in
("strong matches" / "worth considering" / never emailed prominently but
still in the CSV). These are read live at digest-build time — changing
them in `.env` takes effect on the next `run_digest`, no rescoring needed.

### Email

Gmail SMTP over STARTTLS on port 587 with an **App Password**, not your
regular password (requires 2FA on the Google account — generate one at
https://myaccount.google.com/apppasswords). `DIGEST_TIME_PDT` and
`TIMEZONE` only matter if you're driving `run_digest` from a scheduler
that reads them (cron itself doesn't — see [Scheduling](#scheduling)
below).

### Paths / Polling

Sensible defaults for everything (`data/`, `logs/`, `output/`,
`config/resumes/manifest.json`, `data/coldstart.sqlite3`, and the live
manifest URL) — only override these if you need non-default locations.

---

## Resumes

Drop 4 resumes (PDF or DOCX) into `config/resumes/`. On the next
`run_poll`, they're automatically text-extracted, classified into one of
4 slots by filename (falling back to a one-time LLM classification call
only if the filename is ambiguous), normalized to JSON, and the originals
moved into `config/resumes/originals/`.

Filename convention — see [`config/resumes/README.md`](config/resumes/README.md)
for the full table, in short:

| Filename contains | Slot |
|---|---|
| `swe`, `general`, `standard`, or `software` (no `fde`/`ai`) | A — general SWE |
| `ai`, `agentic`, or `ml` (no `fde`) | B — AI / Agentic Engineer |
| `fde` (no `ai`/`agentic`/`ml`) | C — Forward Deployed Engineer, general |
| `fde` + one of `ai`/`agentic`/`ml` | D — Forward Deployed Engineer, AI |

A name with none of these markers (e.g. `resume.pdf`) is genuinely
ambiguous and escalates to an LLM call rather than silently guessing.
`run_poll` **hard-stops before any network or LLM spend** if all 4 slots
can't be resolved — fix the filenames (or add the missing resume) and
run again.

`config/resumes/` is entirely gitignored except its own README — resumes
contain PII and must never be committed.

---

## Running it

### Manually

```bash
uv run python scripts/run_poll.py     # fetch, filter, score, export CSV
uv run python scripts/run_digest.py   # build + send today's email digest
```

Run `run_poll` as many times as you like — it's incremental (only
downloads ATS slices whose content hash changed since last time) and
idempotent (a job already in the database is never re-scored, so re-running
against unchanged data costs nothing).

### Scheduling

`run_poll` and `run_digest` are separate entrypoints on separate cron
schedules on purpose — `run_digest` only ever reads from the database, it
never re-scores. Match the cron cadence to your `.env`:

```cron
# Poll every 30 minutes (matches the default POLL_INTERVAL_MINUTES)
*/30 * * * * cd /path/to/coldstart && /path/to/uv run python scripts/run_poll.py >> logs/cron.log 2>&1

# Send the digest once a day at 08:00 (matches the default DIGEST_TIME_PDT,
# in TIMEZONE — cron itself runs in the system's local time, so adjust the
# hour here if your system clock isn't already in TIMEZONE)
0 8 * * * cd /path/to/coldstart && /path/to/uv run python scripts/run_digest.py >> logs/cron.log 2>&1
```

Find your `uv` path with `which uv`. Both scripts exit non-zero on
failure, so cron's own mail-on-error behavior (or a monitoring wrapper)
will surface a broken run.

---

## What gets produced

- **CSV** — `output/scored_YYYY-MM-DD.csv`, one file per day, appended to
  across multiple polls (not overwritten). Every scored and excluded job,
  every band — including `reject` — sorted by score descending. This is
  the full audit trail; the digest email is a curated subset of it.
- **Email digest** — one per day: Strong matches (≥ `SCORE_THRESHOLD_STRONG`)
  → Worth considering (`SCORE_THRESHOLD_CONSIDER`–`SCORE_THRESHOLD_STRONG`)
  → Location uncertain → Eligibility uncertain, followed by a footer with
  fetched/filtered/scored/failed counts, today's spend, provider(s) used,
  the CSV path, and a count of unresolved errors. A day with nothing new
  still sends a short "no new matches today" note — silence would be
  ambiguous (the pipeline could just be broken).
- **SQLite** (`data/coldstart.sqlite3`) — the durable record of everything;
  see [Operations](#operations) to query it directly.

---

## Operations

Everything lives in one SQLite file (`data/coldstart.sqlite3` by default).
Browse it with any SQLite client (e.g. the VS Code SQLite extension), or
the `sqlite3` CLI:

```bash
sqlite3 data/coldstart.sqlite3
```

**Check for failures** (every failure across polling, filtering, routing,
scoring, or email lands here — this is the single place to look):

```sql
SELECT ts, stage, provider, job_ref, error_type, error_message
FROM errors
WHERE resolved = 0
ORDER BY ts DESC
LIMIT 20;
```

**Today's spend by provider:**

```sql
SELECT provider, model, COUNT(*) AS calls, ROUND(SUM(est_cost_usd), 4) AS spend_usd
FROM spend_log
WHERE ts >= datetime('now', 'start of day')
GROUP BY provider, model;
```

**Funnel counts for a given run** (fetched/filtered/scored/failed — the
same numbers `run_poll` prints and the digest footer sums):

```sql
SELECT run_id, started_at, fetched_count, filtered_count, scored_count, failed_count
FROM run_log
ORDER BY started_at DESC
LIMIT 10;
```

**Job outcomes by status/band:**

```sql
SELECT status, score_band, COUNT(*) FROM jobs GROUP BY status, score_band;
```

**Which ATS slices are tracked and when they last changed:**

```sql
SELECT ats_type, last_processed_at, row_count FROM slice_state ORDER BY last_processed_at DESC;
```

**Digest send history:**

```sql
SELECT sent_at, job_count, status, error FROM email_log ORDER BY sent_at DESC LIMIT 10;
```

Logs (rotating, 10 MB × 5 backups) live in `logs/coldstart.log`, tagged
with a per-run `run_id` that also appears in `run_log` and every log line
— grep one run's activity with `grep <run_id> logs/coldstart.log`.

---

## Troubleshooting

| Symptom | What's happening | Fix |
|---|---|---|
| `run_poll` exits 1, prints a resume-related message | Fewer than 4 resume slots could be resolved | Check the message for the missing slot(s); add/rename files in `config/resumes/` per the filename convention |
| `run_poll` exits 1 with "Daily budget exceeded" | The circuit breaker tripped | Wait for the next local day, or raise `DAILY_TOKEN_SPEND_CEILING_USD` |
| `run_poll`/`run_digest` exit 1 printing config problems | `.env` failed validation | Fix each listed problem — all of them are reported together, not just the first |
| `run_digest` prints "Digest send FAILED" | SMTP send failed after 3 retries | Check `email_log`/`errors` for the reason (commonly a wrong `SMTP_APP_PASSWORD`, or 2FA not enabled on the Gmail account) — the CSV for the day is unaffected either way |
| A slice never seems to update | Its content hash hasn't changed upstream | Expected — `run_poll` only reprocesses slices whose sha256 changed since last time |
| One ATS slice keeps failing but others are fine | A single slice's download/processing failure doesn't abort the run | Check `errors` for `stage='poll'` rows with that ATS name as `job_ref` |

---

## Development

```bash
uv run pytest                              # full test suite
uv run pytest --cov=coldstart --cov-report=term-missing   # with coverage
uv run ruff check .                        # lint
```

No live network calls happen in tests — `httpx` and all LLM SDKs are
mocked. See `DEVELOPMENT_PLAN.md` §21 for testing standards and coverage
gates.

---

## Architecture

| Concern | Module(s) | File(s) |
|---|---|---|
| Logging, config | 1, 2 | `logging_setup.py`, `settings.py` |
| Domain models, DB, errors | 3, 4 | `models.py`, `db.py`, `errors.py` |
| Resume ingestion | 2.5 | `resume_ingest.py` |
| Manifest polling, slice download | 5, 6 | `manifest_watch.py`, `fetcher.py` |
| Filtering (title, location, eligibility, dedupe) | 7–10 | `filters/`, `dedupe.py` |
| Resume routing | 11 | `routing.py` |
| LLM provider interface + implementations | 12, 13 | `scoring/base.py`, `scoring/providers.py` |
| Scoring rubric + orchestration | 14, 15 | `scoring/rubric.py`, `scoring/scorer.py` |
| Budget circuit breaker | 16 | `budget.py` |
| CSV export | 17 | `export.py` |
| Email digest | 18 | `digest.py` |
| Pipeline orchestration + entrypoints | 19 | `pipeline.py`, `scripts/run_poll.py`, `scripts/run_digest.py` |

Full module-by-module rationale, including every real-data finding that
shaped the design, is in [`DEVELOPMENT_PLAN.md`](DEVELOPMENT_PLAN.md).

---

## Security notes

- `.env` is gitignored from the first commit — never commit real
  credentials or `EXPERIENCE_YEARS`.
- `config/resumes/` is gitignored except its own README — resumes are PII.
- Email auth is a Gmail App Password (requires 2FA), not OAuth or your
  main account password.
- No UI, no exposed network service — everything runs as local scheduled
  scripts against a local SQLite file.
