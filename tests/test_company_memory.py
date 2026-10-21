from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pandas as pd
import pytest

from coldstart import company_memory
from coldstart.company_memory import (
    APPLIED,
    BLOCKED,
    CAPPED,
    COOLDOWN,
    filter_company_memory,
)
from coldstart.db import block_company, connection, init_schema, unblock_company, upsert_job
from coldstart.filters.company import company_key
from coldstart.models import EligibilityFlag, JobRecord, JobStatus, LocationFlag
from coldstart.settings import Settings

NOW = datetime(2026, 10, 21, 12, 0, tzinfo=UTC)


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
    with connection(settings.db_path) as c:
        init_schema(c)
        yield c


def _job(conn, global_id: str, company: str) -> None:
    upsert_job(
        conn,
        JobRecord(
            global_id=global_id,
            company=company,
            title="Software Engineer",
            ats_type="greenhouse",
            status=JobStatus.SCORED,
            score=90,
            location_flag=LocationFlag.ACCEPTED,
            eligibility_flag=EligibilityFlag.PASSED,
            first_seen_at=NOW,
        ),
    )


def _mark(conn, global_id: str, state: str, when: datetime) -> None:
    """Marks with a chosen date — set_job_state always stamps now."""
    conn.execute(
        "INSERT OR REPLACE INTO job_state (global_id, state, updated_at, note) "
        "VALUES (?, ?, ?, NULL)",
        (global_id, state, when.isoformat()),
    )
    conn.commit()


# --- identity -------------------------------------------------------------------


@pytest.mark.parametrize(
    ("a", "b"),
    [
        # Real pairs from the live database (2026-10-20): YC writes one form,
        # greenhouse/ashby the other.
        ("Stripe", "stripe"),
        ("Flock Safety", "flock safety"),
        ("Retell AI", "retell-ai"),
        ("HappyRobot", "happyrobot.ai"),
        ("Capital One", "Capital One, Inc."),
    ],
)
def test_one_employer_one_key(a, b):
    assert company_key(a) == company_key(b) != ""


@pytest.mark.parametrize(("a", "b"), [("Meta", "Meta Group"), ("Apple", "Apple Roofing")])
def test_different_employers_stay_apart(a, b):
    assert company_key(a) != company_key(b)


def test_a_company_named_only_by_a_suffix_word_keeps_it():
    assert company_key("Company") == "company"
    assert company_key(None) == "" and company_key("  ") == ""


# --- what is remembered -------------------------------------------------------------


def test_nothing_marked_means_nothing_remembered(conn, settings):
    _job(conn, "gh:1", "Stripe")
    assert company_memory.load(conn, settings, NOW).status("Stripe") is None


def test_one_application_collapses_but_does_not_hide(conn, settings):
    _job(conn, "gh:1", "Stripe")
    _mark(conn, "gh:1", "applied", NOW - timedelta(days=3))
    status = company_memory.load(conn, settings, NOW).status("stripe")
    assert status.kind == APPLIED
    assert (status.applications, status.cap) == (1, 2)
    assert status.since == (NOW - timedelta(days=3)).date().isoformat()
    assert not status.hides


def test_two_applications_reach_the_cap(conn, settings):
    _job(conn, "gh:1", "Stripe")
    _job(conn, "gh:2", "stripe")  # same employer, other source's spelling
    _mark(conn, "gh:1", "applied", NOW - timedelta(days=10))
    _mark(conn, "gh:2", "applied", NOW - timedelta(days=2))
    status = company_memory.load(conn, settings, NOW).status("Stripe")
    assert status.kind == CAPPED and status.hides
    assert status.applications == 2


def test_an_old_application_stops_counting(conn, settings):
    _job(conn, "gh:1", "Stripe")
    _job(conn, "gh:2", "Stripe")
    _mark(conn, "gh:1", "applied", NOW - timedelta(days=settings.application_window_days + 1))
    _mark(conn, "gh:2", "applied", NOW - timedelta(days=1))
    status = company_memory.load(conn, settings, NOW).status("Stripe")
    assert status.kind == APPLIED and status.applications == 1


def test_a_rejection_hides_the_company_for_the_cooldown(conn, settings):
    _job(conn, "gh:1", "Peraton")
    rejected = NOW - timedelta(days=10)
    _mark(conn, "gh:1", "rejected", rejected)
    status = company_memory.load(conn, settings, NOW).status("Peraton")
    assert status.kind == COOLDOWN and status.hides
    assert status.until == (rejected + timedelta(days=90)).date().isoformat()


def test_the_cooldown_ends(conn, settings):
    _job(conn, "gh:1", "Peraton")
    _mark(conn, "gh:1", "rejected", NOW - timedelta(days=settings.company_cooldown_days + 1))
    assert company_memory.load(conn, settings, NOW).status("Peraton") is None


def test_a_rejected_application_no_longer_counts_toward_the_cap(conn, settings):
    """Once they've answered, it isn't an application in flight — the
    cooldown governs instead, and when it ends the company is back with a
    clean slate."""
    _job(conn, "gh:1", "Peraton")
    _job(conn, "gh:2", "Peraton")
    _mark(conn, "gh:1", "applied", NOW - timedelta(days=5))
    _mark(conn, "gh:2", "rejected", NOW - timedelta(days=200))
    status = company_memory.load(conn, settings, NOW).status("Peraton")
    assert status.kind == APPLIED and status.applications == 1


def test_not_this_company_outranks_everything_and_can_be_undone(conn, settings):
    _job(conn, "gh:1", "Capital One")
    _mark(conn, "gh:1", "rejected", NOW - timedelta(days=1))
    block_company(conn, company_key("Capital One"), "Capital One")
    assert company_memory.load(conn, settings, NOW).status("Capital One").kind == BLOCKED
    assert unblock_company(conn, company_key("Capital One"))
    assert company_memory.load(conn, settings, NOW).status("Capital One").kind == COOLDOWN


def test_settings_drive_the_numbers(conn, settings):
    tighter = settings.model_copy(update={"applications_per_company": 1})
    _job(conn, "gh:1", "Stripe")
    _mark(conn, "gh:1", "applied", NOW - timedelta(days=1))
    assert company_memory.load(conn, tighter, NOW).status("Stripe").kind == CAPPED


# --- the gate before the LLM ----------------------------------------------------------


def test_hidden_companies_never_reach_scoring(conn, settings):
    _job(conn, "gh:1", "Capital One")
    block_company(conn, company_key("Capital One"), "Capital One")
    _job(conn, "gh:2", "Peraton")
    _mark(conn, "gh:2", "rejected", NOW - timedelta(days=3))
    _job(conn, "gh:3", "Stripe")
    _mark(conn, "gh:3", "applied", NOW - timedelta(days=3))

    df = pd.DataFrame({"company": ["capital one", "Peraton", "stripe", "Notion"]})
    kept = filter_company_memory(df, company_memory.load(conn, settings, NOW))
    # One application at Stripe still gets its other roles scored.
    assert kept["company"].tolist() == ["stripe", "Notion"]


def test_an_empty_batch_passes_through(conn, settings):
    df = pd.DataFrame({"company": pd.Series([], dtype=str)})
    assert filter_company_memory(df, company_memory.load(conn, settings, NOW)).empty
