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

**Non-goals for v1:** no resume auto-tailoring, no live per-company
scraping/watchlist, no support for non-US roles.

**Reversed after v1 shipped: "no UI" is no longer a non-goal.** The original
reasoning was that email plus CSV covers delivery and a UI is undifferentiated
work. That held for *delivery* and still does — the digest is unchanged. What
it missed is *inspection*: §7 assumed a SQLite viewer extension would cover
"what's in there right now," and in practice answering "which strong matches
came in this week, sorted by score, and why did that one score 72" meant
hand-writing SQL against JSON-encoded columns every time. A single read-only
page (§8.1) closes that gap without touching the pipeline. Resume tailoring,
watchlists, and non-US roles all remain out of scope.

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

**Exclude 29 sources** on two grounds. The first 15 are structurally
non-English or non-US — not a judgment call on relevance, a hard fact about
the audience the data serves. These cannot contain a US SWE/AI role by
construction, and together they are ~946 MB (42% of the total snapshot):

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

The other 14 were added after the first real run, on a different ground:
**single-employer feeds and job-board aggregators**. `amazon` alone was
33,888 rows *per poll* — the entire Amazon org, warehouse ops included — and
the title filter narrows it downstream but the whole slice is still
downloaded and scanned every time its hash moves. Which sources are genuinely
single-employer was checked against the manifest's own `by_ats_companies`
breakdown rather than guessed from the name: `oracle`, for instance, turned
out to be a 1,279-company platform and correctly stayed in. Excluded:
`amazon`, `tesla`, `apple`, `tiktok`, `google`, `uber`, `meta` (single
employers) and `weworkremotely`, `builtin`, `remoteok`, `thehub`, `manfred`
(aggregators).

**Revised (2026-08-25): `ycombinator` and `wellfound` are back in.** They
are aggregators, and they were excluded with the rest on that basis, but the
actual reason the aggregators went was *volume with no matching signal* —
and neither of these has any volume: `ycombinator` is 3,419 rows and
`wellfound` 348, against 33,888 for one excluded employer. What they carry
is early-stage startup postings that reach this pipeline no other way, since
a seed-stage company on Work at a Startup usually has no Greenhouse or Ashby
board to scrape. Measured through the real filter chain, `ycombinator` puts
281 rows in front of the LLM and `wellfound` 0 — the cost of being wrong here
is a rounding error, and the two carry very different data quality (§3.1.1).
Including YC also surfaced a false exclusion in §4.3, fixed there.

**Revised (2026-10-20): `workday` is out, at the operator's call** ("Completely
ignore workday"). It was the largest source by far — 854k rows, ~10 GB of
memory to load, hours per poll (the in-flight poll that day had queued 979
Workday jobs for the LLM) — and the memory problem below was always its
problem. Excluded in `config/excluded_ats.json`; rows already stored are
removed by `scripts/purge_excluded_ats.py`. The Workday-specific code (the
live CXS check, per-employer keys, `raw.externalPath` location slugs) stays,
tested, for the day it is re-enabled. In scope now: ashby, gem, greenhouse,
icims, lever, oracle, rippling, ycombinator, wellfound.

#### 3.1.1 What these two sources actually contain

Both carry the same 26-column schema as every other slice, so nothing in the
fetcher or normalizer needed changing. What differs is what is *in* the
columns, and it differs enough between the two to be worth writing down.

**`ycombinator` (3,419 rows, 0.2 MB).** Company names are real employers
(DoorDash, Instacart, Checkr), not "via YC", so the company filter and the
dashboard read correctly. `description` is **not the job description** — it
is the company's one-line blurb ("Restaurant delivery."), identical across
every posting from that company; 942 of 952 companies have exactly one
distinct description. The real per-job signal is in `raw`, a small JSON blob
carrying `role`, `skills`, `companyBatchName`, and `visa`. Consequence: the
LLM scores these on title + company + a tagline, so its years-of-experience
estimate and its eligibility safety net are both running blind. Scores from
this source are weaker evidence than scores from a real ATS slice.

**`wellfound` (348 rows, 0.03 MB).** Substantially empty: `apply_url` null on
all 348 rows, `description` present on 1, `raw` null on all of them, 49 rows
have `company` literally `"Unknown"`, only 17 rows are `country_iso == US`,
and `posted_at` reaches back to 2022. Through the real filter chain it yields
**zero** scored jobs — 4 rows survive freshness and all 4 stop at the
location filter as UNCERTAIN. It is included because it costs nothing and
upstream may improve it; it is not currently a source of jobs.

**Neither source has `requisition_id`** — null on every row of both. Since
`global_id` is `ats_type:ats_id`, a YC posting for a company that also runs
its own Greenhouse board will **not** dedupe against that board's copy;
both rows land. Cross-source dedupe by (company, title) does not exist and
is not proposed here — at 79 rows/poll the duplicate volume is small enough
to eyeball on the dashboard.

**Neither is liveness-checked.** `verify.CHECKED_ATS_TYPES` covers workday,
greenhouse and lever only, so both return `UNKNOWN/unsupported_ats` — the
documented graceful path, not a failure. YC's `apply_url` points at
`account.ycombinator.com/authenticate?continue=...workatastartup.com/...`,
which needs a YC login to open, so a check would be hard to add anyway; the
public listing on `ycombinator.com/companies/...` is in `url`.

**Include everything else**, including large mixed US/global enterprise ATS
platforms (Workday, SuccessFactors, SmartRecruiters, Oracle, iCIMS) —
these do carry real US postings, and the location + title filters (§5) do
the actual narrowing. No further ATS-based exclusion beyond this list.

Net effect, re-verified against the live manifest: **36 relevant slices,
2,881,672 rows, ~1.36 GB of parquet** — down from 65 sources and 4,854,656
rows, with no loss of realistically relevant postings.

### 3.2 Freshness / Polling Strategy

- Poll **`manifest.json` only** every 30 minutes — a few KB, negligible cost.
- Compare each **per-slice `sha256`** (not just the top-level
  `generated_at`) against the last-processed value stored locally.
- Only re-download a slice when *its own* hash has changed — avoids
  re-pulling all sources just because one unrelated slice updated.
- This design is self-adapting to whatever the real update cadence turns
  out to be (strict daily, delayed, irregular) without hardcoding an
  assumption that isn't backed by evidence.

**Who executes this (added with the daemon, §12).** The 30-minute cadence
was specified here from the start but had nothing running it — `run_poll`
was a one-shot script and `POLL_INTERVAL_MINUTES` was read by no code at
all. The daemon now owns it, with one refinement in front of the per-slice
hash comparison: the manifest fetch is a **conditional GET** carrying the
previous `ETag`, so the common case ("nothing changed") is a `304` with no
body and no subprocess, rather than re-downloading 41 KB of JSON 48 times a
day. That is a cheap pre-filter only; the per-slice `sha256` comparison
above remains the authority on what actually gets reprocessed, and a
`FORCE_POLL_HOURS` interval polls unconditionally regardless, since an ETag
can go stale on a CDN and a run that died mid-slice leaves work that no
manifest change will ever re-trigger.

**Observed cadence, still unknown and still irregular.** The manifest was
last regenerated 2026-08-07 and had not moved 12 days later. The design
handles that correctly — it simply does nothing — but it means most polls
and many digests will legitimately have nothing to report, which is why
"last upstream change" is a first-class metric on the dashboard (§8.1)
rather than something you infer from silence.

---

## 4. Filtering Pipeline

All filtering happens **in-memory on vectorized pandas operations** before
any database interaction — no per-row DB hits, no per-row LLM calls at this
stage. Order matters: cheapest, most decisive filters run first.

**Measured funnel (real cached slices, 2026-08-19).** The filters carry
nearly all the load; the LLM only ever sees single-digit percentages:

| Slice | Fetched | After title | After location | Reaches LLM |
|---|---|---|---|---|
| `greenhouse` | 181,350 | 11,503 (6.3%) | 6,798 (3.8%) | 5,442 (3.00%) |
| `lever` | 70,864 | 2,966 (4.2%) | 1,662 (2.3%) | 1,302 (1.84%) |
| first live run (darwinbox, pageup, remoteok, softgarden) | 33,186 | — | — | 168 (0.51%) |

The spread is the location filter doing its job: the 0.51% slices are
non-US ATSes (darwinbox is Indian, softgarden German, pageup APAC) where
almost nothing survives, while US-centric platforms run 4–6× higher. Across
2.88M relevant rows a full cold-start backfill is therefore on the order of
30k–85k scored jobs, not millions — but also not the couple of hundred the
first run suggested.

**Real-data finding — the load, not the filters, is the memory ceiling.**
DEVELOPMENT_PLAN Module 6 called column projection "the single biggest
memory lever," but `description` and `raw` are both required (the former for
scoring, the latter for §4.3's eligibility scan) and together are **96% of
the uncompressed bytes**. So the full JD text of every row is materialized
*before* the title filter discards ~94% of them: measured 2.69 GB peak RSS
for `greenhouse`, ~10 GB projected for `workday` (839k rows, 4.13 GB
uncompressed). Correct, but it means "cheapest filters first" doesn't hold
for the read itself, and it is why §12 runs the pipeline as a child process
that exits. Fixing it properly means reading the cheap columns, filtering,
then re-reading `description`/`raw` only for survivors — not yet done. **Largely moot since 2026-10-20:** `workday`, the ~10 GB
case, is excluded (§3.1); the largest remaining slice is `greenhouse` at
2.69 GB peak.

### 4.1 Location Filter — "no compromise" tier

Runs as a cascade. **Three-way outcome on every job.** Since the 2026-08-23
overhaul the cascade runs every *foreign* signal before any acceptance path,
because the whole bug class it fixes is a foreign ISO code being read as a US
state abbreviation.

**Real-data finding (2026-08-23) — 44.4% of everything scored was not US.**
Re-classifying the 3,037 jobs the pipeline had actually scored and paid for:

| | Before | After |
|---|---|---|
| Still scored | 3,037 | 1,689 (55.6%) |
| Rejected outright | — | 1,077 (35.5%) |
| Held back for review | — | 271 (8.9%) |
| **LLM calls avoided** | — | **1,348 (44.4%)** |

Two causes, roughly equal:

1. **`UNCERTAIN` was treated as "send to the LLM."** 1,266 of the 3,037
   scored rows (42%) were location-uncertain, and the pipeline only dropped
   `REJECTED`. That bucket was overwhelmingly foreign. It is now default-deny
   (§4.1.1).
2. **Foreign ISO codes read as US state abbreviations.** `München, BY, DE,
   80809` was *accepted* because `, DE` matched Delaware; likewise `Herzliya,
   HA, IL` (Illinois), `Indore, MP, IN` (Indiana), and `Den Haag, NL, 2597
   AK`, where the Dutch postcode's trailing letters matched Alaska.

The single largest source was **Workday**: 558 uncertain scored jobs whose
`location` was empty or the literal string `"2 Locations"` — but 100% of them
carried the real city in `raw.externalPath`, a field already downloaded and
never read. Top segments: Bengaluru 136, Pune 75, Hyderabad 65, Chennai 38,
Mumbai 30, Gurugram 29. Reading that one field accounts for 507 of the 1,077
rejections.

**The filter was also wrong in the other direction.** Verified live before
the change: `Vienna, VA`, `Paris, TX`, `Athens, GA`, `Dublin, OH`, `Rome,
NY`, `Berlin, NH`, `Moscow, ID` and `Cairo, GA` were *all* rejected as
foreign, because `foreign_markers.json` was a single flat list. It is now
split into `foreign_countries.json` (decisive, never rescued) and
`foreign_cities.json` (stands down when the string carries an unambiguous US
state signal). Across a 916k-row sample this rescued **503 distinct US
locations** that the old filter was discarding.

Whole-corpus effect on the same 916k rows: accepted 563,515 → 555,962,
rejected 214,992 → 294,635, **uncertain 137,881 → 65,791 (−52%)**. Every one
of the 34 US-looking accepted→rejected transitions was hand-audited and found
genuinely foreign (Tbilisi/Georgia, Medellín/CO, Casablanca/MA, Surabaya/ID).

**Three known, accepted trade-offs**, all measured rather than assumed:

- Bare `Warsaw, IN` and `Delhi, CA` stay rejected — `IN`/`CA`/`DE`/`IL` are
  the four codes where the country appears at real volume, so they cannot
  rescue a foreign city name. Written as `Warsaw, IN, us` (928 rows corpus-wide
  use that shape) they are accepted, because a bare trailing `us` is
  unambiguously the country slot.
- `Perth, WA, Australia` is rejected but `Perth, WA` (9 rows) is accepted —
  `WA` is Western Australia *and* Washington, and no signal separates them.
- Canadian provinces stay in the country list, so `Ontario, CA` is rejected
  while `Ontario, California` is accepted. Measured 10,121 `X, <province>`
  rows against 537 Ontario-California rows, a 19:1 split.

#### 4.1.1 The cascade

1. `country_iso` foreign → immediate reject. **Exception found on real
   data:** `country_iso == "CA"` is not trusted. It's the real ISO code for
   Canada, but empirically (181k-row live sample) it's also a frequent
   data-quality bug where California ends up in `country_iso` instead of
   `US` — outnumbering real Canada roughly 2:1. `CA` falls through to the
   text cascade; every other non-US value is a hard reject.
2. **Foreign signals, all before any acceptance path:**
   - `raw.externalPath` (Workday) names a foreign city or country;
   - a foreign **country** marker, or a foreign **city** marker with no US
     state signal to rescue it. A bare abbreviation that is also a country
     code (`GA`, `ID`, `CO`) rescues only the towns in
     `us_namesake_cities.json` — `Rome, GA` yes, `Jakarta, ID` no (§4.1.3);
   - the token before a postal code is a foreign ISO2 (`München, BY, DE,
     80809`). Gated on the ISO2 lexicon, because US data uses the same shape
     with a *state* there — `Fate, TX, 75189` must survive. `CA` + a bare
     5-digit ZIP is California; Canadian postcodes are always alphanumeric;
   - a trailing foreign ISO2 in a 3+-part string (`Herzliya, HA, IL`). The
     3-part minimum protects `San Carlos, CA` and `Somerville, MA`, US
     cities absent from the city lexicon;
   - an `XX-City` ISO prefix (`RS-Belgrade`) — but a US state code in that
     position is the state (`NC-Winston-Salem`), except the six codes that
     really are split in the corpus (CA, CO, DE, ID, IN, MA) plus WA;
   - a non-state-twin country code in the country slot, two parts enough
     (`San Jose, CR`, `Richardson, GD, CN`, `Birmingham, GBR`). A US city
     name does not veto it; only explicit US evidence (a US marker, a
     state+ZIP, a no-twin state code elsewhere in a multi-location string);
   - a state-twin code with an uppercase foreign region code in front of it
     (`Tanger, TNG, MA` is Morocco, `DUITAMA, COL, CO` is Colombia);
   - a Canadian province, Australian state or Indian state code as its own
     part (`Richmond, BC`, `Durham Region, ON`), unless another listed
     location in the same posting is US;
   - a non-ASCII **letter** with no US signal. Category-based, not
     `ord(c) > 127` — otherwise the en-dash in `Remote – California Bay
     Area` rejects a US job.
3. `country_iso == "US"` → accept. Runs *below* the foreign signals because
   upstream stamps `US` on `Wilhelmstraße 118` (Berlin) and `Fabryczna 20A`
   (Wrocław).
4. Country-level US markers, then the 50-state lexicon (full name **and**
   abbreviation, word-bounded) plus a state+ZIP or a `ST-City` prefix, then
   the city→state table. A city in `ambiguous_us_cities.json` counts only
   beside its own state; alone, a `foreign_leaning` one (Manchester,
   Cambridge, Birmingham, Richmond, Essex) is held back as `ambiguous_city`.
5. Anything unresolved → **`location_uncertain`**. No longer passed to
   scoring: it is persisted with `status='excluded_location'` and the rule
   that stopped it, and surfaced in the dashboard's "Held back" view and a
   digest section. See §4.1.2.

#### 4.1.2 Default-deny, but never a silent drop

`UNCERTAIN` no longer reaches an LLM. It is **persisted, not dropped**, with
`location_reason` recording which rule fired — or `unresolved`/`bare_remote`,
meaning no rule fired and the lexicon has a gap. A recognisable US city
appearing there repeatedly is the signal to extend `config/us_cities.json`.

Because held-back rows live in `jobs`, `load_seen_keys` would normally
suppress them forever, so a later lexicon fix could not rescue them. Two
things prevent that: `load_seen_keys` ignores `PENDING` rows, and
`scripts/recheck_held_back.py` re-classifies the held-back bucket and
requeues anything that now comes out accepted.

#### 4.1.3 Closing the leaks (Module 36, 2026-10-20)

**Trigger:** "I see lot of them are coming from other countries." Verified
against the live database before touching code: the operator was right, and
the cause was mostly not the filter of the day.

- **Most of it was old.** Of 1,308 open shortlisted jobs, 191 were foreign
  or unresolvable — and every one was scored before §4.1's Aug 23 overhaul,
  when "uncertain" still went to the LLM. Postings scored since were clean
  bar a handful. A filter change never reaches a row that is already scored,
  so `scripts/recheck_location.py` re-runs today's filter over the scored
  set (reading `country_iso`/`is_remote`/`raw` back from the snapshots) and
  moves the non-US ones to Held back with the score kept. Dry run on
  2026-10-20: **883 jobs, 192 of them in the 80+ shortlist**; 45 more foreign
  jobs already applied to or declined are left alone.
- **The rest were real holes, each found by measurement:**
  - *A US city name overruled an explicit foreign country code.*
    `Richardson, GD, CN`, `San Jose, CR`, `Alexandria, EG`, `Worcester,
    Western Cape, ZA` were all accepted because the city is in
    `us_cities.json`.
  - *A country's own code read as a state.* `Jakarta, ID` (390 rows
    corpus-wide), `Bogota, CO` (160), `Buenos Aires, AR` (70), `Casablanca,
    MA` (59), `Chennai, TN` (41), `Tirana, AL` were accepted as Idaho,
    Colorado, Arkansas, Massachusetts, Tennessee, Alabama. The rescue that
    keeps `Rome, GA` now needs the town on a measured namesake list (50
    names, from a scan of all 388k distinct location strings).
  - *Ambiguous US city names vetoed foreign signals.* `Richmond, BC`,
    `Cambridge, ON`, `Columbia - Bogotá`, `Richmond, Melbourne VIC`,
    `Edinburgh, Leeds, Manchester or London` — the US name both accepted the
    string and switched off the rule that would have caught it. In the
    operator's own shortlist a bare "Cambridge" was Healx and Red Gate (UK).
  - *Canadian provinces had no rule at all* — `ON`, `BC`, `QC` collide with
    no state, so nothing ever looked for them.
- **The filter was also wrong the other way, at larger volume.** Measured on
  every location in all 49 snapshots (2.93M rows) against the committed
  filter: the `XX-City` prefix rule had been rejecting US jobs written
  `NC-Winston-Salem`, `IL-Downers Grove`, `GA-Marietta` (New Caledonia,
  Israel, Gabon), and a foreign city marker beat a full US address
  (`Ontario, CA 91764`, `Lebanon, IN 46052`, `Paris, TN 38242`).

**Whole-corpus effect** (2.93M rows): **8,456 rows rejected → accepted**
and **4,210 held back → accepted** (all US — prefix and state+ZIP forms);
**3,060 accepted → rejected** and **1,793 accepted → held back**;
16,574 held back → rejected. Every accepted→rejected location was audited;
the only US-looking ones are junk-tagged (`Dallas, Texas, dz`, `Hartford,
CN`, `Pueblo West CO, SI`, 6 rows). Gains in recall land almost entirely on
non-tech roles (retail, clinics); the losses are concentrated where the
operator saw them.

**Known, accepted trade-offs:**

- A bare `Cambridge`, `Manchester`, `Birmingham`, `Richmond`, `Essex` is held
  back (1,793 rows). Some are Cambridge, MA; they stay visible in Held back,
  and the operator's own data showed the bare form going to the UK.
  `Durham` and `Lincoln` were measured and left off: bare, they are Durham
  NC and street/park names.
- `WA - Seattle` style prefixes are not read as Washington, because one
  Workday tenant writes Western Australia the same way (`WA - Mandurah
  Area`). Seattle, Bellevue, Redmond are accepted by city name regardless.
- `Hyderabad - Phoenix Equinox Tower 2` is still accepted (Phoenix is a
  building name). One posting.
- `ats_id` is not unique within a slice — 70k Workday and 42k iCIMS ids
  belong to more than one company — so `recheck_location.py` matches on
  company and location text, not id alone. The pipeline's own
  `global_id = ats_type:ats_id` has the same collision; that is a separate
  problem, not fixed here.

#### 4.1.4 Original cascade (pre-2026-08-23), for reference

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

`tests/fixtures/locations.json` is the contract — 95 cases as of
2026-08-23. Every aggressive rule carries both its positive case and its
US-protection case, because each of these rules was caught deleting real US
jobs during development: `Fate, TX, 75189` (postal), `San Carlos, CA` and
`777 Hemlock St, Macon, GA` (trailing ISO), `Cañon City, CO` and `Remote –
California Bay Area` (non-ASCII), `Ontario, CA` vs `Ontario, California`.

`scripts/location_baseline.py` is the before/after harness: snapshot the
classifier over real slices, change something, diff, and read the
`accepted → rejected` transitions. That set is the only metric that tells
you whether a new rule ate real US jobs.

### 4.2 Title Filter

**Allow-list** (the roles actually being targeted):
Software Engineer, SDE, SWE, Founding Software Engineer, AI Engineer,
Agentic Engineer, Forward Deployed Engineer (general-SWE flavor), Forward
Deployed Engineer (AI-specific flavor).

**Deny-list** — restricted to genuinely different job functions, not
seniority tiers of the same IC track: Manager, Director, Sales, Intern. Plus
two seniority words denied outright as of 2026-08-25 (below): Staff, Lead.

> **Design note (partially superseded 2026-08-25):** Seniority is still
> generally handled as a **soft scoring penalty** (§6), not a filter-stage
> exclusion — Senior and Principal remain scoring-only, consistent with the
> "reach roles are still worth seeing, just scored lower" approach agreed on
> for years-of-experience. **Staff and Lead are the deliberate exception**,
> on explicit instruction: both are now hard-denied before an LLM ever sees
> the posting ("Staff Software Engineer", "Lead Platform Engineer").
>
> The one collision this creates: **"Member of Technical Staff"** (and its
> "MTS" abbreviation) is a distinct senior-IC title used by AI labs — the
> word "staff" there names the role, it isn't a "Staff `<role>`" seniority
> prefix. `filters/title.py` checks for that phrase *before* the deny list
> and short-circuits straight to a keep (`_MTS_RESCUE_RE`), the same
> rescue-signal shape §4.1's location filter uses for its own word
> collisions. The rescue bypasses the ordinary allow-list check too, since
> "Member of Technical Staff" on its own contains no "engineer" and
> wouldn't otherwise match any allow phrase.

### 4.2.1 Company Block List — hard exclusion, no exceptions

Some employers are excluded outright: **never scored, and their job
descriptions never sent to an LLM.** §3.1 already drops their own ATS feeds,
which is a hard guarantee for the common case but structurally cannot cover
the same employer posting through *someone else's* platform. A scan of all
2,881,672 rows across the 36 downloaded slices found exactly that: 170 rows,
of which 8 survive the title filter and would otherwise have been scored —
`uberfreight` (84 rows, 2 surviving) and `googlefiber` (83, 6) on greenhouse,
plus `amazon.jobs.personio.com` (3, 0) on personio.

So the block list is a second gate, running immediately after the title
filter — before location, eligibility, routing, and scoring, i.e. before
anything that costs money or writes a row. Blocked rows are dropped and
logged at `WARNING`, not persisted, consistent with how an excluded ATS
source is skipped without leaving any record.

**Matching is exact on the normalized name, never a substring**, and that
constraint is doing real work. The same scan surfaced 191 look-alike
names — `apple-roofing` (58 postings, a roofing company), `Metabase`,
`Metabo`, `Applebank`, `uberall`, `appletreedental`, `Meta House`,
`Meta Group`, and dozens of German `metallbau-*` metalworking firms — every
one of which a substring match would have silently discarded. That is the
§10 "never silently drop" failure mode, and it is why normalization only
strips trailing legal/descriptor tokens (`Inc`, `LLC`, `Platforms`,
`Technologies`) and deliberately omits words like `group` and `house`. A
value shaped like a hostname is matched on its first DNS label only, so
`amazon.jobs.personio.com` is caught while `apple-roofing.breezy.hr` is not.

Also worth recording, since it looks like a miss and isn't: join.com carries
companies named `facebook761`, `google731` and similar. These are European
recruiters advertising via Facebook/Google ads — French cleaning jobs,
German dental assistants, Colombian cafeteria staff — not Meta or Google,
and none of them survives the title or location filter anyway. They are
deliberately left unblocked.

### 4.2.2 Freshness Filter — skip postings too old to apply to

A posting that went up months ago is usually filled, so scoring it spends
money on something that can't be acted on. Jobs older than
`MAX_POSTING_AGE_DAYS` (default 15) are dropped before reaching an LLM.

**This is the single largest cost lever in the pipeline.** Measured across
greenhouse, ashby, lever, smartrecruiters and workable, it takes the jobs
reaching the LLM from **11,900 to 626 — a ~95% reduction**, which moves a
full cold-start backfill from roughly $45–130 to a few dollars.

**Postings with no date are kept.** The rule is conditional on having a date,
and the source data omits it often enough that treating "unknown" as "old"
would silently discard real opportunities — the §10 failure mode. Future-dated
postings (a data quirk) are kept for the same reason.

**Known interaction with §3.2's irregular upstream cadence.** Age is measured
against today, not against the snapshot. The manifest regenerates
irregularly — observed sitting unchanged for 13 days — so the freshest
posting in any slice is already as old as the snapshot itself. If upstream
ever goes quiet for longer than the window, every posting is stale and
nothing gets scored. That is arguably correct behaviour, but it would be
indistinguishable from a broken pipeline, so dropping an entire batch logs a
WARNING naming the cause. Same reasoning as §8's "no new matches today"
email: silence is ambiguous.

### 4.3 Eligibility Filter — Citizenship / Clearance / Export Control

Hard exclusion gate, runs before any LLM call, same three-way pattern:

**Definite-exclude patterns:** `"U.S. Citizen[ship] required"`,
`"security clearance"`, `"secret clearance"`, `"top secret"`, `"TS/SCI"`,
`"active clearance"`, `"ITAR"`, `"export control"`, `"EAR"`, `"green card
holders only"`.

**Explicitly NOT excluded on:** `"must be authorized to work in the US"` or
`"no visa sponsorship"` alone — these are common and not equivalent to a
citizenship/clearance requirement. *Not excluded **here**, that is: since
2026-10-20 a sponsorship refusal is §4.7's job, a separate filter that only
acts when the operator needs a visa. This filter still never reads one as a
citizenship bar.*

**Structured sponsorship fields in `raw` are dropped before the scan, not
read.** Added 2026-08-25 when `ycombinator` came in scope (§3.1): it ships a
`visa` key on every row, whose values are `"Will sponsor"`, `"US citizen/visa
only"` and `"US citizenship/visa not required"`. Scanned as free text the
last two both match the exclude patterns — including the one asserting
citizenship is *not* required, since the phrase is a substring of its own
negation — which hard-excluded 202 of the 281 YC rows that reached this
filter, 7 of them on that outright-backwards reading. A sponsorship stance is
not a citizenship bar, so the rule above already said these should pass. The
field is dropped rather than interpreted: whether an employer sponsors is a
scoring signal, not an eligibility gate. Every other key in `raw` is still
scanned, and a bar stated in the description is unaffected. (§4.7 now reads
that same `visa` key deliberately — as a sponsorship stance, which is what it
is.)

**Outcomes:**
- Clear match → excluded pre-LLM, logged with reason, never scored.
- No match → proceeds normally.
- Ambiguous phrasing → `eligibility_uncertain`, still scored, surfaced
  separately in digest.
- **Safety net:** the LLM scoring prompt (§6) also carries an explicit
  instruction to set `eligible: false` and score 0 if it detects a
  citizenship/clearance/export-control requirement the regex missed.

### 4.4 Deduplication

- Primary key: `global_id` — "have I already processed this exact row."
  Synthesized (upstream has no such column): `ats_type:ats_id`, with the
  posting site's host in the middle for sources that number postings per
  employer, and a hash of the `url` standing in for a missing id — see the
  finding below.
- Secondary signal: **`(company, requisition_id, location)`**, not bare
  `requisition_id` — the *employer's* internal id, shared across ATS
  platforms when one real job is mirrored on two of them. Used to avoid
  scoring/emailing the same underlying role twice.
  **Real-data finding:** bare `requisition_id` is not safe to dedup on, even
  scoped to one company — on a live 181k-row sample, `requisition_id="1"`
  alone was shared by 4,509 unrelated postings across different companies,
  and even within a single company, one (`svetness`) reused
  `requisition_id="1"` as an apparent default across 4,382 genuinely
  different openings (same title, different cities). Adding `location` to
  the key fixes both without breaking the intended cross-ATS-mirror case,
  since a truly mirrored posting shares location too (DEVELOPMENT_PLAN.md
  Module 10 has the full investigation).
- Mechanism: load all existing `seen` keys from SQLite into a single
  in-memory Python `set()` once per run, then do a set-difference against
  the filtered candidate list. **Not** a per-row database query — at
  realistic volumes (tens of thousands of keys/year) this is instant and
  trivial in memory.

**Real-data finding (2026-10-20): `global_id` is not one posting either.**
It is synthesized as `ats_type:ats_id`, and sources that number postings per
employer reuse ids across employers — 70k Workday ids and 42k iCIMS ids
belong to more than one company; `workday:R0003795` is both OCLC's software
job and a university instructor posting. A posting whose id another employer
already holds is dropped as "already processed" with nothing recorded, and
two such postings in one poll are both scored with the second overwriting
the first. Measured on the live database: 1,391 of 13,853 stored jobs share
their id with a different employer; **6 relevant postings are being hidden
right now**; no applied or declined mark has ended up on a different
employer's job. Successfactors also has 25,901 postings with no id at all,
which all collapse onto one key.

**Fixed the same day** (DEVELOPMENT_PLAN.md Module 37): for the eleven
per-employer sources the key now carries the employer's site —
`workday:oclc.wd1.myworkdayjobs.com:R0003795` — chosen over the company name
because one employer lists the same opening on several career sites under
different labels ("Disney", "Disney (Disneycareerdc)"), which a company key
would have scored twice. Every other source keeps `ats_type:ats_id`. What is
still shared (106 keys, none a role that passes the title filter) is logged
as a warning on every load, and a batch never sends two postings with one
key to the LLM. **The operator chose to start from an empty database rather
than migrate**: the old one is kept at
`data/coldstart.sqlite3.bak-20261020-pre-global-id`, and a poll refuses to
run against any database still holding old-shape keys.

---

### 4.5 Liveness Check — is the posting still there?

**Trigger.** Operator clicked a "strong" (95) match minutes after it was
scored and found the job gone. §3's design is "hosted snapshot only" — every
filter above scores against `description`/`raw` text already embedded in a
third-party parquet slice, never against the live posting. §3.2 already
documents that the upstream manifest can sit unchanged for 13+ days, so a
posting in any slice can already be stale before it's even downloaded. This
module is what happens when that stale-on-arrival case is confirmed live: the
LLM never touched Workday or Genpact at all, and nothing in the pipeline ever
checked whether `apply_url` still resolved.

**`apply_url` itself cannot answer that question.** Workday's
`myworkdayjobs.com` job pages are a client-rendered SPA — confirmed live,
2026-08-24, on the exact dead URL: `curl` returns HTTP 200 and the same
~6.8KB shell whether the requisition exists or not. A naive "ping the URL"
check would report every posting as live, dead or not.

**What each ATS's own frontend calls instead does distinguish the two
cases** — confirmed live against real rows pulled from the production
database, both directions, same day:

| ATS | Endpoint | Live | Dead |
|---|---|---|---|
| workday | `apply_url` with `/wday/cxs/{tenant}` spliced in after the domain | `200` + `jobPostingInfo` | `403 {"errorCode":"S22"}` (or `404`) |
| greenhouse | `boards-api.greenhouse.io/v1/boards/{token}/jobs/{id}` | `200` | `404 {"status":404,"error":"Job not found"}` |
| lever | `api.lever.co/v0/postings/{company}/{postingId}` | `200` | `404 {"ok":false,"error":"Document not found"}` |

The Workday transform needs no extra data — `{tenant}` is the subdomain's
first label, already present in the stored `apply_url`.

**The Greenhouse token is not always in the URL.** Most `apply_url`s carry it
directly, but some sources embed the widget behind the company's own domain
instead (`coinbase.com/careers/positions/8113286?gh_jid=8113286` — no token
visible). For those, the token is *guessed* from the `company` field. A `200`
from a guessed token confirms both the guess and liveness — trust it. A `404`
from a guessed token is ambiguous (wrong guess vs. genuinely gone) and is
therefore **UNKNOWN, not DEAD** — trusting it would silently delist live
postings behind any custom career-page domain, exactly the §10 failure mode
this project exists to avoid. Only workday/greenhouse/lever are covered;
every other `ats_type` (iCIMS, SuccessFactors, Oracle, ...) is always UNKNOWN.

**Two independent hooks, because they catch two different failures:**

1. **Pre-LLM, in `_process_slice`.** The last, most expensive gate before
   scoring — it runs after every free filter, on the smallest surviving set,
   because it's the only one that costs a network request. Catches a posting
   that was already dead in the snapshot at scoring time; persisted as
   `status='delisted'` with a `delist_reason` and never sent to an LLM.
2. **A periodic sweep (`scripts/run_liveness_sweep.py`, its own daemon
   thread, default every 24h)**, re-checking every already-`scored`,
   unactioned row on a checkable `ats_type`. This is the one that actually
   fixes the trigger case: a posting that was live when scored and died
   before anyone looked at it. It cannot be folded into `run_poll` — it
   hits third-party ATS endpoints directly and must not scale with poll
   frequency (every 30 min, per §3.2) or queue behind a multi-hour
   ingestion. Rows already marked `applied`/`declined` in `job_state` are
   skipped — no point re-checking a job the operator has already acted on.
   The sweep flips `status` via a dedicated `UPDATE` (`db.mark_delisted`),
   never `upsert_job`'s full replace, so the original score/reasoning stay
   on the row for reference instead of being clobbered by a check that never
   looked at them.

**Default-deny would be wrong here — this is default-trust.** Unlike the
location filter (§4.1), where UNCERTAIN is held back, a liveness check that
can't get a clear signal (unsupported ATS, unrecognized URL shape, timeout,
5xx, a guessed Greenhouse token that 404s) leaves the job exactly as it was.
Only a confirmed not-found response moves it. One knob,
`LIVENESS_CHECK_ENABLED` (default on), turns off both hooks — a real HTTP
call per checkable job against a third-party site should be switchable in
one place.

**Bonus signal from the same call: Workday's own posting age.** A live
posting's CXS response also carries `jobPostingInfo.postedOn` — a string
Workday's own site renders directly (`"Posted 24 Days Ago"`, `"Posted
Today"`, `"Posted 30+ Days Ago"`). That's more trustworthy than the
snapshot's `posted_at`, which §4.2.2's freshness filter already ran against
upstream of here: this same section documents the manifest sitting
unchanged for 13+ days, so `posted_at` can under-report a posting's true
age by that much. Since a checkable workday job already costs one CXS
request for the liveness check, parsing `postedOn` out of it is free.

A posting that comes back LIVE but whose live-parsed age exceeds
`MAX_POSTING_AGE_DAYS` gets the identical treatment §4.2.2 gives any other
stale posting: **dropped, not persisted** (the row would tell an operator
nothing a log line doesn't already), and marked as already-seen so the next
poll doesn't spend a second Workday request re-learning the same fact.
`"30+ Days Ago"` parses as a lower bound of 30, not an exact age — correct
at the 15-day default, and only under-reports if `MAX_POSTING_AGE_DAYS` is
ever raised above 30.

---

### 4.6 Stack / Experience Filter — cost-only pre-LLM cut

Runs after §4.3 (eligibility) and §4.7 (sponsorship), on whatever survives
them. Unlike
every filter above, this one is not a compliance or relevance judgment call
against the operator's actual profile — it exists purely to stop spending an
LLM call on a posting that can never plausibly score well, given the
candidate's real resume stack and the rubric's own arithmetic. Module 32.

**Stack mismatch.** The JD names a language/stack the candidate doesn't have
(Java, .NET/C#, PHP, Ruby, Go, Rust, Kotlin, Scala, SAP/ABAP, Salesforce
development, or a Terraform/Ansible/Puppet/SRE/Kubernetes-admin/sysadmin-only
role) and never once mentions the one they do (Python, Node.js, TypeScript,
FastAPI, Flask, LangChain, RAG, LLM). Default-trust like §4.3's uncertain
tier: any mention of the candidate's own stack, even alongside a mismatch
keyword, passes the posting through untouched rather than guessing which one
dominates.

**Experience floor.** Not a judgment call — a deterministic consequence of
§6.5's own scoring formula. `experience_fit` floors to 15 once required years
`R` exceeds candidate years `X` by more than 4, and at that floor the
composite score caps at 78.75 even with a perfect 100 on every other
dimension, below `score_threshold_strong` (80). A JD stating "10+ years
required" cannot score "strong" under the rubric as written; cutting it here
before the LLM call changes zero outcomes, only cost.

**Real-data validation, and what it caught.** Measured against the true
survivor set of the filters above (19,317 rows, from a 2.9M-row historical
corpus across every ATS source): 14.9% cut on stack mismatch, 6.0% further
on the experience floor, ~20.8% combined. The first pass of both rules was
built from plausible-looking keywords and initially measured near 22%, but
sampling the actual matched JD text — not just trusting the aggregate count —
surfaced three real bugs before shipping: a lowercase `.net` pattern matching
URL domains inside press-release boilerplate (`c212.net/c/link`); a bare
`"salesforce"` keyword firing on founder bios and investor names ("co-CEO of
Salesforce", "backed by ... Salesforce Ventures") at a ~37% false-positive
rate in-sample, fixed by requiring a Salesforce-specific compound phrase
(`salesforce developer`, `force.com`, `visualforce`, ...) instead of the bare
company name; and the years-extraction regex reading "years of age" as an
experience requirement, misreading company-history bragging ("a 90+ year
history", "40+ years at Palantir") as a candidate floor, and taking the high
end of a range ("5-8+ years") instead of the low end that's the real minimum.
`"chef"` and `"on-call rotation"` were dropped from the devops keyword list
entirely — the former collided with cafeteria-menu copy, the latter with
ordinary backend on-call duty that plenty of good-fit roles also carry, which
is not a discriminator specific to ops-only roles. All fixed and covered by
regression tests in `tests/test_stack.py` naming the exact real posting each
one came from.

**Experience cap (Module 39, 2026-10-20) — this one *is* a judgment call.**
The operator asked for "max 5 years": `MAX_REQUIRED_EXPERIENCE_YEARS=5` cuts
any posting asking for 6+ years, with `experience_cap:<n>yrs_required` as the
reason. It can only tighten the floor above, never loosen it. Unlike the
floor it changes outcomes — the LLM can rate a 6–7-year job 80+ — so it was
measured against the operator's own decisions before shipping: of 64
applied jobs whose posting text was still available, **none** asked for 6–7
years, and 35 declined ones did. On the live shortlist it cut 36 scored
jobs, none of them 80+.

At a 5-year line, degree alternatives stop being noise. The one shortlisted
job the cap first cut (doowii, 82) asked for 3+ years; its "6+" was a
no-degree substitute ("BS … (or similar field with 6+ years)"). So the years
parser now skips a number that only applies to someone without the
candidate's degree, read from `candidate_degree` in `stack_rules.json`
(`masters` — the operator holds a Master's in Computer Science):
"Bachelor's with 6+ years **or a Master's with 3+ years**" counts as 3;
"…in lieu of a degree" doesn't count; a PhD path doesn't count for a
master's holder. Audited on every job in the live database (1,828 postings,
31 skips, each read).

**Persistence.** A hard-excluded row is persisted the same way §4.1 and §4.3
excluded rows are — `status=excluded_stack`, `stack_reason` naming the rule
and matched text (`"stack_mismatch:java:Java"`,
`"experience_floor:12yrs_required"`) — auditable the same way, never silently
dropped.

**Correction, 2026-10-20: it also silently overwrote 384 scored jobs.**
Held-back rows are written *before* dedupe, so this filter re-saw postings
scored on earlier polls that were still in the feed, and its upsert replaced
them — score and reasoning wiped, 3 of them jobs the operator had applied to.
Found while building §4.7, which would have done the same. Fixed in the write
itself (`db._JOB_UPSERT`): an unscored row can no longer replace a `scored` or
`delisted` one. The 384 lost scores are not recoverable.

### 4.7 Visa Sponsorship Filter — postings that say they won't sponsor

Runs right after §4.3, because it is the same kind of rule: "you can't take
this job", not "this job is a poor fit". **Only acts when
`NEEDS_VISA_SPONSORSHIP=true`** — someone who doesn't need a visa loses
nothing. Module 33, `src/coldstart/filters/sponsorship.py`.

**Why it exists.** The operator is in the US on F-1 OPT: authorised to work
today, but needs an employer to sponsor an H-1B later. Measured on the
operator's own decisions (2026-10-20): above score 80 the score barely
predicted what got applied to, and 81% of decisions took under 5 seconds — the job description
is not being read, so a sentence like "we will not sponsor" never gets seen.
Of 118 applications whose posting text was still available, 7 went to jobs
that state outright they won't sponsor; one names F-1 OPT as unsupported.

**What it reads.** Three stances, or none (most postings say nothing):

| Stance | From | Effect |
|---|---|---|
| `wont_sponsor` | "we will not sponsor", "authorized to work without sponsorship now or in the future", "not eligible for CPT, OPT, or visa sponsorship", "No H1B", a form field answering No | held back, `status=excluded_sponsorship`, never reaches the LLM |
| `will_sponsor` | "visa sponsorship is available", "we sponsor H-1B visas", a benefits bullet "- Visa sponsorship", a form field answering Yes, YC's "Will sponsor" tag | recorded; will rank higher (Module 35) |
| `existing_authorization_only` | YC's "US citizen/visa only" tag | recorded; ranks lower, not held back |

A plain "must be authorized to work in the US" is **not** a refusal — it is the
commonest sentence in this whole area, and an OPT holder meets it.

**Rules of precedence.** A form field ("Sponsorship Available : Not
Available", "Is role eligible for Immigration Sponsorship?: Yes") decides
outright. Otherwise a refusal beats an offer: "we support H-1B transfers but
are not initiating new visa sponsorships" is a refusal for someone who needs a
new petition, and H-1B transfers alone are never read as a yes. The JD text
beats YC's tag when it says anything. ycombinator is the only in-scope source
with a structured field (checked across all ten); everything else is prose.

**Real-data validation, and what it caught.** Built against the true survivor
set of today's snapshots — every in-scope posting passing title, company,
location, eligibility and stack, 13,003 rows — reading the matched sentence
for every rule. 5.2% say they won't sponsor (13.9% of workday, under 1% of
icims, gem and ycombinator), 3.1% say they will, 4.4% are YC "visa only".
Five draft rounds, each catching something a plausible pattern got wrong:

- "Sponsor" the verb is not visa language — "we sponsor events", "we sponsor
  bright women from disadvantaged backgrounds", "company-sponsored employee
  benefits are not provided". Verb forms only count with a visa or
  work-authorisation word in the same sentence; "sponsorship" the noun counts
  alone.
- Markdown split the negation: "does \*\*not\*\* provide visa support" read as
  an offer.
- "U.S." and "etc." ended sentences, so "US work visa sponsorship (such as TN,
  O-1, H-1B, etc.) is not available" was missed.
- Hedges from companies that *do* sponsor read as refusals: "We sponsor visas
  ... We can't successfully sponsor for every role", "work authorization is
  preferred but not required, can sponsor visas".
- Real refusals run long — "without the need for current or future
  employment-based visa sponsorship" — hence up-to-8-word gaps that never
  cross a full stop.

Every remaining refusal sampled was genuine. Then a dry run of
`scripts/recheck_sponsorship.py` against the live database: of 4,270 scored
jobs whose posting is still in a local snapshot, 356 say they won't sponsor —
293 not yet acted on would move, **102 of them currently shortlisted**, and
the deciding sentence was read for every one of the 102.

**Corrected the same day:** the script found each job's description by its
posting id alone, and ids are shared between employers (§4.4), so it read
*another company's* description for 549 scored jobs — 10 would have moved on
someone else's "we don't sponsor" (Adobe on Baker Hughes's), and 40 real
refusals were missed (14 of them Capital One). It now takes the description
from the posting with the job's own company. Corrected dry run: 4,166
re-checkable, 386 say they won't sponsor, **318 would move, 109 of them
shortlisted**; every changed verdict's deciding sentence was read.

**Backstop.** With the flag on, the scoring prompt gets a sponsorship safety
net alongside the citizenship and location ones (§6.4), for refusals worded
in a way the filter has never seen. A plain work-authorisation line and
"we can't promise it for every role" are spelled out as not disqualifying.

**Already-scored jobs.** A poll never re-classifies a row it has scored, so
the shortlist from before this filter keeps its refusals until
`scripts/recheck_sponsorship.py --apply` is run. That moves them with an
explicit UPDATE — score and reasoning kept — and never touches a job the
operator has applied to or declined. Report-only by default, and on a
read-only connection.

---

### 4.8 Company Memory — decisions about an employer, not a posting

Module 34, `src/coldstart/company_memory.py`. Runs last, right before
scoring, on only what would otherwise go to the LLM.

**Why.** Measured on the operator's 1,190 applied/declined decisions
(2026-10-20): above score 80 the score barely predicted what got applied to
(80–84: 27%, 90–94: 25%), and 81% of decisions took under 5 seconds — the
decision is about the company, read off the list. 63 companies were declined
3+ times and never applied to (a third of all declines); 151 declines came
after already applying at that company; companies with 10+ shortlisted roles
were applied to 8% of the time, single-role ones 44%. The shortlist forgot
all of that between polls.

**What it remembers:**

| Decision | Stored as | Effect |
|---|---|---|
| "Not this company" | `company_state` row, `blocked` | Hidden until undone from the Hidden view |
| "Rejected me" (on an applied job) | `job_state` = `rejected` | Company hidden for COMPANY_COOLDOWN_DAYS (90 — the operator's choice) from the mark's date, then back |
| Applications | `job_state` = `applied`, counted for APPLICATION_WINDOW_DAYS (90) or until marked rejected | At 1: the company's other roles are still scored but collapse under "Applied here · 1 of 2". At APPLICATIONS_PER_COMPANY (2): hidden |
| Decline reason | `job_state.note`, one of company / role type / seniority / location / visa / stack / already applied | Recorded only — the data for tuning later, instead of guesses |

The cap's counting rule (a window, and rejections leaving the count) was
not specified by the operator; it is a default, and all three numbers are
settings. Many employers let you reapply after a waiting period, which is
why a rejection is a cooldown and not a block.

**Hidden means skipped, not deleted.** A hidden company's new postings are
dropped before scoring and never persisted (like §4.2.1's block list), so
the day a hide lifts the next poll scores them. Already-scored roles stay in
the database; the dashboard moves them from Open to a **Hidden** view, which
also lists every hidden company with its reason and the undo.

**Identity.** One employer is one `company_key`: the block list's
normalisation (hostname first label, trailing legal suffixes) run together,
so "Stripe"/"stripe" and "Retell AI"/"retell-ai" match while "Meta"/"Meta
Group" don't. On the live database: 1,278 names → 1,258 keys, all 20 merges
genuine.

**History it starts from.** Module 37 began an empty database; the 861
marks on sources still in scope were restored from the pre-reset backup
(`scripts/restore_marks.py`) before this shipped, re-keyed, each with its
job row. Marks on excluded sources (277 Workday, 52 others) were not.

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
  future extension) actually needs one. **Note:** `full_text` (stored on
  disk, gitignored) is still the complete extracted resume, name/contact
  info included — it's `text_utils.filter_resume_for_llm` (Module 14
  addendum, post-Module-19) that strips identity/contact info specifically
  at the point resume content is handed to an LLM, not at ingestion time.
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
- Model/provider selection is entirely `.env`-driven, and there is **no
  cross-provider fallback** — exactly one provider is used per run.
  `LLM_MODE` is the switch: `dev` always means Ollama (`OLLAMA_MODEL`
  directly, free, no key); `prod` means exactly the one provider named by
  `LLM_PROVIDER`, using that provider's API key. `BATCH_SIZE` (default
  `1`, grouping multiple *jobs* into one call) is a separate, orthogonal
  knob. (Originally spec'd as a `PROVIDER_FALLBACK_ORDER` list tried in
  order per job — removed post-Module-19 as unwanted complexity; see
  DEVELOPMENT_PLAN.md Module 13's "Addendum 2.")

### 6.2 Prompt Structure for Caching

Resume text and system instructions go at the **start** of the prompt; the
variable JD content goes at the **end**. This maximizes automatic caching
on DeepSeek, Kimi, and Gemini (all cache repeated prefixes with no code
changes required); Anthropic requires explicit `cache_control` breakpoints
to get the same benefit.

### 6.3 Reliability

- On 429/5xx: exponential backoff, limited retries against the one active
  provider (`MAX_RETRIES_PER_PROVIDER`) — no cross-provider failover.
- If retries are exhausted: log to `errors`, mark job `status = 'failed'`
  for manual review. **Never silently dropped** — a well-fitted job must
  not be lost to an infrastructure issue.
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

**Years-of-experience formula** — let **X** = Manish's years of experience,
entered directly via `.env` as `EXPERIENCE_YEARS` (e.g. `3.5`), **R** =
years required by the posting.

**Design note (revised after build):** X was originally meant to be derived
from an `.env` start date (`today − start`). Dropped in favor of a direct
number — a start-date derivation silently assumes continuous employment,
so a career break would overcount X with no way to detect the error. A
direct number is exact at the time it's set; the cost is that it doesn't
auto-increment and needs manual updating every so often, which is an
acceptable, visible tradeoff instead of a silent wrong one.

| Condition | `experience_fit` sub-score |
|---|---|
| `R ≤ X` | 100 |
| `X < R ≤ X+2` | linear 100 → 80 ("companies may flex" zone) |
| `X+2 < R ≤ X+4` | linear 80 → 40 (real stretch) |
| `R > X+4` | floor ~15 (long shot, not excluded) |
| `R` unstated (common — `experience` field is `None` on most postings) | LLM estimates from JD prose, same bands applied |

**Score bands (binary, changed 2026-09-01 — see §8 update below):**
- `≥ 80` → **strong** (headline section of digest, `SCORE_THRESHOLD_STRONG`)
- `< 80` → **reject** (still logged in `jobs`/CSV, never emailed, never
  deleted)

There used to be a third `60–69` "worth considering" tier, shown de-emphasised
at the end of the digest and included by default on the dashboard. Dropped at
the operator's request: it sat unused as a real workflow (postings never got
actioned out of it) and just added noise to both surfaces. `JobScore.score_band`
below (the LLM's own self-assigned opinion) still has three values — that is
independent of the operator's threshold and was deliberately left alone; only
`band_for()` (`web/queries.py`) and the digest's own bucketing are binary now.

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
    role_type_fit: int            # added: §6.4's role-type/domain dimension (15%
                                   # weight) had no field to carry its sub-score
    matched_skills: list[str]
    missing_skills: list[str]
    reasoning: str                # short justification, shown in digest
```

### 6.6 Per-Company LLM Cap (Module 40)

**Requested 2026-10-21**, after one poll put five base-power roles on the
shortlist at once (Software Engineer, Growth; Full Stack, Growth; Full Stack,
Core Experience; Software Engineer, Core Experience; Forward Deployed
Engineer). Nobody sends five tailored applications to one employer on one
day, so paying an LLM to score all five, and hemline (§8.4) to tailor five
resumes, is waste. The operator's rule: **at most two of a company's postings
go to the LLM, even under different titles; the rest are scored without it,
marked "manual", and shown for review.**

- **The cap** is `LLM_SCORES_PER_COMPANY_PER_DAY` (2), per local day in
  `TIMEZONE`, per employer as `company_key` (§4.8) identifies it. It counts
  LLM-scored jobs already in `jobs` for that day (earlier polls, earlier
  slices) plus this slice's own calls, so it holds across the daemon's
  every-30-minutes polls. A call that failed still used its slot.
- **Which two:** every candidate gets a keyword score first (free), and a
  company's slots go to its best-scoring postings still waiting. A posting
  that leaves without an LLM call (delisted or stale by the liveness check,
  §4.5) frees its slot for the next one. Processing order is unchanged.
- **The keyword score** (`scoring/manual.py`) uses §6.4's four dimensions and
  weights: the skills the JD names that the resume also names; years asked
  for, via the stack filter's parser and the rubric's banding; title level
  against `EXPERIENCE_YEARS`; FDE/AI/founding titles via routing's keywords.
  Resume choice is keyword routing only, since routing's fallback for a bare
  title is itself an LLM call. The reasoning starts "Scored by keyword match,
  not the LLM" and says why.
- **Stored like any scored job:** `status='scored'`, with
  `provider_used='manual'`. That one value is what the dashboard's amber
  **manual** flair and hemline's skip key off. Nothing new in the schema.
- **Never sent to hemline**, whatever the score: `hemline.job_scored` skips
  `provider_used='manual'` itself, so no caller can forget to.

Not handled (out of scope of the request): a manual job is never re-scored by
the LLM later, even on a day the company's slots go unused.

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
  status          TEXT,      -- 'scored' / 'failed' / 'pending' / 'excluded' /
                              -- 'excluded_location' / 'delisted'
  location_flag   TEXT,      -- 'accepted' / 'rejected' / 'uncertain'
  location_reason TEXT,      -- which rule fired (Module 26)
  eligibility_flag TEXT,     -- 'passed' / 'excluded' / 'uncertain'
  provider_used   TEXT,
  first_seen_at   TEXT,
  scored_at       TEXT,
  delist_reason   TEXT,      -- which liveness check fired (Module 27)
  delisted_at     TEXT
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

hemline_events (             -- Module 38, §8.4
  global_id   TEXT PRIMARY KEY,
  event_id    TEXT,          -- the payload's "id"
  payload     TEXT,          -- exact JSON body, frozen at score time
  status      TEXT,          -- 'pending' / 'sent' / 'failed'
  attempts    INTEGER,
  last_error  TEXT,
  created_at  TEXT,
  sent_at     TEXT
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
- **Coverage: everything since the previous digest actually went out** — not
  "everything since midnight". **Revised after a real-data finding.** The
  original midnight-anchored window, paired with an 08:00 send time, meant
  each digest reported only the overnight hours; anything found between 08:00
  and midnight was scored, saved, shown on the dashboard, and never emailed
  by anything — a 16-hour blind spot every day. Observed concretely: a poll
  interrupted at 20:25 PT had scored 146 jobs including 84 strong matches,
  and the next morning's window contained none of them.

  The original assumption was reasonable — a once-a-night upstream refresh
  makes an overnight window sufficient. §12's daemon (polling around the
  clock) and §3.2's finding (upstream regenerates irregularly, not nightly)
  both broke it. Anchoring to the last successful send makes coverage
  continuous by construction. A *failed* send deliberately does not advance
  the window, so its jobs are carried into the next one rather than lost.
- **Content, in order:** Strong matches (≥80) → Location-uncertain items →
  Eligibility-uncertain items. (There used to be a "Worth considering"
  60–69 section here too — dropped 2026-09-01, see the §6.4 note.) The two
  uncertain sections aren't exclusive with the score section — a job
  lands in Strong by score *and* in an uncertain section if its
  location/eligibility flag needs a manual check, since those answer
  different questions. Section membership is computed from the numeric
  score against `.env`'s configurable threshold, not from the LLM's own
  `score_band` field (Module 18 — the rubric prompt never tells the model
  about the configured threshold, so trusting `score_band` would make
  threshold changes silently no-op in the digest).
- **CSV export:** full log of every scored job (all bands), separate from
  the digest, for audit/analysis.
- Email send failures are logged to `email_log`, never silent.
- **Presentation:** a real transactional-email template — 600px shell,
  branded header, summary band, and one card per job with a prominent apply
  button. Revised from the original 9-column `<table border="1">`, which put
  a whole reasoning paragraph in a table cell and was unreadable on a phone.
  Email HTML constraints drive the structure: table-based layout with inline
  styles (flexbox/grid/stylesheets are unreliable across clients), no images
  (clients block remote content), and a hidden preheader for the inbox
  preview line.
- **The layout is an editable file, not code** (`config/email/`). Read from
  disk on every render, so an edit takes effect on the next digest with no
  restart, and previewable live on the dashboard while editing. A template
  that fails to render falls back to a plain built-in layout that says so —
  applying §10's "never silently drop" to presentation: a styling mistake
  must never cost a day's matches.
- **Every job carries a link, and the link has to actually show the job.**
  `apply_url` is absent on entire sources — all 33,888 amazon rows have it as
  `NaN` — so the link falls back to the posting `url`, which is always present
  and was previously discarded at persistence time. A job with neither says so
  rather than showing an empty cell.

  **A present `apply_url` can be just as useless (2026-08-25).** All 295
  `ycombinator` rows stored
  `account.ycombinator.com/authenticate?continue=...workatastartup.com/...` —
  a YC sign-in form that shows nothing about the posting, so every YC job on
  the dashboard and in the digest was unclickable without a YC account. The
  public listing (`ycombinator.com/companies/<co>/jobs/<id>`) was in `url` all
  along (§3.1.1). `_best_link` now treats a login-walled `apply_url` the same
  way it treats a missing one and falls back to `url`; all 295 existing rows
  were repointed by `scripts/backfill_walled_links.py`. Matched on exact
  host+path — an ordinary application link that merely contains the word
  "login" in a query string is untouched.
- **Exactly one digest per local day**, enforced by querying `email_log`
  rather than by remembering in memory. `run_digest` itself has no such
  guard — the day window alone bounds what it selects, so calling it twice
  sends the same jobs twice. Putting the check in durable storage is what
  makes a daemon restart at 08:05 safe.

### 8.1 Live Dashboard

A single web page, served by the daemon (§12), showing every non-reject
scored job — "shortlisted", in the interface's own words — in a sortable
nine-column table over a strip of six metric tiles. This is the
"no UI" reversal from §1: the digest answers *"what should I look at
today"*, and the dashboard answers *"what does the whole pipeline currently
hold, and why did this job score what it scored"* — the question §7's
"browsable via a SQLite extension" was supposed to cover and didn't, once
`matched_skills`/`missing_skills` became JSON-encoded columns.

Design constraints that follow from the rest of this document:

- **Strictly read-only.** Every request opens its own `mode=ro` connection.
  It is structurally incapable of locking or mutating the database while a
  poll is committing. WAL makes the two coexist, and because §10's
  incremental writes commit per job, rows appear on the page *during* a run.
- **Bands are recomputed from the configured thresholds**, exactly as §8
  requires for the digest and for the same reason — the rubric prompt never
  tells the model what those thresholds are, so its self-assigned
  `score_band` is an opinion. The dashboard shows both and flags the
  disagreement, rather than silently preferring either.
- **Funnel counts come from `run_log`**, not from `jobs`. Title- and
  location-rejected rows are deliberately never persisted (§7), so there is
  nowhere else for fetched/filtered counts to come from.
- **One write: "applied".** *Reversed — the original decision here was "no
  write-back, it is a view not a tracker."* At a thousand-plus scored jobs
  that stopped holding: without somewhere to record "I applied to this one",
  every future visit re-presents work already done. Everything else stays
  read-only.

  The mark lives in its own `job_state` table, **not** as a column on
  `jobs`. That is the load-bearing decision: `jobs` is pipeline output and
  `upsert_job`'s ON CONFLICT clause rewrites every column each time a
  posting is re-scored, so a flag stored there would be silently wiped by
  the next poll — precisely the §10 "never silently wrong" failure. A
  separate table cannot be clobbered that way and survives a row being
  rebuilt.

  Still out of scope: notes, dismissed/starred, multi-user, and any write
  that touches pipeline data.
- **Loopback by default.** No authentication, and it displays the full match
  list; exposing it has to be a deliberate act.
- **Light and dark are designed as a pair**, with an explicit
  light/dark/system control rather than silently following the OS. Contrast
  is measured per theme rather than assumed — 39 pairs each, in the test
  suite — because the first light palette failed four text pairs at ~3:1
  while dark passed everywhere. Both clear WCAG AA with no failures.
- **The visual language is taken from simcricketx.app** and the palette is
  adopted, not invented: Space Grotesk over IBM Plex Mono, teal `#0f766e`
  running to amber, warm near-white in light and teal-black in dark,
  generous radii, wide soft shadows. Bands share that vocabulary rather than
  fighting it — strong is the brand, reject recedes. (The amber `--consider`/
  `--consider-bg` tokens outlived the "consider" band itself — dropped
  2026-09-01 — and stayed on as a general amber accent, e.g. analytics'
  "pending" badge.) A borrowed palette still has to be measured: the
  reference's amber is
  2.1:1 on white, fine for an 80px headline and not for a 12px table label,
  so three light-mode inks were darkened until every pair cleared 4.5:1.
- **The page never scrolls sideways.** The table declared `min-width: 1120px`
  and scrolled inside its wrapper, so reading one row meant dragging the
  whole view. Fourteen columns is more than any laptop has width for: five
  now live only in the expanded row, and the remaining nine drop by priority
  as the viewport narrows. The expanded row carries every field at every
  width, so nothing is ever unreachable — on a phone four columns survive and
  the employer folds into the title cell rather than disappearing.

---

### 8.2 Analytics Page

A second page at `/analytics`, served by the same daemon thread and reading
the same database read-only. §8.1's dashboard answers *"what should I look at
today"*; this one answers *"is the machine healthy, and what has it actually
done"* — a question the system could already answer from stored data and had
no way to display.

The split is deliberate rather than a growth of §8.1. The dashboard is a
working surface you act on (mark applied, decline, filter, export); this is a
report you read. Bolting a dozen charts onto the jobs table would have made
the daily act of triaging jobs slower in order to serve a question asked
weekly.

- **One request, one instant.** `GET /api/analytics` returns every section in
  a single payload from a single `mode=ro` connection. The page is read whole
  and the numbers must all describe the same moment.
- **No network at render time.** Source rows come from the manifest the
  daemon already cached in `daemon_state`, never a fresh fetch — this is the
  same snapshot the running poll is working against, and a dashboard that
  makes an outbound request to draw itself is a dashboard that fails when
  upstream does.
- **Days are the operator's days.** Every per-day bucket converts UTC
  timestamps through `TIMEZONE` before grouping, so these charts agree with
  the digest window and the spend ceiling (§9). Grouping on the ISO string
  would cut the day at 17:00 local and file an evening's work under tomorrow.
- **What cannot be measured says so.** `est_cost_usd` is 0 for every row when
  the active model has no `PRICING` entry, which is the current production
  case. The page leads with token counts — always real, straight from the
  provider's usage object — and labels the dollar figures as unavailable
  rather than rendering "$0.00 spent" as though it were a measurement. Same
  principle as §10's "never silently wrong", applied to a number rather than
  to a job.
- **A live poll reports what it is doing.** `run_poll` is a subprocess (§12),
  so it writes a throttled heartbeat into `daemon_state` — current source,
  slice N of M, phase, rows processed, running totals. A heartbeat with no
  `finished_at` and a dead pid is reported as *interrupted*, never as
  running: a poll killed by its timeout leaves its last heartbeat behind, and
  trusting the row's presence would hide exactly the stall this exists to
  surface.
- **The sources table is the join nothing did before.** `slice_state` knew
  when a source was last processed, `jobs` knew what it yielded, the manifest
  knew what exists and how big it is; nothing put them together. It also
  keeps sources that have *never* been processed — "never" answers "when was
  lever last done"; dropping the row does not.
- **Charts are hand-drawn SVG using the §8.1 palette tokens.** No chart
  library, because there is no build step and the page makes no outbound
  request. Using the same custom properties is what makes them follow the
  light/dark control with no JavaScript, and keeps them inside the contrast
  guarantee already measured for the palette.

**Two long-standing defects surfaced by building it**, both fixed: `run_log`
had never recorded a single row on the real database (`log_run` ran only when
a poll reached its natural end, and no real poll ever had), which meant the
dashboard's fetched/filtered funnel had always shown zeros; and the top bar's
action row did not wrap, putting the theme control off-screen and unreachable
below ~430px on both pages.

---

### 8.3 Source Queue Control

§3.2 decides *when* to poll and *which* sources have changed. It never said
anything about **order**, and order turns out to matter as much: `workday` is
839,633 rows and legitimately runs for hours, so every source behind it waits
hours. Before this the sequence was whatever the manifest happened to list,
and the only way to influence it was to kill the daemon.

The order is now the operator's, expressed as two independent controls on the
analytics page:

- **Priority** — an ordered "do these first" list. A preference, not a filter:
  a prioritised source leaves the list once it is done and everything else
  carries on in its normal place.
- **Hold** — "do not process this until I release it". A filter, and
  deliberately separate from `config/excluded_ats.json` (§3.1), which is a
  standing decision about which sources this project cares about at all. A
  hold is a switch you flip and flip back.

**A running source gives way.** The point of the control is to act on the poll
you are watching, not the next one, so a slice that is overtaken stops between
jobs and goes back in the queue. Three properties make that safe rather than
wasteful, and all three are consequences of decisions already in this
document:

- The snapshot is already downloaded and sha256-verified (§3.2), so a source
  that comes back round does **not** re-download.
- Scoring commits per job (§10), so everything scored before the yield is
  kept, and §4.4's dedupe drops it from the re-run — **no LLM spend is ever
  repeated**.
- The yield skips writing `slice_state`, which is the single fact that makes
  the source still count as outstanding. Nothing else has to remember.

What is lost is the parquet read and the filter pass: minutes of CPU, no
money. The download itself is not interruptible — it streams to a temporary
file and renames on success — so a yield requested mid-download takes effect
once that finishes, which costs nothing because the result is cached.

**The queue is re-derived from the database before every slice**, not fixed
when the run starts. This matters more than it sounds: the source an operator
promotes is usually one the current run never queued, because it was up to
date when the run began — and most sources usually are. Recomputing means a
finished source drops out by itself, a source that gave way is still there,
and a newly-requested one joins mid-run.

**"Run this source" has to mean something when it is already up to date.**
Otherwise the control silently does nothing in the most common situation
there is. A re-run forgets the source's change-detection row so it counts as
outstanding again — narrow by construction (one row, nothing in `jobs`), and
cheap for the same reasons a yield is. Its purpose is re-applying a lexicon or
rule change to a source the upstream snapshot hasn't touched. Resetting the
order does not undo it: that was a request about data, not about sequence.

Everything here is a preference the pipeline consults, never a guarantee it
enforces on the operator's behalf — consistent with §10, a source that is held
is *visibly* held on the page, with its own state and count, rather than
quietly absent.

### 8.4 Push to hemline (Module 38)

**Requested 2026-10-20.** The operator's own service, hemline, acts on strong
matches (it picks the resume to use per job), so every new strong match is
pushed to it as it happens rather than waiting for the digest:

```
POST https://hemline.botta.in/jobs
Authorization: Bearer <HEMLINE_TOKEN>

{"id": "<uuid>", "job_title": "...", "profile_id": "software-engineer",
 "company": "Stripe", "job_link": "https://..."}
```

The operator's rules, each a decision rather than a default:

- **Cutoff 85, not the dashboard's 80** (`HEMLINE_MIN_SCORE`). Every event
  costs money on hemline's side; at 80 it would carry about 55% more jobs
  (1,788 vs 1,153 in the database on 2026-10-20). Score only — the LLM's own
  `score_band` is ignored here for the same reason it is everywhere else
  (§8): it doesn't respect the operator's numbers.
- **One request per job, sent the moment the job is scored.** No batching.
- **New jobs only.** Nothing scored before the token was set is ever sent —
  1,153 jobs at 85+ already in the database stay where they are. Nor is a
  *refill*: a source's first-ever run scores every posting in its window, none
  of them new (asked for 2026-10-20, when Module 37 had just emptied the
  database).
- **`profile_id`** is routing's choice (§5.2) under hemline's names:
  A → `software-engineer`, B → `ai-engineer`,
  C → `forward-deployed-engineer-swe`, D → `forward-deployed-engineer-ai`.
- **`company`** is null only if blank; in practice every row has one (0 of
  1,153 blank), even if some are ugly (`achtbytes.jobs.personio.com`).
  **`job_link`** is the same link the dashboard and digest show (§8, apply
  links).
- **`id`** is a UUID derived from `global_id`, so the same job always carries
  the same id and hemline can drop a repeat.

How the guarantees are kept (§10's "never silently drop" applied to an
outbound call):

- A job is written to a `hemline_events` row **before** it is sent, with the
  exact payload frozen. A failed send leaves the row `pending`; the start of
  every poll — including the ones that find nothing new — retries pending
  rows, oldest first, and stops at the first sign hemline is unreachable.
  After 10 failed rounds the row is marked `failed` and an `errors` row is
  written, which the dashboard counts.
- **At most once:** one row per job ever (`INSERT OR IGNORE`), and only a
  `pending` row is ever sent. Both checks live in the sender, not its caller.
- **New jobs only** falls out of where rows are created: only at score time,
  never by a sweep over `jobs`. With no token set nothing is queued either,
  so adding the token later can't release a backlog.
- **A refill is skipped** by the same test §3.2 uses for change detection: a
  job whose source has no `slice_state` row is not sent or queued. That row
  is written only when a source's run completes, so it is absent for a whole
  first run (an emptied database, a source newly let in past
  `excluded_ats.json`), for a first run resumed after a yield or failure, and
  for a §8.3 re-run, which deletes it — re-runs surface old postings, not new
  ones. From the next snapshot on, the source's new postings are sent.
- **Hemline never costs a score.** The send happens after the job is saved,
  and nothing in it can raise into the pipeline. A network failure gets one
  inline retry after 2 s; a 4xx doesn't (the same request would be refused
  again). 409 is read as "already have it".

Deliberately not handled (the operator's call): a job that is later delisted,
or removed by a recheck script, is not withdrawn from hemline.

---

## 9. Configuration (`.env`)

`.env.example` is the authoritative, current list — this was the original
representative sketch from early planning; kept for the narrative but not
maintained in lockstep (e.g. it predates `LLM_PROVIDER` becoming the real
prod-mode switch, `PROVIDER_FALLBACK_ORDER` being removed entirely, and
the per-provider `*_MODEL` overrides):

```
EXPERIENCE_YEARS=                # X in the YOE formula, e.g. 3.5 — entered
                                  # directly, not derived from a start date
LLM_MODE=prod                   # prod = the one provider below, dev = ollama local
LLM_PROVIDER=deepseek           # only used when LLM_MODE=prod; no fallback list
BATCH_SIZE=1
OLLAMA_MODEL=                   # 3-4B class, dev only
DAILY_TOKEN_SPEND_CEILING=
MAX_RETRIES=
SMTP_APP_PASSWORD=
DIGEST_TIME_PDT=
SCORE_THRESHOLD_STRONG=80        # binary: >= this is "strong", else rejected
NEEDS_VISA_SPONSORSHIP=false     # true = hold back postings that won't sponsor (§4.7)
HEMLINE_TOKEN=                   # set = push each new 85+ job to hemline (§8.4)
HEMLINE_MIN_SCORE=85
LLM_SCORES_PER_COMPANY_PER_DAY=2 # the rest of a company's postings are keyword-scored (§6.6)
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

- **Dev (now):** Manish's laptop, running a single foreground daemon
  (`scripts/run_daemon.py`) started once and left alone. **Revised from the
  original "cron-scheduled, scheduling itself is his responsibility."** That
  worked, but it left §3.2's 30-minute cadence and §8's digest time as
  configuration nothing read, split the system across two crontab lines that
  had to be kept in sync with `.env` by hand, and gave the dashboard (§8.1)
  nowhere to live. The daemon owns the clock instead.

  The requirement that **the pipeline must not assume or require the machine
  to stay awake still holds, and is now a design constraint on the loop**:
  it wakes every 15 seconds and compares wall clocks rather than sleeping
  for a whole interval, so a laptop asleep for six hours produces exactly
  one catch-up poll on wake, not twelve. A machine off past the digest hour
  sends the digest when it comes back. Combined with §10's incremental
  writes, sleeping mid-run costs nothing already scored.

  The daemon runs the pipeline as **child processes**, not in-process. That
  is a memory decision, from §4's finding: one large slice peaks around
  10 GB RSS, and process exit is the only reliable way to return that to the
  OS. It also bounds `routing.py`'s per-run title cache, which would
  otherwise grow for the daemon's entire lifetime. A crash or OOM in a poll
  takes the child, never the daemon.

  Exactly one instance can run, enforced by a kernel `flock` — nothing
  previously stopped two overlapping polls, and WAL protects the database
  but not the LLM spend.

- **Prod (later, deferred):** decision depends on whether the LLM stays
  API-based or moves to self-hosted OSS models (→ a paid box with adequate
  RAM/GPU). **The original "Oracle OCI free tier, 1 GB RAM is sufficient"
  is now known to be wrong** — §4's measurement puts peak RSS at ~10 GB on
  the largest slice, because the parquet read materializes every row's JD
  text before the filters run. A 1 GB target only becomes real after that
  read is made lazy. Still not decided; revisit once real cost and quality
  data exist.

---

## 13. Explicitly Out of Scope (v1)

- ~~No UI — email + CSV only.~~ **Reversed (§1, §8.1):** a single
  read-only dashboard now ships. What remains out of scope is anything
  *writable* — no "applied"/"dismissed"/"starred" state, no notes, no
  multi-user access, no authentication. It is a view, not a tracker.
- No automated resume tailoring (flagged as a **future extension**, using
  an OSS/cheap model like Kimi or DeepSeek, once the sourcing pipeline is
  proven).
- No live per-company scraping or curated watchlist — snapshot-only.
- No processing of the full unfiltered 4.8M-row dataset.
- No cloning/forking the ATS scraper adapters themselves.

---

## 14. Open Items / Pending Confirmation

Resolved during implementation:

- ~~Whether X differs per resume~~ — **resolved: no.** One shared
  `EXPERIENCE_YEARS` anchors all 4 resumes (confirmed during Module 2.5).
- ~~Rubric weight confirmation~~ — **resolved: kept as proposed.**
  35/25/25/15 (stack/YOE/seniority/role-type), locked in as
  `RUBRIC_VERSION = "v1"` (Module 14).
- ~~`EXPERIENCE_YEARS`~~ — **resolved: 3.5.**
- ~~Long-shot band visibility~~ — **resolved: excluded before scoring.**
  Module 32 cuts `R > X+4` (the rubric can never rate them 80+), and the
  operator's 5-year cap (§4.6, Module 39) cuts anything asking for 6+.
- ~~Lazy column loading on the parquet read~~ — **largely moot:** the ~10 GB
  case was `workday`, excluded 2026-10-20 (§3.1). `greenhouse` still peaks
  at 2.69 GB, above §12's 1 GB prod target, if that target becomes real.

Still open: see DEVELOPMENT_PLAN.md §23, which tracks the full list.
