"""Module 29 — the operator's control over which source runs next."""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta

import pytest
from fastapi.testclient import TestClient

import coldstart.daemon as daemon
from coldstart.ats_control import (
    AtsControl,
    clear_priority,
    hold,
    max_yields_for,
    order_slices,
    read_control,
    reason_to_yield,
    release,
    reset,
    run_next,
)
from coldstart.db import (
    connection,
    get_slice_state,
    init_schema,
    set_daemon_state,
    set_slice_state,
)
from coldstart.models import SliceState
from coldstart.progress import PollProgress, write_progress
from coldstart.settings import Settings
from coldstart.web import analytics as analytics_queries
from coldstart.web.analytics import queue
from coldstart.web.app import create_app

_HEADERS = {"X-Coldstart-Action": "1"}


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


@pytest.fixture(autouse=True)
def _no_daemon_state():
    daemon._set_state(None)
    yield
    daemon._set_state(None)


@pytest.fixture(autouse=True)
def _no_config_exclusions(monkeypatch):
    """These tests name their own sources; the real exclusion list is not
    what is under test here."""
    monkeypatch.setattr(analytics_queries, "_load_excluded_ats", set)


def _entry(sha: str, rows: int = 100) -> dict:
    return {
        "parquet": "https://example.com/x.parquet",
        "parquet_sha256": sha,
        "parquet_size_bytes": 4096,
        "rows": rows,
    }


@pytest.fixture
def db(settings):
    """A database that knows about four sources, two of them already done."""
    with connection(settings.db_path) as conn:
        init_schema(conn)
        set_daemon_state(
            conn,
            "manifest_body",
            json.dumps(
                {
                    "by_ats": {
                        "workday": _entry("w" * 64, rows=839633),
                        "ashby": _entry("a" * 64, rows=53528),
                        "greenhouse": _entry("g" * 64, rows=181350),
                        "lever": _entry("l" * 64, rows=70864),
                    }
                }
            ),
        )
        # ashby and greenhouse are up to date; workday and lever are not.
        for ats_type, sha in (("ashby", "a" * 64), ("greenhouse", "g" * 64)):
            set_slice_state(
                conn,
                SliceState(
                    ats_type=ats_type,
                    last_sha256=sha,
                    last_processed_at=datetime.now(UTC) - timedelta(days=5),
                    row_count=10,
                ),
            )
        yield conn


class _Slice:
    def __init__(self, ats_type: str) -> None:
        self.ats_type = ats_type

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return f"<{self.ats_type}>"


# --- the control row --------------------------------------------------------


def test_run_next_moves_a_source_to_the_front(db):
    run_next(db, "ashby")
    run_next(db, "lever")
    assert read_control(db).priority == ["lever", "ashby"]


def test_run_next_releases_a_source_that_was_held(db):
    """"never run this" and "run this next" are contradictory instructions;
    the newer one is the one meant."""
    hold(db, "workday")
    run_next(db, "workday")
    control = read_control(db)
    assert control.held == []
    assert control.priority == ["workday"]


def test_holding_a_source_drops_any_priority_it_had(db):
    run_next(db, "ashby")
    hold(db, "ashby")
    control = read_control(db)
    assert control.priority == []
    assert control.held == ["ashby"]


def test_release_and_clear_priority_are_independent(db):
    hold(db, "workday")
    run_next(db, "ashby")
    release(db, "workday")
    clear_priority(db, "ashby")
    assert read_control(db) == AtsControl(
        priority=[], held=[], updated_at=read_control(db).updated_at
    )


def test_reset_drops_everything(db):
    run_next(db, "ashby")
    hold(db, "workday")
    reset(db)
    control = read_control(db)
    assert (control.priority, control.held) == ([], [])


def test_an_unreadable_control_row_means_no_preferences(db):
    """The pipeline reads this between every job. A corrupt value must mean
    "no instructions", never a crashed poll."""
    set_daemon_state(db, "ats_control", "{not json")
    assert read_control(db) == AtsControl()


def test_repeated_actions_do_not_duplicate_entries(db):
    hold(db, "workday")
    hold(db, "workday")
    run_next(db, "ashby")
    run_next(db, "ashby")
    control = read_control(db)
    assert control.held == ["workday"]
    assert control.priority == ["ashby"]


# --- ordering ---------------------------------------------------------------


def test_priority_runs_first_and_everything_else_keeps_its_place(db):
    """A stable sort with one shared rank for the unprioritised: setting a
    priority must reorder exactly what was asked for and nothing else."""
    slices = [_Slice(a) for a in ("workday", "ashby", "greenhouse", "lever")]
    control = AtsControl(priority=["lever"])
    runnable, held = order_slices(slices, control)
    assert [s.ats_type for s in runnable] == ["lever", "workday", "ashby", "greenhouse"]
    assert held == []


def test_held_sources_are_split_out_of_the_run_order(db):
    slices = [_Slice(a) for a in ("workday", "ashby", "lever")]
    runnable, held = order_slices(slices, AtsControl(held=["workday"]))
    assert [s.ats_type for s in runnable] == ["ashby", "lever"]
    assert [s.ats_type for s in held] == ["workday"]


def test_priority_order_is_honoured_between_several_prioritised_sources(db):
    slices = [_Slice(a) for a in ("workday", "ashby", "lever")]
    runnable, _ = order_slices(slices, AtsControl(priority=["lever", "ashby"]))
    assert [s.ats_type for s in runnable] == ["lever", "ashby", "workday"]


# --- when to give way -------------------------------------------------------


def test_a_held_source_gives_way():
    assert reason_to_yield(AtsControl(held=["workday"]), "workday", {"lever"}) == "held"


def test_a_source_gives_way_to_a_higher_priority_one_still_pending():
    control = AtsControl(priority=["ashby"])
    assert reason_to_yield(control, "workday", {"ashby", "lever"}) == "priority:ashby"


def test_the_top_priority_source_never_gives_way_to_a_lower_one():
    """Walking the priority list in order is what guarantees this: hitting
    `current` first means it is exactly what was asked for."""
    control = AtsControl(priority=["workday", "ashby"])
    assert reason_to_yield(control, "workday", {"ashby", "lever"}) is None


def test_a_priority_already_dealt_with_is_not_a_reason_to_abandon_work():
    """Otherwise a finished priority would preempt every remaining source in
    turn, forever."""
    control = AtsControl(priority=["ashby"])
    assert reason_to_yield(control, "workday", {"lever"}) is None


def test_no_instructions_means_never_give_way():
    assert reason_to_yield(AtsControl(), "workday", {"ashby"}) is None


def test_the_yield_budget_scales_with_the_queue_but_has_a_floor():
    assert max_yields_for(0) == 4
    assert max_yields_for(1) == 4
    assert max_yields_for(8) == 16


# --- the queue payload ------------------------------------------------------


def test_the_queue_lists_every_source_not_just_the_outstanding_ones(settings, db):
    """The operator asked for the full list — you cannot choose between
    sources you cannot see."""
    payload = queue(db, settings)
    assert {row["ats_type"] for row in payload["rows"]} == {
        "workday", "ashby", "greenhouse", "lever"
    }
    by_ats = {row["ats_type"]: row for row in payload["rows"]}
    assert by_ats["ashby"]["status"] == "up_to_date"
    assert by_ats["workday"]["status"] == "queued"


def test_the_queue_numbers_sources_in_the_order_they_will_run(settings, db):
    run_next(db, "lever")
    by_ats = {row["ats_type"]: row for row in queue(db, settings)["rows"]}
    assert by_ats["lever"]["position"] == 1
    assert by_ats["workday"]["position"] == 2
    # Up to date, so not in the run order at all.
    assert by_ats["ashby"]["position"] is None


def test_the_queue_marks_the_source_being_processed_right_now(settings, db):
    import os

    now = datetime.now(UTC)
    write_progress(
        db,
        PollProgress(
            run_id="r", pid=os.getpid(), started_at=now, updated_at=now,
            ats_type="workday", phase="scoring",
        ),
    )
    payload = queue(db, settings)
    assert payload["running"] == "workday"
    by_ats = {row["ats_type"]: row for row in payload["rows"]}
    assert by_ats["workday"]["status"] == "running"


def test_a_stale_heartbeat_does_not_make_the_queue_claim_something_is_running(
    settings, db
):
    write_progress(
        db,
        PollProgress(
            run_id="r", pid=2**22, started_at=datetime.now(UTC),
            updated_at=datetime.now(UTC), ats_type="workday",
        ),
    )
    assert queue(db, settings)["running"] is None


def test_held_sources_are_reported_as_held_and_lose_their_position(settings, db):
    hold(db, "workday")
    payload = queue(db, settings)
    by_ats = {row["ats_type"]: row for row in payload["rows"]}
    assert by_ats["workday"]["status"] == "held"
    assert by_ats["workday"]["position"] is None
    assert payload["held"] == ["workday"]
    assert payload["has_overrides"] is True


def test_no_overrides_is_reported_so_the_page_can_hide_the_reset(settings, db):
    assert queue(db, settings)["has_overrides"] is False


# --- the endpoints ----------------------------------------------------------


@pytest.fixture
def client(settings, db) -> TestClient:
    return TestClient(create_app(settings))


def test_queue_actions_require_the_csrf_header(client):
    """Same guard as every other write: loopback, no auth, so a custom header
    is what stops a page you happen to have open from POSTing here."""
    response = client.post("/api/queue/workday", json={"action": "hold"})
    assert response.status_code == 403


def test_an_unknown_action_is_rejected(client):
    response = client.post(
        "/api/queue/workday", json={"action": "delete_everything"}, headers=_HEADERS
    )
    assert response.status_code == 422


def test_an_unknown_source_is_rejected(client):
    """A typo must never reach the control row — it would sit in the priority
    list forever, matching nothing, implying an order that isn't there."""
    response = client.post(
        "/api/queue/nosuchats", json={"action": "run_next"}, headers=_HEADERS
    )
    assert response.status_code == 404


def test_run_next_through_the_api_reorders_the_queue(client):
    response = client.post(
        "/api/queue/lever", json={"action": "run_next"}, headers=_HEADERS
    )
    assert response.status_code == 200
    assert response.json()["control"]["priority"] == ["lever"]

    payload = client.get("/api/analytics").json()["live"]["queue"]
    by_ats = {row["ats_type"]: row for row in payload["rows"]}
    assert by_ats["lever"]["position"] == 1


def test_hold_then_release_through_the_api(client):
    client.post("/api/queue/workday", json={"action": "hold"}, headers=_HEADERS)
    assert client.get("/api/analytics").json()["live"]["queue"]["held"] == ["workday"]

    client.post("/api/queue/workday", json={"action": "release"}, headers=_HEADERS)
    assert client.get("/api/analytics").json()["live"]["queue"]["held"] == []


def test_rerun_forgets_the_change_detection_row_and_queues_the_source(client, settings):
    """"Run ashby" has to mean something even when ashby is up to date —
    otherwise the control does nothing in the most common situation."""
    response = client.post(
        "/api/queue/ashby", json={"action": "rerun"}, headers=_HEADERS
    )
    assert response.status_code == 200
    assert response.json()["rerun"] is True

    with connection(settings.db_path) as conn:
        assert get_slice_state(conn, "ashby") is None

    by_ats = {
        row["ats_type"]: row
        for row in client.get("/api/analytics").json()["live"]["queue"]["rows"]
    }
    assert by_ats["ashby"]["status"] == "queued"
    assert by_ats["ashby"]["position"] == 1
    assert by_ats["ashby"]["never_processed"] is True


def test_rerun_reports_when_there_was_nothing_to_forget(client):
    # workday has no slice_state row to begin with.
    response = client.post(
        "/api/queue/workday", json={"action": "rerun"}, headers=_HEADERS
    )
    assert response.json()["rerun"] is False


def test_reset_drops_every_override(client):
    client.post("/api/queue/lever", json={"action": "run_next"}, headers=_HEADERS)
    client.post("/api/queue/workday", json={"action": "hold"}, headers=_HEADERS)

    response = client.post("/api/queue", json={"action": "reset"}, headers=_HEADERS)
    assert response.status_code == 200

    payload = client.get("/api/analytics").json()["live"]["queue"]
    assert payload["has_overrides"] is False
    assert (payload["priority"], payload["held"]) == ([], [])


def test_reset_requires_the_csrf_header_too(client):
    assert client.post("/api/queue", json={"action": "reset"}).status_code == 403


def test_reset_rejects_anything_but_reset(client):
    response = client.post("/api/queue", json={"action": "hold"}, headers=_HEADERS)
    assert response.status_code == 422
