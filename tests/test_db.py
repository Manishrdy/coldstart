import sqlite3
from datetime import UTC, datetime

import pytest

from coldstart.db import (
    connection,
    get_digest_jobs,
    get_slice_state,
    init_schema,
    last_digest_sent_at,
    load_seen_keys,
    log_email,
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


@pytest.fixture
def conn(tmp_path):
    with connection(tmp_path / "test.sqlite3") as c:
        init_schema(c)
        yield c


def _job(**overrides) -> JobRecord:
    kwargs = dict(
        global_id="job-1",
        requisition_id="req-1",
        company="Acme",
        title="Software Engineer",
        location="Remote — US",
        apply_url="https://example.com/apply",
        ats_type="greenhouse",
        posted_at=datetime(2026, 8, 1),
        resume_used=ResumeId.A,
        score=75,
        score_band=ScoreBand.STRONG,
        eligible=True,
        matched_skills=["python", "sql"],
        missing_skills=["go"],
        reasoning="Strong overlap.",
        status=JobStatus.SCORED,
        location_flag=LocationFlag.ACCEPTED,
        eligibility_flag=EligibilityFlag.PASSED,
        provider_used="deepseek",
        first_seen_at=datetime(2026, 8, 1, 9, 0, 0),
        scored_at=datetime(2026, 8, 1, 9, 5, 0),
    )
    kwargs.update(overrides)
    return JobRecord(**kwargs)


def test_init_schema_is_idempotent(conn):
    init_schema(conn)
    init_schema(conn)
    tables = {
        row[0]
        for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")
    }
    assert {"jobs", "errors", "email_log", "slice_state", "spend_log"} <= tables


def test_wal_mode_is_set(conn):
    mode = conn.execute("PRAGMA journal_mode").fetchone()[0]
    assert mode.lower() == "wal"


def test_upsert_job_inserts_then_updates(conn):
    upsert_job(conn, _job())
    row = conn.execute("SELECT score, title FROM jobs WHERE global_id = 'job-1'").fetchone()
    assert row["score"] == 75
    assert row["title"] == "Software Engineer"

    upsert_job(conn, _job(score=90, title="Senior Software Engineer"))
    rows = conn.execute("SELECT score, title FROM jobs WHERE global_id = 'job-1'").fetchall()
    assert len(rows) == 1
    assert rows[0]["score"] == 90
    assert rows[0]["title"] == "Senior Software Engineer"


def test_upsert_job_preserves_first_seen_at_on_update(conn):
    upsert_job(conn, _job())
    upsert_job(conn, _job(first_seen_at=datetime(2099, 1, 1)))
    row = conn.execute("SELECT first_seen_at FROM jobs WHERE global_id = 'job-1'").fetchone()
    assert row["first_seen_at"] == "2026-08-01T09:00:00"


def test_upsert_job_round_trips_skills_and_flags(conn):
    upsert_job(conn, _job())
    [record] = get_digest_jobs(conn, since=datetime(2026, 1, 1))
    assert record.matched_skills == ["python", "sql"]
    assert record.missing_skills == ["go"]
    assert record.resume_used == ResumeId.A
    assert record.score_band == ScoreBand.STRONG
    assert record.eligible is True


def test_load_seen_keys_returns_both_sets(conn):
    upsert_job(
        conn, _job(global_id="job-1", requisition_id="req-1", company="Acme", location="NYC")
    )
    upsert_job(conn, _job(global_id="job-2", requisition_id=None))
    global_ids, req_keys = load_seen_keys(conn)
    assert global_ids == {"job-1", "job-2"}
    assert req_keys == {("Acme", "req-1", "NYC")}


def test_load_seen_keys_req_key_distinguishes_by_company_and_location(conn):
    # Regression for the real-data finding: bare requisition_id collides
    # across unrelated companies and across different-location postings from
    # the same company (DEVELOPMENT_PLAN.md Module 10).
    upsert_job(
        conn,
        _job(global_id="job-1", requisition_id="1", company="svetness", location="Tyler, TX"),
    )
    upsert_job(
        conn,
        _job(global_id="job-2", requisition_id="1", company="svetness", location="Troy, TX"),
    )
    upsert_job(
        conn,
        _job(global_id="job-3", requisition_id="1", company="otherco", location="Tyler, TX"),
    )
    _, req_keys = load_seen_keys(conn)
    assert req_keys == {
        ("svetness", "1", "Tyler, TX"),
        ("svetness", "1", "Troy, TX"),
        ("otherco", "1", "Tyler, TX"),
    }


def test_get_digest_jobs_filters_by_since(conn):
    upsert_job(conn, _job(global_id="old", scored_at=datetime(2020, 1, 1)))
    upsert_job(conn, _job(global_id="new", scored_at=datetime(2027, 1, 1)))
    results = get_digest_jobs(conn, since=datetime(2026, 1, 1))
    assert {r.global_id for r in results} == {"new"}


def test_get_digest_jobs_orders_by_score_desc_nulls_last(conn):
    upsert_job(conn, _job(global_id="low", score=10, scored_at=datetime(2026, 1, 2)))
    upsert_job(conn, _job(global_id="high", score=90, scored_at=datetime(2026, 1, 2)))
    upsert_job(
        conn,
        _job(
            global_id="unscored",
            score=None,
            score_band=None,
            eligible=None,
            status=JobStatus.FAILED,
            scored_at=None,
            first_seen_at=datetime(2026, 1, 2),
        ),
    )
    results = get_digest_jobs(conn, since=datetime(2026, 1, 1))
    assert [r.global_id for r in results] == ["high", "low", "unscored"]


def test_slice_state_roundtrip(conn):
    assert get_slice_state(conn, "greenhouse") is None
    set_slice_state(
        conn,
        SliceState(
            ats_type="greenhouse",
            last_sha256="abc123",
            last_processed_at=datetime(2026, 8, 1),
            row_count=42,
        ),
    )
    state = get_slice_state(conn, "greenhouse")
    assert state.last_sha256 == "abc123"
    assert state.row_count == 42

    set_slice_state(
        conn,
        SliceState(ats_type="greenhouse", last_sha256="def456", row_count=50),
    )
    state = get_slice_state(conn, "greenhouse")
    assert state.last_sha256 == "def456"
    assert state.row_count == 50


def test_log_email_writes_row(conn):
    log_email(conn, sent_at=datetime(2026, 8, 1, 8, 0, 0), job_count=5, status="sent")
    row = conn.execute("SELECT job_count, status FROM email_log").fetchone()
    assert row["job_count"] == 5
    assert row["status"] == "sent"


def test_log_error_writes_row_and_never_raises(tmp_path):
    from coldstart.errors import log_error

    with connection(tmp_path / "err.sqlite3") as c:
        init_schema(c)
        log_error(
            c,
            stage="poll",
            exc=ValueError("boom"),
            source_file="fetcher.py",
            function_name="download_slice",
        )
        row = c.execute("SELECT stage, error_type FROM errors").fetchone()
        assert row["stage"] == "poll"
        assert row["error_type"] == "ValueError"

    closed_conn = sqlite3.connect(":memory:")
    closed_conn.close()
    log_error(
        closed_conn,
        stage="poll",
        message="should not raise",
        source_file="fetcher.py",
        function_name="download_slice",
    )


def test_capture_errors_decorator_swallows_and_logs(tmp_path):
    from coldstart.errors import capture_errors

    with connection(tmp_path / "capture.sqlite3") as c:
        init_schema(c)

        @capture_errors(stage="location_filter")
        def _boom(conn):
            raise RuntimeError("kaboom")

        result = _boom(c)
        assert result is None
        row = c.execute("SELECT stage, error_type FROM errors").fetchone()
        assert row["stage"] == "location_filter"
        assert row["error_type"] == "RuntimeError"


# --- digest window anchor (Module 18 addendum) --------------------------------------


def test_last_digest_sent_at_is_none_before_anything_is_sent(conn):
    assert last_digest_sent_at(conn) is None


def test_last_digest_sent_at_returns_the_most_recent_send(conn):
    first = datetime(2026, 8, 18, 15, 0, tzinfo=UTC)
    second = datetime(2026, 8, 19, 15, 0, tzinfo=UTC)
    log_email(conn, sent_at=first, job_count=3, status="sent")
    log_email(conn, sent_at=second, job_count=5, status="sent")
    assert last_digest_sent_at(conn) == second


def test_a_failed_send_does_not_advance_the_window(conn):
    """If a digest fails, the next successful one has to cover that period
    too — otherwise those jobs are never reported to anyone."""
    good = datetime(2026, 8, 18, 15, 0, tzinfo=UTC)
    log_email(conn, sent_at=good, job_count=3, status="sent")
    log_email(
        conn,
        sent_at=datetime(2026, 8, 19, 15, 0, tzinfo=UTC),
        job_count=0,
        status="failed",
        error="smtp down",
    )
    assert last_digest_sent_at(conn) == good
