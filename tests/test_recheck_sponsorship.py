"""scripts/recheck_sponsorship.py — reading a scored job's description back
from the snapshot without picking up another company's.

`ats_id` is shared between companies inside one slice (122k ids on
2026-10-20; `R0003795` is both OCLC's software job and Lynn University's
instructor posting), and the original lookup by id alone read another
employer's description for 549 of 5,543 scored jobs. Lookups now go by the
full key (global_ids.py), and the company check covers the few keys that are
still shared, like one Taleo host serving many employers."""

import importlib.util
import sqlite3
import sys
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace

import pandas as pd
import pytest

from coldstart.db import connection, init_schema, set_job_state, upsert_job
from coldstart.models import EligibilityFlag, JobRecord, JobStatus, LocationFlag

_SCRIPT = Path(__file__).resolve().parent.parent / "scripts" / "recheck_sponsorship.py"

REFUSES = "Great team. We will not sponsor visas for this role."
SILENT = "Great team. Python, Postgres, AWS."


@pytest.fixture(scope="module")
def script():
    spec = importlib.util.spec_from_file_location("recheck_sponsorship", _SCRIPT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _write_slice(data_dir: Path, ats_type: str, rows: list[dict]) -> None:
    df = pd.DataFrame(rows)
    df["raw"] = None
    df.to_parquet(data_dir / f"{ats_type}.parquet")


OCLC = "https://oclc.wd1.myworkdayjobs.com/oclc_careers/job/Dublin-OH/Lead-SWE_R0003795"
LYNN = "https://lynn.wd5.myworkdayjobs.com/careers/job/Freiburger/Instructor_R0003795"
OCLC_ID = "workday:oclc.wd1.myworkdayjobs.com:R0003795"
# Taleo serves many employers from one host, so these two keep sharing a key.
ECCO = "https://phe.tbe.taleo.net/phe03/ats/careers/v2/viewRequisition?org=ECCO&rid=2537"
TASSAL = "https://phe.tbe.taleo.net/phe03/ats/careers/v2/viewRequisition?org=TASSAL&rid=2537"
SHARED_TALEO_ID = "taleo:phe.tbe.taleo.net:2537"


def _posting(ats_id, url, company, location, description) -> dict:
    return {
        "ats_id": ats_id,
        "url": url,
        "company": company,
        "location": location,
        "description": description,
    }


# --- _load_descriptions ------------------------------------------------------


def test_load_descriptions_keys_postings_the_way_load_slice_does(tmp_path, script):
    _write_slice(tmp_path, "workday", [
        _posting("R0003795", OCLC, "Oclc", "Dublin, OH / Hybrid", SILENT),
        _posting("R0003795", LYNN, "Lynn University", "Freiburger Residence Hall", REFUSES),
        _posting("R0009999", OCLC, "Oclc", "Austin, TX", SILENT),
    ])

    found = script._load_descriptions(tmp_path, "workday", {"R0003795"})

    assert found == {
        OCLC_ID: [("Oclc", "Dublin, OH / Hybrid", SILENT, None)],
        "workday:lynn.wd5.myworkdayjobs.com:R0003795": [
            ("Lynn University", "Freiburger Residence Hall", REFUSES, None)
        ],
    }


def test_load_descriptions_keeps_every_posting_on_a_still_shared_key(tmp_path, script):
    _write_slice(tmp_path, "taleo", [
        _posting(2537, ECCO, "Ecco USA, Inc.", "Seattle, WA", SILENT),
        _posting(2537, TASSAL, "Tassal Group Limited", "Tasmania", REFUSES),
    ])

    found = script._load_descriptions(tmp_path, "taleo", {"2537"})

    assert [c[0] for c in found[SHARED_TALEO_ID]] == ["Ecco USA, Inc.", "Tassal Group Limited"]


def test_load_descriptions_missing_snapshot_is_empty(tmp_path, script):
    assert script._load_descriptions(tmp_path, "workday", {"R1"}) == {}


# --- _match ------------------------------------------------------------------


def _candidate(company, location, description):
    return (company, location, description, None)


def test_match_ignores_another_company_even_when_it_comes_last(script):
    # The original lookup was a dict keyed by id, so the last row won.
    candidates = [
        _candidate("Ecco USA, Inc.", "Seattle, WA", SILENT),
        _candidate("Tassal Group Limited", "Tasmania", REFUSES),
    ]
    assert script._match(candidates, "Ecco USA, Inc.", "Seattle, WA") == (SILENT, None)


def test_match_none_when_only_another_company_has_the_key(script):
    candidates = [_candidate("Tassal Group Limited", "Tasmania", REFUSES)]
    assert script._match(candidates, "Ecco USA, Inc.", "Seattle, WA") is None


def test_match_none_when_nothing_has_the_key(script):
    assert script._match([], "Oclc", "Dublin, OH / Hybrid") is None


def test_match_accepts_the_same_company_after_its_location_text_changed(script):
    # Capital One's "McLean, VA" is "2 Locations" in the current snapshot: the
    # same posting, still the right description.
    candidates = [_candidate("Capital One", "2 Locations", REFUSES)]
    assert script._match(candidates, "Capital One", "McLean, VA") == (REFUSES, None)


def test_match_treats_duplicate_rows_of_one_posting_as_one(script):
    # successfactors lists one job under several rows with the same id.
    candidates = [
        _candidate("Acme", "Berlin, DE", REFUSES),
        _candidate("Acme", "Munich, DE", REFUSES),
    ]
    assert script._match(candidates, "Acme", "Austin, TX") == (REFUSES, None)


def test_match_uses_location_to_choose_between_same_company_postings(script):
    candidates = [
        _candidate("Acme", "Austin, TX", SILENT),
        _candidate("Acme", "Berlin, DE", REFUSES),
    ]
    assert script._match(candidates, "Acme", "Austin, TX") == (SILENT, None)
    assert script._match(candidates, "Acme", "Denver, CO") is None


def test_match_treats_empty_and_missing_location_alike(script):
    candidates = [
        _candidate("Acme", "", SILENT),
        _candidate("Acme", "Berlin, DE", REFUSES),
    ]
    assert script._match(candidates, "Acme", None) == (SILENT, None)


# --- main, end to end --------------------------------------------------------


def _scored(global_id: str, company: str, location: str, title: str) -> JobRecord:
    return JobRecord(
        global_id=global_id,
        company=company,
        title=title,
        location=location,
        ats_type=global_id.split(":", 1)[0],
        score=90,
        status=JobStatus.SCORED,
        location_flag=LocationFlag.ACCEPTED,
        eligibility_flag=EligibilityFlag.PASSED,
        first_seen_at=datetime(2026, 9, 1),
        scored_at=datetime(2026, 9, 1),
    )


ACME = "https://acme.wd1.myworkdayjobs.com/acme/job/Austin-TX/SWE_R0000001"
ACME_ID = "workday:acme.wd1.myworkdayjobs.com:R0000001"


@pytest.fixture
def live(tmp_path, script, monkeypatch):
    """A scored OCLC job whose id Lynn University's refusing posting shares
    (and comes after it in the file); a scored Ecco job whose whole key a
    refusing Tassal posting shares; and a scored Acme job that refuses."""
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    _write_slice(data_dir, "workday", [
        _posting("R0003795", OCLC, "Oclc", "Dublin, OH / Hybrid", SILENT),
        _posting("R0003795", LYNN, "Lynn University", "Freiburger Residence Hall", REFUSES),
        _posting("R0000001", ACME, "Acme", "Austin, TX", REFUSES),
    ])
    _write_slice(data_dir, "taleo", [
        _posting(2537, ECCO, "Ecco USA, Inc.", "Seattle, WA", SILENT),
        _posting(2537, TASSAL, "Tassal Group Limited", "Tasmania", REFUSES),
    ])
    db_path = tmp_path / "coldstart.sqlite3"
    with connection(db_path) as conn:
        init_schema(conn)
        upsert_job(conn, _scored(OCLC_ID, "Oclc", "Dublin, OH / Hybrid", "SWE III"))
        upsert_job(conn, _scored(SHARED_TALEO_ID, "Ecco USA, Inc.", "Seattle, WA", "SWE"))
        upsert_job(conn, _scored(ACME_ID, "Acme", "Austin, TX", "SWE"))

    settings = SimpleNamespace(
        db_path=db_path,
        data_dir=data_dir,
        needs_visa_sponsorship=True,
        score_threshold_strong=80,
    )
    monkeypatch.setattr(script, "load_settings", lambda: settings)
    return db_path


def _statuses(db_path: Path) -> dict[str, tuple]:
    conn = sqlite3.connect(db_path)
    try:
        return {
            gid: (status, sponsorship)
            for gid, status, sponsorship in conn.execute(
                "SELECT global_id, status, sponsorship FROM jobs"
            )
        }
    finally:
        conn.close()


def test_apply_judges_each_job_on_its_own_description(live, script, monkeypatch):
    monkeypatch.setattr(sys, "argv", ["recheck_sponsorship.py", "--apply"])
    script.main()

    assert _statuses(live) == {
        OCLC_ID: (JobStatus.SCORED.value, None),
        SHARED_TALEO_ID: (JobStatus.SCORED.value, None),
        ACME_ID: (JobStatus.EXCLUDED_SPONSORSHIP.value, "wont_sponsor"),
    }


def test_apply_leaves_a_job_whose_key_only_another_company_still_uses(
    live, script, monkeypatch, capsys
):
    with connection(live) as conn:
        conn.execute("UPDATE jobs SET company = 'Gone Inc' WHERE global_id = ?", (OCLC_ID,))
        conn.commit()

    monkeypatch.setattr(sys, "argv", ["recheck_sponsorship.py", "--apply"])
    script.main()

    assert _statuses(live)[OCLC_ID] == (JobStatus.SCORED.value, None)
    assert "only on another company's posting: 1" in capsys.readouterr().out


def test_report_mode_writes_nothing(live, script, monkeypatch, capsys):
    with connection(live) as conn:
        set_job_state(conn, ACME_ID, "declined")
    before = live.read_bytes()

    monkeypatch.setattr(sys, "argv", ["recheck_sponsorship.py"])
    script.main()

    out = capsys.readouterr().out
    assert "would move to the won't-sponsor list: 0" in out
    assert "already applied/declined (left in place): 1" in out
    assert live.read_bytes() == before
