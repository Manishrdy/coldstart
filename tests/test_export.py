from __future__ import annotations

import csv
from datetime import date, datetime

from coldstart.export import export_csv
from coldstart.models import (
    EligibilityFlag,
    JobRecord,
    JobStatus,
    LocationFlag,
    ResumeId,
    ScoreBand,
)

_HEADER = [
    "scored_at",
    "score",
    "score_band",
    "company",
    "title",
    "location",
    "resume_used",
    "matched_skills",
    "missing_skills",
    "reasoning",
    "apply_url",
    "ats_type",
    "posted_at",
    "location_flag",
    "location_reason",
    "eligibility_flag",
    "status",
    "provider_used",
]


def _job(**overrides) -> JobRecord:
    defaults = dict(
        global_id="job-1",
        company="Acme",
        title="Senior Engineer",
        ats_type="greenhouse",
        status=JobStatus.SCORED,
        location_flag=LocationFlag.ACCEPTED,
        eligibility_flag=EligibilityFlag.PASSED,
        first_seen_at=datetime(2026, 8, 19, 12, 0, 0),
    )
    defaults.update(overrides)
    return JobRecord(**defaults)


def _read_rows(path) -> list[list[str]]:
    with path.open(encoding="utf-8-sig", newline="") as f:
        return list(csv.reader(f))


def test_creates_file_with_header_and_row(tmp_path):
    job = _job(score=80, score_band=ScoreBand.STRONG, reasoning="great fit")
    path = export_csv([job], tmp_path, date(2026, 8, 19))

    assert path == tmp_path / "scored_2026-08-19.csv"
    rows = _read_rows(path)
    assert rows[0] == _HEADER
    assert len(rows) == 2
    assert rows[1][_HEADER.index("company")] == "Acme"
    assert rows[1][_HEADER.index("score")] == "80"


def test_append_does_not_duplicate_header(tmp_path):
    run_date = date(2026, 8, 19)
    export_csv([_job(global_id="a", score=50)], tmp_path, run_date)
    export_csv([_job(global_id="b", score=60)], tmp_path, run_date)

    rows = _read_rows(tmp_path / "scored_2026-08-19.csv")
    assert rows.count(_HEADER) == 1
    assert len(rows) == 3


def test_append_uses_same_file_for_multiple_polls_same_day(tmp_path):
    run_date = date(2026, 8, 19)
    first = export_csv([_job(global_id="a")], tmp_path, run_date)
    second = export_csv([_job(global_id="b")], tmp_path, run_date)
    assert first == second
    assert len(list(tmp_path.glob("scored_*.csv"))) == 1


def test_reject_band_included(tmp_path):
    job = _job(score=10, score_band=ScoreBand.REJECT, status=JobStatus.SCORED)
    path = export_csv([job], tmp_path, date(2026, 8, 19))

    rows = _read_rows(path)
    assert rows[1][_HEADER.index("score_band")] == "reject"


def test_sorted_by_score_descending(tmp_path):
    jobs = [
        _job(global_id="low", score=10, score_band=ScoreBand.REJECT),
        _job(global_id="high", score=90, score_band=ScoreBand.STRONG),
        _job(global_id="mid", score=65, score_band=ScoreBand.CONSIDER),
    ]
    path = export_csv(jobs, tmp_path, date(2026, 8, 19))

    rows = _read_rows(path)
    scores = [row[_HEADER.index("score")] for row in rows[1:]]
    assert scores == ["90", "65", "10"]


def test_null_score_jobs_sort_after_scored_jobs(tmp_path):
    jobs = [
        _job(
            global_id="excluded",
            score=None,
            score_band=None,
            status=JobStatus.EXCLUDED,
            eligibility_flag=EligibilityFlag.EXCLUDED,
        ),
        _job(global_id="scored", score=40, score_band=ScoreBand.REJECT),
    ]
    path = export_csv(jobs, tmp_path, date(2026, 8, 19))

    rows = _read_rows(path)
    scores = [row[_HEADER.index("score")] for row in rows[1:]]
    assert scores == ["40", ""]


def test_reasoning_with_commas_and_newlines_round_trips(tmp_path):
    reasoning = 'Strong match, but missing "Kubernetes".\nAlso lacks Go experience, prefers Python.'
    job = _job(reasoning=reasoning)
    path = export_csv([job], tmp_path, date(2026, 8, 19))

    rows = _read_rows(path)
    assert rows[1][_HEADER.index("reasoning")] == reasoning


def test_matched_and_missing_skills_joined(tmp_path):
    job = _job(matched_skills=["Python", "AWS"], missing_skills=["Go"])
    path = export_csv([job], tmp_path, date(2026, 8, 19))

    rows = _read_rows(path)
    assert rows[1][_HEADER.index("matched_skills")] == "Python; AWS"
    assert rows[1][_HEADER.index("missing_skills")] == "Go"


def test_optional_fields_render_as_empty_string(tmp_path):
    job = _job()  # score, score_band, resume_used, location, etc. all default None/empty
    path = export_csv([job], tmp_path, date(2026, 8, 19))

    rows = _read_rows(path)
    row = rows[1]
    assert row[_HEADER.index("score")] == ""
    assert row[_HEADER.index("score_band")] == ""
    assert row[_HEADER.index("resume_used")] == ""
    assert row[_HEADER.index("location")] == ""
    assert row[_HEADER.index("posted_at")] == ""
    assert row[_HEADER.index("provider_used")] == ""


def test_resume_used_and_posted_at_rendered(tmp_path):
    job = _job(
        resume_used=ResumeId.B,
        posted_at=datetime(2026, 8, 18, 9, 30),
        scored_at=datetime(2026, 8, 19, 10, 0),
        provider_used="deepseek",
        apply_url="https://example.com/apply",
    )
    path = export_csv([job], tmp_path, date(2026, 8, 19))

    rows = _read_rows(path)
    row = rows[1]
    assert row[_HEADER.index("resume_used")] == "B"
    assert row[_HEADER.index("posted_at")] == "2026-08-18T09:30:00"
    assert row[_HEADER.index("scored_at")] == "2026-08-19T10:00:00"
    assert row[_HEADER.index("provider_used")] == "deepseek"
    assert row[_HEADER.index("apply_url")] == "https://example.com/apply"


def test_different_run_dates_produce_different_files(tmp_path):
    export_csv([_job(global_id="a")], tmp_path, date(2026, 8, 19))
    export_csv([_job(global_id="b")], tmp_path, date(2026, 8, 20))
    assert (tmp_path / "scored_2026-08-19.csv").exists()
    assert (tmp_path / "scored_2026-08-20.csv").exists()


def test_creates_output_dir_if_missing(tmp_path):
    output_dir = tmp_path / "nested" / "output"
    path = export_csv([_job()], output_dir, date(2026, 8, 19))
    assert path.exists()


def test_file_starts_with_utf8_bom(tmp_path):
    path = export_csv([_job()], tmp_path, date(2026, 8, 19))
    raw = path.read_bytes()
    assert raw.startswith(b"\xef\xbb\xbf")


def test_bom_not_duplicated_on_append(tmp_path):
    run_date = date(2026, 8, 19)
    export_csv([_job(global_id="a")], tmp_path, run_date)
    export_csv([_job(global_id="b")], tmp_path, run_date)

    raw = (tmp_path / "scored_2026-08-19.csv").read_bytes()
    assert raw.count(b"\xef\xbb\xbf") == 1


def test_append_starts_a_new_file_when_the_existing_header_is_stale(tmp_path):
    """A column added to _COLUMNS mid-day must not append wider rows under the
    narrower header already on disk — every field after the new one would be
    silently shifted."""
    path = tmp_path / "scored_2026-08-19.csv"
    path.write_text('"scored_at","score","company"\n"2026-08-19T10:00:00","91","Acme"\n')

    written = export_csv([_job(score=64)], tmp_path, date(2026, 8, 19))

    assert written != path
    assert written.name == "scored_2026-08-19_v2.csv"
    # The stale file is left exactly as it was, not rewritten or appended to.
    assert path.read_text().count("\n") == 2
    assert _read_rows(written)[0] == _HEADER


def test_append_reuses_the_file_when_the_header_already_matches(tmp_path):
    first = export_csv([_job(score=91)], tmp_path, date(2026, 8, 19))
    second = export_csv([_job(score=64)], tmp_path, date(2026, 8, 19))
    assert first == second
    assert len(_read_rows(second)) == 3  # header + two rows


def test_location_reason_is_exported(tmp_path):
    path = export_csv([_job(location_reason="postal_country_de")], tmp_path, date(2026, 8, 19))
    row = _read_rows(path)[1]
    assert row[_HEADER.index("location_reason")] == "postal_country_de"
