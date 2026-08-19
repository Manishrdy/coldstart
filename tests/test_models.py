from datetime import datetime

import pytest
from pydantic import ValidationError

from coldstart.models import (
    JobRecord,
    JobScore,
    JobStatus,
    LocationFlag,
    RawJob,
    ScoreBand,
)


def _job_score_kwargs(**overrides):
    kwargs = dict(
        eligible=True,
        score=75,
        score_band=ScoreBand.STRONG,
        tech_stack_match=80,
        seniority_fit=70,
        experience_fit=75,
        role_type_fit=65,
        matched_skills=["python"],
        missing_skills=["rust"],
        reasoning="Good fit.",
    )
    kwargs.update(overrides)
    return kwargs


def test_job_score_ineligible_with_nonzero_score_raises():
    with pytest.raises(ValidationError):
        JobScore(**_job_score_kwargs(eligible=False, score=85))


def test_job_score_ineligible_with_zero_score_is_valid():
    score = JobScore(**_job_score_kwargs(eligible=False, score=0))
    assert score.eligible is False
    assert score.score == 0


def test_job_score_out_of_range_raises():
    with pytest.raises(ValidationError):
        JobScore(**_job_score_kwargs(score=101))


def test_job_score_negative_raises():
    with pytest.raises(ValidationError):
        JobScore(**_job_score_kwargs(tech_stack_match=-1))


def test_raw_job_tolerates_all_none_optionals():
    job = RawJob(
        global_id="abc123",
        requisition_id=None,
        company="Acme",
        title="Software Engineer",
        location=None,
        country_iso=None,
        is_remote=None,
        apply_url=None,
        url="https://example.com/job/abc123",
        ats_type="greenhouse",
        posted_at=None,
        description=None,
        experience=None,
        raw=None,
    )
    assert job.global_id == "abc123"
    assert job.location is None


def test_raw_job_requires_core_fields():
    with pytest.raises(ValidationError):
        RawJob(company="Acme", title="Software Engineer", url="https://x", ats_type="greenhouse")


def test_job_record_minimal_pending_record():
    record = JobRecord(
        global_id="abc123",
        company="Acme",
        title="Software Engineer",
        ats_type="greenhouse",
        status=JobStatus.PENDING,
        location_flag=LocationFlag.ACCEPTED,
        eligibility_flag="uncertain",
        first_seen_at=datetime(2026, 8, 19, 12, 0, 0),
    )
    assert record.score is None
    assert record.matched_skills == []
    assert record.status is JobStatus.PENDING
