# Job Sourcing Pipeline — Scope Document

**Project:** Coldstart — Automated Job Sourcing & Matching
**Owner:** Manish
**Status:** Design locked, pending final config values before build
**Last updated:** 2026-08-19

---

## 0. Name

**Coldstart.** The name comes from the cold-start problem in recommender
systems: producing good matches for an entity with no interaction history.
That is precisely this system's job — it sees a job posting it has never
encountered, from a company it has no relationship with, and has to judge
fit from content alone, with no clicks, applications, or feedback signal to
learn from. Every job enters cold and is scored on its merits.

It also describes the situation the tool exists to solve. A job search
starts cold: no pipeline, no inbound, no warm intros. Coldstart is the
engine that gets it moving.

---

## 1. Objective

Automate discovery of relevant US-based software/AI engineering job postings,
score each one against the right resume, and deliver a daily curated digest
by email — without manually trawling job boards or re-checking companies by
hand. Ship this as an MVP fast; extend it later (e.g. automated resume
tailoring) only once the core pipeline proves itself over real usage.

**Non-goals for v1:** no UI, no resume auto-tailoring, no live per-company
scraping/watchlist, no support for non-US roles.

---

## 2. Origin & Key Decision: Use Upstream, Don't Build Scrapers

Two paths were considered: cloning/building ATS scrapers from scratch vs.
building on top of the open-source
[`kalil0321/ats-scrapers`](https://github.com/kalil0321/ats-scrapers) package
(MIT licensed, PyPI: `ats-scrapers`).

**Decision: use upstream.** Maintaining 50+ ATS adapters (auth quirks,
pagination, blocking/backoff, HTML entity decoding, etc.) is undifferentiated
work already solved upstream. Our value-add is the **filtering, matching, and
scoring layer**, not the scraping layer. Only fork/contribute a scraper if a
specific needed ATS has no adapter — treat that as a possible future
open-source contribution, not a v1 requirement.

**Known risks of the dependency:**
- Single maintainer, 131 stars, 41 open PRs vs. 1 open issue → real
  maintenance backlog, bus factor of 1.
- Hosted dataset (`data.stapply.ai`) has no published SLA. Snapshot observed
  at `generated_at: 2026-08-07` with an **empty `by_date` field** — there is
  no confirmed daily-delta mechanism. Freshness cadence is unknown and must
  be discovered empirically (see §4).

---

## 3. Data Source

**Hosted snapshot only** — no live per-company polling, no curated
watchlist. Manish has other channels for direct/targeted company tracking;
this pipeline exists purely for **broad, low-effort discovery** across the
full ATS long tail.

- Manifest: `https://storage.stapply.ai/jobhive/v1/manifest.json`
- Full snapshot: 4,854,656 jobs / 79,906 companies / 65 ATS sources /
  2.25 GB parquet (as observed) — **never downloaded in full.**

### 3.1 ATS Source Inclusion Rule

**Exclude 15 sources that are structurally non-English or non-US** — not a
judgment call on relevance, a hard fact about the audience the data serves.
These cannot contain a US SWE/AI role by construction, and together they are
~946 MB (42% of the total snapshot):

| Excluded source | Reason |
|---|---|
| `eures` | EU public job feed (758 MB alone) |
| `bundesagentur` | German-language, German government |
| `arbetsformedlingen` | Swedish |
| `wanted` | Korean |
| `welcometothejungle` | French |
| `gupy`, `programathor` | Brazilian Portuguese |
| `beisen`, `beisen_legacy`, `moka`, `bytedance` | Chinese |
| `hrmos` | Japanese |
| `getonbrd` | Spanish/LatAm |
| `jobbankca` | Canada (not US) |
| `jobsch` | Swiss |

**Include everything else**, including large mixed US/global enterprise ATS
platforms (Workday, SuccessFactors, SmartRecruiters, Oracle, iCIMS) —
these do carry real US postings, and the location + title filters (§5) do
the actual narrowing. No further ATS-based exclusion beyond this list.

Net effect: ~1.31 GB processed instead of 2.25 GB, with no loss of
realistically relevant postings.

### 3.2 Freshness / Polling Strategy

- Poll **`manifest.json` only** every 30 minutes — a few KB, negligible cost.
- Compare each **per-slice `sha256`** (not just the top-level
  `generated_at`) against the last-processed value stored locally.
- Only re-download a slice when *its own* hash has changed — avoids
  re-pulling all sources just because one unrelated slice updated.
- This design is self-adapting to whatever the real update cadence turns
  out to be (strict daily, delayed, irregular) without hardcoding an
  assumption that isn't backed by evidence.

---

## 4. Filtering Pipeline

All filtering happens **in-memory on vectorized pandas operations** before
any database interaction — no per-row DB hits, no per-row LLM calls at this
stage. Order matters: cheapest, most decisive filters run first.

### 4.1 Location Filter — "no compromise" tier

Runs as a cascade, cheapest/most reliable signal first. **Three-way outcome
on every job — never a silent drop:**

1. `country_iso == "US"` → accept immediately (best signal, use first).
   **Exception found on real data:** `country_iso == "CA"` is not trusted as
   an automatic reject. It's the real ISO code for Canada, but empirically
   (181k-row live sample) it's also a frequent data-quality bug where
   California ends up in `country_iso` instead of `US` — outnumbering real
   Canada roughly 2:1 in that sample. `CA` alone falls through to the
   location-text steps below instead of being trusted outright; every other
   non-US `country_iso` value is still a hard, immediate reject (see
   DEVELOPMENT_PLAN.md Module 7 for the full investigation).
2. Country-level remote markers (`\bUS\b`, `\bUSA\b`, `\bUnited States\b`,
   `\bNorth America\b` combined with `is_remote`) → accept.
3. Full 50-state lexicon (full name **and** abbreviation) matched with
   **word boundaries**, applied only after stripping obvious non-US country
   mentions first (so `"Ontario, CA"` — a real Canadian city — doesn't
   false-positive on the `CA` abbreviation).
4. City→state fallback table for the largest US metros (covers postings
   like `"Los Angeles"` with no state suffix at all).
5. Anything unresolved after 1–4 → **`location_uncertain`**, not rejected
   and not silently accepted. Surfaced in a dedicated digest section for
   manual review, and also still passed through to scoring rather than
   dropped.

A dedicated pytest fixture file will cover known tricky cases: `"Ontario,
CA"`, `"Remote — US"`, `"Remote (USA)"`, state abbreviations that collide
with English words (`OR`, `IN`, `HI`, `ME`, `PA`), and city-only locations.

### 4.2 Title Filter

**Allow-list** (the roles actually being targeted):
Software Engineer, SDE, SWE, Founding Software Engineer, AI Engineer,
Agentic Engineer, Forward Deployed Engineer (general-SWE flavor), Forward
Deployed Engineer (AI-specific flavor).

**Deny-list** — restricted to genuinely different job functions, not
seniority tiers of the same IC track: Manager, Director, Sales, Intern.

> **Design note:** Staff/Principal-level IC titles are *not* hard-denied at
> this stage. Seniority is handled as a **soft scoring penalty** (§6), not a
> filter-stage exclusion — consistent with the "reach roles are still worth
> seeing, just scored lower" approach agreed on for years-of-experience.

### 4.3 Eligibility Filter — Citizenship / Clearance / Export Control

Hard exclusion gate, runs before any LLM call, same three-way pattern:

**Definite-exclude patterns:** `"U.S. Citizen[ship] required"`,
`"security clearance"`, `"secret clearance"`, `"top secret"`, `"TS/SCI"`,
`"active clearance"`, `"ITAR"`, `"export control"`, `"EAR"`, `"green card
holders only"`.

**Explicitly NOT excluded on:** `"must be authorized to work in the US"` or
`"no visa sponsorship"` alone — these are common and not equivalent to a
citizenship/clearance requirement.

**Outcomes:**
- Clear match → excluded pre-LLM, logged with reason, never scored.
- No match → proceeds normally.
- Ambiguous phrasing → `eligibility_uncertain`, still scored, surfaced
  separately in digest.
- **Safety net:** the LLM scoring prompt (§6) also carries an explicit
  instruction to set `eligible: false` and score 0 if it detects a
  citizenship/clearance/export-control requirement the regex missed.

### 4.4 Deduplication

- Primary key: `global_id` (or `url` if `global_id` absent) — "have I
  already processed this exact row."
- Secondary signal: `requisition_id` — the *employer's* internal id, shared
  across ATS platforms when one real job is mirrored on two of them. Used
  to avoid scoring/emailing the same underlying role twice.
- Mechanism: load all existing `seen` keys from SQLite into a single
  in-memory Python `set()` once per run, then do a set-difference against
  the filtered candidate list. **Not** a per-row database query — at
  realistic volumes (tens of thousands of keys/year) this is instant and
  trivial in memory.

---

## 5. Resume Routing

Four resumes, mapped as a 2×2 matrix:

| | General flavor | AI flavor |
|---|---|---|
| **Standard role** | Resume A — SWE/SDE/SE/Founding SWE | Resume B — AI/Agentic Engineer |
| **FDE role** | Resume C — FDE, general-SWE flavor | Resume D — FDE, AI-specific |

### 5.1 Resume Ingestion (PDF/DOCX → normalized JSON)

The pipeline never asks Manish to hand-produce four plain-text resume
files. Instead, `config/resumes/` is where real resumes (PDF or DOCX) get
dropped in, and an ingestion pre-step — run automatically at the start of
every pipeline invocation — resolves them into the four slots:

- **Hard gate, not a soft check.** If the four resume slots (A/B/C/D)
  can't be fully resolved — folder is empty, a file fails to extract, or
  the mapping to slots is ambiguous or incomplete — **the application
  stops and asks the user to fix it**, before any polling, filtering, or
  LLM spend happens. This mirrors the existing config-validation
  fail-fast principle (§9): a run with an incomplete or wrong resume set
  is worse than no run at all, since every score it produces would be
  against the wrong (or missing) resume.
- **Extraction is deterministic, not LLM-based** — `pypdf` for PDF,
  `python-docx` for DOCX. No model call is needed to pull text out of a
  resume, and using one would add cost/latency/risk for zero benefit.
  **Known v1 limitation:** neither library performs OCR, so a
  scanned/image-only PDF with no text layer will fail extraction and
  block the run rather than silently producing an empty resume.
- **Slot classification (which of A/B/C/D a file is) is filename-first,
  LLM-fallback-only-if-ambiguous** — the same two-tier pattern already
  used for job-title routing below: a free keyword match on the filename
  (e.g. a stem containing `fde` and `ai` → slot D) resolves the clear
  majority of cases; only a genuinely ambiguous filename escalates to one
  LLM classification call. This keeps the common case free and fast while
  still handling an arbitrarily-named upload.
- **Normalized output is JSON**, matching every other config file in this
  project (`manifest.json`, `title_rules.json`, `eligibility_rules.json`)
  — Pydantic reads/writes it natively, no extra dependency, no YAML
  whitespace/type-coercion footguns. Each normalized resume is flat text
  plus a little metadata (source filename/hash, extracted-at, is_fde/is_ai,
  a one-line description, the full resume text) — not a fully structured
  schema of sections/dates/bullets. That's a deliberate MVP choice: the
  scoring prompt (§6) only ever needed a plain resume string, so there's
  no reason to build a richer schema until resume auto-tailoring (§13,
  future extension) actually needs one.
- **Originals are archived, never deleted** — moved into
  `config/resumes/originals/` after successful extraction, so re-running
  is safe and nothing the user uploaded is ever lost.
- **Privacy:** raw resumes and the normalized JSON both contain full
  personal history (PII) and are gitignored, same as `.env` — never
  committed.

### 5.2 Routing Logic (job → resume)

Once all four resumes are resolved, routing a given job posting to one of
them is plain classification, not agentic:

1. **Deterministic keyword regex first pass** (free, instant) — resolves
   the clear majority of titles (e.g. "Senior Software Engineer" → A,
   "AI Engineer" → B).
2. **LLM classification fallback**, only for titles the regex can't
   confidently resolve — a single structured call given the JD title (and
   optionally opening JD text) plus a short manifest of resume
   filename+description, returning which resume to use. One request, no
   tool loops, no planning — genuinely just classification.
3. Only the **selected** resume's full text is loaded into the scoring
   prompt — never all four at once.

---

## 6. LLM Scoring

### 6.1 Provider Architecture

Common interface across all providers: `score_batch(jobs, resume) ->
list[ScoreResult]`.

- **Build first:** DeepSeek.
- **Stub (same interface, wired later):** Kimi, Gemini, Mistral, OpenAI,
  Anthropic, Grok.
- DeepSeek, Kimi, Mistral, Grok, and OpenAI are OpenAI-SDK-compatible
  endpoints — effectively one implementation with a `base_url`/model-string
  swap. Anthropic and Gemini need their own request/response handling.
- Model/provider selection is entirely `.env`-driven:
  `LLM_PROVIDER`, `PROVIDER_FALLBACK_ORDER` (e.g.
  `deepseek,kimi`), `BATCH_SIZE` (default `1`), and a dev/prod switch
  (dev → local 3–4B model via Ollama; prod → hosted API, budget ~$100/mo).

### 6.2 Prompt Structure for Caching

Resume text and system instructions go at the **start** of the prompt; the
variable JD content goes at the **end**. This maximizes automatic caching
on DeepSeek, Kimi, and Gemini (all cache repeated prefixes with no code
changes required); Anthropic requires explicit `cache_control` breakpoints
to get the same benefit.

### 6.3 Reliability

- On 429/5xx: exponential backoff, limited retries, then failover to the
  next provider in `PROVIDER_FALLBACK_ORDER`.
- If every provider in the chain is exhausted: log to `errors`, mark job
  `status = 'failed'` for manual review. **Never silently dropped** — a
  well-fitted job must not be lost to an infrastructure issue.
- LLM output is validated against a strict Pydantic schema. On validation
  failure: one correction retry with an explicit "your last response didn't
  match the required schema" message. On second failure: log to `errors`,
  mark job `status = 'failed'`.
- Global max-retry ceiling; on exhaustion, stop and switch provider rather
  than looping indefinitely.
- Daily cumulative token-spend tracked; hard-stop if it crosses a
  `.env`-configured ceiling (circuit breaker against a runaway bug).

### 6.4 Scoring Rubric

Weighted composite score, 0–100 (**weights provisional, pending final
confirmation**):

| Dimension | Weight | What it checks |
|---|---|---|
| Tech stack / language / tools overlap | 35% | Match against the routed resume's stack |
| Years-of-experience fit | 25% | See formula below |
| Seniority/title fit | 25% | Soft penalty, not a hard filter (see §4.2 design note) |
| Role-type/domain fit | 15% | Founding-ownership / FDE customer-facing / AI-specific signals |

**Years-of-experience formula** — let **X** = Manish's years of experience
(computed at runtime from an `.env` start date, not hardcoded), **R** =
years required by the posting:

| Condition | `experience_fit` sub-score |
|---|---|
| `R ≤ X` | 100 |
| `X < R ≤ X+2` | linear 100 → 80 ("companies may flex" zone) |
| `X+2 < R ≤ X+4` | linear 80 → 40 (real stretch) |
| `R > X+4` | floor ~15 (long shot, not excluded) |
| `R` unstated (common — `experience` field is `None` on most postings) | LLM estimates from JD prose, same bands applied |

**Score bands:**
- `≥ 70` → **strong** (headline section of digest)
- `60–69` → **consider** ("worth considering" section, shown at end of
  digest/CSV)
- `< 60` → **reject** (still logged in `jobs`/CSV, never emailed
  prominently, never deleted)

### 6.5 Structured Output Schema

```python
class JobScore(BaseModel):
    eligible: bool
    disqualification_reason: str | None
    score: int                    # 0 if eligible is False
    score_band: Literal["strong", "consider", "reject"]
    tech_stack_match: int
    seniority_fit: int
    experience_fit: int
    matched_skills: list[str]
    missing_skills: list[str]
    reasoning: str                # short justification, shown in digest
```

---

## 7. Storage

**SQLite** — chosen over Supabase/Postgres for the MVP: single-user,
single-machine, low write volume, no need for a network dependency, and
avoids the Supabase free-tier auto-pause-on-inactivity failure mode that
would actively hurt an intermittent polling job. Browsable via a VS Code
SQLite extension.

**No JD text is ever stored** — it's read in-memory purely for
comparison against the resume, then discarded. Only the link, score, and
reasoning persist.

### 7.1 Schema

```sql
jobs (
  global_id       TEXT PRIMARY KEY,
  requisition_id  TEXT,
  company         TEXT,
  title           TEXT,
  location        TEXT,
  apply_url       TEXT,
  ats_type        TEXT,
  posted_at       TEXT,
  resume_used     TEXT,      -- A / B / C / D
  score           INTEGER,
  score_band      TEXT,      -- 'strong' / 'consider' / 'reject'
  eligible        INTEGER,
  matched_skills  TEXT,      -- JSON-encoded list[str]
  missing_skills  TEXT,      -- JSON-encoded list[str]
  reasoning       TEXT,
  status          TEXT,      -- 'scored' / 'failed' / 'pending' / 'excluded'
  location_flag   TEXT,      -- 'accepted' / 'rejected' / 'uncertain'
  eligibility_flag TEXT,     -- 'passed' / 'excluded' / 'uncertain'
  provider_used   TEXT,
  first_seen_at   TEXT,
  scored_at       TEXT
)

errors (
  id            INTEGER PRIMARY KEY,
  ts            TEXT,
  stage         TEXT,        -- poll / location_filter / title_filter /
                              -- eligibility_filter / resume_routing /
                              -- llm_score / email
  source_file   TEXT,
  function_name TEXT,
  provider      TEXT,        -- nullable, only for llm_score stage
  job_ref       TEXT,        -- global_id if applicable
  error_type    TEXT,
  error_message TEXT,
  retry_count   INTEGER,
  resolved      INTEGER DEFAULT 0
)

email_log (
  id         INTEGER PRIMARY KEY,
  sent_at    TEXT,
  job_count  INTEGER,
  status     TEXT,           -- 'sent' / 'failed'
  error      TEXT
)
```

**Retention:** kept indefinitely — dedup keys, scores, and reasoning are a
few bytes to a few KB per row, and there's no volume pressure that
justifies pruning. CSV exports serve as the durable audit trail.

---

## 8. Delivery

- **Channel:** Gmail via plain SMTP + an app password (not OAuth/API) —
  simplest path for a scheduled outbound digest; requires 2FA enabled on
  the Google account to generate the app password.
- **Cadence:** one digest per day, at a `.env`-configured time in PDT.
- **Content, in order:** Strong matches (≥70) → Worth considering (60–69)
  → Location-uncertain items → Eligibility-uncertain items.
- **CSV export:** full log of every scored job (all bands), separate from
  the digest, for audit/analysis.
- Email send failures are logged to `email_log`, never silent.

---

## 9. Configuration (`.env`)

Representative set of variables (finalize exact names during
implementation):

```
EXPERIENCE_START_DATE=          # anchors X in the YOE formula
LLM_PROVIDER=deepseek
PROVIDER_FALLBACK_ORDER=deepseek,kimi
BATCH_SIZE=1
LLM_MODE=prod                   # prod = API, dev = ollama local
OLLAMA_MODEL=                   # 3-4B class, dev only
DAILY_TOKEN_SPEND_CEILING=
MAX_RETRIES=
SMTP_APP_PASSWORD=
DIGEST_TIME_PDT=
SCORE_THRESHOLD_STRONG=70
SCORE_THRESHOLD_CONSIDER=60
```

Config is validated at startup — fail fast and loud on a missing required
variable or resume file path, rather than failing mid-run after partial
LLM spend.

---

## 10. Reliability & Ops Principles (apply throughout)

- **Never silently drop a job** — every job lands in exactly one of:
  scored, excluded (with reason), uncertain (location/eligibility), or
  failed (with error logged). This is the single hardest constraint in the
  whole system.
- **Never run without a complete resume set** — the four-slot resume
  ingestion check (§5.1) is a hard gate at the very start of every run.
  An incomplete, missing, or ambiguously-mapped resume set stops the
  application rather than proceeding degraded — every downstream score is
  only meaningful if it was scored against the right resume.
- **Incremental writes** — results written to SQLite as each job completes,
  not batched at the end, so a crash or laptop sleep doesn't lose or
  re-pay for already-scored jobs.
- **Secrets hygiene** — `.env` gitignored from the first commit.
- **Global error/fallback log** — the `errors` table above is the single
  place to check any failure across polling, filtering, routing, scoring,
  or email.

---

## 11. Testing

- Pytest fixture set covering location edge cases (§4.1).
- A small hand-scored eval set (~10 JDs) re-run whenever the model,
  provider, or rubric prompt changes, to catch quality regressions before
  they reach the real digest.
- Config validation exercised at startup.

---

## 12. Deployment

- **Dev (now):** Manish's laptop, cron-scheduled, scheduling itself is his
  responsibility — the pipeline should not assume or require the machine
  to stay awake.
- **Prod (later, deferred):** decision depends on whether the LLM stays
  API-based (→ Oracle OCI free tier, 1 GB RAM is sufficient) or moves to
  self-hosted OSS models (→ a paid box with adequate RAM/GPU). Not decided
  now; revisit once real cost and quality data exist.

---

## 13. Explicitly Out of Scope (v1)

- No UI — email + CSV only.
- No automated resume tailoring (flagged as a **future extension**, using
  an OSS/cheap model like Kimi or DeepSeek, once the sourcing pipeline is
  proven).
- No live per-company scraping or curated watchlist — snapshot-only.
- No processing of the full unfiltered 4.8M-row dataset.
- No cloning/forking the ATS scraper adapters themselves.

---

## 14. Open Items / Pending Confirmation

These remain unresolved and should be settled before or during
implementation:

1. **`EXPERIENCE_START_DATE`** — exact date to anchor X in the YOE formula.
2. **Whether X differs per resume** — e.g. total SWE experience vs. a
   shorter slice of hands-on AI/agentic experience for resumes B/D.
3. **Rubric weight confirmation** — 35/25/25/15 (stack/YOE/seniority/role-
   type) is a proposal, not yet explicitly re-confirmed after the YOE
   formula refinement.
4. **Long-shot band visibility** — whether jobs with `R > X+4` should ever
   be excluded from the digest, or always shown (current default: always
   shown, sorted low, consistent with the "never silently drop" principle).
