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
    # Held back by the location filter, never sent to an LLM. A separate value
    # rather than EXCLUDED + location_flag=uncertain, because that combination
    # already exists in the live database on eligibility-excluded rows and
    # cannot be told apart after the fact.
    EXCLUDED_LOCATION = "excluded_location"
    # Held back by the stack/experience filter (filters/stack.py), never sent
    # to an LLM: a JD naming a stack the candidate doesn't have with no trace
    # of the one they do, or an explicit years-required floor the rubric's
    # own formula already dooms to a sub-threshold score. scope.md §4.6.
    EXCLUDED_STACK = "excluded_stack"
    FAILED = "failed"
    # The posting was confirmed gone at the source ATS — either caught before
    # scoring (verify.check_still_live ran pre-LLM and got a DEAD verdict) or
    # by the periodic liveness sweep re-checking an already-SCORED row that
    # died after it was scored. Module 27 / scope.md §3.3.
    DELISTED = "delisted"


class LivenessFlag(StrEnum):
    """Outcome of verify.check_still_live. UNKNOWN is not a synonym for DEAD:
    an ATS this project can't verify, a network error, or an unrecognized
    URL shape must never be treated as evidence the posting is gone — only a
    confirmed "not found" response counts. See verify.py's module docstring."""

    LIVE = "live"
    DEAD = "dead"
    UNKNOWN = "unknown"


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
    role_type_fit: int = Field(ge=0, le=100)
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
    # Which rule decided location_flag ("us_marker", "postal_country_de",
    # "unresolved", ...). With a default-deny filter this is the audit trail:
    # "unresolved" appearing on a recognisable US string means a lexicon gap.
    location_reason: str | None = None
    eligibility_flag: EligibilityFlag
    provider_used: str | None = None
    first_seen_at: datetime
    scored_at: datetime | None = None
    # Which liveness check fired ("workday_cxs_403", "greenhouse_404", ...),
    # set only when status=DELISTED. Mirrors location_reason's audit-trail
    # role: it tells you *why*, not just *that*.
    delist_reason: str | None = None
    delisted_at: datetime | None = None
    # Which stack/experience rule fired ("stack_mismatch:java:...",
    # "experience_floor:10yrs_required"), set only when
    # status=EXCLUDED_STACK. Same audit-trail role as location_reason.
    stack_reason: str | None = None


class LivenessCheck(BaseModel):
    """Result of verify.check_still_live: what we learned about a posting
    from the source ATS's own API, in one HTTP call. `posted_days_ago` is a
    bonus, not a separate check — Workday's CXS response already carries a
    human-readable "Posted N Days Ago" string, so a LIVE workday result gets
    it for free. Populated only for workday, and only on a LIVE verdict;
    None everywhere else (unchecked ats_type, DEAD, UNKNOWN, or the string
    didn't parse)."""

    flag: LivenessFlag
    reason: str
    posted_days_ago: int | None = None


class LivenessCandidate(BaseModel):
    """One row eligible for the periodic liveness sweep — already SCORED, on
    a checkable ats_type, and not yet applied/declined in job_state."""

    global_id: str
    ats_type: str
    apply_url: str | None
    company: str
