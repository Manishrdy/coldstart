"use strict";

/* Coldstart analytics page.
 *
 * One fetch of /api/analytics fills the whole page. That is deliberate: every
 * figure here has to describe the same instant, and a dozen endpoints racing
 * each other would put a spend total from one second next to a scored count
 * from the next.
 *
 * Charts are hand-drawn SVG. There is no build step and no CDN request at
 * render time, so a chart library would cost more than the eighty lines of
 * <rect> below. Every colour comes from the CSS custom properties styles.css
 * already defines, which is what makes them theme-aware for free — an SVG
 * fill of `var(--h-teal)` follows the light/dark switch with no JS at all.
 *
 * The small formatting helpers are re-declared rather than shared with
 * app.js: both files are plain scripts in the global scope, so a shared file
 * would have to be loaded by both and would then collide with app.js's own
 * `const` declarations. Twenty duplicated lines beat a module system on a
 * page with no bundler.
 */

// --- formatting ------------------------------------------------------------

const esc = s => String(s ?? "").replace(/[&<>"']/g, c =>
  ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));

const num = n => (n === null || n === undefined || Number.isNaN(n))
  ? "—" : Number(n).toLocaleString();

// Tokens run to eight figures; a raw 9,692,917 in a 158px tile is unreadable.
const compact = n => {
  if (n === null || n === undefined) return "—";
  const value = Number(n);
  if (Math.abs(value) >= 1e9) return (value / 1e9).toFixed(1).replace(/\.0$/, "") + "B";
  if (Math.abs(value) >= 1e6) return (value / 1e6).toFixed(1).replace(/\.0$/, "") + "M";
  if (Math.abs(value) >= 10e3) return Math.round(value / 1e3) + "k";
  return value.toLocaleString();
};

const bytes = n => {
  if (n === null || n === undefined) return "—";
  const units = ["B", "KB", "MB", "GB", "TB"];
  let value = Number(n), unit = 0;
  while (value >= 1024 && unit < units.length - 1) { value /= 1024; unit++; }
  return `${value < 10 && unit > 0 ? value.toFixed(1) : Math.round(value)} ${units[unit]}`;
};

const usd = n => (n === null || n === undefined) ? "—" : `$${Number(n).toFixed(2)}`;
const pct = n => (n === null || n === undefined) ? "—" : `${Number(n).toFixed(1)}%`;

function rel(secs) {
  if (secs < 90) return `${Math.round(secs)}s`;
  if (secs < 5400) return `${Math.round(secs / 60)}m`;
  if (secs < 172800) return `${Math.round(secs / 3600)}h`;
  return `${Math.round(secs / 86400)}d`;
}
function when(iso) {
  if (!iso) return "never";
  const secs = (Date.now() - new Date(iso).getTime()) / 1000;
  return secs < 0 ? `in ${rel(-secs)}` : `${rel(secs)} ago`;
}
function stamp(iso) {
  if (!iso) return "—";
  const d = new Date(iso);
  return Number.isNaN(d.getTime()) ? String(iso) : d.toLocaleString();
}
function duration(secs) {
  if (secs === null || secs === undefined) return "—";
  if (secs < 60) return `${Math.round(secs)}s`;
  if (secs < 3600) return `${Math.floor(secs / 60)}m ${Math.round(secs % 60)}s`;
  return `${Math.floor(secs / 3600)}h ${Math.round((secs % 3600) / 60)}m`;
}
const shortDay = day => (day || "").slice(5);          // 2026-08-24 -> 08-24
const shortHour = hour => (hour || "").slice(-2) + "h";

// --- chart primitives ------------------------------------------------------

/* A fixed viewBox scaled to 100% width. Text scales with the bars, which
   keeps the proportions right at every breakpoint without a resize handler. */
const CHART_W = 720;

/**
 * Stacked column chart.
 * points: [{ label, tip, parts: [{ value, color }] }]
 */
function columnChart(points, opts = {}) {
  const height = opts.height || 150;
  const padTop = 8, padBottom = 2, padSide = 2;
  const plot = height - padTop - padBottom;
  const max = Math.max(1, ...points.map(p => p.parts.reduce((sum, part) => sum + part.value, 0)));
  const step = (CHART_W - padSide * 2) / Math.max(points.length, 1);
  const barWidth = Math.max(2, step * 0.72);
  const every = opts.labelEvery || Math.ceil(points.length / 10);

  const bars = points.map((point, index) => {
    const x = padSide + index * step + (step - barWidth) / 2;
    let y = padTop + plot;
    const total = point.parts.reduce((sum, part) => sum + part.value, 0);
    const rects = point.parts.filter(part => part.value > 0).map(part => {
      const h = (part.value / max) * plot;
      y -= h;
      return `<rect class="bar" x="${x.toFixed(1)}" y="${y.toFixed(1)}" ` +
             `width="${barWidth.toFixed(1)}" height="${h.toFixed(1)}" ` +
             `fill="var(${part.color})" rx="1.5"/>`;
    }).join("");
    // Zero days still get a hairline, so a gap reads as "nothing happened"
    // rather than as a missing bar.
    const floor = total === 0
      ? `<rect x="${x.toFixed(1)}" y="${(padTop + plot - 1).toFixed(1)}" ` +
        `width="${barWidth.toFixed(1)}" height="1" fill="var(--border)"/>` : "";
    return `<g><title>${esc(point.tip || `${point.label}: ${total}`)}</title>${floor}${rects}</g>`;
  }).join("");

  // Labels live in HTML below the plot, not in <text> inside it: the SVG is
  // stretched to the card's width with preserveAspectRatio="none", which would
  // stretch any glyph drawn inside it by the same factor.
  const labels = `<div class="chart-labels">${points.map((point, index) =>
    `<span>${index % every === 0 ? esc(point.label) : ""}</span>`).join("")}</div>`;

  return `<svg class="chart" viewBox="0 0 ${CHART_W} ${height}" style="height:${height}px"
       role="img" aria-label="${esc(opts.title || "chart")}" preserveAspectRatio="none">
    <line class="zero" x1="0" y1="${padTop + plot}" x2="${CHART_W}" y2="${padTop + plot}"/>
    ${bars}
  </svg>${labels}`;
}

function legend(items) {
  return `<div class="legend">${items.map(item =>
    `<span><i style="background:var(${item.color})"></i>${esc(item.label)}</span>`).join("")}</div>`;
}

/** Ranked horizontal bars. `rows` is [{label, count, note}]. */
function hbars(rows, opts = {}) {
  if (!rows || !rows.length) return `<p class="empty-note">${esc(opts.empty || "Nothing yet.")}</p>`;
  const max = Math.max(1, ...rows.map(row => row.count));
  const color = opts.color || "--h-teal";
  return `<div class="hbars">${rows.map(row => `
    <div class="hbar" style="--hue:var(${color})">
      <span class="hbar-label" title="${esc(row.label)}">${esc(row.label)}</span>
      <span class="hbar-count">${num(row.count)}${row.note ? ` <span class="muted">${esc(row.note)}</span>` : ""}</span>
      <span class="hbar-track"><i style="width:${(row.count / max * 100).toFixed(1)}%"></i></span>
    </div>`).join("")}</div>`;
}

/** Funnel stages as full-width bars, so a drop is a length not a division. */
function funnelBars(stages) {
  const max = Math.max(1, ...stages.map(stage => stage.count));
  return `<div class="funnel">${stages.map(stage => `
    <div class="funnel-row" style="--hue:var(${stage.color || "--h-teal"})">
      <span class="funnel-label">${esc(stage.stage)}</span>
      <span class="funnel-track"><i style="width:${(stage.count / max * 100).toFixed(1)}%"></i></span>
      <span class="funnel-value">${num(stage.count)}</span>
    </div>`).join("")}</div>`;
}

/** Donut. `slices` is [{label, value, color}]. */
function donut(slices, opts = {}) {
  const total = slices.reduce((sum, slice) => sum + slice.value, 0);
  const size = opts.size || 148, r = size / 2 - 14, c = 2 * Math.PI * r;
  let offset = 0;
  const rings = slices.filter(slice => slice.value > 0).map(slice => {
    const portion = total ? slice.value / total : 0;
    const ring = `<circle cx="${size / 2}" cy="${size / 2}" r="${r}" fill="none"
      stroke="var(${slice.color})" stroke-width="15"
      stroke-dasharray="${(portion * c).toFixed(2)} ${c.toFixed(2)}"
      stroke-dashoffset="${(-offset * c).toFixed(2)}"
      transform="rotate(-90 ${size / 2} ${size / 2})"><title>${esc(slice.label)}: ${num(slice.value)}</title></circle>`;
    offset += portion;
    return ring;
  }).join("");
  return `<svg class="chart" viewBox="0 0 ${size} ${size}" style="height:${size}px;width:auto;margin:0 auto"
       role="img" aria-label="${esc(opts.title || "breakdown")}">
    <circle cx="${size / 2}" cy="${size / 2}" r="${r}" fill="none" stroke="var(--surface-3)" stroke-width="15"/>
    ${rings}
    <text x="${size / 2}" y="${size / 2 - 2}" text-anchor="middle"
          style="font:700 22px var(--font);fill:var(--text)">${compact(total)}</text>
    <text x="${size / 2}" y="${size / 2 + 15}" text-anchor="middle"
          style="font:600 8px var(--font);letter-spacing:.14em;fill:var(--text-3)">
      ${esc((opts.caption || "TOTAL").toUpperCase())}</text>
  </svg>`;
}

function kpi(label, value, note, tone) {
  return `<div class="kpi${tone ? " " + tone : ""}">
    <div class="kpi-label" title="${esc(label)}">${esc(label)}</div>
    <div class="kpi-value">${value === null || value === undefined ? "—" : esc(String(value))}</div>
    ${note ? `<div class="kpi-note">${note}</div>` : ""}
  </div>`;
}

function card(title, sub, body, cls = "") {
  return `<div class="card ${cls}">
    <h3>${esc(title)}</h3>
    ${sub ? `<p class="card-sub">${esc(sub)}</p>` : ""}
    ${body}
  </div>`;
}

function factlet(label, value, small) {
  return `<div class="factlet"><span class="k">${esc(label)}</span>
    <span class="v${small ? " small" : ""}">${value}</span></div>`;
}

function table(columns, rows, opts = {}) {
  if (!rows.length) return `<p class="empty-note">${esc(opts.empty || "Nothing recorded yet.")}</p>`;
  const head = columns.map(col =>
    `<th class="${col.numeric ? "n" : ""}">${esc(col.label)}</th>`).join("");
  const body = rows.map(row => `<tr>${columns.map(col =>
    `<td class="${col.numeric ? "n" : ""}${col.wrap ? " wrap" : ""}">${col.render(row)}</td>`
  ).join("")}</tr>`).join("");
  return `<div class="dtable-wrap"><table class="dtable">
    <thead><tr>${head}</tr></thead><tbody>${body}</tbody></table></div>`;
}

// --- state -----------------------------------------------------------------

const state = {
  data: null,
  sourceSort: { key: "jobs", dir: -1 },
  showExcludedSources: false,
};

// --- 1. right now ----------------------------------------------------------

const PHASE_TEXT = {
  preparing: "Loading résumés and checking config",
  checking_upstream: "Asking the snapshot server what changed",
  queued: "Work queued",
  downloading: "Downloading the snapshot",
  reading: "Reading the snapshot",
  filtering: "Filtering titles, dates and locations",
  scoring: "Scoring against your résumés",
  exporting: "Writing the CSV export",
  done: "Finished",
};

// Downloading and reading a 476 MB parquet has no meaningful percentage —
// showing a bar creeping through 3% for ten minutes is worse than showing
// motion and saying what it's doing.
const INDETERMINATE = new Set(["preparing", "checking_upstream", "downloading", "reading", "filtering"]);

function renderLive() {
  const data = state.data;
  const daemon = data.daemon;
  const live = data.live;
  const progress = live.progress;
  const running = progress && progress.is_running;

  let headline, chip, bar = "", detail = "";

  if (running) {
    const phase = PHASE_TEXT[progress.phase] || progress.phase;
    const indeterminate = INDETERMINATE.has(progress.phase);
    const done = progress.slice_candidates
      ? Math.min(100, progress.slice_processed / progress.slice_candidates * 100) : 0;
    headline = progress.ats_type
      ? `<span class="mono">${esc(progress.ats_type)}</span> · ${esc(phase)}`
      : esc(phase);
    chip = `<span class="state-chip on">Running</span>`;
    bar = `<div class="progress${indeterminate ? " indeterminate" : ""}">
      <i style="width:${done.toFixed(1)}%"></i></div>`;
    detail = `<div class="facts-row">
      ${factlet("Source", progress.slice_total
        ? `${progress.slice_index} of ${progress.slice_total}` : "—")}
      ${factlet("Snapshot rows", compact(progress.slice_rows))}
      ${factlet("Survived filters", progress.slice_candidates
        ? `${num(progress.slice_processed)} / ${num(progress.slice_candidates)}` : "—")}
      ${factlet("Scored this run", num(progress.scored))}
      ${factlet("Held back", num(progress.location_excluded))}
      ${factlet("Delisted", num(progress.delisted))}
      ${factlet("Failed", num(progress.failed))}
      ${factlet("Running for", duration((Date.now() - new Date(progress.started_at).getTime()) / 1000))}
      ${factlet("Heartbeat", `${progress.age_seconds}s ago`, true)}
    </div>`;
  } else if (progress && progress.was_killed) {
    headline = `Last run stopped without finishing`;
    chip = `<span class="state-chip bad">Interrupted</span>`;
    detail = `<div class="facts-row">
      ${factlet("Stopped at", progress.ats_type ? esc(progress.ats_type) : "—")}
      ${factlet("Last heartbeat", when(progress.updated_at))}
      ${factlet("Scored before stopping", num(progress.scored))}
      ${factlet("Started", stamp(progress.started_at), true)}
    </div>
    <div class="note"><span>The poll process ended without recording a finish — a timeout kill,
      or the machine going down mid-source. Anything it had already scored is saved;
      the unprocessed sources stay in the queue below.</span></div>`;
  } else if (daemon) {
    headline = daemon.manually_paused ? "Polling is paused" : "Idle — waiting for the next poll";
    chip = daemon.manually_paused
      ? `<span class="state-chip warn">Paused</span>`
      : `<span class="state-chip">Idle</span>`;
    detail = `<div class="facts-row">
      ${factlet("Last poll finished", when(daemon.last_poll_finished_at))}
      ${factlet("Last result", daemon.last_poll_exit_code === null ? "—"
        : daemon.last_poll_exit_code === 0 ? "ok" : `exit ${daemon.last_poll_exit_code}`)}
      ${factlet("Last run scored", progress ? num(progress.scored) : "—")}
      ${factlet("Summary", esc(daemon.last_poll_summary || "—"), true)}
    </div>`;
  } else {
    headline = "No daemon attached";
    chip = `<span class="state-chip off">Stopped</span>`;
    detail = `<p class="card-sub">Showing stored data only — start the daemon to see live progress.</p>`;
  }

  const activity = `<div class="card live-card${running ? " is-running" : ""}">
    <div class="live-head"><span class="live-title">${headline}</span>${chip}</div>
    ${bar}${detail}
  </div>`;

  const schedule = daemon ? `<div class="facts-row">
      ${factlet("Next poll", when(daemon.next_poll_at))}
      ${factlet("Next digest", when(daemon.next_digest_at))}
      ${factlet("Next liveness sweep", when(daemon.next_liveness_sweep_at))}
      ${factlet("Upstream last checked", when(daemon.last_upstream_check_at))}
      ${factlet("Upstream last changed", when(daemon.last_upstream_change_at))}
      ${factlet("Daemon up", duration((Date.now() - new Date(daemon.started_at).getTime()) / 1000))}
      ${factlet("Failed polls in a row", num(daemon.consecutive_poll_failures))}
      ${factlet("Digest sent today", daemon.digest_sent_today ? "yes" : "no")}
    </div>
    ${daemon.budget_paused_until
      ? `<div class="note"><span><b>Spend ceiling hit.</b> Polling resumes ${esc(when(daemon.budget_paused_until))}.</span></div>`
      : ""}` : `<p class="empty-note">No daemon attached.</p>`;

  document.getElementById("live-grid").innerHTML =
    activity + card("Schedule", null, schedule) + queueCard(live.queue);
}

// --- 1b. the work queue ----------------------------------------------------

const QUEUE_BADGE = {
  running:    ["ok",      "running"],
  queued:     ["pending", "queued"],
  up_to_date: ["",        "up to date"],
  held:       ["off",     "held"],
};

/* Every source the poll can run, in the order it will run them — and the
   controls to change that order.
 *
 * The order is not cosmetic. `workday` is 839k rows and legitimately takes
 * hours, so whatever sits behind it waits hours; before this the operator's
 * only lever was killing the daemon. What makes the controls safe to offer is
 * that yielding a half-done source is cheap: the snapshot is already
 * downloaded and verified, and every job scored before the yield is committed
 * and gets deduped out of the re-run, so no LLM spend is ever repeated. */
function queueCard(q) {
  if (!q || !q.manifest_known) {
    return card("Work queue", null,
      `<p class="empty-note">No snapshot cached yet — the daemon fills this in on its
       first upstream check.</p>`, "grid-span");
  }

  const rows = q.rows.map(row => {
    const [tone, label] = QUEUE_BADGE[row.status] || ["", row.status];
    const first = row.position === 1;

    // One slot, two meanings. An outstanding source jumps the queue; an
    // up-to-date one has nothing to jump *to*, so the equivalent request is
    // "read it again anyway", which is a different action and says so.
    const promote = row.status === "running"
      ? `<span class="muted">in progress</span>`
      : row.outstanding
        ? `<button class="qbtn primary" type="button" data-queue-action="run_next"
             data-ats="${esc(row.ats_type)}" ${first && !row.held ? "disabled" : ""}
             title="${first && !row.held ? "Already next in line" : "Move to the front of the queue"}">
             Run next</button>`
        : `<button class="qbtn" type="button" data-queue-action="rerun"
             data-ats="${esc(row.ats_type)}"
             title="Nothing new upstream. Re-reads the snapshot and re-applies the filters — postings already scored are skipped, so this costs a few minutes and almost no LLM spend.">
             Re-run</button>`;

    const holdBtn = row.held
      ? `<button class="qbtn" type="button" data-queue-action="release"
           data-ats="${esc(row.ats_type)}" title="Put this source back in the queue">Release</button>`
      : `<button class="qbtn" type="button" data-queue-action="hold"
           data-ats="${esc(row.ats_type)}"
           title="Skip this source until you release it. A run already in progress stops and keeps its place.">Hold</button>`;

    return `<tr${row.status === "running" ? ' class="is-running"' : ""}>
      <td class="n qpos">${row.position ? `<b>${row.position}</b>` : `<span class="muted">—</span>`}</td>
      <td><span class="ats" style="--hue:var(${hueFor(row.ats_type)})">${esc(row.ats_type)}</span>
        ${row.prioritised ? `<span class="badge pending" title="You moved this up">#${row.priority_rank} by you</span>` : ""}</td>
      <td><span class="badge ${tone}">${esc(label)}</span></td>
      <td class="n">${compact(row.rows)}</td>
      <td class="n">${bytes(row.size_bytes)}</td>
      <td>${row.last_processed_at
            ? `<span title="${esc(stamp(row.last_processed_at))}">${esc(when(row.last_processed_at))}</span>`
            : `<span class="muted">never</span>`}</td>
      <td class="qactions">${promote}${holdBtn}</td>
    </tr>`;
  }).join("");

  const reset = q.has_overrides
    ? `<button class="qbtn" type="button" data-queue-reset
         title="Drop every hold and reordering, back to the snapshot's own order">Reset order</button>`
    : "";

  const note = q.has_overrides
    ? `<div class="note plain"><span>Your order is in force.
        ${q.priority.length ? `<b>${esc(q.priority.join(" → "))}</b> run first. ` : ""}
        ${q.held.length ? `<b>${esc(q.held.join(", "))}</b> ${q.held.length === 1 ? "is" : "are"} held and will not be processed. ` : ""}
        A source that gives way keeps everything it already scored and goes back in the
        queue.</span></div>`
    : "";

  return card("Work queue", null, `
    <div class="qhead">
      <div class="stat-strip">
        ${factlet("Sources", num(q.total))}
        ${factlet("Queued", num(q.queued))}
        ${factlet("Held", num(q.held.length))}
        ${factlet("Running", q.running ? esc(q.running) : "—", true)}
      </div>
      ${reset}
    </div>
    <div class="dtable-wrap"><table class="dtable qtable">
      <thead><tr>
        <th class="n">#</th><th>Source</th><th>State</th>
        <th class="n">Snapshot rows</th><th class="n">Size</th>
        <th>Last processed</th><th>Order</th>
      </tr></thead>
      <tbody>${rows}</tbody>
    </table></div>${note}`, "grid-span");
}

async function queueAction(button) {
  const action = button.dataset.queueAction;
  const ats = button.dataset.ats;
  for (const b of document.querySelectorAll("[data-queue-action],[data-queue-reset]")) {
    b.disabled = true;
  }
  try {
    const response = await fetch(
      action ? `/api/queue/${encodeURIComponent(ats)}` : "/api/queue",
      {
        method: "POST",
        headers: { "Content-Type": "application/json", "X-Coldstart-Action": "1" },
        body: JSON.stringify({ action: action || "reset" }),
      });
    if (!response.ok) throw new Error(await response.text());
    // The server owns the resulting order — re-read it rather than guessing.
    await fetchAll(true);
  } catch (err) {
    console.error("could not change the queue:", err);
    alert("Could not change the queue — see the console for why.");
    for (const b of document.querySelectorAll("[data-queue-action],[data-queue-reset]")) {
      b.disabled = false;
    }
  }
}

document.getElementById("live-grid").addEventListener("click", e => {
  const button = e.target.closest("[data-queue-action],[data-queue-reset]");
  if (button && !button.disabled) queueAction(button);
});

// --- 2. KPIs ---------------------------------------------------------------

function renderKpis() {
  const d = state.data;
  const db = d.database, sc = d.scoring, sp = d.spend, dec = d.decisions;
  const bands = sc.bands;

  document.getElementById("kpis").innerHTML = [
    kpi("Jobs in database", compact(db.total_jobs),
        `${num(db.companies)} companies · ${num(db.ats_types)} sources`),
    kpi("Scored", compact(db.by_status.scored),
        `${num(bands.strong + bands.consider)} shortlisted`),
    kpi("Strong", compact(bands.strong),
        `≥ ${d.config.score_threshold_strong} points`),
    kpi("Consider", compact(bands.consider),
        `${d.config.score_threshold_consider}–${d.config.score_threshold_strong - 1}`),
    kpi("Applied", num(dec.applied),
        dec.applied_rate === null ? "none yet" : `${pct(dec.applied_rate)} of shortlist`),
    kpi("Declined", num(dec.declined), `${num(dec.untouched)} still untouched`),
    kpi("Held back", compact(db.by_status.excluded_location),
        `${num(d.location.unresolved)} unresolved`,
        d.location.unresolved > 0 ? "warn" : ""),
    kpi("Delisted", compact(db.by_status.delisted), "gone at the source"),
    kpi("Median score", sc.median === null ? "—" : sc.median,
        `p90 ${sc.p90 ?? "—"} · max ${sc.max ?? "—"}`),
    kpi("LLM calls", compact(sp.totals.calls),
        `${compact(sp.today.calls)} today`),
    kpi("Tokens in", compact(sp.totals.input),
        `${pct(sp.cache_hit_rate)} served from cache`),
    kpi("Tokens out", compact(sp.totals.output),
        `${num(sp.avg_output_tokens)} per call`),
    kpi("Spend today", sp.cost_is_measured ? usd(sp.today.cost) : "n/a",
        sp.cost_is_measured
          ? `of ${usd(sp.ceiling_usd)} ceiling`
          : "this model has no price list",
        sp.cost_is_measured && sp.ceiling_used_pct >= 80 ? "warn" : ""),
    kpi("Emails sent", num(d.digests.sent),
        d.digests.last_sent_at ? `last ${when(d.digests.last_sent_at)}` : "none yet"),
    kpi("Unresolved errors", num(d.errors.unresolved),
        d.errors.unresolved ? "see the errors section" : "clean",
        d.errors.unresolved ? "bad" : ""),
    kpi("Database size", bytes(db.size_bytes),
        `+ ${bytes(db.wal_bytes)} write-ahead log`),
  ].join("");
}

// --- 3. funnel -------------------------------------------------------------

function renderFunnel() {
  const f = state.data.funnel;
  const colors = ["--h-slate", "--h-rose", "--h-amber", "--h-sky", "--h-teal", "--h-lime"];
  const persisted = f.persisted.map((stage, index) => ({ ...stage, color: colors[index] }));

  const runsRows = [
    { label: "Fetched from snapshots", key: "fetched", color: "--h-slate" },
    { label: "Survived the cheap filters", key: "filtered", color: "--h-sky" },
    { label: "Scored by the model", key: "scored", color: "--h-teal" },
    { label: "Failed to score", key: "failed", color: "--h-rose" },
  ];

  const window_ = f.runs.all;
  const runsBody = window_.runs
    ? funnelBars(runsRows.map(row => ({
        stage: row.label, count: window_[row.key], color: row.color })))
      + `<div class="stat-strip">
          ${factlet("Runs recorded", num(f.runs.all.runs))}
          ${factlet("Today", num(f.runs.today.runs))}
          ${factlet("Last 7 days", num(f.runs.week.runs))}
        </div>`
    : `<p class="empty-note">No completed run has been recorded yet.</p>
       <div class="note"><span><b>Why this is empty.</b> Fetched and filtered counts are
       never written to the jobs table — the rejected volume is enormous — so
       <code>run_log</code> is the only place they exist, and a row is written when a poll
       finishes its sources. A poll killed by its timeout leaves nothing here. The funnel on
       the left is derived from the stored jobs instead and is always accurate.</span></div>`;

  const recent = table([
    { label: "Started", render: r => `<span class="mono">${esc(stamp(r.started_at))}</span>` },
    { label: "Took", numeric: true, render: r => duration(r.duration_seconds) },
    { label: "Fetched", numeric: true, render: r => num(r.fetched_count) },
    { label: "Filtered", numeric: true, render: r => num(r.filtered_count) },
    { label: "Scored", numeric: true, render: r => num(r.scored_count) },
    { label: "Failed", numeric: true, render: r => num(r.failed_count) },
  ], f.recent_runs, { empty: "No runs recorded yet." });

  document.getElementById("funnel-grid").innerHTML =
    card("Where postings stop", "derived from every row stored in the database",
         funnelBars(persisted)) +
    card("Run totals", "from the run log — only complete polls appear here", runsBody) +
    card("Recent runs", null, recent, "grid-span");
}

// --- 4. activity -----------------------------------------------------------

function renderActivity() {
  const t = state.data.timeline;
  document.getElementById("tz-note").textContent =
    `days in ${state.data.config.timezone}`;

  const daily = columnChart(t.per_day.map(day => ({
    label: shortDay(day.day),
    tip: `${day.day}: ${day.discovered} found, ${day.scored} scored, ${day.held_back} held back`,
    parts: [
      { value: day.scored, color: "--h-teal" },
      { value: day.held_back, color: "--h-amber" },
      { value: day.excluded, color: "--h-slate" },
    ],
  })), { title: "Jobs per day by outcome", height: 160 });

  const hourly = columnChart(t.per_hour.map(hour => ({
    label: shortHour(hour.hour),
    tip: `${hour.hour}:00 — ${hour.discovered} found, ${hour.scored} scored`,
    parts: [{ value: hour.scored, color: "--h-teal" },
            { value: Math.max(0, hour.discovered - hour.scored), color: "--h-slate" }],
  })), { title: "Last 48 hours", height: 140, labelEvery: 6 });

  document.getElementById("activity-grid").innerHTML =
    card("Last 30 days", "by the day a posting was first seen",
         daily + legend([
           { label: "Scored", color: "--h-teal" },
           { label: "Held back (location)", color: "--h-amber" },
           { label: "Excluded (eligibility)", color: "--h-slate" },
         ]) + `<div class="stat-strip">
           ${factlet("Days with activity", num(t.active_days))}
           ${factlet("Busiest day", t.busiest_day ? `${t.busiest_day} · ${num(t.busiest_day_count)}` : "—", true)}
         </div>`) +
    card("Last 48 hours", "when the work actually happens",
         hourly + legend([
           { label: "Scored", color: "--h-teal" },
           { label: "Found but not scored", color: "--h-slate" },
         ]));
}

// --- 5. scoring ------------------------------------------------------------

const BAND_COLOR = { strong: "--h-teal", consider: "--h-amber", reject: "--h-slate" };

function renderScoring() {
  const s = state.data.scoring;
  const cfg = state.data.config;

  const histogram = columnChart(s.histogram.map(bucket => ({
    label: String(bucket.bucket),
    tip: `${bucket.label}: ${bucket.count} jobs (${bucket.band})`,
    parts: [{ value: bucket.count, color: BAND_COLOR[bucket.band] }],
  })), { title: "Score distribution", height: 170, labelEvery: 1 });

  const perDay = columnChart(s.per_day.map(day => ({
    label: shortDay(day.day),
    tip: `${day.day}: ${day.strong} strong, ${day.consider} consider, ${day.reject} reject`,
    parts: [
      { value: day.strong, color: "--h-teal" },
      { value: day.consider, color: "--h-amber" },
      { value: day.reject, color: "--h-slate" },
    ],
  })), { title: "Scored per day by band", height: 150 });

  const resumes = table([
    { label: "Résumé", render: r => `<span class="rz">${esc(r.resume)}</span>` },
    { label: "Scored", numeric: true, render: r => num(r.count) },
    { label: "Shortlist", numeric: true, render: r => num(r.shortlisted) },
    { label: "Avg", numeric: true, render: r => r.avg ?? "—" },
    { label: "Best", numeric: true, render: r => r.max || "—" },
  ], s.by_resume, { empty: "Nothing routed yet." });

  document.getElementById("scoring-grid").innerHTML =
    card("Score distribution", `banded by your thresholds — strong ≥ ${cfg.score_threshold_strong}, consider ≥ ${cfg.score_threshold_consider}`,
         histogram + legend([
           { label: "Strong", color: "--h-teal" },
           { label: "Consider", color: "--h-amber" },
           { label: "Reject", color: "--h-slate" },
         ]) + `<div class="stat-strip">
           ${factlet("Median", s.median ?? "—")}
           ${factlet("Mean", s.mean ?? "—")}
           ${factlet("p25", s.p25 ?? "—")}
           ${factlet("p75", s.p75 ?? "—")}
           ${factlet("p90", s.p90 ?? "—")}
           ${factlet("Max", s.max ?? "—")}
         </div>`) +
    card("Band split", "what the shortlist is made of",
         donut([
           { label: "Strong", value: s.bands.strong, color: "--h-teal" },
           { label: "Consider", value: s.bands.consider, color: "--h-amber" },
           { label: "Reject", value: s.bands.reject, color: "--h-slate" },
         ], { caption: "scored" }) +
         legend([
           { label: `Strong · ${num(s.bands.strong)}`, color: "--h-teal" },
           { label: `Consider · ${num(s.bands.consider)}`, color: "--h-amber" },
           { label: `Reject · ${num(s.bands.reject)}`, color: "--h-slate" },
         ]) +
         (s.llm_band_disagreements
           ? `<div class="note plain"><span><b>${num(s.llm_band_disagreements)}</b> jobs were
              banded differently by the model than by your thresholds. The thresholds win —
              the model is never told what yours are.</span></div>`
           : "")) +
    card("Résumé routing", "which of your four résumés each job was scored against", resumes) +
    card("Scored per day", "by band, last 30 days",
         perDay + legend([
           { label: "Strong", color: "--h-teal" },
           { label: "Consider", color: "--h-amber" },
           { label: "Reject", color: "--h-slate" },
         ]), "grid-span");
}

// --- 6. sources ------------------------------------------------------------

const SOURCE_BADGE = {
  up_to_date: ["ok", "up to date"],
  outstanding: ["pending", "queued"],
  never_processed: ["pending", "never run"],
  not_in_manifest: ["off", "not offered"],
  excluded_by_config: ["off", "excluded"],
};

const SOURCE_COLUMNS = [
  { key: "ats_type", label: "Source", render: r =>
      `<span class="ats" style="--hue:var(${hueFor(r.ats_type)})">${esc(r.ats_type)}</span>` },
  { key: "status", label: "State", render: r => {
      const [tone, text] = SOURCE_BADGE[r.status] || ["off", r.status];
      return `<span class="badge ${tone}">${esc(text)}</span>`; } },
  { key: "last_processed_at", label: "Last processed", render: r =>
      r.last_processed_at
        ? `<span title="${esc(stamp(r.last_processed_at))}">${esc(when(r.last_processed_at))}</span>`
        : `<span class="muted">never</span>` },
  { key: "manifest_rows", label: "Snapshot rows", numeric: true, render: r => compact(r.manifest_rows) },
  { key: "manifest_size_bytes", label: "Size", numeric: true, render: r => bytes(r.manifest_size_bytes) },
  { key: "jobs", label: "Stored", numeric: true, render: r => num(r.jobs) },
  { key: "scored", label: "Scored", numeric: true, render: r => num(r.scored) },
  { key: "shortlisted", label: "Shortlisted", numeric: true, render: r => num(r.shortlisted) },
  { key: "strong", label: "Strong", numeric: true, render: r => num(r.strong) },
  { key: "held_back", label: "Held back", numeric: true, render: r => num(r.held_back) },
  { key: "delisted", label: "Delisted", numeric: true, render: r => num(r.delisted) },
  { key: "avg_score", label: "Avg score", numeric: true, render: r => r.avg_score ?? "—" },
  { key: "liveness_checkable", label: "Verifiable", render: r =>
      r.liveness_checkable
        ? `<span class="badge ok">yes</span>`
        : `<span class="badge" title="No API this project can check — a dead posting here is invisible">no</span>` },
];

const ATS_HUES = ["--h-teal", "--h-sky", "--h-violet", "--h-lime", "--h-amber", "--h-rose", "--h-slate"];
const hueFor = value => {
  const text = String(value ?? "");
  let h = 7;
  for (let i = 0; i < text.length; i++) h = (h * 31 + text.charCodeAt(i)) >>> 0;
  return ATS_HUES[h % ATS_HUES.length];
};

function renderSources() {
  const src = state.data.sources;
  let rows = src.rows.filter(row =>
    state.showExcludedSources || !row.excluded_by_config);

  const { key, dir } = state.sourceSort;
  rows = rows.slice().sort((a, b) => {
    const x = a[key], y = b[key];
    if (x === y) return 0;
    if (x === null || x === undefined) return 1;
    if (y === null || y === undefined) return -1;
    const cmp = typeof x === "number" && typeof y === "number"
      ? x - y : String(x).localeCompare(String(y));
    return cmp * dir;
  });

  const head = SOURCE_COLUMNS.map(col => {
    const active = key === col.key;
    const sort = active ? (dir === 1 ? "ascending" : "descending") : "none";
    const arrow = active ? (dir === 1 ? "▲" : "▼") : "↕";
    return `<th class="sortable ${col.numeric ? "n" : ""}" data-key="${col.key}" ` +
           `aria-sort="${sort}" scope="col" tabindex="0">${esc(col.label)}` +
           `<span class="arrow" aria-hidden="true">${arrow}</span></th>`;
  }).join("");

  const body = rows.map(row => `<tr>${SOURCE_COLUMNS.map(col =>
    `<td class="${col.numeric ? "n" : ""}">${col.render(row)}</td>`).join("")}</tr>`).join("");

  const counts = src.counts;
  document.getElementById("sources-panel").innerHTML = `
    <div class="card">
      <div class="stat-strip" style="margin-bottom:.9rem">
        ${factlet("Sources shown", num(rows.length))}
        ${factlet("Up to date", num(counts.up_to_date || 0))}
        ${factlet("Queued", num((counts.outstanding || 0) + (counts.never_processed || 0)))}
        ${factlet("Excluded by config", num(src.excluded_count))}
        ${factlet("Verifiable at source", num(src.rows.filter(r => r.liveness_checkable).length))}
      </div>
      <div class="dtable-wrap"><table class="dtable" id="sources-table">
        <thead><tr>${head}</tr></thead><tbody>${body}</tbody></table></div>
      ${src.manifest_known ? "" : `<div class="note"><span>No snapshot manifest is cached yet,
        so snapshot rows and sizes are blank. The daemon caches it on its first upstream
        check.</span></div>`}
    </div>`;
}

// --- 7. spend --------------------------------------------------------------

function renderSpend() {
  const sp = state.data.spend;

  const daily = columnChart(sp.per_day.map(day => ({
    label: shortDay(day.day),
    tip: `${day.day}: ${day.calls} calls · ${compact(day.input)} in / ${compact(day.output)} out`,
    parts: [
      { value: day.input - day.cached, color: "--h-sky" },
      { value: day.cached, color: "--h-violet" },
      { value: day.output, color: "--h-teal" },
    ],
  })), { title: "Tokens per day", height: 160 });

  const models = table([
    { label: "Provider", render: r => esc(r.provider) },
    { label: "Model", render: r => `<span class="mono">${esc(r.model)}</span>` },
    { label: "Calls", numeric: true, render: r => num(r.calls) },
    { label: "Tokens in", numeric: true, render: r => compact(r.input) },
    { label: "Cached", numeric: true, render: r => compact(r.cached) },
    { label: "Tokens out", numeric: true, render: r => compact(r.output) },
    { label: "Cost", numeric: true, render: r => r.priced
        ? usd(r.cost) : `<span class="badge">not priced</span>` },
  ], sp.per_model, { empty: "No LLM calls recorded yet." });

  const ceiling = sp.cost_is_measured
    ? `<div class="progress"><i style="width:${Math.min(100, sp.ceiling_used_pct || 0)}%"></i></div>
       <p class="card-sub">${usd(sp.today.cost)} of ${usd(sp.ceiling_usd)} used today (${pct(sp.ceiling_used_pct)})</p>`
    : `<div class="note"><span><b>Dollar figures here are not measurements.</b>
        ${esc(sp.unpriced_models.join(", ") || "The active model")} has no entry in the
        project's price list, so every <code>est_cost_usd</code> is stored as 0 and the
        $${sp.ceiling_usd.toFixed(2)} daily ceiling can never trip. Token counts come
        straight from the provider and are real — read those instead.</span></div>`;

  document.getElementById("spend-grid").innerHTML =
    card("Tokens per day", "input, cache hits and output, last 30 days",
         daily + legend([
           { label: "Input (billed)", color: "--h-sky" },
           { label: "Input (cache hit)", color: "--h-violet" },
           { label: "Output", color: "--h-teal" },
         ]) + `<div class="stat-strip">
           ${factlet("Total calls", num(sp.totals.calls))}
           ${factlet("Cache hit rate", pct(sp.cache_hit_rate))}
           ${factlet("Avg tokens in", num(sp.avg_input_tokens))}
           ${factlet("Avg tokens out", num(sp.avg_output_tokens))}
         </div>`) +
    card("Today against the ceiling", null, ceiling +
         `<div class="stat-strip" style="margin-top:.8rem">
           ${factlet("Calls today", num(sp.today.calls))}
           ${factlet("Tokens in today", compact(sp.today.input))}
           ${factlet("Tokens out today", compact(sp.today.output))}
         </div>`) +
    card("By model", null, models, "grid-span");
}

// --- 8. liveness -----------------------------------------------------------

function renderLiveness() {
  const lv = state.data.liveness;
  const cfg = state.data.config;

  const perDay = columnChart(lv.per_day.map(day => ({
    label: shortDay(day.day),
    tip: `${day.day}: ${day.count} delisted`,
    parts: [{ value: day.count, color: "--h-rose" }],
  })), { title: "Delisted per day", height: 130 });

  const recent = table([
    { label: "Company", wrap: true, render: r => `<b>${esc(r.company)}</b>` },
    { label: "Title", wrap: true, render: r => esc(r.title) },
    { label: "Score", numeric: true, render: r => r.score ?? "—" },
    { label: "Reason", render: r => `<span class="mono">${esc(r.delist_reason || "—")}</span>` },
    { label: "When", render: r => esc(when(r.delisted_at)) },
  ], lv.recent, { empty: "Nothing has been delisted yet." });

  const coverage = lv.shortlisted_checkable + lv.shortlisted_unverifiable;
  document.getElementById("liveness-grid").innerHTML =
    card("Confirmed gone", `${lv.enabled ? "checking enabled" : "checking is turned off"}`,
         hbars(lv.by_reason.map(r => ({ label: r.key, count: r.count })), { color: "--h-rose" }) +
         `<div class="stat-strip">
           ${factlet("Total delisted", num(lv.total_delisted))}
           ${factlet("Last sweep", when(lv.last_sweep_at))}
           ${factlet("Next sweep", when(lv.next_sweep_at))}
           ${factlet("Queue for next sweep", num(lv.sweep_queue))}
         </div>`) +
    card("Coverage", "only three ATS platforms expose an API this can check",
         donut([
           { label: "Verifiable", value: lv.shortlisted_checkable, color: "--h-teal" },
           { label: "No way to check", value: lv.shortlisted_unverifiable, color: "--h-slate" },
         ], { caption: "shortlisted" }) +
         legend([
           { label: `Verifiable · ${num(lv.shortlisted_checkable)}`, color: "--h-teal" },
           { label: `Unverifiable · ${num(lv.shortlisted_unverifiable)}`, color: "--h-slate" },
         ]) +
         (lv.shortlisted_unverifiable
           ? `<div class="note"><span><b>${num(lv.shortlisted_unverifiable)} of ${num(coverage)}</b>
              shortlisted jobs are on a platform with no check
              (${esc(cfg.checked_ats_types.join(", "))} are the covered ones). A posting
              there can be dead with no way to find out.</span></div>`
           : "")) +
    card("Delisted per day", null, perDay) +
    card("Recently delisted", null, recent, "grid-span");
}

// --- 9. location -----------------------------------------------------------

function renderLocation() {
  const loc = state.data.location;

  const perDay = columnChart(loc.per_day.map(day => ({
    label: shortDay(day.day),
    tip: `${day.day}: ${day.count} held back`,
    parts: [{ value: day.count, color: "--h-amber" }],
  })), { title: "Held back per day", height: 130 });

  const sample = table([
    { label: "Company", wrap: true, render: r => `<b>${esc(r.company)}</b>` },
    { label: "Title", wrap: true, render: r => esc(r.title) },
    { label: "Location string", wrap: true, render: r => `<span class="mono">${esc(r.location || "—")}</span>` },
    { label: "Reason", render: r => `<span class="badge pending">${esc(r.location_reason)}</span>` },
  ], loc.recent_unresolved, { empty: "Nothing unresolved — the lexicon is covering everything." });

  document.getElementById("location-grid").innerHTML =
    card("Why jobs were held back", "the rule that fired, per job",
         hbars(loc.by_reason.map(r => ({ label: r.key, count: r.count })), { color: "--h-amber" }) +
         `<div class="stat-strip">
           ${factlet("Total held back", num(loc.total_held_back))}
           ${factlet("Held per job scored", loc.held_per_scored ?? "—")}
         </div>` +
         (loc.unresolved
           ? `<div class="note"><span><b>${num(loc.unresolved)} jobs matched no rule at all.</b>
              That is not "these were foreign" — it means the location lexicon has a gap and
              real US jobs may be sitting here unscored. Recognisable US cities in the sample
              below are the signal to extend <code>config/us_cities.json</code>.</span></div>`
           : "")) +
    card("Which sources", "held-back jobs by ATS",
         hbars(loc.by_ats.map(r => ({ label: r.key, count: r.count })), { color: "--h-amber" })) +
    card("Held back per day", null, perDay) +
    card("Unresolved sample", "the newest jobs no location rule could classify", sample, "grid-span");
}

// --- 10. digests -----------------------------------------------------------

function renderDigests() {
  const dg = state.data.digests;
  const daemon = state.data.daemon;

  const perDay = columnChart(dg.per_day.map(day => ({
    label: shortDay(day.day),
    tip: `${day.day}: ${day.count} jobs emailed`,
    parts: [{ value: day.count, color: "--h-sky" }],
  })), { title: "Jobs emailed per day", height: 140 });

  const history = table([
    { label: "Sent", render: r => `<span class="mono">${esc(stamp(r.sent_at))}</span>` },
    { label: "Jobs", numeric: true, render: r => num(r.job_count) },
    { label: "Status", render: r => r.status === "sent"
        ? `<span class="badge ok">sent</span>` : `<span class="badge bad">${esc(r.status)}</span>` },
    { label: "Error", wrap: true, render: r => r.error ? esc(r.error) : `<span class="muted">—</span>` },
  ], dg.recent, { empty: "No email has been sent yet." });

  document.getElementById("digest-grid").innerHTML =
    card("Email history", `scheduled daily at ${dg.scheduled_at} ${state.data.config.timezone}`,
         `<div class="stat-strip" style="margin-bottom:.9rem">
           ${factlet("Last sent", when(dg.last_sent_at))}
           ${factlet("Jobs in it", num(dg.last_job_count))}
           ${factlet("Sent today", num(dg.sent_today))}
           ${factlet("Next due", daemon ? when(daemon.next_digest_at) : "—")}
           ${factlet("Total sent", num(dg.sent))}
           ${factlet("Failed sends", num(dg.failed))}
           ${factlet("Avg per digest", dg.avg_jobs_per_digest ?? "—")}
           ${factlet("Jobs emailed all time", num(dg.jobs_emailed))}
         </div>` + history +
         (dg.failed ? `<div class="note"><span><b>${num(dg.failed)} send(s) failed.</b>
           A failed send never advances the digest window, so those jobs are still owed to
           the next successful email — they were not skipped.</span></div>` : "")) +
    card("Jobs emailed per day", null, perDay);
}

// --- 11. companies & skills ------------------------------------------------

function renderCompanies() {
  const co = state.data.companies;
  const sc = state.data.scoring;

  const top = table([
    { label: "Company", wrap: true, render: r => `<b>${esc(r.company)}</b>` },
    { label: "Source", render: r =>
        `<span class="ats" style="--hue:var(${hueFor(r.ats_type)})">${esc(r.ats_type)}</span>` },
    { label: "Scored", numeric: true, render: r => num(r.scored) },
    { label: "Shortlisted", numeric: true, render: r => num(r.shortlisted) },
    { label: "Strong", numeric: true, render: r => num(r.strong) },
    { label: "Avg", numeric: true, render: r => r.avg },
    { label: "Best", numeric: true, render: r => r.best },
  ], co.top, { empty: "Nothing scored yet." });

  document.getElementById("companies-grid").innerHTML =
    card("Top companies", `ranked by shortlisted jobs — ${num(co.with_shortlist)} of ${num(co.distinct)} companies have at least one`,
         top, "grid-span") +
    card("Skills you match", "counted across every scored job",
         hbars(sc.top_matched_skills.map(r => ({ label: r.key, count: r.count })), { color: "--h-teal" })) +
    card("Skills you're missing", "what the postings ask for that your résumés don't show",
         hbars(sc.top_missing_skills.map(r => ({ label: r.key, count: r.count })), { color: "--h-rose" }));
}

// --- 12. errors ------------------------------------------------------------

function renderErrors() {
  const er = state.data.errors;
  const recent = table([
    { label: "When", render: r => `<span class="mono">${esc(stamp(r.ts))}</span>` },
    { label: "Stage", render: r => `<span class="badge">${esc(r.stage)}</span>` },
    { label: "Where", render: r => `<span class="mono">${esc(r.function_name || "—")}</span>` },
    { label: "Type", render: r => esc(r.error_type || "—") },
    { label: "Message", wrap: true, render: r =>
        esc((r.error_message || "—").split("\n")[0].slice(0, 220)) },
    { label: "Fixed", render: r => r.resolved
        ? `<span class="badge ok">yes</span>` : `<span class="badge bad">open</span>` },
  ], er.recent, { empty: "No errors recorded. " });

  document.getElementById("errors-grid").innerHTML =
    card("By stage", `${num(er.unresolved)} of ${num(er.total)} still unresolved`,
         hbars(er.by_stage.map(r => ({ label: r.key, count: r.count })), { color: "--h-rose" }) +
         (er.by_type.length
           ? `<div style="margin-top:1rem">${hbars(
               er.by_type.map(r => ({ label: r.key, count: r.count })), { color: "--h-slate" })}</div>`
           : "")) +
    card("Recent", null, recent, "grid-span");
}

// --- config ----------------------------------------------------------------

const CONFIG_LABELS = {
  timezone: "Timezone",
  score_threshold_strong: "Strong threshold",
  score_threshold_consider: "Consider threshold",
  max_posting_age_days: "Max posting age (days)",
  poll_interval_minutes: "Poll interval (min)",
  force_poll_hours: "Force a poll every (h)",
  poll_timeout_minutes: "Poll timeout (min)",
  digest_time: "Digest time",
  llm_mode: "LLM mode",
  llm_provider: "Provider",
  llm_model: "Model",
  experience_years: "Experience (years)",
  daily_spend_ceiling_usd: "Daily spend ceiling",
  liveness_check_enabled: "Liveness check",
  liveness_sweep_interval_hours: "Liveness sweep every (h)",
  db_path: "Database",
};

function renderConfig() {
  const cfg = state.data.config;
  document.getElementById("config-grid").innerHTML =
    Object.entries(CONFIG_LABELS).map(([key, label]) => {
      const value = cfg[key];
      return `<div><span class="k">${esc(label)}</span>
        <span class="v">${esc(value === true ? "on" : value === false ? "off" : String(value ?? "—"))}</span></div>`;
    }).join("") +
    `<div><span class="k">Verifiable ATS</span>
      <span class="v">${esc(cfg.checked_ats_types.join(", "))}</span></div>`;
}

// --- render + data ---------------------------------------------------------

function renderAll() {
  renderLive();
  renderKpis();
  renderFunnel();
  renderActivity();
  renderScoring();
  renderSources();
  renderSpend();
  renderLiveness();
  renderLocation();
  renderDigests();
  renderCompanies();
  renderErrors();
  renderConfig();
  document.getElementById("freshness").textContent =
    `updated ${new Date(state.data.generated_at).toLocaleTimeString()}`;
}

let inFlight = false;
let lastFetchAt = 0;

// The payload is ~60 KB and a poll commits per job, so an un-throttled SSE
// refetch would pull it several times a second during a run. Ten seconds is
// well under the 5-second heartbeat's usefulness and keeps the page cheap.
const MIN_REFETCH_MS = 10_000;

async function fetchAll(force = false) {
  if (inFlight) return;
  if (!force && Date.now() - lastFetchAt < MIN_REFETCH_MS) return;
  inFlight = true;
  try {
    const data = await fetch("/api/analytics").then(r => r.json());
    lastFetchAt = Date.now();
    if (data.db_ready === false) {
      document.getElementById("skeleton").textContent =
        "No database yet — run the pipeline once and this page fills in.";
      document.getElementById("skeleton").hidden = false;
      document.getElementById("report").hidden = true;
      return;
    }
    state.data = data;
    document.getElementById("skeleton").hidden = true;
    document.getElementById("report").hidden = false;
    renderAll();
  } catch (err) {
    console.error("could not load analytics:", err);
    document.getElementById("skeleton").textContent =
      "Could not load the analytics — see the browser console.";
    document.getElementById("skeleton").hidden = false;
  } finally {
    inFlight = false;
  }
}

// --- wiring ----------------------------------------------------------------

document.getElementById("refresh").addEventListener("click", () => fetchAll(true));

document.getElementById("show-all-sources").addEventListener("change", e => {
  state.showExcludedSources = e.target.checked;
  if (state.data) renderSources();
});

document.getElementById("sources-panel").addEventListener("click", e => sortSources(e.target.closest("th")));
document.getElementById("sources-panel").addEventListener("keydown", e => {
  if (e.key === "Enter" || e.key === " ") { e.preventDefault(); sortSources(e.target.closest("th")); }
});
function sortSources(th) {
  if (!th || !th.dataset.key) return;
  const key = th.dataset.key;
  if (state.sourceSort.key === key) state.sourceSort.dir *= -1;
  else state.sourceSort = { key, dir: typeof (state.data.sources.rows[0] || {})[key] === "number" ? -1 : 1 };
  renderSources();
  const again = document.querySelector(`#sources-panel th[data-key="${key}"]`);
  if (again) again.focus();
}

document.getElementById("export").addEventListener("click", () => {
  if (!state.data) return;
  const blob = new Blob([JSON.stringify(state.data, null, 2)], { type: "application/json" });
  const url = URL.createObjectURL(blob);
  const a = document.createElement("a");
  a.href = url;
  a.download = `coldstart_analytics_${new Date().toISOString().slice(0, 19).replace(/[:T]/g, "-")}.json`;
  a.click();
  URL.revokeObjectURL(url);
});

// Same live stream the jobs page uses — the server pushes only when the data
// or the daemon's state actually moved.
const dot = document.getElementById("live-dot");
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
  source.onmessage = () => fetchAll();
  source.onerror = () => setLive("stale");
}

fetchAll(true).then(connect);

// A running poll heartbeats every 5s but does not always land a new job row,
// so the SSE token can sit still while the progress bar should be moving.
setInterval(() => {
  const running = state.data && state.data.live.progress && state.data.live.progress.is_running;
  if (running || !dot.classList.contains("live")) fetchAll(true);
}, 10_000);

// Keeps every "3m ago" honest between fetches.
setInterval(() => { if (state.data) { renderLive(); renderSources(); } }, 30_000);
