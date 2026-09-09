# Using Coldstart

Coldstart ships configured for one person. This guide is how you make it
yours: where your resumes go, which roles it looks for, which locations
count, which companies it must never show you, and what it costs.

If you just want to get it running, follow the Quick start in
[`README.md`](README.md) first — this document assumes it already runs and
picks up where "make it match my job search" begins.

**Two things to internalise before you edit anything:**

1. **Config edits only affect jobs that arrive *after* the edit.** Nothing
   in this repo re-evaluates rows already in the database on its own. The
   [Making a change take effect](#making-a-change-take-effect) section lists
   the retroactive scripts for the cases where that matters.
2. **The JSON files in `config/` are read fresh on every run.** No restart,
   no rebuild, no code change. Edit, save, and the next poll uses it — but a
   malformed JSON file crashes the run, so keep a backup of anything you
   heavily rewrite.

---

## The map

Everything you'd want to change lives in exactly two places: `.env` for
values and secrets, `config/` for lexicons.

```
.env                             your secrets and numeric settings
config/
├── resumes/                     ← your resumes go here (gitignored, PII)
│   ├── manifest.json            the 4 slots and what each one means
│   └── originals/               where ingested source files are archived
├── title_rules.json             which job titles are in scope
├── stack_rules.json             which tech stacks are yours, which aren't
├── eligibility_rules.json       citizenship / clearance phrases that exclude
├── us_states.json               52 state names + abbreviations
├── us_cities.json               219 US metros and aliases
├── foreign_countries.json       125 country names that veto a location
├── foreign_cities.json          427 foreign city names (weaker veto)
├── foreign_iso2.json            every ISO2 code, used to find state collisions
├── excluded_ats.json            55 ATS feeds never downloaded at all
├── excluded_companies.json      20 employers never scored, never sent to an LLM
└── email/                       the digest template (theme.json + 2 Jinja files)
```

---

## Your resumes

### Where they go

Drop **4 files** (PDF or DOCX) directly into `config/resumes/`. That's it —
no config edit, no registration step.

```bash
cp ~/Documents/resumes/*.pdf config/resumes/
```

On the next `run_poll` they are text-extracted, classified into a slot,
normalized into `resume_<slot>.json`, and the source files moved into
`config/resumes/originals/`. To do just that step without running a full
poll:

```bash
uv run python scripts/ingest_resumes.py
```

### The four slots

Coldstart routes each job to exactly one resume, so it needs all four:

| Slot | Resume | Normalized file |
|---|---|---|
| A | General Software Engineer / SDE | `resume_a_swe.json` |
| B | AI / Agentic Engineer | `resume_b_ai.json` |
| C | Forward Deployed Engineer, general | `resume_c_fde_swe.json` |
| D | Forward Deployed Engineer, AI | `resume_d_fde_ai.json` |

**The slot is decided by the filename**, case-insensitively, against the
filename stem:

| Stem contains | Resolves to |
|---|---|
| `swe`, `general`, `standard`, or `software` (and no FDE/AI marker) | A |
| `ai`, `agentic`, or `ml` (and no `fde`) | B |
| `fde` (and no AI marker) | C |
| `fde` **and** one of `ai` / `agentic` / `ml` | D |
| none of the above | *ambiguous* — costs one LLM call to read the content |

So `ManishBotta-SWE.pdf` → A, `resume-ai.pdf` → B, `FDE.pdf` → C,
`fde-agentic.pdf` → D. A bare `resume.pdf` has no marker at all and is
**not** silently assumed to be the general one — it escalates to a one-time
LLM classification and logs a warning. Renaming the file is free; the LLM
call isn't.

**`run_poll` hard-stops before any network or LLM spend if all four slots
can't be filled.** That's exit code `3` and a message naming the missing
slot. Add or rename the file and run again — no restart needed if the daemon
is up, it retries on the next cycle.

### Don't have four distinct resumes?

Copy the same file four times with the four naming patterns. Routing still
works, scoring still works — you just get the same resume scored against
every job, which is exactly what a single-resume setup should do.

### They are never committed

`config/resumes/*` is gitignored except its own README — raw uploads,
normalized JSON, and archived originals all stay local. Resumes are PII, and
this repo is public. Verify before you push:

```bash
git status --porcelain config/resumes/
```

Anything but the README showing up there is a bug in your `.gitignore`, not
something to `git add -f` past.

---

## Your experience level

`EXPERIENCE_YEARS` in `.env` is **the one required value with no safe
default**. A direct number of years — `3.5`, not a start date, since a
career break makes date-derived experience silently overcount.

It is shared across all four resumes, and it does two jobs:

- **Scoring.** It's `X` in the years-of-experience formula. The scorer floors
  the experience dimension to 15/100 when a posting's required years exceed
  yours by more than 4.
- **Filtering.** `stack.py` reads the same number to drop postings whose
  stated floor is more than 4 years above you — at that gap the composite
  score caps at 78.75 even with a perfect 100 everywhere else, which is below
  the default bar of 80. Cutting them early changes no outcomes, only cost.

Raise it and both move together. There is no separate AI-experience value.

---

## Which roles it looks for

`config/title_rules.json`. Two lists, and **deny beats allow**:

```json
{
  "allow": ["software engineer", "SWE", "ai engineer", "..."],
  "deny":  ["manager", "director", "intern", "staff", "lead", "..."]
}
```

**How matching works.** Every entry is matched as a whole word, case-
insensitively, anywhere in the title. `"sales"` matches "Sales Engineer" but
not "Salesforce Developer". You are writing plain phrases here, not regex —
they're escaped for you.

The order is: MTS rescue → deny → allow → drop.

**Three behaviours worth knowing before you edit:**

- **Seniority is deliberately mostly absent.** `senior`, `principal` and
  `staff-level` titles are a *scoring penalty*, not a filter — the LLM sees
  the gap and marks it down. `staff` and `lead` are the two exceptions and
  sit in the deny list; delete them from `deny` if you want those roles back.
- **"Member of Technical Staff" is rescued before the deny list runs.** The
  word "staff" there names the role (a senior IC title at AI labs), it
  doesn't modify it. `MTS` is rescued too.
- **`SE` needs a level.** "SE II", "SE 2", "SE III" are allowed; a bare "SE"
  is too collision-prone to allow-list, unlike SWE/SDE/FDE.

### Recipes

**Add a role type you want.** Append to `allow`:

```json
"allow": ["...", "full stack engineer", "solutions engineer", "research engineer"]
```

**Stop seeing a role type.** Append to `deny` — it wins over `allow`, so you
don't need to remove anything:

```json
"deny": ["...", "embedded", "firmware", "security engineer"]
```

**Sanity-check an edit.** The title filter logs a warning if it drops more
than 99% of a batch, which is the signature of a broken rule. Watch for it in
`logs/coldstart.log` after any large edit.

---

## Which tech stacks are yours

`config/stack_rules.json` is a pure cost filter — it changes what reaches the
LLM, never how anything is scored.

```json
{
  "candidate_stack": ["\\bpython\\b", "\\btypescript\\b", "..."],
  "mismatch": {
    "java": ["\\bjava\\b(?!script)", "spring boot"],
    "dotnet": ["(?-i:\\.NET)\\b", "\\bc#"],
    "other_language": ["\\bphp\\b", "\\brust\\b", "..."],
    "devops_only": ["\\bterraform\\b", "site reliability", "..."]
  }
}
```

**Unlike `title_rules.json`, these are real regex** — write `\\bgo\\b`, not
`go`, or you'll match "going" and "category".

**The rule:** a posting is dropped only when its description names a
mismatch stack **and never once mentions anything in `candidate_stack`**.
That's deliberately conservative — any mention of your own stack, even
alongside a mismatch keyword, sends it to the LLM rather than guessing which
one dominates. Measured on real data it cuts ~15% of survivors.

**To make this yours:** replace `candidate_stack` with your languages and
frameworks, and put everything you'd never want in the `mismatch` groups.
The group names (`java`, `dotnet`, …) are arbitrary labels that show up in
the exclusion reason — add or rename groups freely.

The `mismatch` keywords in this repo were validated by reading the actual
matched text, not just counting hits, which is how three false-positive
classes got caught: lowercase `.net` matching URLs in press-release
boilerplate, bare `salesforce` firing on investor names ("backed by
Salesforce Ventures"), and `on-call rotation` catching ordinary backend duty.
If you add an aggressive keyword, sample what it actually matched before
trusting it.

---

## Where you'll work

Location is the most intricate filter in the repo, and the most
consequential to get wrong — a bad rule silently deletes real jobs. It is
**three-way** on purpose:

| Flag | What happens |
|---|---|
| `accepted` | US location resolved — scored normally |
| `rejected` | Foreign location resolved — dropped, never scored |
| `uncertain` | Couldn't resolve — **still scored**, and shown in its own digest section |

Anything unresolvable ends up scored, never silently dropped. A bare
`"Remote"` with no country is `uncertain`, not accepted.

### The four lexicons

| File | Role |
|---|---|
| `us_states.json` | `{"CA": "California", ...}` — abbreviations and full names |
| `us_cities.json` | `{"Austin": "TX", ...}` — metros and aliases (`NYC`, `SF`, `Bay Area`) for city-only postings |
| `foreign_countries.json` | A flat list. **A country marker is decisive** — it rejects |
| `foreign_iso2.json` | Every ISO2 code. Never hand-edit — it's used to *derive* which US abbreviations collide with a country code (`GA`, `IN`, `DE`, `CA`) |

`foreign_cities.json` is a **weaker** signal on purpose: dozens of US towns
are named after foreign cities. Paris TX, Vienna VA, Athens GA, Dublin OH,
Moscow ID all stay, because a US state signal in the same string makes the
city marker stand down.

### The edit you'll actually make

**"It rejected a real job in my town."** Add the city to `us_cities.json`
with its state:

```json
"Redondo Beach": "CA",
```

Then rescue the ones already held back — a lexicon fix is not retroactive on
its own, because held-back rows are persisted and marked seen:

```bash
uv run python scripts/recheck_held_back.py           # dry run, shows what flips
uv run python scripts/recheck_held_back.py --apply   # requeue them
```

It only ever *rescues* — it flips held-back rows that now classify as
`accepted` back to `pending` so the next poll scores them, and never newly
rejects anything. Rows held back on a signal derived from the posting's raw
JSON stay put, since only `location` and `location_flag` are persisted.

**"I keep getting jobs from a country I can't work in."** Add it to
`foreign_countries.json` — but check `us_states.json` first for a collision.
Adding a two-letter code that doubles as a state abbreviation is how you
delete thousands of real jobs.

### If you're not in the US

Be honest with yourself about the size of this change: the location filter
is a US-acceptance engine, not a general geo-filter. The lexicons are
inverted from what you'd need — you'd be swapping `us_cities`/`us_states`
for your own country's, and moving the US into `foreign_countries.json`.
Doable, but it is a rewrite of the lexicons, not a setting.

`scripts/location_baseline.py` exists for exactly this: run it before your
change and after, and read the `accepted -> rejected` transitions. That set
must be empty or explainable. It is the only measurement that tells you
whether an aggressive rule ate real jobs.

---

## Companies you never want to see

Two independent gates, because either one alone leaks.

### Gate 1 — whole ATS feeds

`config/excluded_ats.json` names sources that are **never downloaded**.
Cheapest possible exclusion — the data never enters the pipeline:

```json
{ "name": "google", "reason": "Single employer, whole org unfiltered at source" }
```

It is a pure blocklist: every source in the upstream manifest that isn't
named here — and has at least one row — gets downloaded. The current list
covers non-English/non-US feeds (`eures`, `bundesagentur`, `gupy`),
job-board aggregators (`weworkremotely`, `builtin`), and single-employer
feeds whose whole org arrives unfiltered (`amazon`, `google`, `meta`).

### Gate 2 — individual employers

`config/excluded_companies.json` catches the same employer posting through
*someone else's* platform, which Gate 1 structurally cannot see. A blocked
company is never scored and **its job descriptions are never sent to an
LLM**. To add one:

```json
{
  "name": "acme corp",
  "reason": "Declined 12 of their postings by hand — stop showing them."
}
```

**Use the plain company name, lowercase.** Both spellings are matched
automatically, so one `"uber freight"` entry catches `Uber Freight` and
`uberfreight`. Legal suffixes are stripped (`Meta Platforms, Inc.` → `meta`),
and hostname-shaped values match on their first DNS label
(`amazon.jobs.personio.com` → `amazon`).

**Matching is exact, never substring** — and that's load-bearing. A scan of
2.9M rows found 191 look-alikes a substring match would have destroyed:
`apple-roofing` (58 real postings), `Metabase`, `Applebank`, `uberall`,
`Meta House`, and dozens of German `metallbau-*` metalworking firms. So
`"apple"` blocks Apple and leaves Apple Roofing alone — but it also means a
typo in your entry silently does nothing. Check the logs: a blocked row logs
at WARNING naming the company.

### Make it retroactive

Adding a name only stops *future* postings. Everything already scored stays
on your dashboard:

```bash
uv run python scripts/purge_excluded_companies.py           # dry run
uv run python scripts/purge_excluded_companies.py --apply   # delete them
```

Jobs you marked applied are kept by default, so a purge can't erase your own
record of applying — `--include-marked` overrides that.

---

## Eligibility rules

`config/eligibility_rules.json` — **regex**, three lists:

| List | Effect |
|---|---|
| `exclude` | Matched → dropped before scoring |
| `exclude_case_sensitive` | Same, matched case-sensitively (`\bEAR\b`, which would otherwise catch "years", "early") |
| `uncertain` | Matched → still scored, flagged, shown in its own digest section |

The shipped list errs toward letting things through. `"must be authorized to
work in the US"` and `"no visa sponsorship"` are deliberately **not**
exclusions — they are not citizenship bars, and treating them as such throws
away jobs. Only hard bars are here: citizenship requirements, security
clearances, ITAR/export control.

If your situation is different (you *are* a citizen, or you *do* hold a
clearance), move those patterns from `exclude` into `uncertain` — or delete
them — rather than leaving money on the table.

---

## Cost controls

Four knobs, in the order they matter:

| Setting | Default | Effect |
|---|---|---|
| `MAX_POSTING_AGE_DAYS` | `15` | **The biggest lever by far.** Measured across five real slices, it cuts jobs reaching the LLM from 11,900 to 626 — ~95% |
| `DAILY_TOKEN_SPEND_CEILING_USD` | `3.0` | Hard circuit breaker, checked before every LLM call. Trips → the whole run halts, exit `2`. The daemon then suspends polling until local midnight and resumes on its own |
| `LLM_MODE` | `dev` | `dev` = local Ollama, free. `prod` = one hosted provider, real money |
| `LLM_PROVIDER` + its model override | `deepseek` | Exactly one provider per run — **no cross-provider fallback**. On a rate limit, that same provider is retried `MAX_RETRIES_PER_PROVIDER` times, then the job is marked `failed` |

**Postings with no date are always kept.** Upstream omits it often enough
that treating unknown as old would discard real jobs.

**Run your first cycle in `dev` mode.** On a fresh database every slice
counts as changed, so the first poll is a full backfill: ~2.9M upstream rows,
1–3% of which survive filtering and reach the LLM. That is tens of thousands
of scored jobs and real spend if you point it at a paid provider on day one.

`SCORE_THRESHOLD_STRONG` (default `80`) is a binary bar — a job either clears
it or is rejected. It's read live when the digest is built, so lowering it
surfaces more jobs on the **next digest with no rescoring**. Rejects are
still in the CSV and behind a checkbox on the dashboard, so nothing is lost
by keeping it high.

---

## Making a change take effect

| You changed | Takes effect | Retroactive? |
|---|---|---|
| A resume file | Next poll (auto-ingested) | n/a |
| `title_rules.json` | Next poll | No — nothing re-runs on stored rows |
| `stack_rules.json` | Next poll | No |
| `eligibility_rules.json` | Next poll | No |
| Location lexicons | Next poll | `scripts/recheck_held_back.py --apply` |
| `excluded_companies.json` | Next poll | `scripts/purge_excluded_companies.py --apply` |
| `excluded_ats.json` | Next poll | No — stored rows from that source stay |
| `MAX_POSTING_AGE_DAYS` | Next poll | `scripts/purge_stale_jobs.py --apply` |
| `SCORE_THRESHOLD_STRONG` | Next digest / dashboard load | Yes, inherently — bands are computed, not stored |
| `config/email/*` | Next digest, and live in `/preview/email` | n/a |
| Anything else in `.env` | Next run of whatever reads it; daemon settings need a daemon restart | n/a |

Every maintenance script defaults to a **dry run** and prints what it would
do. `--apply` is what writes.

A job already in the database is never re-scored, so re-running `run_poll`
against unchanged data costs nothing. That's also why config edits aren't
retroactive: the rows are already terminal.

---

## Day-to-day

```bash
uv run python scripts/run_daemon.py    # everything: poll + digest + dashboard
```

Then <http://127.0.0.1:8787>. Mark jobs applied from the dashboard — that
flag lives in its own `job_state` table precisely so a re-score can't erase
it, and the purge scripts respect it.

One-shot alternatives, if you'd rather drive it yourself:

```bash
uv run python scripts/run_poll.py            # fetch, filter, score, export CSV
uv run python scripts/run_digest.py          # build + send today's digest
uv run python scripts/run_liveness_sweep.py  # re-check whether scored jobs are still live
```

### Where to look when something's off

```bash
sqlite3 data/coldstart.sqlite3 \
  "SELECT ts, stage, error_type, error_message FROM errors WHERE resolved=0 ORDER BY ts DESC LIMIT 20;"
```

Every failure across polling, filtering, routing, scoring and email lands in
that one table. The dashboard's **Unresolved errors** tile turns amber when
it's non-zero. `README.md` has a fuller troubleshooting table and more query
recipes.

---

## Before you fork this publicly

- **`.env` is gitignored from the first commit.** It holds your API keys,
  your Gmail App Password, and `EXPERIENCE_YEARS`. Never commit it.
- **`config/resumes/` is gitignored except its README.** Resumes are PII.
- **The dashboard has no authentication** and shows your entire match list.
  `DASHBOARD_HOST` is `127.0.0.1` deliberately. Only widen it if something
  else is handling access control.
- **`excluded_companies.json` is committed, and its `reason` fields are
  public.** The ones in this repo say things like "29 of its 50 postings
  declined by hand". Write yours knowing anyone can read them.
- Email auth is a Gmail **App Password** (needs 2FA on the account), not
  OAuth and not your account password.
