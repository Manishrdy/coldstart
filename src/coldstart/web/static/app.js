"use strict";

// Column definitions drive both the header and the body, so adding a column
// is one entry here. `sort` picks the value used for ordering (numeric and
// date columns sort by their real type, not their rendered string).
//
// Nine columns, not fourteen. The table used to declare min-width:1120px and
// scroll sideways inside its wrapper, which meant reading one row involved
// dragging the whole view left and right. `col` is the shared class on the
// <th> and every <td>, which is what lets the stylesheet drop a column by
// priority as the viewport narrows. Résumé, Scored, Provider and the two
// flags left the table entirely — they were rarely the thing you were
// looking for, and the expanded row carries every field regardless.
const COLUMNS = [
  { key: "score",       label: "Score",    col: "c-score", sort: j => j.score, descFirst: true,
    render: j => scoreCell(j) },
  { key: "band",        label: "Band",     col: "c-band", render: j => pill(j.band) },
  { key: "company",     label: "Company",  col: "c-company", cls: "wrap",
    render: j => `<span class="company">${esc(j.company)}</span>` },
  { key: "title",       label: "Title",    col: "c-title", cls: "wrap", render: j => titleCell(j) },
  { key: "location",    label: "Location", col: "c-loc", cls: "wrap" },
  { key: "ats_type",    label: "ATS",      col: "c-ats", render: j => atsCell(j.ats_type) },
  { key: "posted_at",   label: "Age",      col: "c-posted", cls: "nowrap",
    sort: j => j.posted_at || j.first_seen_at, descFirst: true,
    render: j => ageCell(j) },
  { key: "apply_url",   label: "Apply",    col: "c-apply", sortable: false,
    render: j => j.apply_url ? `<a href="${esc(j.apply_url)}" target="_blank" rel="noopener">open</a>` : "" },
  { key: "state",       label: "Actions",  col: "c-applied", cls: "applied-cell", sortable: false,
    render: j => markButton(j) + declineButton(j) },
];

// A small, harmonious set rather than one hue per ATS: the bands are what you
// decide on, and nine saturated pills a screen would drown them out. Hashed
// so `ashby` is the same colour tomorrow, whatever order the rows arrive in.
const ATS_HUES = ["--h-teal", "--h-sky", "--h-violet", "--h-lime", "--h-amber", "--h-rose", "--h-slate"];
const hueFor = value => {
  const text = String(value ?? "");
  let h = 7;
  for (let i = 0; i < text.length; i++) h = (h * 31 + text.charCodeAt(i)) >>> 0;
  return ATS_HUES[h % ATS_HUES.length];
};

const atsCell = ats => ats
  ? `<span class="ats" style="--hue:var(${hueFor(ats)})" title="${esc(ats)}">${esc(ats)}</span>` : "";

// The company folds in here when its own column has been dropped on a narrow
// screen — hidden by CSS otherwise, so it is never shown twice.
const titleCell = j =>
  `<span class="title-company">${esc(j.company)}</span>` +
  (isFresh(j) ? `<span class="tag-new">new</span> ` : "") + esc(j.title);

const resumeBadge = id => id
  ? `<span class="rz" title="Résumé ${esc(id)}">${esc(id)}</span>` : "—";

// The label is a separate span so a narrow screen can drop it and leave the
// glyph — with aria-label carrying the meaning, because at that width the
// button has no visible text at all.
const markButton = j => {
  const on = j.state === "applied";
  const label = on ? "Mark as not applied" : "Mark as applied";
  return `<button class="mark" type="button" data-mark="${esc(j.global_id)}" ` +
         `aria-pressed="${on}" aria-label="${label}" title="${label}">` +
         `<span class="mark-glyph" aria-hidden="true">${on ? "✓" : "+"}</span>` +
         `<span class="mark-text">${on ? "Applied" : "Mark applied"}</span></button>`;
};

// Icon-only on purpose — this is a quick "get it off my list" action, not a
// second labelled button competing with markButton for space. Declining
// still just writes to job_state (see db.py), so it's a filter, not a
// delete: the Declined view is where it can be undone.
const declineButton = j => {
  const on = j.state === "declined";
  const label = on ? "Undo decline" : "Decline this job";
  return `<button class="decline" type="button" data-decline="${esc(j.global_id)}" ` +
         `aria-pressed="${on}" aria-label="${label}" title="${label}">` +
         `<span class="decline-glyph" aria-hidden="true">${on ? "↺" : "✕"}</span></button>`;
};

const BAND_RANK = { strong: 2, reject: 1 };

const state = {
  jobs: [],
  // Jobs the location filter held back. Fetched lazily — this list is only
  // ever looked at deliberately, and it is larger than the scored one.
  held: [],
  heldLoaded: false,
  metrics: null,
  status: null,
  view: "open",       // open = no decision made yet (not applied, not declined)
  sortKey: null,      // null = the server's own order (score desc, scored_at desc)
  sortDir: 1,
  expanded: new Set(),

  // How the same rows are arranged. A separate axis from `view` above, which
  // is about *which* rows: the two compose, so "open jobs, by company" is a
  // combination rather than a fourth mode someone had to write.
  group: "none",              // none | company | role
  groupSort: "best",
  // Paths whose open/closed state differs from their default. Storing the
  // exceptions rather than the state is what lets the default flip when you
  // switch axes without touching this set.
  groupToggled: new Set(),
};

// --- rendering helpers -----------------------------------------------------

const esc = s => String(s ?? "").replace(/[&<>"']/g, c =>
  ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));

const pill = b => b ? `<span class="pill ${esc(b)}">${esc(b)}</span>` : "";

// A bar alongside the number: the exact score still matters, but relative
// standing is readable without reading every digit.
const scoreCell = j => j.score == null ? `<span class="muted">&mdash;</span>` : `<span class="score-wrap">
  <span class="score-val">${esc(j.score)}</span>
  <span class="score-bar ${esc(j.band)}"><i style="width:${Math.max(0, Math.min(100, j.score))}%"></i></span>
</span>`;
const flag = f => {
  if (f === "uncertain") return `<span class="flag-uncertain">uncertain</span>`;
  if (f === "excluded") return `<span class="flag-excluded">excluded</span>`;
  return esc(f ?? "—");
};
const day = iso => iso ? `<span class="date">${esc(String(iso).slice(0, 10))}</span>` : "—";

/* How old the posting is, which is the question the bare date was standing in
   for. posted_at is null on roughly half the rows — plenty of boards simply
   do not publish one — so falling back to first_seen_at is the only way this
   column says anything at all for that half. The tilde marks the fallback: an
   age we inferred from when we found it, not one the board stated. Saying
   both with one glyph beats a second column that a narrow window would drop. */
function ageCell(j) {
  const posted = j.posted_at || null;
  const stamp = posted || j.first_seen_at;
  if (!stamp) return `<span class="muted">&mdash;</span>`;
  const age = rel(Math.max(0, (Date.now() - new Date(stamp).getTime()) / 1000));
  const title = posted
    ? `Posted ${String(posted).slice(0, 10)}`
    : `First seen ${String(stamp).slice(0, 10)} — this board does not publish a posting date`;
  return `<span class="date${posted ? "" : " inferred"}" title="${esc(title)}">` +
         `${posted ? "" : "~"}${esc(age)}</span>`;
}

// Whether a job arrived inside the server's current day. Same boundary as the
// Fresh jobs tile, so a row badged "new" is one of the ones that tile counts.
const isFresh = j => {
  const since = windowSince("today");
  return Boolean(since && seenAt(j) >= since);
};

function when(iso) {
  if (!iso) return "never";
  const secs = (Date.now() - new Date(iso).getTime()) / 1000;
  if (secs < 0) return `in ${rel(-secs)}`;
  return `${rel(secs)} ago`;
}
function rel(secs) {
  if (secs < 90) return `${Math.round(secs)}s`;
  if (secs < 5400) return `${Math.round(secs / 60)}m`;
  if (secs < 172800) return `${Math.round(secs / 3600)}h`;
  return `${Math.round(secs / 86400)}d`;
}

// --- data ------------------------------------------------------------------

// Whether the *server* should widen the listing. Derived from the band select
// rather than held separately: "Reject only" and "Incl. reject" are the two
// choices that need rows the default query filters out.
const REJECT_MODES = new Set(["reject", "all"]);
const includeReject = () => REJECT_MODES.has(document.getElementById("band-filter").value);

async function fetchAll() {
  const [jobs, metrics, status] = await Promise.all([
    fetch(`/api/jobs?include_reject=${includeReject()}`).then(r => r.json()),
    fetch("/api/metrics").then(r => r.json()),
    fetch("/api/status").then(r => r.json()),
  ]);
  state.jobs = jobs.jobs;
  state.metrics = metrics;
  state.status = status;
  if (state.heldLoaded) await fetchHeld();
  renderAll();
}

async function fetchHeld() {
  const data = await fetch("/api/jobs/held-back").then(r => r.json());
  state.held = data.jobs;
  state.heldLoaded = true;
}

function renderAll() {
  renderStatus();
  renderTiles();
  renderDigestLine();
  refreshFilterOptions();
  renderTable();
}

// --- status strip ----------------------------------------------------------

const PAUSE_ICON = `<rect x="6" y="4" width="4" height="16" rx="1"/><rect x="14" y="4" width="4" height="16" rx="1"/>`;
const PLAY_ICON = `<path d="M7 4l13 8-13 8V4z"/>`;

// Hidden with no daemon attached — there is nothing to pause when the
// dashboard is showing stored data only.
function renderPollToggle(d) {
  const btn = document.getElementById("poll-toggle");
  if (!d) { btn.hidden = true; return; }
  btn.hidden = false;
  const paused = d.manually_paused;
  btn.setAttribute("aria-pressed", String(paused));
  btn.title = paused
    ? "Job matching is paused — the dashboard keeps working. Click to resume."
    : "Stop new polls from starting. A poll already running finishes on its own.";
  document.getElementById("poll-toggle-label").textContent = paused ? "Resume polling" : "Pause polling";
  document.getElementById("poll-toggle-icon").innerHTML = paused ? PLAY_ICON : PAUSE_ICON;
}

function renderStatus() {
  const d = state.status && state.status.daemon;
  renderPollToggle(d);
  const strip = document.getElementById("status-strip");
  if (!d) {
    strip.innerHTML = `<span class="muted">no daemon attached — showing stored data only</span>`;
    return;
  }
  const item = (k, v) => `<span><span class="k">${k}</span> <b>${v}</b></span>`;
  const parts = [
    item("Activity", esc(d.activity)),
    item("Next poll", when(d.next_poll_at)),
    item("Upstream changed", when(d.last_upstream_change_at)),
    item("Last poll", d.last_poll_exit_code === null ? "—"
         : (d.last_poll_exit_code === 0 ? "ok" : "exit " + d.last_poll_exit_code)),
    // "was it sent" now lives in the top-bar tagline; this is the schedule.
    item("Next digest", when(d.next_digest_at)),
  ];
  if (d.budget_paused_until)
    parts.push(`<span class="alarm">Budget paused until <b>${when(d.budget_paused_until)}</b></span>`);
  if (d.consecutive_poll_failures > 0)
    parts.push(`<span class="alarm"><b>${d.consecutive_poll_failures}</b> failed poll(s)</span>`);
  strip.innerHTML = parts.join("");
}

// --- tiles -----------------------------------------------------------------

function tile(label, value, note, caveat, window) {
  const tag = window ? "button" : "div";
  const attrs = window
    ? ` type="button" data-window="${esc(window)}" title="Show only these"` : "";
  return `<${tag} class="tile${caveat ? " is-warn" : ""}${window ? " tile-action" : ""}"${attrs}>` +
    `<div class="tile-label">${esc(label)}</div>` +
    `<div class="tile-value">${value === null || value === undefined ? "—" : esc(value)}</div>` +
    (note ? `<div class="tile-note${caveat ? " caveat" : ""}">${esc(note)}</div>` : "") + `</${tag}>`;
}

function renderTiles() {
  const m = state.metrics;
  const el = document.getElementById("tiles");
  if (!m || m.db_ready === false) {
    el.innerHTML = tile("Database", "—", "no database yet — run the pipeline once");
    return;
  }
  el.innerHTML = [
    tile("Shortlisted", m.total, `${m.strong} strong`),
    tile("Fresh jobs", m.new_today, `${m.companies} companies`, false, "today"),
    tile("Applied", m.applied ?? 0, m.applied ? "tracked in the Applied view" : "none yet"),
    tile("Median score", m.median_score, `max ${m.max_score ?? "—"} · strong ≥ ${m.thresholds.strong}`),
    tile("Unresolved errors", m.unresolved_errors, m.unresolved_errors > 0 ? "check the errors table" : "clean",
         m.unresolved_errors > 0),
    tile("Slices tracked", m.slices_tracked, m.slice_last_processed_at ? `last ${when(m.slice_last_processed_at)}` : "none yet"),
  ].join("");
}

// The digest moved out of the tiles and into the top bar. It reads email_log
// rather than daemon state, so it is just as true when you are looking at a
// stored database with nothing running.
function renderDigestLine() {
  const m = state.metrics;
  const el = document.getElementById("digest-line");
  if (!m || m.db_ready === false) { el.hidden = true; return; }
  el.hidden = false;
  const icon = `<svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" ` +
    `stroke-width="1.9" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">` +
    `<rect x="2" y="4" width="20" height="16" rx="2"/><path d="m2 7 10 6 10-6"/></svg>`;
  if (!m.digest_sent_at) {
    el.innerHTML = `${icon}<span>no email sent today</span>`;
    el.removeAttribute("title");
    return;
  }
  el.innerHTML = `${icon}<span>last email sent ${esc(when(m.digest_sent_at))}</span>`;
  el.title = `${m.digest_job_count} job(s) at ${m.digest_sent_at}`;
}

// --- filters ---------------------------------------------------------------

function refreshFilterOptions() {
  for (const [id, key] of [["ats-filter", "ats_type"]]) {
    const select = document.getElementById(id);
    const current = select.value;
    const values = [...new Set(state.jobs.map(j => j[key]).filter(Boolean))].sort(
      (a, b) => a.localeCompare(b));
    const first = select.options[0].outerHTML;
    select.innerHTML = first + values.map(v => `<option value="${esc(v)}">${esc(v)}</option>`).join("");
    select.value = values.includes(current) ? current : "";
  }
}

function visibleJobs() {
  const q = document.getElementById("search").value.trim().toLowerCase();
  // "all" widens the fetch but filters nothing client-side.
  const choice = document.getElementById("band-filter").value;
  const band = choice === "all" ? "" : choice;
  const ats = document.getElementById("ats-filter").value;
  const since = windowSince();

  const source = state.view === "held" ? state.held : state.jobs;
  let rows = source.filter(j =>
    (state.view === "held" ? true
      : state.view === "all" ? true
      : state.view === "applied" ? j.state === "applied"
      : state.view === "declined" ? j.state === "declined"
      : !j.state) &&
    (!band || j.band === band) &&
    (!ats || j.ats_type === ats) &&
    // Anchored to the server's own day boundary — see windowSince().
    (!since || (j.scored_at || j.first_seen_at) >= since) &&
    // ats_type and resume_used are searchable now that ATS is a column you
    // can see — typing "ashby" and getting nothing back reads as a bug.
    (!q || [j.company, j.title, j.location, j.reasoning, j.ats_type, j.resume_used,
            j.location_reason,
            (j.matched_skills || []).join(" "), (j.missing_skills || []).join(" ")]
             .join(" ").toLowerCase().includes(q)));

  if (state.sortKey) {
    const col = COLUMNS.find(c => c.key === state.sortKey);
    const value = j => {
      if (col && col.sort) return col.sort(j);
      if (state.sortKey === "band") return BAND_RANK[j.band] || 0;
      return j[state.sortKey];
    };
    rows = rows.slice().sort((a, b) => {
      const x = value(a), y = value(b);
      if (x === y) return 0;
      if (x === null || x === undefined || x === "") return 1;   // blanks always last
      if (y === null || y === undefined || y === "") return -1;
      const cmp = typeof x === "number" && typeof y === "number" ? x - y : String(x).localeCompare(String(y));
      return cmp * state.sortDir;
    });
  }
  return rows;
}

// --- time window -----------------------------------------------------------

// How many calendar days back each option reaches, counting today as 0.
const WINDOW_BACK = { today: 0, "3d": 2, "7d": 6 };

/* The lower bound of the selected window, or null for "any time".
   "Fresh today" returns metrics.today_since *verbatim* — that is the server's
   local midnight in settings.timezone, already converted to UTC, and reusing
   it is what stops this filter and the Fresh jobs tile disagreeing. The wider
   windows shift only the date part and re-attach the server's own time-and-
   offset tail, so the result keeps the exact string shape the job timestamps
   use and the comparison below stays a plain string compare. Going via
   milliseconds would be arithmetically the same but emit a "Z" form, mixing
   two timestamp shapes into that comparison for no gain. */
function windowSince(choice) {
  // Only touch the DOM when the caller has not named a window — this runs
  // once per row via isFresh(), and a getElementById per row is a cost worth
  // not paying.
  const value = choice ?? (document.getElementById("window-filter")?.value ?? "all");
  const base = state.metrics?.today_since;
  if (!base || value === "all") return null;
  const back = WINDOW_BACK[value];
  if (!back) return base;
  const day = new Date(base.slice(0, 10) + "T00:00:00Z");
  day.setUTCDate(day.getUTCDate() - back);
  return day.toISOString().slice(0, 10) + base.slice(10);
}

// --- grouped views ---------------------------------------------------------

/* The company view and the role view are the same code at two depths: company
   is a one-level chain, role is role -> company -> jobs. Writing it once is
   the point — a second renderer would drift from this one the first time a
   badge changed. */

// Six live company names contain "/" (ISS World Services A/S, Cantor
// Fitzgerald/BGC), so the path separator has to be something a company name
// cannot hold. U+001F is the ASCII unit separator, which is exactly this job.
const SEP = "\u001f";

// Grouping only needs case and punctuation folded. Measured against the real
// database, that collapses 18 of 1,725 scored company names (Stripe/stripe,
// Webflow/webflow); the rest are genuinely different employers that merely
// look alike, so anything more aggressive would merge real companies.
const foldCompany = name => String(name ?? "")
  .toLowerCase().replace(/[^a-z0-9]+/g, " ").trim() || "?";

// jobs.resume_used is the routing decision routing.py already made, and it is
// already exactly the four families worth grouping by: FDE+AI -> D, FDE -> C,
// AI -> B, anything else -> A. Short labels here because the manifest's own
// descriptions ("Forward Deployed Engineer, general SWE flavor") are too long
// to sit in a header.
const ROLE_LABELS = { A: "Software Engineer", B: "AI Engineer",
                      C: "FDE — Software", D: "FDE — AI" };
const ROLE_ORDER = { A: 0, B: 1, C: 2, D: 3 };

// NB: `id:` and never `key:`. test_every_column_can_be_dropped_by_priority
// counts `{ key: "..." }` literals in this file to check every table column
// carries a `col` class, and an axis named `key` would be miscounted as a
// tenth column.
const AXES = {
  company: {
    id: "company", unit: "companies",
    groupId: j => foldCompany(j.company),
    displayName: j => (j.company || "").trim() || "Unknown company",
    rollUpSingles: true,
  },
  role: {
    id: "role", unit: "roles",
    groupId: j => j.resume_used || "?",
    displayName: j => ROLE_LABELS[j.resume_used] || "Unclassified",
    rollUpSingles: false,
    // A fixed taxonomy, not a leaderboard: the four families all top out in
    // the high 90s, so ordering them by best score is noise. Keeping them in
    // taxonomy order leaves the sort control to govern the company level,
    // which is where it means something.
    order: g => ROLE_ORDER[g.id] ?? 99,
  },
};

const CHAINS = { none: [], company: ["company"], role: ["role", "company"] };

const grouped = () => state.group !== "none";
const seenAt = j => j.scored_at || j.first_seen_at || "";

// Everything a header needs to be a skip-or-keep decision, in one pass and
// computed identically at every depth.
function summarise(name, id, path, jobs, depth) {
  // "new" always means new *today*, whatever window is selected.
  const since = windowSince("today");
  const mix = { strong: 0, reject: 0 };
  let best = null, band = null, latest = "", fresh = 0, applied = 0, declined = 0;
  for (const j of jobs) {
    if (j.band) mix[j.band]++;
    if (j.score != null && (best === null || j.score > best)) { best = j.score; band = j.band; }
    const at = seenAt(j);
    if (at > latest) latest = at;
    if (since && at >= since) fresh++;
    if (j.state === "applied") applied++;
    else if (j.state === "declined") declined++;
  }
  return { path, id, name, depth, jobs, children: [], count: jobs.length,
           rolled: 0, best, band, mix, fresh, applied, declined, latest };
}

/* Two thirds of the companies have exactly one role. Giving each its own
   header would render a header *and* a row where the flat list rendered one
   row — twice the scrolling, in a feature whose whole purpose is less of it.
   They collapse into a single bucket instead, pinned below the rest. */
const SINGLES_ID = "\u0000singles";

function rollUpSingles(nodes, parentPath, depth) {
  const singles = nodes.filter(g => g.count === 1);
  if (singles.length < 2) return nodes;
  const rest = nodes.filter(g => g.count > 1);
  const node = summarise(`${singles.length} companies with one role`, SINGLES_ID,
                         parentPath + SEP + SINGLES_ID,
                         singles.flatMap(g => g.jobs), depth);
  node.axis = "company";
  node.pinLast = true;
  node.rolled = singles.length;
  // A narrow search can leave every company a singleton. When the bucket is
  // all there is, open it — otherwise the view just looks broken.
  node.forceOpen = rest.length === 0;
  return [...rest, node];
}

function buildLevels(jobs, chain, parentPath, depth) {
  if (!chain.length) return [];
  const [axisName, ...rest] = chain;
  const axis = AXES[axisName];

  // A Map keeps insertion order, so each bucket's jobs arrive already in the
  // active column-sort order — no second sort pass, no state to keep in sync.
  const buckets = new Map();
  for (const j of jobs) {
    const id = axis.groupId(j);
    let bucket = buckets.get(id);
    if (!bucket) buckets.set(id, (bucket = { id, name: axis.displayName(j), jobs: [] }));
    bucket.jobs.push(j);
  }

  let nodes = [...buckets.values()].map(bucket => {
    // The path carries every ancestor, so Stripe under "AI Engineer" and
    // Stripe under "Software Engineer" are different nodes and opening one
    // does not open the other.
    const path = parentPath + SEP + bucket.id;
    const node = summarise(bucket.name, bucket.id, path, bucket.jobs, depth);
    node.axis = axis.id;
    node.children = buildLevels(bucket.jobs, rest, path, depth + 1);
    return node;
  });

  if (axis.rollUpSingles) nodes = rollUpSingles(nodes, parentPath, depth);
  return sortGroups(nodes, axis);
}

const groupTree = rows =>
  buildLevels(rows, CHAINS[state.group] || [], SEP + state.group, 0);

/* Every comparator ends in a name tie-break so that it is *total*. Without
   one, groups tied on score and count reorder on each live refresh — a list
   that reshuffles while you are reading it is the worst thing this view could
   do. `(x ?? -1)` covers the Held back view, where every row has a null score
   and the count tie-break takes over. */
const GROUP_SORTS = {
  best:  (a, b) => (b.best ?? -1) - (a.best ?? -1) || b.count - a.count || a.name.localeCompare(b.name),
  count: (a, b) => b.count - a.count || (b.best ?? -1) - (a.best ?? -1) || a.name.localeCompare(b.name),
  fresh: (a, b) => String(b.latest).localeCompare(String(a.latest)) || b.count - a.count || a.name.localeCompare(b.name),
  name:  (a, b) => a.name.localeCompare(b.name) || b.count - a.count,
};

function sortGroups(nodes, axis) {
  const pinned = nodes.filter(g => g.pinLast);
  const rest = nodes.filter(g => !g.pinLast);
  rest.sort(axis.order
    ? (a, b) => axis.order(a) - axis.order(b)
    : (GROUP_SORTS[state.groupSort] || GROUP_SORTS.best));
  return [...rest, ...pinned];
}

/* Company view opens nothing by default — a few hundred headers are an index
   you drill into. Role view opens its four families but not the companies
   inside them, which reads as a table of contents. Storing only the
   exceptions and comparing against the default is what lets that baseline
   flip when you switch axes without rewriting the set. */
const opensByDefault = g => state.group === "role" && g.depth === 0;
const isGroupOpen = g =>
  g.forceOpen || (opensByDefault(g) !== state.groupToggled.has(g.path));

// A proportional strip rather than three numbers: the mix is the thing you
// read at a glance, and the exact counts ride along in the tooltip.
function bandBar(mix) {
  const seg = b => mix[b] ? `<i class="${b}" style="flex:${mix[b]}"></i>` : "";
  const label = ["strong", "reject"]
    .filter(b => mix[b]).map(b => `${mix[b]} ${b}`).join(" · ");
  return label
    ? `<span class="group-bands" title="${esc(label)}">` +
      `${seg("strong")}${seg("reject")}</span>`
    : "";
}

const canDeclineGroup = g => g.axis === "company" && g.id !== SINGLES_ID;

// Mirrors declineButton's two states: all-declined offers the undo, anything
// else offers the decline. Same glyphs, same aria-pressed contract.
function groupDeclineButton(g) {
  if (!canDeclineGroup(g)) return "";
  const allDeclined = g.declined === g.count;
  const label = allDeclined
    ? `Undo decline for all ${g.count} job(s) at ${g.name}`
    : `Decline all ${g.count} job(s) at ${g.name}`;
  return `<span class="group-decline" role="button" tabindex="0" ` +
         `data-decline-group="${esc(g.path)}" aria-pressed="${allDeclined}" ` +
         `aria-label="${esc(label)}" title="${esc(label)}">` +
         `<span aria-hidden="true">${allDeclined ? "↺" : "✕"}</span></span>`;
}

function groupHeader(g) {
  const open = isGroupOpen(g);
  const done = [g.applied ? `${g.applied} applied` : "",
                g.declined ? `${g.declined} declined` : ""].filter(Boolean).join(" · ");
  const parts = [
    `<span class="group-chev" aria-hidden="true">›</span>`,
    `<span class="group-best">${g.best == null ? "—" : esc(g.best)}</span>`,
    pill(g.band),
    `<span class="group-name">${esc(g.name)}</span>`,
    `<span class="group-count">${g.count}</span>`,
    bandBar(g.mix),
    `<span class="group-gap"></span>`,
    g.fresh ? `<span class="tag-new">${g.fresh} new</span>` : "",
    done ? `<span class="group-done">${esc(done)}</span>` : "",
  ].filter(Boolean).join("");

  // The decline sits outside the <button>, because a control inside a button
  // is not reachable by keyboard and not valid HTML.
  return `<tr class="group" data-gid="${esc(g.path)}">` +
         `<td colspan="${COLUMNS.length}" class="group-cell">` +
         `<div class="group-line">` +
         `<button class="group-head" type="button" style="--depth:${g.depth}" ` +
         `aria-expanded="${open}">${parts}</button>` +
         groupDeclineButton(g) +
         `</div></td></tr>`;
}

// The whole recursion: a header, then either child headers or job rows.
function groupBlock(g) {
  const inner = !isGroupOpen(g) ? ""
    : g.children.length ? g.children.map(groupBlock).join("")
    : g.jobs.map(jobRowHtml).join("");
  return groupHeader(g) + inner;
}

// Walk the current tree for a path. The tree is rebuilt from visibleJobs()
// rather than cached, so this always reflects the filters in force right now
// — declining "all of Accenture" means all of what the filters are showing,
// which is what the header's own count says.
function findGroup(path, nodes = groupTree(visibleJobs())) {
  for (const node of nodes) {
    if (node.path === path) return node;
    if (node.children.length) {
      const hit = findGroup(path, node.children);
      if (hit) return hit;
    }
  }
  return null;
}

/* Declining a company empties it out of whichever view you are in, so the
   header you would click to undo is gone a moment later — unlike the row
   button, this is not one click away from reversible. Hence the confirm, and
   hence it naming where the undo lives. */
async function toggleGroupState(path) {
  const group = findGroup(path);
  if (!group || !canDeclineGroup(group)) return;

  const undo = group.declined === group.count;
  const jobs = group.jobs;
  const next = undo ? null : "declined";
  const verb = undo ? "Restore" : "Decline";
  if (!confirm(
    `${verb} all ${jobs.length} job(s) at ${group.name}?\n\n` +
    (undo ? "They go back to the Open view."
          : "They move to the Declined view, where this can be undone."))) return;

  const previous = new Map(jobs.map(job => [job.global_id, job.state]));
  for (const job of jobs) job.state = next;
  renderAll();

  try {
    const response = await fetch("/api/jobs/state", {
      method: "POST",
      headers: { "Content-Type": "application/json", "X-Coldstart-Action": "1" },
      body: JSON.stringify({ state: next, global_ids: jobs.map(job => job.global_id) }),
    });
    if (!response.ok) throw new Error(await response.text());
    const result = await response.json();
    if (state.metrics) state.metrics.applied = countApplied();
    renderAll();
    if (result.updated !== result.requested) {
      console.warn(`${result.requested} requested, ${result.updated} written —`,
                   "some rows are no longer in the database");
    }
  } catch (err) {
    for (const job of jobs) job.state = previous.get(job.global_id) ?? null;
    renderAll();
    console.error("could not save that:", err);
    alert("Could not save that — the change has been undone. See the console for why.");
  }
}

const countApplied = () => state.jobs.filter(job => job.state === "applied").length;

// Grouped, "1284 of 3359" invites reading 1284 as a group count. Say what the
// groups are as well, counting rolled-up singles individually — they are still
// that many companies, just not that many headers.
function renderRowCount(shown, total, tree) {
  const element = document.getElementById("row-count");
  let label = `<b>${shown}</b>/${total}`;
  if (tree) {
    const groups = tree.reduce((n, g) => n + (g.rolled || 1), 0);
    label += ` · <b>${groups}</b> ${AXES[CHAINS[state.group][0]].unit}`;
  }
  element.innerHTML = label;
  element.title = grouped()
    ? `${shown} of ${total} job(s) shown, in ${AXES[CHAINS[state.group][0]].id} groups. ` +
      "Counts follow every filter in this bar, and the view — Open leaves out " +
      "anything you have already applied to or declined."
    : "Counts follow every filter in this bar, and the view — Open leaves out " +
      "anything you have already applied to or declined.";
}

// --- table -----------------------------------------------------------------

function renderHead() {
  document.getElementById("head-row").innerHTML = COLUMNS.map(c => {
    if (c.sortable === false)
      return `<th class="${c.col}" data-key="" aria-sort="none">${esc(c.label)}</th>`;
    const active = state.sortKey === c.key;
    const sort = active ? (state.sortDir === 1 ? "ascending" : "descending") : "none";
    const arrow = active ? (state.sortDir === 1 ? "▲" : "▼") : "↕";
    return `<th class="${c.col}" data-key="${c.key}" aria-sort="${sort}" scope="col" tabindex="0">` +
           `${esc(c.label)}<span class="arrow" aria-hidden="true">${arrow}</span></th>`;
  }).join("");
}

function renderTable() {
  renderHead();
  const rows = visibleJobs();
  const held = state.view === "held";
  const total = held ? state.held.length : state.jobs.length;
  const tree = grouped() ? groupTree(rows) : null;
  renderRowCount(rows.length, total, tree);

  const empty = document.getElementById("empty");
  empty.hidden = rows.length > 0;
  if (rows.length === 0) {
    empty.innerHTML = total
      ? "<strong>No jobs match these filters.</strong>Try clearing the search or widening the band."
      : held
        ? "<strong>Nothing has been held back yet.</strong>Jobs the location filter could not confirm as US-based land here instead of going to an LLM."
        : "<strong>No scored jobs yet.</strong>The daemon fills this in after its first poll.";
  }

  document.getElementById("body").innerHTML = tree
    ? tree.map(groupBlock).join("")
    : rows.map(jobRowHtml).join("");
}

// One job as its <tr>, plus its detail row when expanded. Extracted from
// renderTable so the grouped views emit the *same* markup rather than a
// parallel copy — that is what keeps expand-for-detail, mark-applied, decline
// and the column-priority CSS working identically inside a group.
function jobRowHtml(j) {
  const cells = COLUMNS.map(c => {
    const html = c.render ? c.render(j) : esc(j[c.key] ?? "");
    return `<td class="${c.col}${c.cls ? " " + c.cls : ""}">${html}</td>`;
  }).join("");
  const open = state.expanded.has(j.global_id);
  const stateClass = j.state === "applied" ? " applied" : j.state === "declined" ? " declined" : "";
  const main = `<tr class="row${open ? " open" : ""}${stateClass}" data-id="${esc(j.global_id)}" ` +
               `aria-expanded="${open}">${cells}</tr>`;
  return open ? main + detailRow(j) : main;
}

function chips(items, hit = false) {
  return (items && items.length)
    ? `<div class="chips">${items.map(s =>
        `<span class="chip${hit ? " hit" : ""}">${esc(s)}</span>`).join("")}</div>`
    : `<span class="muted">none</span>`;
}

/* Everything the nine columns dropped lives here, which is what makes the
   column priority honest: Résumé, ATS, Provider, Scored and both flags never
   appear in the table at all, and Location, Posted, Band and Company drop out
   as the viewport narrows. Nothing is unreachable at any width. */
function detailRow(j) {
  const bandNote = j.llm_band && j.llm_band !== j.band
    ? ` <span class="muted">(the model said “${esc(j.llm_band)}”; the band above is from your .env thresholds)</span>`
    : "";
  const fact = (k, v) => `<span class="fact"><span class="k">${esc(k)}</span> ${v}</span>`;
  return `<tr class="detail"><td colspan="${COLUMNS.length}">
    <dl class="detail-grid">
      <dt>Reasoning</dt><dd>${esc(j.reasoning || "—")}${bandNote}</dd>
      <dt>Matched skills</dt><dd>${chips(j.matched_skills, true)}</dd>
      <dt>Missing skills</dt><dd>${chips(j.missing_skills)}</dd>
      <dt>Signals</dt><dd><div class="facts">
        ${fact("Location", flag(j.location_flag))}
        ${fact("Why", esc(j.location_reason || "—"))}
        ${fact("Eligibility", flag(j.eligibility_flag))}
        ${fact("Résumé", resumeBadge(j.resume_used))}
        ${fact("ATS", atsCell(j.ats_type) || "—")}
        ${fact("Provider", esc(j.provider_used || "—"))}
        ${fact("Posted", day(j.posted_at))}
        ${fact("Scored", day(j.scored_at))}
        ${fact("Where", esc(j.location || "—"))}
      </div></dd>
      <dt>Identity</dt><dd class="mono">${esc(j.global_id)}${j.requisition_id ? " · req " + esc(j.requisition_id) : ""}</dd>
      <dt>First seen</dt><dd class="mono">${esc(j.first_seen_at || "—")}</dd>
    </dl></td></tr>`;
}

// --- CSV -------------------------------------------------------------------

// Grouped, export in the order the groups put them in, so the file matches
// what you were looking at. Collapsed groups still export — this is an export
// of the filter, not of the viewport.
const flattenTree = nodes =>
  nodes.flatMap(g => (g.children.length ? flattenTree(g.children) : g.jobs));

function downloadCsv() {
  const visible = visibleJobs();
  const rows = grouped() ? flattenTree(groupTree(visible)) : visible;
  const cols = ["score", "band", "llm_band", "company", "title", "location", "resume_used",
    "ats_type", "posted_at", "scored_at", "provider_used", "location_flag",
    "location_reason", "eligibility_flag", "matched_skills", "missing_skills",
    "reasoning", "apply_url"];
  const cell = v => `"${String(Array.isArray(v) ? v.join("; ") : (v ?? "")).replace(/"/g, '""')}"`;
  const csv = [cols.join(","), ...rows.map(j => cols.map(c => cell(j[c])).join(","))].join("\n");

  const url = URL.createObjectURL(new Blob(["﻿" + csv], { type: "text/csv;charset=utf-8" }));
  const a = document.createElement("a");
  a.href = url;
  a.download = `coldstart_dashboard_${new Date().toISOString().slice(0, 10)}.csv`;
  a.click();
  URL.revokeObjectURL(url);
}

// --- wiring ----------------------------------------------------------------

function sortByHeader(th) {
  if (!th || !th.dataset.key) return;
  const column = COLUMNS.find(c => c.key === th.dataset.key);
  const first = column && column.descFirst ? -1 : 1;
  if (state.sortKey !== th.dataset.key) {
    state.sortKey = th.dataset.key;
    state.sortDir = first;
  } else if (state.sortDir === first) {
    state.sortDir = -first;
  } else {
    state.sortKey = null;                                  // third click: back to default
    state.sortDir = 1;
  }
  renderTable();
  const again = document.querySelector(`th[data-key="${th.dataset.key}"]`);
  if (again) again.focus();
}

const headRow = document.getElementById("head-row");
headRow.addEventListener("click", e => sortByHeader(e.target.closest("th")));
document.getElementById("body").addEventListener("keydown", e => {
  const declineGroup = e.target.closest("[data-decline-group]");
  if (!declineGroup || (e.key !== "Enter" && e.key !== " ")) return;
  e.preventDefault();
  e.stopPropagation();
  toggleGroupState(declineGroup.dataset.declineGroup);
});

headRow.addEventListener("keydown", e => {
  if (e.key === "Enter" || e.key === " ") { e.preventDefault(); sortByHeader(e.target.closest("th")); }
});

document.getElementById("body").addEventListener("click", e => {
  // Before the row branch: a header is a <button>, so it is keyboard- and
  // screen-reader-reachable without any extra handling here.
  const declineGroup = e.target.closest("[data-decline-group]");
  if (declineGroup) {
    e.stopPropagation();
    toggleGroupState(declineGroup.dataset.declineGroup);
    return;
  }
  const head = e.target.closest("tr.group");
  if (head) {
    const path = head.dataset.gid;
    state.groupToggled.has(path)
      ? state.groupToggled.delete(path)
      : state.groupToggled.add(path);
    renderTable();
    // Same refocus as sortByHeader: the element was destroyed by the
    // re-render, so put the caret back where the user left it.
    const again = document.querySelector(`tr.group[data-gid="${CSS.escape(path)}"] .group-head`);
    if (again) again.focus();
    return;
  }
  const mark = e.target.closest("[data-mark]");
  if (mark) { e.stopPropagation(); toggleJobState(mark, "applied"); return; }
  const decline = e.target.closest("[data-decline]");
  if (decline) { e.stopPropagation(); toggleJobState(decline, "declined"); return; }
  if (e.target.closest("a")) return;
  const row = e.target.closest("tr.row");
  if (!row) return;
  const id = row.dataset.id;
  state.expanded.has(id) ? state.expanded.delete(id) : state.expanded.add(id);
  renderTable();
});

// Shared by the mark-applied and decline buttons: `job_state` holds one state
// per job (see db.py), so pressing either toggles it against that single
// column — clicking "decline" on an applied job replaces the applied mark,
// rather than stacking a second state on top of it.
async function toggleJobState(button, targetState) {
  const id = button.dataset.mark || button.dataset.decline;
  const job = state.jobs.find(j => j.global_id === id);
  if (!job) return;
  const next = job.state === targetState ? null : targetState;
  const previous = job.state;

  // Optimistic: the row moves immediately, and snaps back if the write fails.
  job.state = next;
  button.disabled = true;
  renderTable();
  renderTiles();

  try {
    const response = await fetch(`/api/jobs/${encodeURIComponent(id)}/state`, {
      method: "POST",
      headers: { "Content-Type": "application/json", "X-Coldstart-Action": "1" },
      body: JSON.stringify({ state: next }),
    });
    if (!response.ok) throw new Error(await response.text());
    if (state.metrics) {
      if (previous === "applied") state.metrics.applied = (state.metrics.applied || 0) - 1;
      if (next === "applied") state.metrics.applied = (state.metrics.applied || 0) + 1;
    }
    renderTiles();
  } catch (err) {
    job.state = previous;
    renderTable();
    renderTiles();
    console.error("could not save that:", err);
    alert("Could not save that — the change has been undone. See the console for why.");
  }
}

async function togglePolling() {
  const btn = document.getElementById("poll-toggle");
  const d = state.status && state.status.daemon;
  if (!d) return;
  const endpoint = d.manually_paused ? "resume" : "pause";
  btn.disabled = true;
  try {
    const response = await fetch(`/api/daemon/${endpoint}`, {
      method: "POST",
      headers: { "X-Coldstart-Action": "1" },
    });
    if (!response.ok) throw new Error(await response.text());
    state.status = await fetch("/api/status").then(r => r.json());
    renderStatus();
  } catch (err) {
    console.error("could not change polling state:", err);
    alert("Could not change that — see the console for why.");
  } finally {
    btn.disabled = false;
  }
}
document.getElementById("poll-toggle").addEventListener("click", togglePolling);

document.getElementById("view-seg").addEventListener("click", async e => {
  const button = e.target.closest("[data-view]");
  if (!button) return;
  state.view = button.dataset.view;
  for (const b of document.querySelectorAll("[data-view]")) {
    b.setAttribute("aria-pressed", String(b === button));
  }
  if (state.view === "held" && !state.heldLoaded) {
    try {
      await fetchHeld();
    } catch (err) {
      console.error("could not load held-back jobs:", err);
    }
  }
  syncGroupControls();
  renderTable();
});

document.getElementById("group-seg").addEventListener("click", e => {
  const button = e.target.closest("[data-group]");
  if (!button) return;
  setGroup(button.dataset.group);
  savePrefs();
  renderTable();
});

// Held-back rows never reached the router or the scorer, so resume_used,
// score and band are all null on every one of them: grouping those by role
// would produce a single "Unclassified" pile. The button goes away rather
// than producing an empty answer.
function syncGroupControls() {
  const roleOff = state.view === "held";
  if (roleOff && state.group === "role") state.group = "company";
  for (const button of document.querySelectorAll("#group-seg [data-group]")) {
    button.setAttribute("aria-pressed", String(button.dataset.group === state.group));
    if (button.dataset.group === "role") {
      button.disabled = roleOff;
      button.title = roleOff
        ? "Held-back jobs were never routed to a résumé, so there is no role to group them by"
        : "";
    }
  }
  // The group order only means something once there are groups to order.
  document.getElementById("group-sort").hidden = !grouped();
}

function setGroup(name) {
  state.group = CHAINS[name] ? name : "none";
  syncGroupControls();
}

document.getElementById("group-sort").addEventListener("change", e => {
  state.groupSort = e.target.value;
  savePrefs();
  renderTable();
});

// Typed input, unlike the discrete selects: visibleJobs() rebuilds a joined,
// lowercased blob of company + title + location + full reasoning + both skill
// lists for every row on each keystroke, and grouping adds a pass on top.
let searchTimer;
document.getElementById("search").addEventListener("input", () => {
  clearTimeout(searchTimer);
  searchTimer = setTimeout(renderTable, 150);
});

document.getElementById("ats-filter").addEventListener("input", renderTable);

// Only the two reject choices change what the server sends, so only crossing
// that line costs a round trip; the rest filter what is already here.
let servedRejects = false;
document.getElementById("band-filter").addEventListener("change", () => {
  if (includeReject() !== servedRejects) {
    servedRejects = includeReject();
    fetchAll();
  } else {
    renderTable();
  }
});
document.getElementById("window-filter").addEventListener("change", () => {
  savePrefs();
  renderTable();
});
document.getElementById("download").addEventListener("click", downloadCsv);

// The Fresh jobs tile counts non-reject jobs only, so clicking it also clears
// "include reject band" — otherwise the list it opens would not match the
// number that was clicked.
document.getElementById("tiles").addEventListener("click", e => {
  const tile = e.target.closest("[data-window]");
  if (!tile) return;
  document.getElementById("window-filter").value = tile.dataset.window;
  savePrefs();
  const band = document.getElementById("band-filter");
  if (REJECT_MODES.has(band.value)) {
    band.value = "";
    servedRejects = false;
    fetchAll();
  } else {
    renderTable();
  }
});

// --- preferences -----------------------------------------------------------

const PREFS_KEY = "coldstart-view";

/* One try/catch around the whole read, and every value checked against the
   map that will consume it. This file is a single classic script: an uncaught
   throw here would abort the rest of it and take every addEventListener below
   with it, leaving a page that renders once and then ignores every click. A
   stale or hand-edited preference has to degrade to the default instead. */
function loadPrefs() {
  try {
    const saved = JSON.parse(localStorage.getItem(PREFS_KEY) || "{}");
    if (CHAINS[saved.group]) state.group = saved.group;
    if (GROUP_SORTS[saved.groupSort]) state.groupSort = saved.groupSort;
    const window = document.getElementById("window-filter");
    if (saved.window === "all" || saved.window in WINDOW_BACK) window.value = saved.window;
    document.getElementById("group-sort").value = state.groupSort;
  } catch (err) {
    console.warn("ignoring unreadable view preferences:", err);
  }
  syncGroupControls();
}

function savePrefs() {
  try {
    localStorage.setItem(PREFS_KEY, JSON.stringify({
      group: state.group,
      groupSort: state.groupSort,
      window: document.getElementById("window-filter").value,
    }));
  } catch (err) {
    console.warn("could not save view preferences:", err);
  }
}

// Live updates: the server pushes only when the data or the daemon state
// actually changed. EventSource reconnects on its own; the interval below is
// the fallback for when SSE can't be established at all.
const dot = document.getElementById("live-dot");
let lastToken = null;

function setLive(status) {
  dot.classList.toggle("live", status === "live");
  dot.classList.toggle("stale", status === "stale");
  dot.setAttribute("aria-label", {
    live: "Live — receiving updates",
    stale: "Stale — the update stream dropped, polling instead",
  }[status] || "Offline");
}

function connect() {
  const source = new EventSource("/api/events");
  source.onopen = () => setLive("live");
  source.onmessage = e => {
    if (e.data !== lastToken) {
      lastToken = e.data;
      fetchAll();
    }
  };
  source.onerror = () => setLive("stale");
}

loadPrefs();
fetchAll().then(connect);
setInterval(() => { if (!dot.classList.contains("live")) fetchAll(); }, 15000);
// Keeps the relative "3m ago" labels honest between data updates.
setInterval(() => { renderStatus(); renderTiles(); renderDigestLine(); }, 30000);
