from __future__ import annotations

from coldstart.models import JobScore, RawJob
from coldstart.text_utils import filter_resume_for_llm

RUBRIC_VERSION = "v1"

# Keys match JobScore's *_match/*_fit field names minus the suffix, except
# role_type (JobScore.role_type_fit). composite_score's caller is responsible
# for mapping JobScore fields onto these keys.
WEIGHTS = {"tech_stack": 0.35, "experience": 0.25, "seniority": 0.25, "role_type": 0.15}

_MAX_DESCRIPTION_CHARS = 8000


def experience_fit_score(required_years: float | None, x_years: float) -> int | None:
    if required_years is None:
        return None

    r, x = required_years, x_years
    if r <= x:
        return 100
    if r <= x + 2:
        fraction = (r - x) / 2
        return round(100 - fraction * 20)
    if r <= x + 4:
        fraction = (r - x - 2) / 2
        return round(80 - fraction * 40)
    return 15


def composite_score(sub: dict[str, int]) -> int:
    total = sum(sub[key] * weight for key, weight in WEIGHTS.items())
    return round(total)


_SYSTEM_PROMPT_TEMPLATE = """\
You are an expert technical recruiter scoring how well a candidate's resume \
fits a job posting. You will be given the candidate's resume, then a job \
title/company/location/description in a separate message.

Score the fit across four weighted dimensions:
- Tech stack / language / tools overlap — {tech_stack_weight:.0%} weight
- Years-of-experience fit — {experience_weight:.0%} weight
- Seniority/title fit — {seniority_weight:.0%} weight
- Role-type/domain fit (founding-ownership / FDE customer-facing / \
AI-specific signals) — {role_type_weight:.0%} weight

Years-of-experience banding: the candidate has X = {x_years:.2f} years of \
relevant experience. Let R = the years of experience the posting requires.
- R <= {x_years:.2f} -> experience_fit = 100
- {x_years:.2f} < R <= {x_plus_2:.2f} -> experience_fit = linear 100 -> 80
- {x_plus_2:.2f} < R <= {x_plus_4:.2f} -> experience_fit = linear 80 -> 40
- R > {x_plus_4:.2f} -> experience_fit = 15 (a long shot, not a disqualifier)
- If the posting does not state R, estimate it from the job description \
prose and apply the same bands.

Seniority (Staff/Principal/Lead/etc.) is a SOFT SCORING PENALTY ONLY — \
never a disqualifier. A senior-leaning posting is still worth seeing for a \
less-experienced candidate, just scored lower on the seniority and \
experience dimensions rather than excluded.

ELIGIBILITY SAFETY NET: if the job description requires US citizenship, a \
security clearance, or is export-controlled (ITAR/EAR), set "eligible": \
false, "score": 0, and populate "disqualification_reason" with the \
specific requirement you found — even if it looks like upstream filtering \
should already have caught this.

Candidate resume:
---
{resume_text}
---

Respond with ONLY strict JSON matching this schema — no prose, no markdown \
code fences:
{{
  "eligible": true or false,
  "disqualification_reason": string or null,
  "score": integer 0-100,
  "score_band": "strong" or "consider" or "reject",
  "tech_stack_match": integer 0-100,
  "seniority_fit": integer 0-100,
  "experience_fit": integer 0-100,
  "role_type_fit": integer 0-100,
  "matched_skills": [string, ...],
  "missing_skills": [string, ...],
  "reasoning": string
}}

Rubric version: {rubric_version}
"""


def build_system_prompt(
    resume_text: str, x_years: float, rubric_version: str = RUBRIC_VERSION
) -> str:
    return _SYSTEM_PROMPT_TEMPLATE.format(
        tech_stack_weight=WEIGHTS["tech_stack"],
        experience_weight=WEIGHTS["experience"],
        seniority_weight=WEIGHTS["seniority"],
        role_type_weight=WEIGHTS["role_type"],
        x_years=x_years,
        x_plus_2=x_years + 2,
        x_plus_4=x_years + 4,
        resume_text=filter_resume_for_llm(resume_text),
        rubric_version=rubric_version,
    )


def build_user_prompt(job: RawJob) -> str:
    description = (job.description or "")[:_MAX_DESCRIPTION_CHARS]
    return (
        f"Title: {job.title}\n"
        f"Company: {job.company}\n"
        f"Location: {job.location or 'Not specified'}\n\n"
        f"Job description:\n{description}"
    )


# Sanity check that JobScore still carries a sub-score field for each WEIGHTS
# dimension, so a field added/removed from JobScore without updating this
# module fails loudly at import time instead of silently under-scoring later
# (this is exactly how role_type_fit went missing before Module 14 caught it).
_REQUIRED_SCORE_FIELDS = {"tech_stack_match", "seniority_fit", "experience_fit", "role_type_fit"}
_missing_fields = _REQUIRED_SCORE_FIELDS - set(JobScore.model_fields)
if _missing_fields:
    raise AssertionError(f"JobScore is missing fields WEIGHTS expects: {_missing_fields}")
