from __future__ import annotations

from datetime import datetime
from enum import StrEnum

from pydantic import BaseModel, Field, model_validator


class LocationFlag(StrEnum):
    ACCEPTED = "accepted"
    REJECTED = "rejected"
    UNCERTAIN = "uncertain"


class EligibilityFlag(StrEnum):
    PASSED = "passed"
    EXCLUDED = "excluded"
    UNCERTAIN = "uncertain"


class JobStatus(StrEnum):
    PENDING = "pending"
    SCORED = "scored"
    EXCLUDED = "excluded"
    FAILED = "failed"


class ScoreBand(StrEnum):
    STRONG = "strong"
    CONSIDER = "consider"
    REJECT = "reject"


class ResumeId(StrEnum):
    A = "A"
    B = "B"
    C = "C"
    D = "D"


class RawJob(BaseModel):
    """One row off the parquet slice. JD text lives here and is never persisted."""

    global_id: str
    requisition_id: str | None = None
    company: str
    title: str
    location: str | None = None
    country_iso: str | None = None
    is_remote: bool | None = None
    apply_url: str | None = None
    url: str
    ats_type: str
    posted_at: datetime | None = None
    description: str | None = None
    experience: int | None = None
    raw: dict | None = None


class JobScore(BaseModel):
    """LLM output contract — see scope.md §6.5."""

    eligible: bool
    disqualification_reason: str | None = None
    score: int = Field(ge=0, le=100)
    score_band: ScoreBand
    tech_stack_match: int = Field(ge=0, le=100)
    seniority_fit: int = Field(ge=0, le=100)
    experience_fit: int = Field(ge=0, le=100)
    matched_skills: list[str]
    missing_skills: list[str]
    reasoning: str

    @model_validator(mode="after")
    def enforce_ineligible_zero(self) -> JobScore:
        if not self.eligible and self.score != 0:
            raise ValueError("score must be 0 when eligible is False")
        return self


class SliceState(BaseModel):
    """One row of the slice_state table — tracks per-ATS change detection (scope.md §3.2)."""

    ats_type: str
    last_sha256: str | None = None
    last_processed_at: datetime | None = None
    row_count: int | None = None


class JobRecord(BaseModel):
    """What actually persists. No JD text. Columns per scope.md §7.1."""

    global_id: str
    requisition_id: str | None = None
    company: str
    title: str
    location: str | None = None
    apply_url: str | None = None
    ats_type: str
    posted_at: datetime | None = None
    resume_used: ResumeId | None = None
    score: int | None = None
    score_band: ScoreBand | None = None
    eligible: bool | None = None
    matched_skills: list[str] = []
    missing_skills: list[str] = []
    reasoning: str | None = None
    status: JobStatus
    location_flag: LocationFlag
    eligibility_flag: EligibilityFlag
    provider_used: str | None = None
    first_seen_at: datetime
    scored_at: datetime | None = None
