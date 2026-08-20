"use strict";

// Column definitions drive both the header and the body, so adding a column
// is one entry here. `sort` picks the value used for ordering (numeric and
// date columns sort by their real type, not their rendered string).
const COLUMNS = [
  { key: "score",            label: "Score",    cls: "score", sort: j => j.score, descFirst: true,
    render: j => scoreCell(j) },
  { key: "band",             label: "Band",     render: j => pill(j.band) },
  { key: "company",          label: "Company",  render: j => `<span class="company">${esc(j.company)}</span>` },
  { key: "title",            label: "Title",    cls: "title wrap" },
  { key: "location",         label: "Location", cls: "wrap loc" },
  { key: "resume_used",      label: "Resume",   cls: "nowrap" },
  { key: "ats_type",         label: "ATS",      cls: "nowrap" },
  { key: "posted_at",        label: "Posted",   cls: "nowrap", render: j => day(j.posted_at), descFirst: true },
  { key: "scored_at",        label: "Scored",   cls: "nowrap", render: j => day(j.scored_at), descFirst: true },
  { key: "provider_used",    label: "Provider" },
  { key: "location_flag",    label: "Loc",      render: j => flag(j.location_flag) },
  { key: "eligibility_flag", label: "Elig",     render: j => flag(j.eligibility_flag) },
  { key: "apply_url",        label: "Apply",    sortable: false,
    render: j => j.apply_url ? `<a href="${esc(j.apply_url)}" target="_blank" rel="noopener">open</a>` : "" },
];

const BAND_RANK = { strong: 3, consider: 2, reject: 1 };

const state = {
  jobs: [],
  metrics: null,
  status: null,
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
const scoreCell = j => `<span class="score-wrap">
  <span class="score-val">${esc(j.score)}</span>
  <span class="score-bar ${esc(j.band)}"><i style="width:${Math.max(0, Math.min(100, j.score))}%"></i></span>
</span>`;
const flag = f => f === "uncertain" ? `<span class="flag-uncertain">uncertain</span>` : esc(f ?? "");
const day = iso => iso ? esc(String(iso).slice(0, 10)) : "";

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
  renderAll();
}

function renderAll() {
  renderStatus();
  renderTiles();
  refreshFilterOptions();
  renderTable();
}

// --- status strip ----------------------------------------------------------

function renderStatus() {
  const d = state.status && state.status.daemon;
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
    item("Digest", d.digest_sent_today ? "sent today" : "due " + when(d.next_digest_at)),
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
  const f = m.funnel || {};
  el.innerHTML = [
    tile("Non-reject", m.total, `${m.strong} strong · ${m.consider} consider`),
    tile("New today", m.new_today, `${m.companies} companies`),
    tile("Median score", m.median_score, `max ${m.max_score ?? "—"} · strong ≥ ${m.thresholds.strong}`),
    tile("Fetched today", f.fetched, `filtered ${f.filtered} · scored ${f.scored} · failed ${f.failed}`,
         f.filtered === 0 && f.fetched > 0),
    tile("Spend today", `$${(m.spend_today_usd ?? 0).toFixed(2)}`,
         (m.spend_today_usd === 0 && (m.providers_used || []).length)
           ? `${m.providers_used.join(", ")} — model has no PRICING entry`
           : (m.providers_used || []).join(", ") || "no calls yet",
         m.spend_today_usd === 0 && (m.providers_used || []).length > 0),
    tile("Unresolved errors", m.unresolved_errors, m.unresolved_errors > 0 ? "check the errors table" : "clean",
         m.unresolved_errors > 0),
    tile("Slices tracked", m.slices_tracked, m.slice_last_processed_at ? `last ${when(m.slice_last_processed_at)}` : "none yet"),
    tile("Digest", m.digest_sent_at ? "sent" : "pending",
         m.digest_sent_at ? `${m.digest_job_count} job(s), ${when(m.digest_sent_at)}` : "not sent today"),
  ].join("");
}

// --- filters ---------------------------------------------------------------

function refreshFilterOptions() {
  for (const [id, key] of [["ats-filter", "ats_type"], ["company-filter", "company"]]) {
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
  const company = document.getElementById("company-filter").value;

  let rows = state.jobs.filter(j =>
    (!band || j.band === band) &&
    (!ats || j.ats_type === ats) &&
    (!company || j.company === company) &&
    (!q || [j.company, j.title, j.location, j.reasoning,
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
    if (c.sortable === false) return `<th data-key="" aria-sort="none">${esc(c.label)}</th>`;
    const active = state.sortKey === c.key;
    const sort = active ? (state.sortDir === 1 ? "ascending" : "descending") : "none";
    const arrow = active ? (state.sortDir === 1 ? "▲" : "▼") : "↕";
    return `<th data-key="${c.key}" aria-sort="${sort}" scope="col" tabindex="0">` +
           `${esc(c.label)}<span class="arrow" aria-hidden="true">${arrow}</span></th>`;
  }).join("");
}

function renderTable() {
  renderHead();
  const rows = visibleJobs();
  document.getElementById("row-count").textContent =
    `${rows.length} of ${state.jobs.length} shown`;

  const empty = document.getElementById("empty");
  empty.hidden = rows.length > 0;
  if (rows.length === 0) {
    empty.innerHTML = state.jobs.length
      ? "<strong>No jobs match these filters.</strong>Try clearing the search or widening the band."
      : "<strong>No scored jobs yet.</strong>The daemon fills this in after its first poll.";
  }

  document.getElementById("body").innerHTML = rows.map(j => {
    const cells = COLUMNS.map(c => {
      const html = c.render ? c.render(j) : esc(j[c.key] ?? "");
      return `<td class="${c.cls || ""}">${html}</td>`;
    }).join("");
    const open = state.expanded.has(j.global_id);
    const main = `<tr class="row${open ? " open" : ""}" data-id="${esc(j.global_id)}" ` +
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

function detailRow(j) {
  const bandNote = j.llm_band && j.llm_band !== j.band
    ? ` <span class="muted">(LLM said “${esc(j.llm_band)}”; band above is from your .env thresholds)</span>`
    : "";
  return `<tr class="detail"><td colspan="${COLUMNS.length}">
    <dl class="detail-grid">
      <dt>Reasoning</dt><dd>${esc(j.reasoning || "—")}${bandNote}</dd>
      <dt>Matched skills</dt><dd>${chips(j.matched_skills, true)}</dd>
      <dt>Missing skills</dt><dd>${chips(j.missing_skills)}</dd>
      <dt>Identity</dt><dd class="mono">${esc(j.global_id)}${j.requisition_id ? " · req " + esc(j.requisition_id) : ""}</dd>
      <dt>First seen</dt><dd class="mono">${esc(j.first_seen_at || "—")}</dd>
    </dl></td></tr>`;
}

// --- CSV -------------------------------------------------------------------

function downloadCsv() {
  const rows = visibleJobs();
  const cols = ["score", "band", "llm_band", "company", "title", "location", "resume_used",
    "ats_type", "posted_at", "scored_at", "provider_used", "location_flag",
    "eligibility_flag", "matched_skills", "missing_skills", "reasoning", "apply_url"];
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
  if (e.target.closest("a")) return;
  const row = e.target.closest("tr.row");
  if (!row) return;
  const id = row.dataset.id;
  state.expanded.has(id) ? state.expanded.delete(id) : state.expanded.add(id);
  renderTable();
});

for (const id of ["search", "band-filter", "ats-filter", "company-filter"]) {
  document.getElementById(id).addEventListener("input", renderTable);
}
document.getElementById("include-reject").addEventListener("change", fetchAll);
document.getElementById("download").addEventListener("click", downloadCsv);

// Live updates: the server pushes only when the data or the daemon state
// actually changed. EventSource reconnects on its own; the interval below is
// the fallback for when SSE can't be established at all.
const dot = document.getElementById("live-dot");
let lastToken = null;

function connect() {
  const source = new EventSource("/api/events");
  source.onopen = () => dot.classList.add("live");
  source.onmessage = e => {
    if (e.data !== lastToken) {
      lastToken = e.data;
      fetchAll();
    }
  };
  source.onerror = () => { dot.classList.remove("live"); dot.classList.add("stale"); };
}

fetchAll().then(connect);
setInterval(() => { if (!dot.classList.contains("live")) fetchAll(); }, 15000);
// Keeps the relative "3m ago" labels honest between data updates.
setInterval(() => { renderStatus(); renderTiles(); }, 30000);
