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
  { key: "posted_at",   label: "Posted",   col: "c-posted", cls: "nowrap",
    render: j => day(j.posted_at), descFirst: true },
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
  `<span class="title-company">${esc(j.company)}</span>${esc(j.title)}`;

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

const BAND_RANK = { strong: 3, consider: 2, reject: 1 };

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

const includeReject = () => document.getElementById("include-reject").checked;

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

function tile(label, value, note, caveat) {
  return `<div class="tile${caveat ? " is-warn" : ""}">` +
    `<div class="tile-label">${esc(label)}</div>` +
    `<div class="tile-value">${value === null || value === undefined ? "—" : esc(value)}</div>` +
    (note ? `<div class="tile-note${caveat ? " caveat" : ""}">${esc(note)}</div>` : "") + `</div>`;
}

function renderTiles() {
  const m = state.metrics;
  const el = document.getElementById("tiles");
  if (!m || m.db_ready === false) {
    el.innerHTML = tile("Database", "—", "no database yet — run the pipeline once");
    return;
  }
  el.innerHTML = [
    tile("Shortlisted", m.total, `${m.strong} strong · ${m.consider} consider`),
    tile("Fresh jobs", m.new_today, `${m.companies} companies`),
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
  const band = document.getElementById("band-filter").value;
  const ats = document.getElementById("ats-filter").value;
  const freshOnly = document.getElementById("fresh-only").checked;
  const todaySince = state.metrics?.today_since;

  const source = state.view === "held" ? state.held : state.jobs;
  let rows = source.filter(j =>
    (state.view === "held" ? true
      : state.view === "all" ? true
      : state.view === "applied" ? j.state === "applied"
      : state.view === "declined" ? j.state === "declined"
      : !j.state) &&
    (!band || j.band === band) &&
    (!ats || j.ats_type === ats) &&
    // Matches the "Fresh jobs" tile exactly — same today_since boundary
    // from the metrics endpoint, not the browser's own notion of midnight.
    (!freshOnly || (todaySince && (j.scored_at || j.first_seen_at) >= todaySince)) &&
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
  document.getElementById("row-count").innerHTML = `<b>${rows.length}</b> of ${total}`;

  const empty = document.getElementById("empty");
  empty.hidden = rows.length > 0;
  if (rows.length === 0) {
    empty.innerHTML = total
      ? "<strong>No jobs match these filters.</strong>Try clearing the search or widening the band."
      : held
        ? "<strong>Nothing has been held back yet.</strong>Jobs the location filter could not confirm as US-based land here instead of going to an LLM."
        : "<strong>No scored jobs yet.</strong>The daemon fills this in after its first poll.";
  }

  document.getElementById("body").innerHTML = rows.map(j => {
    const cells = COLUMNS.map(c => {
      const html = c.render ? c.render(j) : esc(j[c.key] ?? "");
      return `<td class="${c.col}${c.cls ? " " + c.cls : ""}">${html}</td>`;
    }).join("");
    const open = state.expanded.has(j.global_id);
    const stateClass = j.state === "applied" ? " applied" : j.state === "declined" ? " declined" : "";
    const main = `<tr class="row${open ? " open" : ""}${stateClass}" data-id="${esc(j.global_id)}" ` +
                 `aria-expanded="${open}">${cells}</tr>`;
    return open ? main + detailRow(j) : main;
  }).join("");
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

function downloadCsv() {
  const rows = visibleJobs();
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
headRow.addEventListener("keydown", e => {
  if (e.key === "Enter" || e.key === " ") { e.preventDefault(); sortByHeader(e.target.closest("th")); }
});

document.getElementById("body").addEventListener("click", e => {
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
  renderTable();
});

for (const id of ["search", "band-filter", "ats-filter"]) {
  document.getElementById(id).addEventListener("input", renderTable);
}
document.getElementById("fresh-only").addEventListener("change", renderTable);
document.getElementById("include-reject").addEventListener("change", fetchAll);
document.getElementById("download").addEventListener("click", downloadCsv);

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

fetchAll().then(connect);
setInterval(() => { if (!dot.classList.contains("live")) fetchAll(); }, 15000);
// Keeps the relative "3m ago" labels honest between data updates.
setInterval(() => { renderStatus(); renderTiles(); renderDigestLine(); }, 30000);
