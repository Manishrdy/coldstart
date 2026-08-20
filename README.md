# Coldstart

Coldstart polls public job-board data, filters it down to roles actually
worth your time, scores each surviving posting against the right one of
your 4 resumes using an LLM, and delivers a curated daily email digest plus
a full CSV audit trail. No manual job-board trawling.

Start it once and walk away:

```bash
uv run python scripts/run_daemon.py
```

That checks upstream every 30 minutes, ingests whatever actually changed,
emails your digest at 08:00, and serves a live dashboard at
<http://127.0.0.1:8787> — all from the one process, until you stop it.

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
  download + verify → title filter → company block list → location filter
  → eligibility filter → dedupe → route to one of 4 resumes → LLM score
  → persist
→ CSV export (all outcomes, every band)
→ daily digest email (strong / consider / uncertain sections + footer stats)
```

The daemon wraps that loop: every `POLL_INTERVAL_MINUTES` it makes one
conditional request for the manifest, and only when upstream has genuinely
changed does it run the pipeline. Once a day, past `DIGEST_TIME_PDT`, it
sends the digest — exactly once, guarded by the `email_log` table rather
than by in-memory state, so a restart never re-sends.

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

# 5. Start it. Polls, scores, emails, and serves the dashboard until you
#    Ctrl-C. Ingests your resumes on the first poll.
uv run python scripts/run_daemon.py
```

Then open <http://127.0.0.1:8787>.

**Before the first real run**, know what you're starting: on a fresh
database every relevant slice counts as changed, so the first poll is a
full backfill across all 36 of them — roughly 2.9M upstream rows, of which
1–3% survive the filters and reach the LLM. That is tens of thousands of
scored jobs and real API spend. To watch the machinery first without
spending anything, set `LLM_MODE=dev` (local Ollama) for the first cycle.

The one-shot scripts still work standalone if you'd rather drive things
yourself — see [Running it](#running-it).

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
`TIMEZONE` are read by the daemon, which fires the digest at that local
time. They are inert if you drive `run_digest` from cron instead — cron
has its own schedule and never looks at your `.env`.

### Paths

Sensible defaults for everything (`data/`, `logs/`, `output/`,
`config/resumes/manifest.json`, `data/coldstart.sqlite3`, and the live
manifest URL) — only override these if you need non-default locations.

### Logging

`LOG_LEVEL` (default `INFO`) sets verbosity for everything. `DEBUG` gives
per-job detail, which is useful while investigating and noisy for a process
that runs for weeks. The daemon writes `logs/daemon.log`; the poll and
digest runs it spawns keep writing `logs/coldstart.log`.

### Daemon

Only `scripts/run_daemon.py` reads these:

| Variable | Default | What it does |
|---|---|---|
| `POLL_INTERVAL_MINUTES` | `30` | How often to check upstream |
| `POLL_TIMEOUT_MINUTES` | `240` | Kill a `run_poll` that exceeds this |
| `DIGEST_TIMEOUT_MINUTES` | `10` | Same, for `run_digest` |
| `FORCE_POLL_HOURS` | `6` | Poll anyway this often, even with no upstream change |

`FORCE_POLL_HOURS` exists because the cheap upstream check can be wrong in
one direction: a CDN can serve a stale ETag, and a run that died mid-slice
left work behind that no future manifest change will re-trigger. A periodic
unconditional pass costs one early-returning poll and closes both holes.

### Dashboard

| Variable | Default | What it does |
|---|---|---|
| `DASHBOARD_ENABLED` | `true` | Serve the dashboard from the daemon |
| `DASHBOARD_HOST` | `127.0.0.1` | Bind address |
| `DASHBOARD_PORT` | `8787` | Port |

The default bind is loopback deliberately: the page has no authentication
and shows your entire match list. Only widen it if something else is
handling access control.

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

## Blocked companies

Some employers are excluded outright: **their postings are never scored and
their job descriptions are never sent to an LLM.** Two independent gates
enforce this, because one of them alone doesn't actually guarantee it.

**Gate 1 — `config/excluded_ats.json`.** These employers run their own ATS
feed, and that whole slice is never downloaded. Verified against the live
manifest: `amazon`, `tesla`, `apple`, `tiktok`, `google`, `uber`, and `meta`
are all present in the manifest and none of them is ever selected — 36 of
65 sources are.

**Gate 2 — `config/excluded_companies.json`.** Gate 1 cannot cover the same
employer posting through *someone else's* platform, and that does happen.
Scanning all 2,881,672 rows across the 36 downloaded slices found 170 such
rows, **8 of which survive the title filter and would otherwise have been
scored**:

| Company | Source | Rows | Would have reached the LLM |
|---|---|---|---|
| `uberfreight` | greenhouse | 84 | 2 |
| `googlefiber` | greenhouse | 83 | 6 |
| `amazon.jobs.personio.com` | personio | 3 | 0 |

The block list holds the seven companies plus their parents and known
subsidiaries: `bytedance`, `alphabet`, `facebook`, `uber freight`,
`google fiber`. Blocked rows are dropped and logged at `WARNING` naming the
company — not written to the database, consistent with how an excluded ATS
source is skipped without leaving rows.

**Matching is exact on the normalized name, never a substring.** That
matters more than it sounds: the same scan found 191 look-alike names that a
substring match would have silently destroyed, including `apple-roofing`
(58 real postings), `Metabase`, `Metabo`, `Applebank`, `uberall`,
`appletreedental`, `Meta House`, and dozens of German `metallbau-*`
metalworking firms. Normalization handles legal suffixes and separators, so
`Meta Platforms, Inc.`, `Google LLC` and `uberfreight` are all caught, while
`Applebee's` and `Googol Analytics` are not. Hostname-shaped values are
matched on their first DNS label only — hence `amazon.jobs.personio.com` is
blocked and `apple-roofing.breezy.hr` would not be.

To block another company, add `{"name": "...", "reason": "..."}` to
`config/excluded_companies.json`. Use the plain company name; both the
spaced and run-together spellings are matched.

---

## Running it

### As a daemon (recommended)

```bash
uv run python scripts/run_daemon.py
```

One foreground process. Ctrl-C stops it cleanly: it finishes the current
step, terminates any running child, and releases its lock. What it does on
a loop:

- **Every `POLL_INTERVAL_MINUTES`** — one conditional `GET` of the
  manifest. If upstream is unchanged you get a `304` with no body and
  nothing else happens at all. If it changed, `run_poll` runs as a child
  process, which then does the authoritative per-slice sha256 comparison.
- **Once past `DIGEST_TIME_PDT`** — `run_digest`, exactly once per local
  day. The guard is a query against `email_log`, not in-memory state, so a
  restart at 08:05 doesn't re-send.
- **Continuously** — serves the dashboard on `DASHBOARD_PORT`.

Some behaviour worth knowing:

- **It survives your laptop sleeping.** The loop wakes every 15 seconds and
  compares wall clocks rather than sleeping for the whole interval, so a
  six-hour sleep produces exactly one catch-up poll on wake, not twelve.
  If the machine was off past 08:00, the digest goes out when you turn it
  back on.
- **Only one can run.** A second instance sees the lock on
  `data/coldstart.lock` and refuses, naming the PID that holds it. The lock
  is a kernel `flock`, so a crash never strands it.
- **It runs the pipeline as child processes**, not in-process. That's for
  memory: a single large parquet slice peaks around 10 GB RSS, and process
  exit is the only reliable way to hand that back to the OS.
- **A broken run never kills it.** A bad `.env`, an unresolvable resume
  set, or a crashed poll all get logged loudly and retried on the next
  cycle — so fixing the underlying problem needs no restart. If the daily
  spend ceiling trips, polling suspends until the next local midnight and
  then resumes on its own.

### Manually

Both one-shot entrypoints still work on their own, and the daemon changes
nothing about them:

```bash
uv run python scripts/run_poll.py     # fetch, filter, score, export CSV
uv run python scripts/run_digest.py   # build + send today's email digest
```

Run `run_poll` as many times as you like — it's incremental (only
downloads ATS slices whose content hash changed since last time) and
idempotent (a job already in the database is never re-scored, so re-running
against unchanged data costs nothing).

Their exit codes distinguish failure modes, which is how the daemon decides
what to do next:

| Code | Meaning |
|---|---|
| `0` | Success |
| `1` | Unexpected failure, or (for `run_digest`) the send failed |
| `2` | Daily spend ceiling reached |
| `3` | Resume set isn't ready |
| `4` | Invalid configuration |

### With cron instead

If you'd rather not keep a process alive, the original two-cron setup still
works — you just don't get the dashboard, and `POLL_INTERVAL_MINUTES` /
`DIGEST_TIME_PDT` become documentation rather than behaviour, since cron
never reads your `.env`:

```cron
*/30 * * * * cd /path/to/coldstart && /path/to/uv run python scripts/run_poll.py >> logs/cron.log 2>&1
0 8 * * * cd /path/to/coldstart && /path/to/uv run python scripts/run_digest.py >> logs/cron.log 2>&1
```

Find your `uv` path with `which uv`. Every failure code is non-zero, so
cron's mail-on-error behaviour still surfaces a broken run.

Note that `run_digest` has no idempotency guard of its own — that check
lives in the daemon. Two cron invocations in one day send two emails.

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
- **Dashboard** — <http://127.0.0.1:8787> while the daemon is running. Every
  non-reject scored job, live, with sortable columns and a metrics strip.
  See [Dashboard](#dashboard-1) below.
- **SQLite** (`data/coldstart.sqlite3`) — the durable record of everything;
  see [Operations](#operations) to query it directly.

---

## Dashboard

The daemon serves it at <http://127.0.0.1:8787>. It is strictly read-only —
every request opens its own `mode=ro` SQLite connection, so it physically
cannot lock or modify the database while a poll is writing to it. WAL means
rows show up on the page *during* a poll, not after, since `run_poll`
commits per job.

**What it shows.** By default, every job with `status='scored'` scoring at
or above `SCORE_THRESHOLD_CONSIDER` — i.e. the non-reject set. A checkbox
widens it to include the reject band.

Columns are all sortable, click-cycling through descending, ascending, and
back to the default order: score, band, company, title, location, resume
slot, ATS, posted date, scored date, provider, and the location/eligibility
flags. Click a row to expand the LLM's reasoning, matched and missing
skills, and its ids. There's also free-text search across company, title,
location, skills and reasoning; band/ATS/company filters; and a CSV export
of whatever you currently have on screen (distinct from the pipeline's own
`output/` CSV, which is the full audit trail).

**One thing worth understanding about the Band column.** It is computed from
your `.env` thresholds, not from the `score_band` the LLM assigned. The
scoring prompt never tells the model what your thresholds are, so its own
label is just an opinion — if you change `SCORE_THRESHOLD_STRONG`, the
dashboard and the digest both move together, and the model's label doesn't.
Where the two disagree, the expanded row says so.

**Two tiles carry a caveat**, and the page will tell you when they apply:

- **Spend today** reads `$0.00` if your configured model has no entry in
  `PRICING` (`scoring/providers.py`). Cost estimation degrades to zero
  rather than crashing, which also means the budget ceiling can't trip for
  that model. Add a `PRICING` entry, or track spend at the provider.
- **Fetched today** comes from `run_log`, not from `jobs` — title- and
  location-rejected rows are deliberately never persisted, so the funnel
  counts have nowhere else to come from.

Updates arrive over Server-Sent Events: the server watches a cheap change
token and pushes only when the data or the daemon's state actually moved.
The green dot next to the title means that stream is connected; if it goes
amber the page falls back to polling every 15 seconds.

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
| Daemon exits printing "another coldstart daemon is already running" | Something else holds the lock on `data/coldstart.lock` | The message names the PID — stop it, or use the one-shot scripts. The lock is a kernel `flock`, so it's never stale after a crash |
| Daemon exits with "cannot bind the dashboard to …" | `DASHBOARD_PORT` is taken | Set a free `DASHBOARD_PORT`, or `DASHBOARD_ENABLED=false` |
| Daemon logs "suspending polls until …" and stops polling | The daily spend ceiling tripped | Nothing to do — polling resumes by itself at the next local midnight. Raise `DAILY_TOKEN_SPEND_CEILING_USD` to lift it sooner |
| Daemon keeps logging a config or resume error every cycle | A run aborted on one of the hard gates; the daemon stays up on purpose | Fix `.env` or `config/resumes/` — it's picked up on the next cycle, no restart needed |
| Dashboard shows stale numbers and the dot is amber | The event stream dropped | It reconnects on its own and polls every 15s meanwhile; check that the daemon is still running |
| Dashboard says "no daemon attached" | You're viewing a dashboard whose daemon isn't running | Expected if you started the web layer another way — the job data is still real, only the live status is missing |

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
| Company block list | 22 | `filters/company.py`, `config/excluded_companies.json` |
| Resume routing | 11 | `routing.py` |
| LLM provider interface + implementations | 12, 13 | `scoring/base.py`, `scoring/providers.py` |
| Scoring rubric + orchestration | 14, 15 | `scoring/rubric.py`, `scoring/scorer.py` |
| Budget circuit breaker | 16 | `budget.py` |
| CSV export | 17 | `export.py` |
| Email digest | 18 | `digest.py` |
| Pipeline orchestration + entrypoints | 19 | `pipeline.py`, `scripts/run_poll.py`, `scripts/run_digest.py` |
| Daemon / scheduler | 20 | `daemon.py`, `exit_codes.py`, `scripts/run_daemon.py` |
| Live dashboard | 21 | `web/app.py`, `web/queries.py`, `web/static/` |

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
