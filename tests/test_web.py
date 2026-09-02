from __future__ import annotations

import re
import socket
import sqlite3
import time
from datetime import UTC, datetime, timedelta

import httpx
import pytest
from fastapi.testclient import TestClient
from freezegun import freeze_time

import coldstart.daemon as daemon
from coldstart.db import (
    connection,
    init_schema,
    log_email,
    log_run,
    readonly_connection,
    upsert_job,
)
from coldstart.models import (
    EligibilityFlag,
    JobRecord,
    JobStatus,
    LocationFlag,
    ResumeId,
    ScoreBand,
)
from coldstart.settings import Settings
from coldstart.web.app import create_app, serve_in_thread
from coldstart.web.queries import band_for, data_version


@pytest.fixture
def settings(tmp_path) -> Settings:
    return Settings(
        experience_years=3.5,
        smtp_user="user@example.com",
        smtp_app_password="app-pw",
        digest_recipient="user@example.com",
        data_dir=tmp_path / "data",
        log_dir=tmp_path / "logs",
        output_dir=tmp_path / "output",
        resume_manifest=tmp_path / "resumes" / "manifest.json",
        db_path=tmp_path / "coldstart.sqlite3",
        dashboard_enabled=False,
    )


def _job(global_id, *, score=None, band=None, status=JobStatus.SCORED, company="Acme", **extra):
    now = datetime.now(UTC)
    return JobRecord(
        global_id=global_id,
        requisition_id=extra.pop("requisition_id", "R-1"),
        company=company,
        title=extra.pop("title", "Software Engineer"),
        location=extra.pop("location", "Austin, TX"),
        apply_url=extra.pop("apply_url", "https://example.com/apply"),
        ats_type=extra.pop("ats_type", "greenhouse"),
        posted_at=extra.pop("posted_at", now - timedelta(days=3)),
        resume_used=extra.pop("resume_used", ResumeId.A),
        score=score,
        score_band=band,
        eligible=extra.pop("eligible", True),
        matched_skills=extra.pop("matched_skills", ["Python", "FastAPI"]),
        missing_skills=extra.pop("missing_skills", ["Kubernetes"]),
        reasoning=extra.pop("reasoning", "Solid overlap on the backend stack."),
        status=status,
        location_flag=extra.pop("location_flag", LocationFlag.ACCEPTED),
        location_reason=extra.pop("location_reason", None),
        eligibility_flag=extra.pop("eligibility_flag", EligibilityFlag.PASSED),
        provider_used=extra.pop("provider_used", "deepseek"),
        first_seen_at=now,
        scored_at=now if status is JobStatus.SCORED else None,
    )


@pytest.fixture
def seeded(settings):
    with connection(settings.db_path) as conn:
        init_schema(conn)
        upsert_job(conn, _job("gh:1", score=91, band=ScoreBand.STRONG, company="Alpha"))
        upsert_job(conn, _job("gh:2", score=64, band=ScoreBand.CONSIDER, company="Beta"))
        upsert_job(conn, _job("gh:3", score=22, band=ScoreBand.REJECT, company="Gamma"))
        # Never scored — persisted for audit only, must never reach the dashboard.
        upsert_job(
            conn,
            _job(
                "gh:4",
                status=JobStatus.EXCLUDED,
                company="Delta",
                eligibility_flag=EligibilityFlag.EXCLUDED,
            ),
        )
        upsert_job(conn, _job("gh:5", status=JobStatus.FAILED, company="Epsilon"))
        # Held back by the location filter: never scored, never emailed, but
        # reviewable in its own dashboard view.
        upsert_job(
            conn,
            _job(
                "gh:6",
                status=JobStatus.EXCLUDED_LOCATION,
                company="Zeta",
                location="2 Locations",
                location_flag=LocationFlag.UNCERTAIN,
                location_reason="workday_path_foreign",
                eligibility_flag=EligibilityFlag.UNCERTAIN,
                eligible=None,
                provider_used=None,
            ),
        )
        yield conn


@pytest.fixture
def client(settings, seeded) -> TestClient:
    return TestClient(create_app(settings))


@pytest.fixture(autouse=True)
def _no_daemon_state():
    daemon._set_state(None)
    yield
    daemon._set_state(None)


# --- job listing -----------------------------------------------------------


def test_default_listing_is_scored_and_non_reject(client):
    # Binary bands: gh:2 (score 64) no longer clears score_threshold_strong
    # (80), so only gh:1 (91) shows without include_reject.
    jobs = client.get("/api/jobs").json()["jobs"]
    assert [j["global_id"] for j in jobs] == ["gh:1"]


def test_include_reject_widens_the_listing(client):
    jobs = client.get("/api/jobs", params={"include_reject": True}).json()["jobs"]
    assert [j["global_id"] for j in jobs] == ["gh:1", "gh:2", "gh:3"]


def test_excluded_and_failed_rows_never_appear(client):
    jobs = client.get("/api/jobs", params={"include_reject": True}).json()["jobs"]
    ids = {j["global_id"] for j in jobs}
    assert "gh:4" not in ids and "gh:5" not in ids
    # Held-back rows have no score, so they must not leak into the scored list.
    assert "gh:6" not in ids


# --- held back by the location filter --------------------------------------


def test_held_back_listing_returns_only_location_excluded_rows(client):
    payload = client.get("/api/jobs/held-back").json()
    assert payload["db_ready"] is True
    assert [j["global_id"] for j in payload["jobs"]] == ["gh:6"]


def test_held_back_rows_carry_the_rule_that_stopped_them(client):
    job = client.get("/api/jobs/held-back").json()["jobs"][0]
    assert job["location_reason"] == "workday_path_foreign"
    assert job["location_flag"] == "uncertain"
    # Never scored: a null score must not blow up band_for or render as NaN.
    assert job["score"] is None
    assert job["band"] is None


def test_metrics_counts_held_back_jobs(client):
    assert client.get("/api/metrics").json()["held_back"] == 1


def test_ordering_is_score_descending(client):
    jobs = client.get("/api/jobs", params={"include_reject": True}).json()["jobs"]
    assert [j["score"] for j in jobs] == [91, 64, 22]


def test_skills_round_trip_from_json_text_to_arrays(client):
    job = client.get("/api/jobs").json()["jobs"][0]
    assert job["matched_skills"] == ["Python", "FastAPI"]
    assert job["missing_skills"] == ["Kubernetes"]


# --- banding ---------------------------------------------------------------


def test_band_is_recomputed_from_settings_not_the_stored_llm_band(settings):
    """scope.md §8 / Module 18: the LLM never learns the operator's thresholds,
    so its self-assigned score_band cannot be trusted for placement."""
    with connection(settings.db_path) as conn:
        init_schema(conn)
        upsert_job(conn, _job("gh:liar", score=10, band=ScoreBand.STRONG))

    client = TestClient(create_app(settings))
    job = client.get("/api/jobs", params={"include_reject": True}).json()["jobs"][0]
    assert job["score"] == 10
    assert job["band"] == "reject"      # what the thresholds say
    assert job["llm_band"] == "strong"  # what the model claimed, surfaced not hidden


def test_changing_the_threshold_moves_the_band(settings):
    with connection(settings.db_path) as conn:
        init_schema(conn)
        upsert_job(conn, _job("gh:edge", score=65, band=ScoreBand.CONSIDER))

    assert band_for(65, settings) == "reject"
    lenient = settings.model_copy(update={"score_threshold_strong": 60})
    assert band_for(65, lenient) == "strong"

    client = TestClient(create_app(lenient))
    assert client.get("/api/jobs").json()["jobs"][0]["band"] == "strong"


def test_band_boundaries_are_inclusive_at_the_threshold(settings):
    # Binary: no middle "consider" tier — score_threshold_strong is the only
    # cut point, and it belongs to "strong".
    assert band_for(settings.score_threshold_strong, settings) == "strong"
    assert band_for(settings.score_threshold_strong - 1, settings) == "reject"


# --- metrics ---------------------------------------------------------------


def test_metrics_count_bands_and_exclude_reject_from_the_headline(client):
    # Binary bands: gh:2 (score 64) falls below score_threshold_strong (80),
    # so only gh:1 counts as strong/shortlisted; gh:2 and gh:3 are both reject.
    m = client.get("/api/metrics").json()
    assert (m["total"], m["strong"], m["reject"]) == (1, 1, 2)
    assert m["companies"] == 1          # Beta and Gamma are both reject-band now
    assert m["max_score"] == 91
    assert m["median_score"] == 91.0


def test_metrics_funnel_reads_run_log_not_jobs(settings, seeded):
    """fetched/filtered counts can't come from `jobs` — title- and
    location-rejected rows are deliberately never persisted."""
    log_run(
        seeded,
        run_id="abc",
        started_at=datetime.now(UTC),
        finished_at=datetime.now(UTC),
        fetched_count=33186,
        filtered_count=412,
        scored_count=5,
        failed_count=1,
    )
    m = TestClient(create_app(settings)).get("/api/metrics").json()
    assert m["funnel"] == {"fetched": 33186, "filtered": 412, "scored": 5, "failed": 1}


def test_metrics_report_the_days_digest(settings, seeded):
    log_email(seeded, sent_at=datetime.now(UTC), job_count=7, status="sent")
    m = TestClient(create_app(settings)).get("/api/metrics").json()
    assert m["digest_job_count"] == 7
    assert m["digest_sent_at"] is not None


def test_metrics_on_an_empty_database(settings):
    with connection(settings.db_path) as conn:
        init_schema(conn)
    m = TestClient(create_app(settings)).get("/api/metrics").json()
    assert m["total"] == 0
    assert m["median_score"] is None


def test_missing_database_renders_an_empty_state_not_a_500(settings):
    client = TestClient(create_app(settings))  # db_path never created
    jobs = client.get("/api/jobs")
    assert jobs.status_code == 200
    assert jobs.json() == {"jobs": [], "db_ready": False}
    assert client.get("/api/metrics").json() == {"db_ready": False}


# --- status and events -----------------------------------------------------


def test_status_is_null_until_the_daemon_starts(client):
    assert client.get("/api/status").json()["daemon"] is None


def test_status_mirrors_the_daemon_state(client):
    now = datetime.now(UTC)
    daemon._set_state(
        daemon.DaemonState(
            started_at=now,
            next_poll_at=now + timedelta(minutes=30),
            next_digest_at=now + timedelta(hours=5),
            next_liveness_sweep_at=now + timedelta(hours=24),
            activity="polling",
            consecutive_poll_failures=2,
        )
    )
    body = client.get("/api/status").json()
    assert body["daemon"]["activity"] == "polling"
    assert body["daemon"]["consecutive_poll_failures"] == 2
    assert body["data_version"].startswith("6:")  # five seeded rows


# --- pausing polling ---------------------------------------------------

def _running_daemon(**overrides):
    now = datetime.now(UTC)
    base = dict(
        started_at=now, next_poll_at=now, next_digest_at=now, next_liveness_sweep_at=now
    )
    base.update(overrides)
    daemon._set_state(daemon.DaemonState(**base))


def test_pausing_sets_manually_paused_and_the_dashboard_keeps_serving(client):
    _running_daemon()
    response = client.post("/api/daemon/pause", headers=_ACT)
    assert response.status_code == 200
    assert response.json() == {"manually_paused": True}
    assert client.get("/api/status").json()["daemon"]["manually_paused"] is True
    # The one write this endpoint makes is to daemon state, not the jobs table.
    assert client.get("/api/jobs").json()["db_ready"] is True


def test_resuming_clears_manually_paused(client):
    _running_daemon(manually_paused=True)
    response = client.post("/api/daemon/resume", headers=_ACT)
    assert response.status_code == 200
    assert response.json() == {"manually_paused": False}
    assert client.get("/api/status").json()["daemon"]["manually_paused"] is False


def test_pause_and_resume_need_the_action_header(client):
    _running_daemon()
    assert client.post("/api/daemon/pause").status_code == 403
    assert client.post("/api/daemon/resume").status_code == 403


def test_pause_and_resume_are_409_with_no_daemon_attached(client):
    assert client.post("/api/daemon/pause", headers=_ACT).status_code == 409
    assert client.post("/api/daemon/resume", headers=_ACT).status_code == 409


def test_the_pause_button_is_wired_up_in_the_page(client):
    body = client.get("/").text
    assert 'id="poll-toggle"' in body
    js = client.get("/static/app.js").text
    assert "/api/daemon/" in js and "poll-toggle" in js


def test_data_version_changes_when_a_job_is_rescored(settings, seeded):
    client = TestClient(create_app(settings))
    before = client.get("/api/status").json()["data_version"]

    with freeze_time(datetime.now(UTC) + timedelta(hours=1)):
        upsert_job(seeded, _job("gh:1", score=95, band=ScoreBand.STRONG, company="Alpha"))

    assert client.get("/api/status").json()["data_version"] != before


# --- static + health -------------------------------------------------------


def test_index_serves_the_page(client):
    response = client.get("/")
    assert response.status_code == 200
    assert "Coldstart" in response.text


def test_static_assets_are_served(client):
    assert client.get("/static/app.js").status_code == 200
    assert client.get("/static/styles.css").status_code == 200


def test_healthz(client):
    assert client.get("/healthz").json() == {"ok": True}


# --- read-only safety ------------------------------------------------------


def test_the_dashboard_connection_cannot_write(settings, seeded):
    with readonly_connection(settings.db_path) as ro:
        with pytest.raises(sqlite3.OperationalError):
            ro.execute("DELETE FROM jobs")


def test_the_dashboard_reads_while_a_write_transaction_is_open(settings, seeded):
    seeded.execute("BEGIN IMMEDIATE")
    seeded.execute("UPDATE jobs SET score = 99 WHERE global_id = 'gh:1'")
    try:
        with readonly_connection(settings.db_path) as ro:
            # WAL: the reader sees the last committed snapshot, and neither
            # side blocks the other.
            row = ro.execute("SELECT score FROM jobs WHERE global_id = 'gh:1'").fetchone()
            assert row["score"] == 91
    finally:
        seeded.rollback()


def test_readonly_connection_refuses_to_create_a_missing_database(settings):
    with pytest.raises(FileNotFoundError):
        with readonly_connection(settings.db_path):
            pass


# --- live updates and the server thread ------------------------------------


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


@pytest.mark.allow_real_network  # loopback to our own server, not a real ATS/manifest call
def test_serve_in_thread_serves_then_stops_cleanly(settings, seeded):
    # Loopback to our own server — not the "no live network calls" that §21
    # bans, which is about real external APIs.
    running = settings.model_copy(update={"dashboard_port": _free_port()})
    server = serve_in_thread(running)
    try:
        body = None
        for _ in range(100):
            try:
                body = httpx.get(f"{server.url}/healthz", timeout=1.0).json()
                break
            except httpx.HTTPError:
                time.sleep(0.05)
        assert body == {"ok": True}

        # The real SSE endpoint, over a real socket where client disconnect
        # actually propagates.
        with httpx.stream("GET", f"{server.url}/api/events", timeout=10.0) as stream:
            assert stream.headers["content-type"].startswith("text/event-stream")
            first = next(line for line in stream.iter_lines() if line.startswith("data: "))
        assert first.split(" ", 1)[1].startswith("6:")  # five seeded rows
    finally:
        server.stop()

    with pytest.raises(httpx.HTTPError):
        httpx.get(f"{server.url}/healthz", timeout=1.0)


def test_a_busy_port_fails_with_an_actionable_message(settings):
    with socket.socket() as taken:
        taken.bind(("127.0.0.1", 0))
        taken.listen(1)
        busy = settings.model_copy(update={"dashboard_port": taken.getsockname()[1]})
        with pytest.raises(OSError, match="cannot bind the dashboard"):
            serve_in_thread(busy)


def test_data_version_token_reflects_row_count_and_latest_timestamp(settings, seeded):
    token = data_version(seeded)
    assert token.startswith("6:")

    upsert_job(seeded, _job("gh:6", score=70, band=ScoreBand.STRONG, company="Zeta"))
    assert data_version(seeded).startswith("6:")


def test_page_assets_revalidate_so_an_upgrade_is_never_stale(client):
    """No build step means no content hash in the filenames, so a cached
    app.js would otherwise outlive an upgrade."""
    for path in ("/", "/static/app.js", "/static/styles.css"):
        assert client.get(path).headers["cache-control"] == "no-cache"


# --- email template preview (realtime editing) -----------------------------


def test_preview_page_is_served(client):
    response = client.get("/preview/email")
    assert response.status_code == 200
    assert "Email template preview" in response.text


def test_preview_renders_the_real_template(client):
    response = client.get("/preview/email/render")
    assert response.status_code == 200
    assert "Coldstart" in response.text
    assert "Template error" not in response.text


def test_preview_can_render_the_plain_text_half(client):
    response = client.get("/preview/email/render", params={"fmt": "text"})
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/plain")
    assert "daily match digest" in response.text


def test_preview_shows_a_template_error_instead_of_hiding_it(client, tmp_path, monkeypatch):
    """This is where you find out you mistyped a template — not at 08:00
    tomorrow."""
    import coldstart.email_template as et

    broken = tmp_path / "email"
    broken.mkdir()
    (broken / "theme.json").write_text((et.TEMPLATE_DIR / "theme.json").read_text())
    (broken / "digest.html.j2").write_text("{% if x %}never closed")
    monkeypatch.setattr(et, "TEMPLATE_DIR", broken)

    response = client.get("/preview/email/render")
    assert response.status_code == 200
    assert "Template error" in response.text
    assert "endif" in response.text


def test_preview_falls_back_to_sample_data_on_an_empty_database(settings):
    """A preview of an empty digest teaches you nothing about the layout."""
    from coldstart.db import connection, init_schema

    with connection(settings.db_path) as conn:
        init_schema(conn)
    body = TestClient(create_app(settings)).get("/preview/email/render").text
    assert "Northwind" in body
    assert "No application link published" in body  # the sample exercises that path


def test_preview_version_changes_when_a_template_is_saved(client, tmp_path, monkeypatch):
    import coldstart.email_template as et

    live = tmp_path / "email"
    live.mkdir()
    for name in ("theme.json", "digest.html.j2", "digest.txt.j2"):
        (live / name).write_text((et.TEMPLATE_DIR / name).read_text())
    monkeypatch.setattr(et, "TEMPLATE_DIR", live)

    before = client.get("/api/preview/version").json()["version"]
    (live / "digest.html.j2").write_text("edited")
    assert client.get("/api/preview/version").json()["version"] != before


# --- theming ---------------------------------------------------------------


def test_theme_script_is_served(client):
    response = client.get("/static/theme.js")
    assert response.status_code == 200
    assert "coldstart-theme" in response.text


def test_the_theme_control_offers_all_three_states(client):
    """System is a real, selectable state — not just the absence of a choice —
    so someone who picked dark at 11pm can get back to following their OS."""
    body = client.get("/").text
    for choice in ("light", "dark", "system"):
        assert f'data-theme-choice="{choice}"' in body
    # Icon-only buttons need labels.
    assert 'aria-label="Light theme"' in body
    assert 'aria-label="Match system theme"' in body
    assert 'aria-pressed' in body


def test_the_theme_is_applied_before_first_paint(client):
    """An inline head script sets data-theme before the stylesheet paints;
    deferring it to app.js would flash the wrong theme on every load."""
    body = client.get("/").text
    head = body[: body.index("</head>")]
    assert "coldstart-theme" in head
    assert "documentElement.dataset.theme" in head


def test_the_preview_page_shares_the_theme_control(client):
    body = client.get("/preview/email").text
    assert 'data-theme-choice="system"' in body
    assert "/static/theme.js" in body
    assert "coldstart-theme" in body[: body.index("</head>")]


def test_every_colour_is_a_token_defined_for_both_modes(client):
    """Dark must be a designed pair, not an inversion — and no colour may be
    defined only inside a media query, or the explicit toggle can't override
    it."""
    css = client.get("/static/styles.css").text
    assert ":root[data-theme=\"dark\"]" in css
    assert ":root:not([data-theme=\"light\"])" in css
    assert "prefers-color-scheme: dark" in css

    def tokens(block: str) -> set[str]:
        return set(re.findall(r"(--[a-z0-9-]+)\s*:", block))

    light = css[css.index(":root {") : css.index("@media (prefers-color-scheme: dark)")]
    dark_start = css.index(':root[data-theme="dark"] {')
    dark = css[dark_start : css.index("}", dark_start)]
    # Every token the dark theme overrides must have a light definition too.
    assert tokens(dark) - tokens(light) == set()


def test_reduced_motion_is_respected(client):
    assert "prefers-reduced-motion: reduce" in client.get("/static/styles.css").text


# --- measured accessibility and the no-sideways-scroll contract -------------
#
# Colour is checked here rather than by eye because "it looks fine in dark
# mode" is the exact reasoning that shipped a 2.91:1 grey in light mode once
# already. Taking the palette from a reference site made that risk worse, not
# better: the reference's own amber measures 2.1:1 on white, which is fine for
# an 80px headline and not fine for a 12px table label.


def _relative_luminance(hex_colour: str) -> float:
    raw = hex_colour.lstrip("#")
    if len(raw) == 3:
        raw = "".join(c * 2 for c in raw)
    channels = [int(raw[i : i + 2], 16) / 255 for i in (0, 2, 4)]
    linear = [c / 12.92 if c <= 0.03928 else ((c + 0.055) / 1.055) ** 2.4 for c in channels]
    return 0.2126 * linear[0] + 0.7152 * linear[1] + 0.0722 * linear[2]


def _contrast(a: str, b: str) -> float:
    high, low = sorted((_relative_luminance(a), _relative_luminance(b)), reverse=True)
    return (high + 0.05) / (low + 0.05)


def _palettes(css: str) -> dict[str, dict[str, str]]:
    def table(start: str, end: str | None) -> dict[str, str]:
        begin = css.index(start)
        stop = css.index(end, begin) if end else css.index("\n}", begin)
        return dict(re.findall(r"(--[a-z0-9-]+)\s*:\s*(#[0-9a-fA-F]{3,6})", css[begin:stop]))

    return {
        "light": table(":root {", "@media (prefers-color-scheme: dark)"),
        "dark": table(':root[data-theme="dark"] {', None),
    }


_HUES = ["teal", "amber", "sky", "violet", "rose", "lime", "slate"]


def test_every_text_colour_clears_wcag_aa_in_both_modes(client):
    css = client.get("/static/styles.css").text
    surfaces = ["--surface", "--surface-2", "--surface-3", "--bg", "--bg-secondary"]
    failures = []

    for mode, tokens in _palettes(css).items():
        pairs = [(text, s) for text in ("--text", "--text-2", "--text-3") for s in surfaces]
        # A hue's ink sits either on its own tint (pills, chips, badges) or
        # straight on a card (tile values, the ATS dot's label).
        pairs += [(f"--h-{h}", f"--h-{h}-bg") for h in _HUES]
        pairs += [(f"--h-{h}", "--surface") for h in _HUES]
        pairs += [(b, f"{b}-bg") for b in ("--strong", "--consider", "--reject", "--danger")]
        pairs += [
            (b, "--surface")
            for b in ("--strong", "--consider", "--reject", "--danger", "--brand", "--amber")
        ]
        for foreground, background in pairs:
            ratio = _contrast(tokens[foreground], tokens[background])
            if ratio < 4.5:
                failures.append(f"{mode}: {foreground} on {background} is {ratio:.2f}:1")

    assert not failures, "below WCAG AA (4.5:1):\n  " + "\n  ".join(failures)


def test_the_table_never_demands_more_width_than_it_is_given(client):
    """The regression guard for the bug this layout exists to fix.

    The table used to declare min-width:1120px inside a horizontally
    scrolling wrapper, so reading a row meant dragging the whole view left
    and right. Columns drop by priority now; nothing may reintroduce a fixed
    floor wider than the narrowest viewport we support."""
    css = client.get("/static/styles.css").text
    # Comments describe the old layout, and a breakpoint asks about the
    # viewport rather than demanding width from it.
    body = re.sub(r"/\*.*?\*/", "", css, flags=re.S)
    body = re.sub(r"@media[^{]*\{", "{", body)
    floors = [int(n) for n in re.findall(r"min-width:\s*(\d+)px", body)]
    assert all(n <= 400 for n in floors), f"fixed width floors too wide: {floors}"
    assert "table-layout: fixed" in css, "auto layout lets one long title widen the table"


def test_every_column_can_be_dropped_by_priority(client):
    """Each column carries a shared `col` class on its <th> and <td>, and the
    stylesheet sizes it. A column added without one would be invisible to the
    priority rules and would silently push the table wide again."""
    js = client.get("/static/app.js").text
    css = client.get("/static/styles.css").text
    columns = re.findall(r'\{\s*key:\s*"[a-z_]+"', js)
    cols = re.findall(r'col:\s*"(c-[a-z]+)"', js)
    assert len(cols) == len(columns) > 0, "a column is missing its `col` class"
    for name in cols:
        assert f".{name}" in css, f"{name} has no width rule"


# --- marking a job applied -------------------------------------------------

_ACT = {"X-Coldstart-Action": "1"}


def test_marking_a_job_applied_persists_and_shows_up_in_the_listing(client, settings):
    response = client.post("/api/jobs/gh:1/state", json={"state": "applied"}, headers=_ACT)
    assert response.status_code == 200
    assert response.json() == {"global_id": "gh:1", "state": "applied"}

    # include_reject: gh:2 (score 64) is reject-band under the binary
    # threshold and would not otherwise appear in this listing.
    jobs = {
        j["global_id"]: j
        for j in client.get("/api/jobs", params={"include_reject": True}).json()["jobs"]
    }
    assert jobs["gh:1"]["state"] == "applied"
    assert jobs["gh:2"]["state"] is None

    assert client.get("/api/metrics").json()["applied"] == 1


def test_the_mark_can_be_cleared(client):
    client.post("/api/jobs/gh:1/state", json={"state": "applied"}, headers=_ACT)
    cleared = client.post("/api/jobs/gh:1/state", json={"state": None}, headers=_ACT)
    assert cleared.status_code == 200
    assert client.get("/api/metrics").json()["applied"] == 0


def test_the_write_needs_the_action_header(client):
    """CSRF guard: the server is loopback-bound with no auth, so without this
    any page you had open could POST here. A cross-origin form can't set a
    custom header."""
    response = client.post("/api/jobs/gh:1/state", json={"state": "applied"})
    assert response.status_code == 403
    assert client.get("/api/metrics").json()["applied"] == 0


def test_an_unknown_state_is_rejected(client):
    response = client.post("/api/jobs/gh:1/state", json={"state": "hired"}, headers=_ACT)
    assert response.status_code == 422
    assert client.get("/api/metrics").json()["applied"] == 0


def test_marking_a_job_that_does_not_exist_is_a_404(client):
    assert client.post(
        "/api/jobs/nope:404/state", json={"state": "applied"}, headers=_ACT
    ).status_code == 404


def test_marking_never_touches_the_jobs_table(client, settings):
    """The dashboard's one write surface stays confined to job_state."""
    from coldstart.db import readonly_connection

    with readonly_connection(settings.db_path) as conn:
        before = conn.execute("SELECT score, status FROM jobs WHERE global_id='gh:1'").fetchone()
    client.post("/api/jobs/gh:1/state", json={"state": "applied"}, headers=_ACT)
    with readonly_connection(settings.db_path) as conn:
        after = conn.execute("SELECT score, status FROM jobs WHERE global_id='gh:1'").fetchone()
    assert tuple(before) == tuple(after)


def test_the_dashboard_offers_open_applied_declined_and_all_views(client):
    body = client.get("/").text
    for view in ("open", "applied", "declined", "all"):
        assert f'data-view="{view}"' in body


def test_the_row_action_is_a_real_button_not_hover_only(client):
    """Hover-only controls are unusable on touch and invisible to keyboards."""
    js = client.get("/static/app.js").text
    assert 'data-mark=' in js
    assert 'aria-pressed' in js
    css = client.get("/static/styles.css").text
    assert ".mark:focus-visible" in css


def test_the_decline_action_is_a_real_button_not_hover_only(client):
    js = client.get("/static/app.js").text
    assert 'data-decline=' in js
    css = client.get("/static/styles.css").text
    assert ".decline:focus-visible" in css


# --- declining a job ---------------------------------------------------

def test_declining_a_job_persists_and_shows_up_in_the_listing(client, settings):
    response = client.post("/api/jobs/gh:1/state", json={"state": "declined"}, headers=_ACT)
    assert response.status_code == 200
    assert response.json() == {"global_id": "gh:1", "state": "declined"}

    # include_reject: gh:2 (score 64) is reject-band under the binary
    # threshold and would not otherwise appear in this listing.
    jobs = {
        j["global_id"]: j
        for j in client.get("/api/jobs", params={"include_reject": True}).json()["jobs"]
    }
    assert jobs["gh:1"]["state"] == "declined"
    assert jobs["gh:2"]["state"] is None

    # A decline is a job_state write like any other — it never touches the
    # `applied` metric.
    assert client.get("/api/metrics").json()["applied"] == 0


def test_a_job_holds_one_state_declining_an_applied_job_replaces_it(client):
    client.post("/api/jobs/gh:1/state", json={"state": "applied"}, headers=_ACT)
    client.post("/api/jobs/gh:1/state", json={"state": "declined"}, headers=_ACT)

    jobs = {j["global_id"]: j for j in client.get("/api/jobs").json()["jobs"]}
    assert jobs["gh:1"]["state"] == "declined"
    assert client.get("/api/metrics").json()["applied"] == 0


# --- grouped views ---------------------------------------------------------


def test_group_definitions_never_look_like_column_definitions(client):
    """A tripwire for a tripwire.

    test_every_column_can_be_dropped_by_priority counts `{ key: "..." }`
    literals in app.js and asserts the count matches the number of `col:`
    classes. Any *other* object literal written with a `key:` property — a
    grouping axis, a sort definition — inflates that count and fails a test
    about column widths, which is a baffling place to land. Pinning the number
    here makes the trap loud instead."""
    js = client.get("/static/app.js").text
    assert len(re.findall(r'\{\s*key:\s*"[a-z_]+"', js)) == 9


def test_the_role_grouping_uses_the_resume_the_router_already_chose(client):
    """The four role families are routing.py's four résumés, not a second
    taxonomy that could drift from it. Adding a résumé E has to fail here
    rather than quietly producing an unlabelled group."""
    js = client.get("/static/app.js").text
    labels = re.search(r"const ROLE_LABELS = \{(.*?)\};", js, re.S).group(1)
    assert set(re.findall(r"(\w+):", labels)) == {resume.value for resume in ResumeId}


def test_the_listing_carries_the_resume_the_role_grouping_needs(client):
    """Grouping by role stays a pure frontend concern only because
    resume_used already ships on every scored row."""
    jobs = client.get("/api/jobs").json()["jobs"]
    assert jobs and all("resume_used" in job for job in jobs)


def test_a_group_header_spans_every_column(client):
    """Both full-width rows — the expanded detail and the group header — take
    their colspan from COLUMNS.length, so a tenth column cannot leave either
    one a cell short."""
    js = client.get("/static/app.js").text
    assert js.count('colspan="${COLUMNS.length}"') == 2
    assert 'colspan="9"' not in js


def test_the_grouped_view_reuses_the_row_markup_rather_than_copying_it(client):
    """Mark-applied, decline, expand-for-detail and the apply link keep
    working inside a group because a grouped row comes from the same
    jobRowHtml() the flat list uses. A second copy would drift from this one
    the first time either changed."""
    js = client.get("/static/app.js").text
    assert js.count("data-mark=") == 1
    assert js.count("data-decline=") == 1
    assert js.count('<tr class="row') == 1


def test_grouping_is_a_separate_axis_from_the_view(client):
    """Which jobs to show and how to arrange them are different questions, so
    they get different controls and compose freely."""
    body = client.get("/").text
    assert 'id="view-seg"' in body and 'id="group-seg"' in body
    for group in ("none", "company", "role"):
        assert f'data-group="{group}"' in body
    # Header rows carry data-gid, never data-group — keeping them distinct
    # stops a future selector matching both the control and the headers.
    assert "data-gid=" in client.get("/static/app.js").text


def test_group_headers_are_real_buttons_that_expose_their_state(client):
    """Same contract the mark and decline actions hold to: reachable by
    keyboard, state announced, not a hover-only affordance."""
    js = client.get("/static/app.js").text
    assert 'class="group-head" type="button"' in js
    assert "aria-expanded" in js
    # The global focus ring covers any button, this one included.
    assert ":where(a, button" in client.get("/static/styles.css").text


def test_the_time_window_is_anchored_to_the_servers_own_day_boundary(client):
    """"Fresh today" is metrics.today_since verbatim and the wider windows are
    derived from it, so the filter and the Fresh jobs tile cannot drift apart.
    Counting in milliseconds would silently anchor to the browser's midnight
    instead, which is a different day for anyone outside the configured
    timezone."""
    body = client.get("/").text
    for window in ("all", "today", "3d", "7d"):
        assert f'value="{window}"' in body
    js = client.get("/static/app.js").text
    assert "today_since" in js
    assert "86400000" not in js, "a day of milliseconds means browser-local time"


def test_every_group_class_the_script_emits_has_a_style(client):
    """The sibling of test_every_column_can_be_dropped_by_priority: a header
    chip with no rule renders as unstyled text in the middle of the table."""
    js = client.get("/static/app.js").text
    css = client.get("/static/styles.css").text
    names = sorted(set(re.findall(r'class="(group-[a-z-]+)"', js)))
    assert names, "no group classes found — did the grouped view move?"
    for name in names:
        assert f".{name}" in css, f"{name} has no style rule"


# --- declining a whole group ----------------------------------------------


def test_declining_a_group_marks_every_job_in_one_request(client):
    """The company-header decline is one decision about many rows, so it is
    one request and one transaction rather than a loop over the per-job
    route."""
    response = client.post(
        "/api/jobs/state",
        json={"state": "declined", "global_ids": ["gh:1", "gh:2"]},
        headers=_ACT,
    )
    assert response.status_code == 200
    assert response.json() == {"updated": 2, "requested": 2, "state": "declined"}

    # include_reject: gh:2 (score 64) is reject-band under the binary
    # threshold and would not otherwise appear in this listing.
    jobs = {
        j["global_id"]: j
        for j in client.get("/api/jobs", params={"include_reject": True}).json()["jobs"]
    }
    assert jobs["gh:1"]["state"] == "declined"
    assert jobs["gh:2"]["state"] == "declined"


def test_a_group_decline_can_be_undone_in_one_request(client):
    """The undo path the confirm dialog promises."""
    client.post(
        "/api/jobs/state",
        json={"state": "declined", "global_ids": ["gh:1", "gh:2"]},
        headers=_ACT,
    )
    cleared = client.post(
        "/api/jobs/state", json={"state": None, "global_ids": ["gh:1", "gh:2"]}, headers=_ACT
    )
    assert cleared.status_code == 200
    assert cleared.json()["updated"] == 2

    # include_reject: gh:2 (score 64) is reject-band under the binary
    # threshold and would not otherwise appear in this listing.
    jobs = {
        j["global_id"]: j
        for j in client.get("/api/jobs", params={"include_reject": True}).json()["jobs"]
    }
    assert jobs["gh:1"]["state"] is None
    assert jobs["gh:2"]["state"] is None


def test_a_group_decline_reports_ids_it_could_not_find(client):
    """A stale tab can hold ids that have since been purged. Writing what
    exists and reporting the difference beats both failing the whole batch and
    silently claiming success."""
    response = client.post(
        "/api/jobs/state",
        json={"state": "declined", "global_ids": ["gh:1", "gone:404"]},
        headers=_ACT,
    )
    assert response.status_code == 200
    assert response.json() == {"updated": 1, "requested": 2, "state": "declined"}


def test_a_group_decline_of_nothing_that_exists_is_a_404(client):
    response = client.post(
        "/api/jobs/state", json={"state": "declined", "global_ids": ["gone:1"]}, headers=_ACT
    )
    assert response.status_code == 404


@pytest.mark.parametrize(
    "payload",
    [
        {"state": "declined", "global_ids": []},
        {"state": "declined", "global_ids": "gh:1"},
        {"state": "declined", "global_ids": [1, 2]},
        {"state": "banished", "global_ids": ["gh:1"]},
        {"state": "declined", "global_ids": ["gh:1"] * 2001},
    ],
)
def test_a_group_decline_rejects_a_malformed_batch(client, payload):
    """Same blast-radius thinking as the single-job route, plus a cap: a whole
    company is a few hundred rows, so a bigger batch is a bug or a stale tab
    rather than an intention."""
    assert client.post("/api/jobs/state", json=payload, headers=_ACT).status_code == 422


def test_a_group_decline_needs_the_csrf_header_like_every_other_write(client):
    response = client.post(
        "/api/jobs/state", json={"state": "declined", "global_ids": ["gh:1"]}
    )
    assert response.status_code == 403


def test_the_decline_all_control_is_offered_on_companies_and_nothing_else(client):
    """A role family is thousands of jobs across every employer and the
    rolled-up bucket is many companies under one heading. Neither is "this
    employer", which is the only thing declining a group can mean."""
    js = client.get("/static/app.js").text
    assert 'const canDeclineGroup = g => g.axis === "company" && g.id !== SINGLES_ID;' in js
    assert "data-decline-group=" in js
