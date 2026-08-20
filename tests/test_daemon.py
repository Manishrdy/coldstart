from __future__ import annotations

import logging
import subprocess
import threading
import time
from datetime import UTC, datetime, timedelta

import httpx
import pytest
from freezegun import freeze_time

import coldstart.daemon as daemon
from coldstart import exit_codes
from coldstart.db import connection, init_schema, log_email
from coldstart.settings import Settings


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


@pytest.fixture
def conn(settings):
    with connection(settings.db_path) as connection_:
        init_schema(connection_)
        yield connection_


@pytest.fixture(autouse=True)
def _fresh_state():
    """The daemon keeps its state in a module global; reset it per test."""
    now = datetime.now(UTC)
    daemon._set_state(
        daemon.DaemonState(started_at=now, next_poll_at=now, next_digest_at=now)
    )
    yield
    daemon._set_state(None)


def _state(**overrides) -> daemon.DaemonState:
    now = datetime.now(UTC)
    base = dict(started_at=now, next_poll_at=now, next_digest_at=now)
    base.update(overrides)
    return daemon.DaemonState(**base)


class _Response:
    def __init__(self, status_code: int, etag: str | None = None):
        self.status_code = status_code
        self.headers = {"etag": etag} if etag else {}

    def raise_for_status(self) -> None:
        if self.status_code >= 400:
            raise httpx.HTTPStatusError("boom", request=None, response=None)


# --- singleton lock --------------------------------------------------------


def test_second_instance_cannot_take_the_lock(settings):
    settings.data_dir.mkdir(parents=True, exist_ok=True)
    first = daemon.acquire_singleton_lock(settings.data_dir)
    try:
        with pytest.raises(daemon.AlreadyRunning) as excinfo:
            daemon.acquire_singleton_lock(settings.data_dir)
        assert "already running" in str(excinfo.value)
    finally:
        first.close()

    # Released on close, so a restart after a crash is not blocked.
    second = daemon.acquire_singleton_lock(settings.data_dir)
    second.close()


def test_sweep_removes_stale_partial_downloads(settings):
    settings.data_dir.mkdir(parents=True, exist_ok=True)
    (settings.data_dir / "workday.parquet.part").write_text("half a download")
    (settings.data_dir / "workday.parquet").write_text("a real one")

    assert daemon.sweep_partial_downloads(settings.data_dir) == 1
    assert not (settings.data_dir / "workday.parquet.part").exists()
    assert (settings.data_dir / "workday.parquet").exists()


# --- upstream change detection ---------------------------------------------


def test_304_means_unchanged_and_never_spawns_a_poll(settings, conn, monkeypatch):
    daemon.set_daemon_state(conn, daemon._MANIFEST_ETAG_KEY, '"abc"')
    monkeypatch.setattr(daemon.httpx, "get", lambda *a, **k: _Response(304))

    def _boom(*args, **kwargs):
        raise AssertionError("run_poll must not be spawned when upstream is unchanged")

    monkeypatch.setattr(daemon, "run_child", _boom)
    assert daemon.upstream_changed(settings, conn) is False


def test_conditional_get_sends_the_stored_etag(settings, conn, monkeypatch):
    daemon.set_daemon_state(conn, daemon._MANIFEST_ETAG_KEY, '"abc"')
    captured = {}

    def _get(url, headers=None, **kwargs):
        captured["headers"] = headers
        return _Response(304)

    monkeypatch.setattr(daemon.httpx, "get", _get)
    daemon.upstream_changed(settings, conn)
    assert captured["headers"] == {"If-None-Match": '"abc"'}


def test_new_etag_means_changed_and_is_persisted(settings, conn, monkeypatch):
    daemon.set_daemon_state(conn, daemon._MANIFEST_ETAG_KEY, '"old"')
    monkeypatch.setattr(daemon.httpx, "get", lambda *a, **k: _Response(200, '"new"'))

    assert daemon.upstream_changed(settings, conn) is True
    assert daemon.get_daemon_state(conn, daemon._MANIFEST_ETAG_KEY) == '"new"'


def test_same_etag_on_a_200_still_means_unchanged(settings, conn, monkeypatch):
    daemon.set_daemon_state(conn, daemon._MANIFEST_ETAG_KEY, '"same"')
    monkeypatch.setattr(daemon.httpx, "get", lambda *a, **k: _Response(200, '"same"'))
    assert daemon.upstream_changed(settings, conn) is False


def test_first_ever_check_has_no_etag_and_reports_changed(settings, conn, monkeypatch):
    monkeypatch.setattr(daemon.httpx, "get", lambda *a, **k: _Response(200, '"first"'))
    assert daemon.upstream_changed(settings, conn) is True


# --- forced poll -----------------------------------------------------------


def test_forced_poll_on_startup_then_only_after_force_poll_hours(settings):
    now = datetime.now(UTC)
    assert daemon.forced_poll_due(_state(), settings, now) is True

    recent = _state(last_poll_started_at=now - timedelta(hours=1))
    assert daemon.forced_poll_due(recent, settings, now) is False

    stale = _state(last_poll_started_at=now - timedelta(hours=settings.force_poll_hours))
    assert daemon.forced_poll_due(stale, settings, now) is True


# --- digest scheduling -----------------------------------------------------


@freeze_time("2026-08-19T14:59:00Z")  # 07:59 America/Los_Angeles
def test_digest_not_due_before_the_configured_time(settings, conn):
    assert daemon.digest_due(settings, conn, datetime.now(UTC)) is False


@freeze_time("2026-08-19T15:00:00Z")  # 08:00 America/Los_Angeles
def test_digest_due_at_the_configured_time(settings, conn):
    assert daemon.digest_due(settings, conn, datetime.now(UTC)) is True


@freeze_time("2026-08-19T15:30:00Z")
def test_digest_not_due_twice_in_one_local_day(settings, conn):
    log_email(conn, sent_at=datetime.now(UTC), job_count=3, status="sent")
    assert daemon.digest_due(settings, conn, datetime.now(UTC)) is False


@freeze_time("2026-08-20T04:00:00Z")  # 21:00 the previous local day
def test_digest_fires_late_when_the_machine_was_off_all_day(settings, conn):
    assert daemon.digest_due(settings, conn, datetime.now(UTC)) is True


@freeze_time("2026-08-19T15:30:00Z")
def test_a_failed_send_does_not_count_as_sent(settings, conn):
    log_email(conn, sent_at=datetime.now(UTC), job_count=0, status="failed", error="smtp down")
    assert daemon.digest_due(settings, conn, datetime.now(UTC)) is True


@freeze_time("2026-08-19T16:00:00Z")  # 09:00 local, past today's 08:00
def test_next_digest_at_rolls_to_tomorrow_once_today_has_passed(settings):
    nxt = daemon.next_digest_at(settings, datetime.now(UTC))
    assert nxt == datetime.fromisoformat("2026-08-20T15:00:00+00:00")


@freeze_time("2026-08-19T18:00:00Z")  # 11:00 local
def test_next_local_midnight_is_the_start_of_the_next_local_day(settings):
    assert daemon.next_local_midnight(settings.timezone) == datetime.fromisoformat(
        "2026-08-20T07:00:00+00:00"
    )


# --- child process handling ------------------------------------------------


def test_run_child_returns_exit_code_and_last_stdout_line(settings, conn, tmp_path):
    script = tmp_path / "ok.py"
    script.write_text("print('warming up')\nprint('run abc123: 2 slice(s)')\n")
    code, summary = daemon.run_child(script, timeout_minutes=1, conn=conn)
    assert code == exit_codes.OK
    assert summary == "run abc123: 2 slice(s)"


def test_run_child_timeout_kills_the_child_and_logs_an_error(settings, conn, tmp_path):
    script = tmp_path / "hang.py"
    script.write_text("import time\ntime.sleep(30)\n")
    code, summary = daemon.run_child(script, timeout_minutes=0.02, conn=conn)

    assert code == exit_codes.FAILURE
    assert "timeout" in summary
    row = conn.execute("SELECT stage, job_ref FROM errors").fetchone()
    assert row["stage"] == "daemon"
    assert row["job_ref"] == "hang.py"


def test_run_child_propagates_a_nonzero_exit_code(settings, conn, tmp_path):
    script = tmp_path / "budget.py"
    script.write_text("raise SystemExit(2)\n")
    code, _summary = daemon.run_child(script, timeout_minutes=1, conn=conn)
    assert code == exit_codes.BUDGET_EXCEEDED


# --- exit-code handling ----------------------------------------------------


@freeze_time("2026-08-19T18:00:00Z")
def test_budget_exit_suspends_polling_until_next_local_midnight(settings):
    daemon._handle_poll_exit(exit_codes.BUDGET_EXCEEDED, "", settings, daemon.get_state())
    state = daemon.get_state()
    assert state.budget_paused_until == datetime.fromisoformat("2026-08-20T07:00:00+00:00")


@pytest.mark.parametrize("code", [exit_codes.RESUMES_NOT_READY, exit_codes.CONFIG_ERROR])
def test_hard_gate_exits_do_not_pause_the_daemon(settings, code):
    daemon._handle_poll_exit(code, "", settings, daemon.get_state())
    state = daemon.get_state()
    assert state.budget_paused_until is None
    assert state.consecutive_poll_failures == 1


def test_consecutive_failures_reset_on_success(settings):
    daemon._handle_poll_exit(exit_codes.FAILURE, "", settings, daemon.get_state())
    daemon._handle_poll_exit(exit_codes.FAILURE, "", settings, daemon.get_state())
    assert daemon.get_state().consecutive_poll_failures == 2

    daemon._handle_poll_exit(exit_codes.OK, "run ok", settings, daemon.get_state())
    state = daemon.get_state()
    assert state.consecutive_poll_failures == 0
    assert state.last_poll_summary == "run ok"


# --- the tick --------------------------------------------------------------


def test_tick_polls_when_upstream_changed_then_schedules_the_next_one(
    settings, conn, monkeypatch
):
    monkeypatch.setattr(daemon, "upstream_changed", lambda s, c: True)
    calls = []

    def _run_child(script, timeout_minutes, conn):
        calls.append(script.name)
        return exit_codes.OK, "run ok"

    monkeypatch.setattr(daemon, "run_child", _run_child)
    monkeypatch.setattr(daemon, "digest_due", lambda *a, **k: False)
    monkeypatch.setattr(daemon, "already_sent_today", lambda *a, **k: False)

    daemon._tick(settings, threading.Event())

    assert calls == ["run_poll.py"]
    state = daemon.get_state()
    assert state.activity == "idle"
    assert state.next_poll_at > datetime.now(UTC)


def test_tick_skips_the_poll_entirely_when_upstream_is_unchanged(
    settings, conn, monkeypatch
):
    monkeypatch.setattr(daemon, "upstream_changed", lambda s, c: False)
    monkeypatch.setattr(daemon, "forced_poll_due", lambda *a, **k: False)
    monkeypatch.setattr(daemon, "digest_due", lambda *a, **k: False)
    monkeypatch.setattr(daemon, "already_sent_today", lambda *a, **k: False)

    def _boom(*args, **kwargs):
        raise AssertionError("no subprocess should be spawned on an unchanged manifest")

    monkeypatch.setattr(daemon, "run_child", _boom)
    daemon._tick(settings, threading.Event())
    assert daemon.get_state().last_upstream_check_at is not None


def test_a_clock_jump_produces_one_catch_up_poll_not_one_per_missed_interval(
    settings, conn, monkeypatch
):
    """A laptop asleep for six hours must not wake up and poll twelve times."""
    monkeypatch.setattr(daemon, "upstream_changed", lambda s, c: True)
    monkeypatch.setattr(daemon, "digest_due", lambda *a, **k: False)
    monkeypatch.setattr(daemon, "already_sent_today", lambda *a, **k: False)
    calls = []
    monkeypatch.setattr(
        daemon,
        "run_child",
        lambda script, timeout_minutes, conn: (calls.append(script.name), (exit_codes.OK, ""))[1],
    )

    daemon._update(next_poll_at=datetime.now(UTC) - timedelta(hours=6))
    daemon._tick(settings, threading.Event())
    daemon._tick(settings, threading.Event())  # immediately after: not due again

    assert calls == ["run_poll.py"]


def test_tick_does_not_poll_while_budget_paused(settings, conn, monkeypatch):
    monkeypatch.setattr(daemon, "digest_due", lambda *a, **k: False)
    monkeypatch.setattr(daemon, "already_sent_today", lambda *a, **k: False)

    def _boom(*args, **kwargs):
        raise AssertionError("polling must stay suspended while the budget pause is active")

    monkeypatch.setattr(daemon, "upstream_changed", _boom)
    daemon._update(budget_paused_until=datetime.now(UTC) + timedelta(hours=2))
    daemon._tick(settings, threading.Event())


def test_budget_pause_lifts_once_it_has_elapsed(settings, conn, monkeypatch):
    monkeypatch.setattr(daemon, "upstream_changed", lambda s, c: False)
    monkeypatch.setattr(daemon, "forced_poll_due", lambda *a, **k: False)
    monkeypatch.setattr(daemon, "digest_due", lambda *a, **k: False)
    monkeypatch.setattr(daemon, "already_sent_today", lambda *a, **k: False)

    daemon._update(budget_paused_until=datetime.now(UTC) - timedelta(seconds=1))
    daemon._tick(settings, threading.Event())
    assert daemon.get_state().budget_paused_until is None


def test_a_manifest_network_failure_is_logged_and_skips_the_cycle(
    settings, conn, monkeypatch
):
    def _raise(*args, **kwargs):
        raise httpx.ConnectError("no route to host")

    monkeypatch.setattr(daemon, "upstream_changed", _raise)
    monkeypatch.setattr(daemon, "digest_due", lambda *a, **k: False)
    monkeypatch.setattr(daemon, "already_sent_today", lambda *a, **k: False)
    monkeypatch.setattr(
        daemon, "run_child", lambda *a, **k: pytest.fail("must not poll on an unknown manifest")
    )

    daemon._tick(settings, threading.Event())

    row = conn.execute("SELECT stage, job_ref FROM errors").fetchone()
    assert (row["stage"], row["job_ref"]) == ("daemon", "manifest")


def test_tick_sends_the_digest_when_due(settings, conn, monkeypatch):
    monkeypatch.setattr(daemon, "upstream_changed", lambda s, c: False)
    monkeypatch.setattr(daemon, "forced_poll_due", lambda *a, **k: False)
    monkeypatch.setattr(daemon, "already_sent_today", lambda *a, **k: True)

    due = iter([True, False])
    monkeypatch.setattr(daemon, "digest_due", lambda *a, **k: next(due, False))

    calls = []
    monkeypatch.setattr(
        daemon,
        "run_child",
        lambda script, timeout_minutes, conn: (calls.append(script.name), (exit_codes.OK, ""))[1],
    )

    daemon._tick(settings, threading.Event())
    assert calls == ["run_digest.py"]


def test_a_set_stop_event_prevents_the_digest_from_starting(settings, conn, monkeypatch):
    monkeypatch.setattr(daemon, "upstream_changed", lambda s, c: False)
    monkeypatch.setattr(daemon, "forced_poll_due", lambda *a, **k: False)
    monkeypatch.setattr(daemon, "digest_due", lambda *a, **k: True)
    monkeypatch.setattr(
        daemon, "run_child", lambda *a, **k: pytest.fail("shutdown must not start new work")
    )

    stop = threading.Event()
    stop.set()
    daemon._tick(settings, stop)


# --- the loop --------------------------------------------------------------


def test_run_daemon_exits_zero_when_stopped(settings, monkeypatch):
    ticks = []

    def _tick(settings_, stop):
        ticks.append(1)
        stop.set()

    monkeypatch.setattr(daemon, "_tick", _tick)
    monkeypatch.setattr(daemon, "_TICK_SECONDS", 0.01)

    assert daemon.run_daemon(settings) == exit_codes.OK
    assert ticks == [1]


def test_an_unhandled_tick_error_does_not_kill_the_daemon(settings, monkeypatch):
    calls = {"n": 0}

    def _tick(settings_, stop):
        calls["n"] += 1
        if calls["n"] == 1:
            raise RuntimeError("something went sideways")
        stop.set()

    monkeypatch.setattr(daemon, "_tick", _tick)
    monkeypatch.setattr(daemon, "_TICK_SECONDS", 0.01)

    assert daemon.run_daemon(settings) == exit_codes.OK
    assert calls["n"] == 2

    with connection(settings.db_path) as conn:
        row = conn.execute("SELECT stage, error_type FROM errors").fetchone()
        assert (row["stage"], row["error_type"]) == ("daemon", "RuntimeError")


def test_run_daemon_releases_the_lock_so_a_restart_works(settings, monkeypatch):
    monkeypatch.setattr(daemon, "_tick", lambda settings_, stop: stop.set())
    monkeypatch.setattr(daemon, "_TICK_SECONDS", 0.01)

    assert daemon.run_daemon(settings) == exit_codes.OK
    assert daemon.run_daemon(settings) == exit_codes.OK


def test_shutdown_signal_stops_the_loop_and_terminates_the_child(settings, monkeypatch):
    stop_holder = {}

    def _tick(settings_, stop):
        stop_holder["stop"] = stop
        # Simulate SIGTERM arriving while the daemon is mid-tick.
        daemon.install_signal_handlers(stop)
        proc = subprocess.Popen(["sleep", "30"])
        with daemon._child_lock:
            daemon._child = proc
        try:
            stop.set()
            daemon._terminate_child()
            proc.wait(timeout=5)
        finally:
            with daemon._child_lock:
                daemon._child = None
        assert proc.returncode != 0  # terminated, not a clean exit

    monkeypatch.setattr(daemon, "_tick", _tick)
    monkeypatch.setattr(daemon, "_TICK_SECONDS", 0.01)
    assert daemon.run_daemon(settings) == exit_codes.OK


def test_a_child_killed_by_shutdown_is_not_counted_as_a_failure(settings):
    daemon._handle_poll_exit(-15, "", settings, daemon.get_state(), shutting_down=True)
    state = daemon.get_state()
    assert state.consecutive_poll_failures == 0
    assert state.last_poll_exit_code == -15
    assert state.budget_paused_until is None


# --- child output streaming ------------------------------------------------


def test_child_output_is_logged_while_the_child_is_still_running(conn, tmp_path):
    """The behaviour change from `communicate()`: a 40-minute poll used to log
    nothing at all until it exited. These two lines must arrive ~1.5s apart,
    mirroring the child's own sleep — buffered output would land together."""
    script = tmp_path / "slow.py"
    script.write_text(
        "import time\nprint('FIRST', flush=True)\ntime.sleep(1.5)\nprint('SECOND', flush=True)\n"
    )

    seen: list[tuple[str, float]] = []

    class _Recorder(logging.Handler):
        def emit(self, record):
            message = record.getMessage()
            if "FIRST" in message or "SECOND" in message:
                seen.append((message, time.monotonic()))

    handler = _Recorder()
    previous = daemon.logger.level
    daemon.logger.addHandler(handler)
    daemon.logger.setLevel(logging.DEBUG)
    try:
        code, summary = daemon.run_child(script, timeout_minutes=1, conn=conn)
    finally:
        daemon.logger.removeHandler(handler)
        daemon.logger.setLevel(previous)

    assert code == exit_codes.OK
    assert [m for m, _ in seen] == ["[slow.py] FIRST", "[slow.py] SECOND"]
    assert seen[1][1] - seen[0][1] > 1.0
    assert summary == "SECOND"


def test_child_log_levels_are_mirrored_not_flattened(conn, tmp_path):
    """A child's own WARNING should read as a WARNING here. Levelling by which
    stream a line arrived on made every healthy run look like a wall of
    warnings, and every killed run look like a wall of errors."""
    script = tmp_path / "levels.py"
    script.write_text(
        "import sys\n"
        "print('2026-08-20 10:00:00,000 | INFO     | ab | m:f:1 | all good', file=sys.stderr)\n"
        "print('2026-08-20 10:00:01,000 | WARNING  | ab | m:f:2 | careful', file=sys.stderr)\n"
        "print('2026-08-20 10:00:02,000 | ERROR    | ab | m:f:3 | broke', file=sys.stderr)\n"
        "print('not a log line at all', file=sys.stderr)\n"
    )

    levels: dict[str, int] = {}

    class _Recorder(logging.Handler):
        def emit(self, record):
            for key in ("all good", "careful", "broke", "not a log line"):
                if key in record.getMessage():
                    levels[key] = record.levelno

    handler = _Recorder()
    previous = daemon.logger.level
    daemon.logger.addHandler(handler)
    daemon.logger.setLevel(logging.DEBUG)
    try:
        daemon.run_child(script, timeout_minutes=1, conn=conn)
    finally:
        daemon.logger.removeHandler(handler)
        daemon.logger.setLevel(previous)

    assert levels["all good"] == logging.INFO
    assert levels["careful"] == logging.WARNING
    assert levels["broke"] == logging.ERROR
    assert levels["not a log line"] == logging.INFO  # unparseable -> INFO, never dropped
