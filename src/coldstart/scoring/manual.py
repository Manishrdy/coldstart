"""Score a job without an LLM (Module 40, scope.md §6.6).

Asked for 2026-10-21, after one poll put five base-power roles on the
shortlist at once: nobody sends five tailored applications to one employer on
the same day, so paying an LLM to score — and hemline to tailor a resume for —
all five is waste. At most LLM_SCORES_PER_COMPANY_PER_DAY of a company's
postings go to the LLM each day; the rest are scored here, shown on the
dashboard with a "manual" flair, and never sent to hemline.

Same four dimensions and weights as the LLM rubric (rubric.py), each read off
the posting with plain rules:

- tech stack: the skills below the JD names, and how many of them the resume
  also names;
- experience: the years the JD asks for — the same parser the stack filter
  uses — through the rubric's own banding formula;
- seniority: the title's level words against the candidate's years;
- role type: FDE / AI / founding titles, the same keywords routing uses.

It is a rough score on purpose. Its first job is ranking a company's postings
against each other, so the LLM gets the most promising ones; its second is
giving you enough to decide whether the rest deserve a look.
"""

from __future__ import annotations

import re

from coldstart.filters.stack import _max_required_years
from coldstart.models import JobScore, RawJob, ResumeId, ScoreBand
from coldstart.routing import route_by_keywords
from coldstart.scoring.rubric import composite_score, experience_fit_score
from coldstart.settings import Settings

# What `provider_used` says on a job scored here. The dashboard's flair and
# hemline's skip both key off it.
MANUAL_PROVIDER = "manual"

# (shown as, pattern). Case-insensitive, matched against the JD and the resume
# alike. Not exhaustive — the skills a software/AI/FDE posting most often
# names, which is what the comparison needs. The ones that are also ordinary
# English words (Go, React, Node, Spark, REST) only count capitalized and not
# opening a sentence, and Go only in a list, so "go to market", "the rest of
# the team" or "React quickly to incidents" isn't a skill.
_MID_SENTENCE = r"(?<=\S\s)(?<![.!?]\s)"
_SKILLS: tuple[tuple[str, str], ...] = (
    ("Python", r"\bpython\b"),
    ("TypeScript", r"\btypescript\b"),
    ("JavaScript", r"\bjavascript\b"),
    ("Node.js", r"\bnode\.?js\b|" + _MID_SENTENCE + r"(?-i:\bNode\b)"),
    ("React", r"\breact\.?js\b|" + _MID_SENTENCE + r"(?-i:\bReact\b)"),
    ("Next.js", r"\bnext\.?js\b"),
    ("Vue", r"\bvue(?:\.?js)?\b"),
    ("Angular", r"\bangular\b"),
    ("FastAPI", r"\bfastapi\b"),
    ("Flask", r"\bflask\b"),
    ("Django", r"\bdjango\b"),
    ("Java", r"\bjava\b(?!script)"),
    ("Go", r"\bgolang\b|" + _MID_SENTENCE + r"(?-i:\bGo\b)(?=\s*[,/)]|\s+(?:and|or)\b)"),
    ("Rust", r"\brust\b"),
    ("C++", r"\bc\+\+"),
    ("C#", r"\bc#"),
    ("Ruby", r"\bruby\b"),
    ("Kotlin", r"\bkotlin\b"),
    ("Scala", r"\bscala\b"),
    ("SQL", r"\bsql\b"),
    ("PostgreSQL", r"\bpostgres(?:ql)?\b"),
    ("MySQL", r"\bmysql\b"),
    ("MongoDB", r"\bmongo(?:db)?\b"),
    ("Redis", r"\bredis\b"),
    ("Kafka", r"\bkafka\b"),
    ("Spark", r"\bpyspark\b|" + _MID_SENTENCE + r"(?-i:\bSpark\b)"),
    ("Airflow", r"\bairflow\b"),
    ("GraphQL", r"\bgraphql\b"),
    ("REST APIs", r"\brestful\b|" + _MID_SENTENCE + r"(?-i:\bREST\b)"),
    ("gRPC", r"\bgrpc\b"),
    ("AWS", r"\baws\b|amazon web services"),
    ("GCP", r"\bgcp\b|google cloud"),
    ("Azure", r"\bazure\b"),
    ("Docker", r"\bdocker\b"),
    ("Kubernetes", r"\bkubernetes\b|\bk8s\b"),
    ("Terraform", r"\bterraform\b"),
    ("CI/CD", r"\bci/cd\b"),
    ("Linux", r"\blinux\b"),
    ("Microservices", r"\bmicro-?services?\b"),
    ("Distributed systems", r"distributed systems?"),
    ("LLMs", r"\bllms?\b|large language models?"),
    ("RAG", r"\brag\b|retrieval[- ]augmented"),
    ("LangChain", r"\blangchain\b"),
    ("Agents", r"\bagentic\b|\bai agents?\b"),
    ("Machine learning", r"machine learning|\bml\b"),
    ("PyTorch", r"\bpytorch\b"),
    ("TensorFlow", r"\btensorflow\b"),
    ("Vector databases", r"vector (?:databases?|stores?|search)|\bpgvector\b|\bpinecone\b"),
    ("Prompt engineering", r"prompt engineering"),
)
_SKILL_RES = tuple((name, re.compile(pattern, re.IGNORECASE)) for name, pattern in _SKILLS)

_LEADERSHIP_RE = re.compile(r"\b(?:staff|principal|lead|architect|distinguished)\b", re.IGNORECASE)
_SENIOR_RE = re.compile(r"\b(?:senior|sr\.?)\b|\biii\b", re.IGNORECASE)
_JUNIOR_RE = re.compile(
    r"\b(?:junior|jr\.?|entry[- ]level|new grad(?:uate)?|associate|early career)\b|\bi\b$",
    re.IGNORECASE,
)
_FOUNDING_RE = re.compile(r"\bfounding\b", re.IGNORECASE)

# When the JD states no years. The LLM estimates from the prose instead; a
# posting that doesn't say is usually not asking for many.
_EXPERIENCE_NOT_STATED = 80
# When the JD names none of the skills above, there is nothing to compare.
_STACK_NOT_STATED = 50


def skills_in(text: str) -> list[str]:
    return [name for name, pattern in _SKILL_RES if pattern.search(text)]


def _seniority_fit(title: str, x_years: float) -> tuple[int, str]:
    if _LEADERSHIP_RE.search(title):
        return 35, "staff/lead-level title"
    if _SENIOR_RE.search(title):
        return (85, "senior title") if x_years >= 5 else (60, "senior title")
    if _JUNIOR_RE.search(title):
        return 95, "entry/junior title"
    return 90, "mid-level title"


def _role_type_fit(title: str) -> int:
    if _FOUNDING_RE.search(title):
        return 90
    resume = route_by_keywords(title)
    if resume in (ResumeId.C, ResumeId.D):
        return 90
    if resume is ResumeId.B:
        return 85
    return 70


def score_manually(
    job: RawJob, resume_text: str, settings: Settings, *, reason: str
) -> JobScore:
    """`reason` is why this job didn't go to the LLM; it leads the reasoning
    the dashboard shows."""
    description = job.description or ""
    resume_skills = set(skills_in(resume_text))
    wanted = skills_in(description)
    matched = [skill for skill in wanted if skill in resume_skills]
    missing = [skill for skill in wanted if skill not in resume_skills]
    tech = round(100 * len(matched) / len(wanted)) if wanted else _STACK_NOT_STATED

    years = _max_required_years(description)
    experience = experience_fit_score(years, settings.experience_years)
    if experience is None:
        experience = _EXPERIENCE_NOT_STATED

    seniority, seniority_note = _seniority_fit(job.title or "", settings.experience_years)
    role_type = _role_type_fit(job.title or "")

    score = composite_score(
        {"tech_stack": tech, "experience": experience, "seniority": seniority,
         "role_type": role_type}
    )
    stack_note = (
        f"{len(matched)} of {len(wanted)} skills the posting names are on your resume"
        if wanted
        else "the posting names none of the skills this checks"
    )
    years_note = f"asks for {years} years" if years is not None else "no years stated"
    return JobScore(
        eligible=True,
        score=score,
        score_band=ScoreBand.STRONG if score >= settings.score_threshold_strong
        else ScoreBand.REJECT,
        tech_stack_match=tech,
        seniority_fit=seniority,
        experience_fit=experience,
        role_type_fit=role_type,
        matched_skills=matched,
        missing_skills=missing,
        reasoning=(
            f"Scored by keyword match, not the LLM: {reason}. "
            f"{stack_note[0].upper()}{stack_note[1:]}; {years_note}; {seniority_note}."
        ),
    )
