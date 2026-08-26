"""Module 28 — the analytics page's queries, the poll heartbeat, and the
run-log fix that made the funnel real.
"""

from __future__ import annotations

import json
import os
from datetime import UTC, datetime, timedelta

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
    set_daemon_state,
    set_job_state,
    set_slice_state,
    upsert_job,
)
from coldstart.models import (
    EligibilityFlag,
    JobRecord,
    JobStatus,
    LocationFlag,
    ResumeId,
    ScoreBand,
    SliceState,
)
from coldstart.progress import PollProgress, read_progress, write_progress
from coldstart.settings import Settings
from coldstart.web import analytics as analytics_queries
from coldstart.web.analytics import analytics
from coldstart.web.app import create_app


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
        timezone="America/Los_Angeles",
    )


@pytest.fixture(autouse=True)
def _no_daemon_state():
    daemon._set_state(None)
    yield
    daemon._set_state(None)


def _job(global_id, *, score=None, status=JobStatus.SCORED, company="Acme", **extra):
    now = extra.pop("now", None) or datetime.now(UTC)
    return JobRecord(
        global_id=global_id,
        requisition_id=extra.pop("requisition_id", "R-1"),
        company=company,
        title=extra.pop("title", "Software Engineer"),
        location=extra.pop("location", "Austin, TX"),
        apply_url="https://example.com/apply",
        ats_type=extra.pop("ats_type", "greenhouse"),
        posted_at=now - timedelta(days=3),
        resume_used=extra.pop("resume_used", ResumeId.A),
        score=score,
        score_band=extra.pop("band", ScoreBand.STRONG if (score or 0) >= 70 else None),
        eligible=extra.pop("eligible", True),
        matched_skills=extra.pop("matched_skills", ["Python", "FastAPI"]),
        missing_skills=extra.pop("missing_skills", ["Kubernetes"]),
        reasoning="Solid overlap.",
        status=status,
        location_flag=extra.pop("location_flag", LocationFlag.ACCEPTED),
        location_reason=extra.pop("location_reason", None),
        eligibility_flag=extra.pop("eligibility_flag", EligibilityFlag.PASSED),
        provider_used="deepseek",
        first_seen_at=now,
        scored_at=now if status is JobStatus.SCORED else None,
        delist_reason=extra.pop("delist_reason", None),
        delisted_at=extra.pop("delisted_at", None),
    )


@pytest.fixture
def seeded(settings):
    with connection(settings.db_path) as conn:
        init_schema(conn)
        upsert_job(conn, _job("gh:1", score=91, company="Alpha"))
        upsert_job(conn, _job("gh:2", score=64, company="Beta", resume_used=ResumeId.B))
        upsert_job(conn, _job("gh:3", score=22, company="Gamma"))
        upsert_job(
            conn,
            _job("gh:4", status=JobStatus.EXCLUDED, company="Delta",
                 eligibility_flag=EligibilityFlag.EXCLUDED),
        )
        upsert_job(
            conn,
            _job("gh:5", status=JobStatus.EXCLUDED_LOCATION, company="Zeta",
                 location="2 Locations", location_flag=LocationFlag.UNCERTAIN,
                 location_reason="unresolved", eligibility_flag=EligibilityFlag.UNCERTAIN),
        )
        upsert_job(
            conn,
            _job("wd:6", status=JobStatus.DELISTED, company="Eta", ats_type="workday",
                 delist_reason="workday_cxs_403", delisted_at=datetime.now(UTC),
                 eligibility_flag=EligibilityFlag.UNCERTAIN),
        )
        # A scored job on a platform this project cannot verify, so the
        # liveness coverage split has something on both sides.
        upsert_job(conn, _job("ic:7", score=88, company="Theta", ats_type="icims"))
        yield conn


def _read(settings):
    with readonly_connection(settings.db_path) as conn:
        return analytics(conn, settings)


# --- endpoint ---------------------------------------------------------------


def test_endpoint_returns_every_section(settings, seeded):
    payload = TestClient(create_app(settings)).get("/api/analytics").json()
    assert payload["db_ready"] is True
    assert set(payload) >= {
        "generated_at", "config", "live", "database", "funnel", "sources", "scoring",
        "spend", "liveness", "location", "timeline", "digests", "errors", "companies",
        "decisions", "daemon",
    }
    assert payload["daemon"] is None  # no daemon attached in a test process


def test_missing_database_is_an_empty_state_not_a_500(settings):
    client = TestClient(create_app(settings))  # db_path never created
    response = client.get("/api/analytics")
    assert response.status_code == 200
    assert response.json() == {"db_ready": False}


def test_the_page_itself_is_served(settings, seeded):
    response = TestClient(create_app(settings)).get("/analytics")
    assert response.status_code == 200
    assert b"Coldstart" in response.content


def test_config_summary_carries_no_secrets(settings, seeded):
    config = _read(settings)["config"]
    blob = json.dumps(config).lower()
    for secret in ("app-pw", "api_key", "password", "smtp_user", "user@example.com"):
        assert secret not in blob


# --- local days, not UTC days ----------------------------------------------


@freeze_time("2026-08-25T12:00:00Z")
def test_days_are_bucketed_in_the_operators_timezone_not_utc(settings):
    """03:00 UTC is still *yesterday* evening in America/Los_Angeles.

    Bucketing on `substr(ts, 1, 10)` would file an evening's work under
    tomorrow and put the daily charts a day out of step with the digest
    window and the spend ceiling, which both already work in local days."""
    with connection(settings.db_path) as conn:
        init_schema(conn)
        for ts in ("2026-08-25T03:00:00+00:00", "2026-08-25T08:00:00+00:00"):
            conn.execute(
                "INSERT INTO spend_log (ts, provider, model, input_tokens, cached_tokens, "
                "output_tokens, est_cost_usd) VALUES (?, 'deepseek', 'm', 10, 0, 5, 0.0)",
                (ts,),
            )
        conn.commit()

    per_day = {row["day"]: row["calls"] for row in _read(settings)["spend"]["per_day"]}
    assert per_day["2026-08-24"] == 1   # 03:00Z == 20:00 the previous day, PDT
    assert per_day["2026-08-25"] == 1   # 08:00Z == 01:00 the same day, PDT


@freeze_time("2026-08-25T12:00:00Z")
def test_daily_series_are_dense_so_a_quiet_day_shows_as_a_gap(settings, seeded):
    per_day = _read(settings)["timeline"]["per_day"]
    assert len(per_day) == 30
    assert [row["day"] for row in per_day] == sorted(row["day"] for row in per_day)
    assert per_day[-1]["day"] == "2026-08-25"


# --- sources ----------------------------------------------------------------


def _cache_manifest(conn, entries: dict) -> None:
    set_daemon_state(conn, "manifest_body", json.dumps({"by_ats": entries}))


def _entry(sha: str, rows: int = 100) -> dict:
    return {
        "parquet": "https://example.com/x.parquet",
        "parquet_sha256": sha,
        "parquet_size_bytes": 4096,
        "rows": rows,
    }


def test_sources_joins_the_manifest_slice_state_and_jobs(settings, seeded, monkeypatch):
    monkeypatch.setattr(analytics_queries, "_load_excluded_ats", set)
    _cache_manifest(seeded, {"greenhouse": _entry("a" * 64)})
    set_slice_state(
        seeded,
        SliceState(
            ats_type="greenhouse",
            last_sha256="a" * 64,
            last_processed_at=datetime.now(UTC),
            row_count=100,
        ),
    )

    rows = {row["ats_type"]: row for row in _read(settings)["sources"]["rows"]}
    greenhouse = rows["greenhouse"]
    assert greenhouse["status"] == "up_to_date"
    assert greenhouse["manifest_rows"] == 100          # from the manifest
    assert greenhouse["last_processed_at"] is not None  # from slice_state
    assert greenhouse["scored"] == 3                    # from jobs
    assert greenhouse["shortlisted"] == 2               # gh:3 scored 22 — reject band
    assert greenhouse["held_back"] == 1
    assert greenhouse["liveness_checkable"] is True


def test_a_source_whose_snapshot_moved_is_outstanding(settings, seeded, monkeypatch):
    monkeypatch.setattr(analytics_queries, "_load_excluded_ats", set)
    _cache_manifest(seeded, {"greenhouse": _entry("b" * 64)})
    set_slice_state(
        seeded,
        SliceState(ats_type="greenhouse", last_sha256="a" * 64,
                   last_processed_at=datetime.now(UTC), row_count=100),
    )
    rows = {row["ats_type"]: row for row in _read(settings)["sources"]["rows"]}
    assert rows["greenhouse"]["status"] == "outstanding"


def test_a_source_never_processed_still_appears_with_a_null_date(settings, seeded, monkeypatch):
    """The question is "when was lever last done", and "never" is an answer.

    A join that only walked slice_state would drop the row entirely, which
    reads as "no such source" rather than "this one has never run"."""
    monkeypatch.setattr(analytics_queries, "_load_excluded_ats", set)
    _cache_manifest(seeded, {"lever": _entry("c" * 64, rows=42)})

    rows = {row["ats_type"]: row for row in _read(settings)["sources"]["rows"]}
    assert rows["lever"]["status"] == "never_processed"
    assert rows["lever"]["last_processed_at"] is None
    assert rows["lever"]["manifest_rows"] == 42


def test_sources_flag_platforms_excluded_by_config(settings, seeded, monkeypatch):
    monkeypatch.setattr(analytics_queries, "_load_excluded_ats", lambda: {"amazon"})
    _cache_manifest(seeded, {"amazon": _entry("d" * 64), "greenhouse": _entry("a" * 64)})

    rows = {row["ats_type"]: row for row in _read(settings)["sources"]["rows"]}
    assert rows["amazon"]["status"] == "excluded_by_config"
    assert rows["amazon"]["excluded_by_config"] is True
    assert rows["greenhouse"]["excluded_by_config"] is False


def test_the_real_excluded_ats_config_is_readable(settings, seeded):
    """The path is resolved relative to this file, four parents up. A rename
    or a move would silently return an empty set and quietly stop excluding
    anything, which is a wrong page rather than a crash."""
    assert len(analytics_queries._load_excluded_ats()) > 0


def test_the_work_queue_lists_every_source_in_the_order_it_will_run(
    settings, seeded, monkeypatch
):
    """Run order, not size order (Module 29): the sequence is the operator's
    to set, so showing anything else would be showing a fiction."""
    monkeypatch.setattr(analytics_queries, "_load_excluded_ats", set)
    _cache_manifest(
        seeded,
        {"lever": _entry("c" * 64, rows=10), "workday": _entry("e" * 64, rows=900)},
    )
    live = _read(settings)["live"]
    assert live["outstanding_slices"] == 2
    assert [row["ats_type"] for row in live["queue"]["rows"]] == ["lever", "workday"]
    assert [row["position"] for row in live["queue"]["rows"]] == [1, 2]


def test_a_zero_row_slice_is_not_counted_as_work(settings, seeded, monkeypatch):
    monkeypatch.setattr(analytics_queries, "_load_excluded_ats", set)
    _cache_manifest(seeded, {"empty": _entry("f" * 64, rows=0)})
    live = _read(settings)["live"]
    assert live["outstanding_slices"] == 0
    # Not offered as something the operator could choose to run, either.
    assert live["queue"]["rows"] == []


# --- funnel -----------------------------------------------------------------


def test_the_persisted_funnel_works_with_an_empty_run_log(settings, seeded):
    """The real database had 0 run_log rows and 3,700 scored jobs. A page
    that could only read run_log would have shown an all-zero funnel."""
    funnel = _read(settings)["funnel"]
    assert funnel["runs"]["all"]["runs"] == 0
    stages = {row["stage"]: row["count"] for row in funnel["persisted"]}
    assert stages["Scored"] == 4
    assert stages["Shortlisted"] == 3
    assert stages["Strong"] == 2
    assert stages["Held back (location)"] == 1
    assert stages["Delisted (gone at source)"] == 1


def test_the_run_funnel_reads_run_log(settings, seeded):
    log_run(
        seeded,
        run_id="abc",
        started_at=datetime.now(UTC),
        finished_at=datetime.now(UTC) + timedelta(seconds=90),
        fetched_count=33186,
        filtered_count=412,
        scored_count=5,
        failed_count=1,
    )
    funnel = _read(settings)["funnel"]
    assert funnel["runs"]["all"] == {
        "runs": 1, "fetched": 33186, "filtered": 412, "scored": 5, "failed": 1
    }
    assert funnel["recent_runs"][0]["duration_seconds"] == 90


# --- scoring ----------------------------------------------------------------


def test_the_histogram_bands_buckets_by_the_operators_thresholds(settings, seeded):
    scoring = _read(settings)["scoring"]
    buckets = {row["bucket"]: row for row in scoring["histogram"]}
    assert buckets[90]["count"] == 1 and buckets[90]["band"] == "strong"
    assert buckets[80]["count"] == 1 and buckets[80]["band"] == "strong"
    assert buckets[60]["count"] == 1 and buckets[60]["band"] == "consider"
    assert buckets[20]["count"] == 1 and buckets[20]["band"] == "reject"
    assert scoring["bands"] == {"strong": 2, "consider": 1, "reject": 1}


def test_percentiles_come_out_of_the_scored_set(settings, seeded):
    scoring = _read(settings)["scoring"]
    assert scoring["min"] == 22
    assert scoring["max"] == 91
    assert scoring["median"] == 88


def test_resume_routing_is_reported_per_slot(settings, seeded):
    by_resume = {row["resume"]: row for row in _read(settings)["scoring"]["by_resume"]}
    assert by_resume["A"]["count"] == 3
    assert by_resume["B"]["count"] == 1
    assert by_resume["B"]["shortlisted"] == 1


def test_skills_are_counted_across_every_scored_job(settings, seeded):
    scoring = _read(settings)["scoring"]
    assert {row["key"]: row["count"] for row in scoring["top_matched_skills"]}["Python"] == 4
    assert {row["key"]: row["count"] for row in scoring["top_missing_skills"]}["Kubernetes"] == 4


# --- spend ------------------------------------------------------------------


def _spend_row(conn, model: str, cost: float, cached: int = 400) -> None:
    conn.execute(
        "INSERT INTO spend_log (ts, provider, model, input_tokens, cached_tokens, "
        "output_tokens, est_cost_usd) VALUES (?, 'deepseek', ?, 1000, ?, 500, ?)",
        (datetime.now(UTC).isoformat(), model, cached, cost),
    )
    conn.commit()


def test_an_unpriced_model_is_reported_as_unmeasured_not_as_zero(settings, seeded):
    """`deepseek-v4-flash` has no PRICING entry, so every est_cost_usd is 0.
    Rendering "$0.00 spent" would read as "nothing was spent"."""
    _spend_row(seeded, "deepseek-v4-flash", 0.0)
    spend = _read(settings)["spend"]

    assert spend["cost_is_measured"] is False
    assert spend["unpriced_models"] == ["deepseek-v4-flash"]
    assert spend["per_model"][0]["priced"] is False
    # Tokens are real regardless — they come from the provider's usage object.
    assert spend["totals"]["input"] == 1000
    assert spend["totals"]["output"] == 500
    assert spend["cache_hit_rate"] == 40.0


def test_a_priced_model_reports_measured_cost(settings, seeded):
    _spend_row(seeded, "deepseek-chat", 0.25)
    spend = _read(settings)["spend"]
    assert spend["cost_is_measured"] is True
    assert spend["unpriced_models"] == []
    assert spend["totals"]["cost"] == 0.25
    assert spend["ceiling_used_pct"] == pytest.approx(0.25 / 3.0 * 100, abs=0.1)


# --- liveness ---------------------------------------------------------------


def test_liveness_splits_the_shortlist_by_whether_it_can_be_verified(settings, seeded):
    """Only workday, greenhouse and lever expose an API this can check, so a
    shortlisted icims job can be dead with no way to find out."""
    liveness = _read(settings)["liveness"]
    assert liveness["shortlisted_checkable"] == 2   # gh:1, gh:2
    assert liveness["shortlisted_unverifiable"] == 1  # ic:7 (icims)


def test_delist_reasons_are_grouped(settings, seeded):
    liveness = _read(settings)["liveness"]
    assert liveness["total_delisted"] == 1
    assert liveness["by_reason"] == [{"key": "workday_cxs_403", "count": 1}]
    assert liveness["by_ats"] == [{"key": "workday", "count": 1}]


def test_the_sweep_queue_skips_jobs_you_have_already_acted_on(settings, seeded):
    before = _read(settings)["liveness"]["sweep_queue"]
    set_job_state(seeded, "gh:1", "applied")
    assert _read(settings)["liveness"]["sweep_queue"] == before - 1


def test_the_next_sweep_is_derived_from_the_last_one(settings, seeded):
    last = datetime.now(UTC) - timedelta(hours=1)
    set_daemon_state(seeded, "liveness_sweep_last_run_at", last.isoformat())
    liveness = _read(settings)["liveness"]
    expected = last + timedelta(hours=settings.liveness_sweep_interval_hours)
    assert datetime.fromisoformat(liveness["next_sweep_at"]) == expected


# --- location ---------------------------------------------------------------


def test_unresolved_is_counted_apart_from_rules_that_actually_fired(settings, seeded):
    """"unresolved" means no rule matched — a lexicon gap that may be holding
    real US jobs back. A rule that fired deliberately is a different fact."""
    location = _read(settings)["location"]
    assert location["total_held_back"] == 1
    assert location["unresolved"] == 1
    assert location["recent_unresolved"][0]["company"] == "Zeta"


# --- decisions, digests, errors ---------------------------------------------


def test_decisions_count_applied_declined_and_untouched(settings, seeded):
    set_job_state(seeded, "gh:1", "applied")
    set_job_state(seeded, "gh:2", "declined")
    decisions = _read(settings)["decisions"]
    assert (decisions["applied"], decisions["declined"]) == (1, 1)
    assert decisions["shortlisted"] == 3
    assert decisions["untouched"] == 1


def test_failed_sends_are_reported_apart_from_successful_ones(settings, seeded):
    """A failed send never advances the digest window, so those jobs are
    still owed to the next email rather than lost — worth showing plainly."""
    log_email(seeded, sent_at=datetime.now(UTC), job_count=7, status="sent")
    log_email(seeded, sent_at=datetime.now(UTC), job_count=0, status="failed", error="smtp down")
    digests = _read(settings)["digests"]
    assert (digests["sent"], digests["failed"], digests["total"]) == (1, 1, 2)
    assert digests["jobs_emailed"] == 7
    assert digests["last_job_count"] == 7


def test_errors_are_grouped_and_listed(settings, seeded):
    from coldstart.errors import log_error

    log_error(seeded, stage="poll", message="boom", source_file="x.py", function_name="f")
    log_error(seeded, stage="daemon", message="bang", source_file="y.py", function_name="g")
    errors = _read(settings)["errors"]
    assert errors["total"] == 2
    assert errors["unresolved"] == 2
    assert {row["key"] for row in errors["by_stage"]} == {"poll", "daemon"}
    assert len(errors["recent"]) == 2


def test_database_section_counts_every_status(settings, seeded):
    database = _read(settings)["database"]
    assert database["total_jobs"] == 7
    assert database["by_status"]["scored"] == 4
    assert database["by_status"]["excluded_location"] == 1
    assert database["by_status"]["delisted"] == 1
    assert database["size_bytes"] > 0
    assert database["tables"]["jobs"] == 7


# --- the poll heartbeat -----------------------------------------------------


def _progress(**overrides) -> PollProgress:
    now = datetime.now(UTC)
    fields = dict(run_id="r1", pid=os.getpid(), started_at=now, updated_at=now)
    fields.update(overrides)
    return PollProgress(**fields)


def test_a_fresh_heartbeat_from_a_live_process_reads_as_running(settings, seeded):
    write_progress(seeded, _progress(phase="scoring", ats_type="workday", scored=12))
    live = _read(settings)["live"]["progress"]
    assert live["is_running"] is True
    assert live["was_killed"] is False
    assert live["ats_type"] == "workday"
    assert live["scored"] == 12


def test_a_stale_heartbeat_never_reads_as_running(settings, seeded):
    """A poll killed by its timeout leaves its last heartbeat behind
    untouched. Trusting the row's presence would show "running" forever and
    hide exactly the stall this is meant to surface."""
    write_progress(seeded, _progress(updated_at=datetime.now(UTC) - timedelta(hours=2)))
    live = _read(settings)["live"]["progress"]
    assert live["is_running"] is False
    assert live["was_killed"] is True


def test_a_heartbeat_from_a_dead_process_never_reads_as_running(settings, seeded):
    # pid 0 is not a real process id on any platform this runs on.
    write_progress(seeded, _progress(pid=2**22))
    assert _read(settings)["live"]["progress"]["is_running"] is False


def test_a_finished_run_is_not_running_however_recent(settings, seeded):
    write_progress(seeded, _progress(finished_at=datetime.now(UTC), phase="done"))
    live = _read(settings)["live"]["progress"]
    assert live["is_running"] is False
    assert live["was_killed"] is False


def test_unreadable_progress_is_ignored_rather_than_fatal(settings, seeded):
    set_daemon_state(seeded, "poll_progress", "{not json")
    assert read_progress(seeded) is None
    assert _read(settings)["live"]["progress"] is None


def test_writing_progress_never_raises(settings, seeded):
    """A poll must not die because its own diagnostics hit a locked
    database — a missed heartbeat costs a briefly stale page, nothing more."""
    with readonly_connection(settings.db_path) as readonly:
        write_progress(readonly, _progress())  # would raise on a real write


# --- the page's own assets --------------------------------------------------
#
# Three regression guards, one per real bug found while building this page.
# All three are inheritance bugs: analytics.html loads styles.css first, so
# rules written for the jobs page's dense fixed-height table silently apply
# to a scrolling report as well.


@pytest.fixture
def assets(settings, seeded) -> TestClient:
    return TestClient(create_app(settings))


def _css(client, name: str) -> str:
    response = client.get(f"/static/{name}")
    assert response.status_code == 200
    return response.text


def test_the_analytics_page_undoes_the_fixed_height_layout(assets):
    """styles.css makes the jobs page a fixed-height flex column above 900px
    (`body { overflow: hidden }`, table scrolls inside its wrapper). Inherited
    unchanged, that made this page physically unscrollable past the fold."""
    css = _css(assets, "analytics.css")
    assert "body.report-page" in css
    assert "overflow: visible" in css
    # Must not be written as html:has(...) — support for it is not the point,
    # the point is that a bare `html` selector in this file is already scoped
    # to this page, since nothing else loads it.
    assert "html { height: auto; }" in css


def test_the_analytics_tables_do_not_inherit_fixed_column_widths(assets):
    """styles.css sets `table { table-layout: fixed }` for the jobs grid, so
    one long title can never widen it. These tables have a dozen narrow
    numeric columns; fixed layout divided the width equally and clipped
    "Last processed" to "Last processe"."""
    css = _css(assets, "analytics.css")
    assert "table-layout: auto" in css
    assert "min-width: max-content" in css


def test_the_topbar_actions_wrap_so_the_theme_switcher_stays_reachable(assets):
    """Measured at 375px: the action row was 428px wide and the theme
    switcher — the last item — sat entirely off-screen, with
    `html { overflow-x: clip }` hiding it rather than letting you scroll."""
    css = _css(assets, "styles.css")
    block = css[css.index(".topbar-actions {") :]
    block = block[: block.index("}")]
    assert "flex-wrap: wrap" in block


def test_the_analytics_page_never_demands_more_width_than_it_is_given(assets):
    """Same contract as the jobs table: no fixed floor wider than the
    narrowest viewport supported."""
    import re

    css = re.sub(r"/\*.*?\*/", "", _css(assets, "analytics.css"), flags=re.S)
    css = re.sub(r"@media[^{]*\{", "{", css)
    floors = [int(n) for n in re.findall(r"min-width:\s*(\d+)px", css)]
    assert all(n <= 400 for n in floors), f"fixed width floors too wide: {floors}"


def test_analytics_colours_are_all_tokens_from_the_shared_palette(assets):
    """Nothing here may hard-code a colour.

    The charts are inline SVG whose fills are `var(--h-teal)` and friends,
    which is the entire reason they follow the light/dark switch with no JS.
    A literal hex would look right in whichever theme it was written in and
    wrong in the other — and it would escape the palette contrast test in
    test_web.py, which only measures the tokens."""
    import re

    for name in ("analytics.css", "analytics.js"):
        body = re.sub(r"/\*.*?\*/", "", _css(assets, name), flags=re.S)
        body = re.sub(r"^\s*//.*$", "", body, flags=re.M)
        literals = re.findall(r"#[0-9a-fA-F]{3,8}\b", body)
        # `#` also starts a CSS id selector and a DOM lookup; only a literal
        # that parses as a colour matters.
        colours = [lit for lit in literals if len(lit) in (4, 7, 9)]
        assert not colours, f"{name} hard-codes colours: {colours}"


def test_both_pages_link_to_each_other(assets):
    jobs = assets.get("/static/index.html").text
    page = assets.get("/static/analytics.html").text
    assert 'href="/analytics"' in jobs
    assert 'href="/"' in page
    assert 'aria-current="page"' in page
