"""scripts/purge_ml_titles.py — the retroactive half of the 2026-10-21
machine-learning title deny. It must delete exactly what the title filter
would now refuse on ML grounds, and nothing a substring match would catch."""

import importlib.util
import sqlite3
import sys
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace

import pytest

from coldstart.db import (
    connection,
    init_schema,
    mark_hemline_sent,
    queue_hemline_event,
    set_job_state,
    upsert_job,
)
from coldstart.filters.title import is_ml_title, is_target_title
from coldstart.models import EligibilityFlag, JobRecord, JobStatus, LocationFlag

_SCRIPT = Path(__file__).resolve().parent.parent / "scripts" / "purge_ml_titles.py"


@pytest.fixture(scope="module")
def script():
    spec = importlib.util.spec_from_file_location("purge_ml_titles", _SCRIPT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _scored(global_id: str, title: str) -> JobRecord:
    return JobRecord(
        global_id=global_id,
        company="Acme",
        title=title,
        location="Austin, TX",
        ats_type="greenhouse",
        score=90,
        status=JobStatus.SCORED,
        location_flag=LocationFlag.ACCEPTED,
        eligibility_flag=EligibilityFlag.PASSED,
        first_seen_at=datetime(2026, 10, 1),
        scored_at=datetime(2026, 10, 1),
    )


TITLES = {
    "greenhouse:ml": "Machine Learning Engineer",
    "greenhouse:swe-ml": "Software Engineer, ML Platform",
    "greenhouse:applied": "Machine Learning Engineer II",  # marked applied below
    "greenhouse:swe": "Software Engineer",
    "greenhouse:html": "Software Engineer, HTML Rendering",
    "greenhouse:mlops": "Software Engineer, MLOps",
}


@pytest.fixture
def live(tmp_path, script, monkeypatch):
    db_path = tmp_path / "coldstart.sqlite3"
    with connection(db_path) as conn:
        init_schema(conn)
        for global_id, title in TITLES.items():
            upsert_job(conn, _scored(global_id, title))
        set_job_state(conn, "greenhouse:applied", "applied")
        queue_hemline_event(conn, "greenhouse:ml", "evt-ml", "{}")
        queue_hemline_event(conn, "greenhouse:swe-ml", "evt-swe-ml", "{}")
        mark_hemline_sent(conn, "greenhouse:swe-ml")

    settings = SimpleNamespace(db_path=db_path, score_threshold_strong=80)
    monkeypatch.setattr(script, "load_settings", lambda: settings)
    return db_path


def _query(db_path: Path, sql: str) -> set:
    conn = sqlite3.connect(db_path)
    try:
        return set(conn.execute(sql))
    finally:
        conn.close()


def _job_ids(db_path: Path) -> set[str]:
    return {gid for (gid,) in _query(db_path, "SELECT global_id FROM jobs")}


def test_dry_run_reports_and_deletes_nothing(live, script, monkeypatch, capsys):
    monkeypatch.setattr(sys, "argv", ["purge_ml_titles.py"])
    script.main()

    assert _job_ids(live) == set(TITLES)
    out = capsys.readouterr().out
    assert "ML titles, deletable: 2 (2 shortlisted)" in out
    assert "dry run" in out


def test_apply_deletes_ml_titles_and_keeps_the_rest(live, script, monkeypatch):
    monkeypatch.setattr(sys, "argv", ["purge_ml_titles.py", "--apply"])
    script.main()

    assert _job_ids(live) == {
        "greenhouse:applied",
        "greenhouse:swe",
        "greenhouse:html",
        "greenhouse:mlops",
    }


def test_apply_drops_pending_hemline_pushes_but_keeps_sent_ones(live, script, monkeypatch):
    monkeypatch.setattr(sys, "argv", ["purge_ml_titles.py", "--apply"])
    script.main()

    assert _query(live, "SELECT global_id, status FROM hemline_events") == {
        ("greenhouse:swe-ml", "sent")
    }


def test_include_marked_deletes_the_job_and_its_mark(live, script, monkeypatch):
    monkeypatch.setattr(sys, "argv", ["purge_ml_titles.py", "--apply", "--include-marked"])
    script.main()

    assert "greenhouse:applied" not in _job_ids(live)
    assert _query(live, "SELECT global_id FROM job_state") == set()


@pytest.mark.parametrize("title", TITLES.values())
def test_is_ml_title_agrees_with_the_title_filter(title):
    """Every ML title the purge deletes is one the filter now rejects."""
    if is_ml_title(title):
        assert is_target_title(title) == (False, "deny_match")


@pytest.mark.parametrize("value", [None, float("nan"), 123, ""])
def test_is_ml_title_never_raises_on_messy_values(value):
    assert is_ml_title(value) is False
