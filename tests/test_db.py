import sqlite3
from datetime import UTC, datetime

import pytest

from coldstart.db import (
    clear_job_state,
    connection,
    get_digest_jobs,
    get_slice_state,
    init_schema,
    job_states,
    last_digest_sent_at,
    load_seen_keys,
    log_email,
    mark_delisted,
    scored_jobs_for_liveness_check,
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


# --- job state (applied) ---------------------------------------------------


def test_job_state_round_trips(conn):
    set_job_state(conn, "gh:1", "applied")
    assert job_states(conn) == {"gh:1": "applied"}

    clear_job_state(conn, "gh:1")
    assert job_states(conn) == {}


def test_declined_is_a_valid_job_state(conn):
    set_job_state(conn, "gh:1", "declined")
    assert job_states(conn) == {"gh:1": "declined"}

    clear_job_state(conn, "gh:1")
    assert job_states(conn) == {}


def test_declining_an_applied_job_replaces_the_applied_mark(conn):
    """job_state has one row per global_id, so a job holds exactly one state —
    declining something you'd marked applied overwrites it rather than
    stacking both."""
    set_job_state(conn, "gh:1", "applied")
    set_job_state(conn, "gh:1", "declined")
    assert job_states(conn) == {"gh:1": "declined"}


def test_marking_the_same_job_twice_is_idempotent(conn):
    set_job_state(conn, "gh:1", "applied")
    set_job_state(conn, "gh:1", "applied", note="second time")
    rows = conn.execute("SELECT global_id, note FROM job_state").fetchall()
    assert len(rows) == 1
    assert rows[0]["note"] == "second time"


def test_an_unknown_state_is_refused(conn):
    with pytest.raises(ValueError, match="unknown job state"):
        set_job_state(conn, "gh:1", "abducted")
    assert job_states(conn) == {}


def test_clearing_a_job_that_was_never_marked_is_a_no_op(conn):
    clear_job_state(conn, "never-seen")
    assert job_states(conn) == {}


def test_rescoring_a_job_does_not_clear_its_applied_mark(conn):
    """The reason this lives in its own table. `jobs` is pipeline output and
    upsert_job's ON CONFLICT rewrites every column, so an "applied" flag
    stored there would vanish the next time the posting was re-scored."""
    job = JobRecord(
        global_id="gh:1",
        requisition_id="R1",
        company="Acme",
        title="Senior Software Engineer",
        location="Austin, TX",
        apply_url="https://example.com/apply",
        ats_type="greenhouse",
        posted_at=datetime(2026, 8, 18, tzinfo=UTC),
        resume_used=ResumeId.A,
        score=80,
        score_band=ScoreBand.STRONG,
        eligible=True,
        matched_skills=["Python"],
        missing_skills=[],
        reasoning="Good.",
        status=JobStatus.SCORED,
        location_flag=LocationFlag.ACCEPTED,
        eligibility_flag=EligibilityFlag.PASSED,
        provider_used="fake",
        first_seen_at=datetime(2026, 8, 20, tzinfo=UTC),
        scored_at=datetime(2026, 8, 20, tzinfo=UTC),
    )
    upsert_job(conn, job)
    set_job_state(conn, "gh:1", "applied")

    upsert_job(conn, job.model_copy(update={"score": 95, "reasoning": "Even better."}))

    assert job_states(conn) == {"gh:1": "applied"}
    assert conn.execute("SELECT score FROM jobs WHERE global_id='gh:1'").fetchone()[0] == 95


# --- additive column migration ---------------------------------------------


def _legacy_jobs_table(conn) -> None:
    """The `jobs` table exactly as it stood before location_reason existed."""
    conn.execute(
        """
        CREATE TABLE jobs (
          global_id        TEXT PRIMARY KEY,
          requisition_id   TEXT,
          company          TEXT NOT NULL,
          title            TEXT NOT NULL,
          location         TEXT,
          apply_url        TEXT,
          ats_type         TEXT NOT NULL,
          posted_at        TEXT,
          resume_used      TEXT,
          score            INTEGER,
          score_band       TEXT,
          eligible         INTEGER,
          matched_skills   TEXT,
          missing_skills   TEXT,
          reasoning        TEXT,
          status           TEXT NOT NULL,
          location_flag    TEXT NOT NULL,
          eligibility_flag TEXT NOT NULL,
          provider_used    TEXT,
          first_seen_at    TEXT NOT NULL,
          scored_at        TEXT
        )
        """
    )
    conn.execute(
        "INSERT INTO jobs (global_id, company, title, ats_type, status, location_flag,"
        " eligibility_flag, first_seen_at, score) VALUES"
        " ('old:1', 'Acme', 'Engineer', 'greenhouse', 'scored', 'accepted', 'passed',"
        " '2026-08-01T00:00:00+00:00', 88)"
    )
    conn.commit()


def test_init_schema_adds_location_reason_to_a_preexisting_table(tmp_path):
    # CREATE TABLE IF NOT EXISTS is a no-op on an existing table, so without the
    # ALTER the live database would silently lack the column.
    with connection(tmp_path / "legacy.sqlite3") as conn:
        _legacy_jobs_table(conn)
        assert "location_reason" not in {
            row["name"] for row in conn.execute("PRAGMA table_info(jobs)")
        }

        init_schema(conn)

        assert "location_reason" in {
            row["name"] for row in conn.execute("PRAGMA table_info(jobs)")
        }
        # The existing row must survive the migration untouched.
        row = conn.execute("SELECT * FROM jobs WHERE global_id = 'old:1'").fetchone()
        assert row["company"] == "Acme"
        assert row["score"] == 88
        assert row["location_reason"] is None


def test_init_schema_migration_is_idempotent(tmp_path):
    with connection(tmp_path / "legacy.sqlite3") as conn:
        _legacy_jobs_table(conn)
        init_schema(conn)
        init_schema(conn)
        assert conn.execute("SELECT COUNT(*) FROM jobs").fetchone()[0] == 1


def test_location_reason_round_trips(tmp_path):
    with connection(tmp_path / "jobs.sqlite3") as conn:
        init_schema(conn)
        upsert_job(
            conn,
            JobRecord(
                global_id="held:1",
                company="ABB",
                title="Junior Engineer",
                location="2 Locations",
                ats_type="workday",
                status=JobStatus.EXCLUDED_LOCATION,
                location_flag=LocationFlag.UNCERTAIN,
                location_reason="workday_path_foreign",
                eligibility_flag=EligibilityFlag.UNCERTAIN,
                first_seen_at=datetime(2026, 8, 23, tzinfo=UTC),
            ),
        )
        jobs = get_digest_jobs(conn, datetime(2026, 8, 1, tzinfo=UTC))
        assert [(j.status, j.location_reason) for j in jobs] == [
            (JobStatus.EXCLUDED_LOCATION, "workday_path_foreign")
        ]


def test_mark_delisted_flips_status_but_preserves_score(conn):
    # The whole point of a dedicated UPDATE rather than upsert_job: the
    # sweep only knows the check outcome, never the score/reasoning, and
    # must not clobber them.
    upsert_job(conn, _job(global_id="job-1", score=91, reasoning="Great fit."))

    mark_delisted(conn, "job-1", "workday_cxs_403", datetime(2026, 8, 24, tzinfo=UTC))

    row = conn.execute(
        "SELECT status, score, reasoning, delist_reason, delisted_at FROM jobs"
        " WHERE global_id = 'job-1'"
    ).fetchone()
    assert row["status"] == "delisted"
    assert row["score"] == 91
    assert row["reasoning"] == "Great fit."
    assert row["delist_reason"] == "workday_cxs_403"
    assert row["delisted_at"] == "2026-08-24T00:00:00+00:00"


def test_scored_jobs_for_liveness_check_excludes_actioned_and_wrong_status(conn):
    upsert_job(
        conn, _job(global_id="checkable", ats_type="workday", status=JobStatus.SCORED)
    )
    upsert_job(
        conn,
        _job(global_id="actioned", ats_type="workday", status=JobStatus.SCORED),
    )
    set_job_state(conn, "actioned", "applied")
    upsert_job(
        conn, _job(global_id="not-scored", ats_type="workday", status=JobStatus.EXCLUDED)
    )
    upsert_job(
        conn, _job(global_id="uncheckable-ats", ats_type="icims", status=JobStatus.SCORED)
    )

    candidates = scored_jobs_for_liveness_check(conn, frozenset({"workday", "greenhouse"}))

    assert [c.global_id for c in candidates] == ["checkable"]


def test_scored_jobs_for_liveness_check_empty_ats_types_returns_nothing(conn):
    upsert_job(conn, _job(global_id="job-1", ats_type="workday", status=JobStatus.SCORED))
    assert scored_jobs_for_liveness_check(conn, frozenset()) == []


def test_delisted_status_and_reason_round_trip_through_upsert(tmp_path):
    with connection(tmp_path / "jobs.sqlite3") as conn:
        init_schema(conn)
        upsert_job(
            conn,
            JobRecord(
                global_id="workday:gone",
                company="Genpact",
                title="AI Engineer 4A",
                ats_type="workday",
                status=JobStatus.DELISTED,
                location_flag=LocationFlag.ACCEPTED,
                eligibility_flag=EligibilityFlag.UNCERTAIN,
                first_seen_at=datetime(2026, 8, 24, tzinfo=UTC),
                delist_reason="workday_cxs_403",
                delisted_at=datetime(2026, 8, 24, tzinfo=UTC),
            ),
        )
        [job] = get_digest_jobs(conn, datetime(2026, 8, 1, tzinfo=UTC))
        assert job.status == JobStatus.DELISTED
        assert job.delist_reason == "workday_cxs_403"
        assert job.delisted_at is not None


def test_init_schema_adds_delist_columns_to_a_preexisting_table(tmp_path):
    with connection(tmp_path / "legacy.sqlite3") as conn:
        _legacy_jobs_table(conn)
        columns_before = {row["name"] for row in conn.execute("PRAGMA table_info(jobs)")}
        assert "delist_reason" not in columns_before
        assert "delisted_at" not in columns_before

        init_schema(conn)

        columns_after = {row["name"] for row in conn.execute("PRAGMA table_info(jobs)")}
        assert {"delist_reason", "delisted_at"} <= columns_after
        row = conn.execute("SELECT * FROM jobs WHERE global_id = 'old:1'").fetchone()
        assert row["score"] == 88  # the pre-existing row survives untouched
        assert row["delist_reason"] is None


def test_pending_rows_are_not_treated_as_seen(tmp_path):
    """A requeued job must be reprocessable.

    scripts/recheck_held_back.py flips a held-back row back to PENDING after a
    lexicon fix. If load_seen_keys still counted it, dedupe would drop it again
    on the next poll and the rescue would silently do nothing."""
    with connection(tmp_path / "jobs.sqlite3") as conn:
        init_schema(conn)
        base = dict(
            company="Acme",
            title="Engineer",
            location="Redondo Beach",
            ats_type="greenhouse",
            location_flag=LocationFlag.ACCEPTED,
            eligibility_flag=EligibilityFlag.PASSED,
            first_seen_at=datetime(2026, 8, 23, tzinfo=UTC),
        )
        upsert_job(
            conn,
            JobRecord(global_id="gh:1", requisition_id="r1", status=JobStatus.SCORED, **base),
        )
        upsert_job(
            conn,
            JobRecord(global_id="gh:2", requisition_id="r2", status=JobStatus.PENDING, **base),
        )

        global_ids, req_keys = load_seen_keys(conn)

        assert global_ids == {"gh:1"}
        assert req_keys == {("Acme", "r1", "Redondo Beach")}
