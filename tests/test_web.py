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
    jobs = client.get("/api/jobs").json()["jobs"]
    assert [j["global_id"] for j in jobs] == ["gh:1", "gh:2"]


def test_include_reject_widens_the_listing(client):
    jobs = client.get("/api/jobs", params={"include_reject": True}).json()["jobs"]
    assert [j["global_id"] for j in jobs] == ["gh:1", "gh:2", "gh:3"]


def test_excluded_and_failed_rows_never_appear(client):
    jobs = client.get("/api/jobs", params={"include_reject": True}).json()["jobs"]
    ids = {j["global_id"] for j in jobs}
    assert "gh:4" not in ids and "gh:5" not in ids


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

    assert band_for(65, settings) == "consider"
    strict = settings.model_copy(update={"score_threshold_strong": 60})
    assert band_for(65, strict) == "strong"

    client = TestClient(create_app(strict))
    assert client.get("/api/jobs").json()["jobs"][0]["band"] == "strong"


def test_band_boundaries_are_inclusive_at_the_threshold(settings):
    assert band_for(settings.score_threshold_strong, settings) == "strong"
    assert band_for(settings.score_threshold_strong - 1, settings) == "consider"
    assert band_for(settings.score_threshold_consider, settings) == "consider"
    assert band_for(settings.score_threshold_consider - 1, settings) == "reject"


# --- metrics ---------------------------------------------------------------


def test_metrics_count_bands_and_exclude_reject_from_the_headline(client):
    m = client.get("/api/metrics").json()
    assert (m["total"], m["strong"], m["consider"], m["reject"]) == (2, 1, 1, 1)
    assert m["companies"] == 2          # Gamma is reject-band, not counted
    assert m["max_score"] == 91
    assert m["median_score"] == 77.5


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
            activity="polling",
            consecutive_poll_failures=2,
        )
    )
    body = client.get("/api/status").json()
    assert body["daemon"]["activity"] == "polling"
    assert body["daemon"]["consecutive_poll_failures"] == 2
    assert body["data_version"].startswith("5:")  # five seeded rows


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
        assert first.split(" ", 1)[1].startswith("5:")  # five seeded rows
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
    assert token.startswith("5:")

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


# --- marking a job applied -------------------------------------------------

_ACT = {"X-Coldstart-Action": "1"}


def test_marking_a_job_applied_persists_and_shows_up_in_the_listing(client, settings):
    response = client.post("/api/jobs/gh:1/state", json={"state": "applied"}, headers=_ACT)
    assert response.status_code == 200
    assert response.json() == {"global_id": "gh:1", "state": "applied"}

    jobs = {j["global_id"]: j for j in client.get("/api/jobs").json()["jobs"]}
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


def test_the_dashboard_offers_open_applied_and_all_views(client):
    body = client.get("/").text
    for view in ("open", "applied", "all"):
        assert f'data-view="{view}"' in body


def test_the_row_action_is_a_real_button_not_hover_only(client):
    """Hover-only controls are unusable on touch and invisible to keyboards."""
    js = client.get("/static/app.js").text
    assert 'data-mark=' in js
    assert 'aria-pressed' in js
    css = client.get("/static/styles.css").text
    assert ".mark:focus-visible" in css
