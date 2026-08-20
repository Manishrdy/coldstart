from __future__ import annotations

import html
import re
import smtplib
import sqlite3
from datetime import UTC, date, datetime
from email.message import EmailMessage
from pathlib import Path

from pydantic import BaseModel
from tenacity import retry, retry_if_exception_type, stop_after_attempt, wait_exponential

from coldstart.db import log_email
from coldstart.errors import log_error
from coldstart.logging_setup import get_logger
from coldstart.models import EligibilityFlag, JobRecord, JobStatus, LocationFlag
from coldstart.settings import Settings

logger = get_logger(__name__)

_TAG_RE = re.compile(r"<[^>]+>")
_BLOCK_BREAK_RE = re.compile(r"(?i)</(tr|p|div|h[1-6]|table)>")


class DigestSections(BaseModel):
    """Pre-categorized input to build_digest_html — see DEVELOPMENT_PLAN.md
    Module 18 for why this type exists and how its fields are sourced."""

    strong: list[JobRecord] = []
    consider: list[JobRecord] = []
    location_uncertain: list[JobRecord] = []
    eligibility_uncertain: list[JobRecord] = []
    fetched_count: int
    filtered_count: int
    scored_count: int
    failed_count: int
    spend_today_usd: float
    providers_used: list[str]
    csv_path: str
    unresolved_errors_count: int
    # Start of the period this digest covers — everything since the last
    # one actually went out. Stated in the footer so the reader knows what
    # 'new' means here, rather than assuming it means 'today'.
    window_start: datetime | None = None


def build_digest_sections(
    jobs: list[JobRecord],
    settings: Settings,
    *,
    fetched_count: int,
    filtered_count: int,
    scored_count: int,
    failed_count: int,
    spend_today_usd: float,
    providers_used: list[str],
    csv_path: str | Path,
    unresolved_errors_count: int,
    window_start: datetime | None = None,
) -> DigestSections:
    # Band membership is computed from the numeric score against settings'
    # configurable thresholds, not from JobScore.score_band — the rubric
    # prompt (Module 14) has the LLM self-assign that label without knowing
    # settings.score_threshold_strong/consider, so it can't be trusted to
    # respect a threshold the operator changed in .env.
    scored = [job for job in jobs if job.status == JobStatus.SCORED and job.score is not None]

    strong = [job for job in scored if job.score >= settings.score_threshold_strong]
    consider = [
        job
        for job in scored
        if settings.score_threshold_consider <= job.score < settings.score_threshold_strong
    ]
    location_uncertain = [job for job in scored if job.location_flag == LocationFlag.UNCERTAIN]
    eligibility_uncertain = [
        job for job in scored if job.eligibility_flag == EligibilityFlag.UNCERTAIN
    ]

    return DigestSections(
        strong=strong,
        consider=consider,
        location_uncertain=location_uncertain,
        eligibility_uncertain=eligibility_uncertain,
        fetched_count=fetched_count,
        filtered_count=filtered_count,
        scored_count=scored_count,
        failed_count=failed_count,
        spend_today_usd=spend_today_usd,
        providers_used=providers_used,
        csv_path=str(csv_path),
        unresolved_errors_count=unresolved_errors_count,
        window_start=window_start,
    )


def _render_job_row(job: JobRecord) -> str:
    apply_cell = (
        f'<a href="{html.escape(job.apply_url, quote=True)}">Apply</a>' if job.apply_url else ""
    )
    return (
        "<tr>"
        f"<td>{job.score}</td>"
        f"<td>{html.escape(job.company)}</td>"
        f"<td>{html.escape(job.title)}</td>"
        f"<td>{html.escape(job.location or '')}</td>"
        f"<td>{html.escape(job.resume_used.value if job.resume_used else '')}</td>"
        f"<td>{html.escape('; '.join(job.matched_skills))}</td>"
        f"<td>{html.escape('; '.join(job.missing_skills))}</td>"
        f"<td>{html.escape(job.reasoning or '')}</td>"
        f"<td>{apply_cell}</td>"
        "</tr>"
    )


def _render_job_section(title: str, jobs: list[JobRecord], *, deemphasize: bool) -> str:
    style = ' style="color:#888888;"' if deemphasize else ""
    ordered = sorted(jobs, key=lambda job: job.score, reverse=True)
    rows = "".join(_render_job_row(job) for job in ordered)
    return (
        f"<h2{style}>{html.escape(title)}</h2>"
        f'<table{style} border="1" cellpadding="4" cellspacing="0">'
        "<tr><th>Score</th><th>Company</th><th>Title</th><th>Location</th>"
        "<th>Resume</th><th>Matched skills</th><th>Missing skills</th>"
        "<th>Reasoning</th><th>Apply</th></tr>"
        f"{rows}</table>"
    )


def _render_footer(sections: DigestSections) -> str:
    providers = ", ".join(sections.providers_used) if sections.providers_used else "none"
    covered = (
        f"Covering everything since the last digest "
        f"({sections.window_start:%Y-%m-%d %H:%M} UTC)<br>"
        if sections.window_start is not None
        else ""
    )
    return (
        "<hr>"
        "<p>"
        f"{covered}"
        f"Fetched: {sections.fetched_count} &middot; "
        f"Filtered: {sections.filtered_count} &middot; "
        f"Scored: {sections.scored_count} &middot; "
        f"Failed: {sections.failed_count}<br>"
        f"Spend today: ${sections.spend_today_usd:.2f}<br>"
        f"Provider(s) used: {html.escape(providers)}<br>"
        f"CSV: {html.escape(sections.csv_path)}<br>"
        f"Unresolved errors: {sections.unresolved_errors_count}"
        "</p>"
    )


def build_digest_html(sections: DigestSections, run_date: date) -> str:
    has_matches = any(
        (
            sections.strong,
            sections.consider,
            sections.location_uncertain,
            sections.eligibility_uncertain,
        )
    )

    if not has_matches:
        body = "<p><strong>No new matches today.</strong></p>"
    else:
        parts = []
        if sections.strong:
            parts.append(_render_job_section("Strong matches", sections.strong, deemphasize=False))
        if sections.consider:
            parts.append(
                _render_job_section("Worth considering", sections.consider, deemphasize=True)
            )
        if sections.location_uncertain:
            parts.append(
                _render_job_section(
                    "Location uncertain — needs a manual eyeball",
                    sections.location_uncertain,
                    deemphasize=False,
                )
            )
        if sections.eligibility_uncertain:
            parts.append(
                _render_job_section(
                    "Eligibility uncertain — needs a manual eyeball",
                    sections.eligibility_uncertain,
                    deemphasize=False,
                )
            )
        body = "".join(parts)

    return (
        "<html><body>"
        f"<h1>Coldstart Digest — {run_date.isoformat()}</h1>"
        f"{body}"
        f"{_render_footer(sections)}"
        "</body></html>"
    )


def _html_to_text(html_body: str) -> str:
    text = _BLOCK_BREAK_RE.sub("\n", html_body)
    text = re.sub(r"(?i)<br\s*/?>", "\n", text)
    text = _TAG_RE.sub("", text)
    text = html.unescape(text)
    lines = [line.strip() for line in text.splitlines()]
    return "\n".join(line for line in lines if line)


@retry(
    retry=retry_if_exception_type(smtplib.SMTPException),
    stop=stop_after_attempt(3),
    wait=wait_exponential(multiplier=1, min=1, max=10),
    reraise=True,
)
def _send_smtp(settings: Settings, message: EmailMessage) -> None:
    with smtplib.SMTP(settings.smtp_host, settings.smtp_port, timeout=30) as smtp:
        smtp.starttls()
        smtp.login(settings.smtp_user, settings.smtp_app_password.get_secret_value())
        smtp.send_message(message)


def send_digest(
    settings: Settings, html_body: str, subject: str, job_count: int, conn: sqlite3.Connection
) -> bool:
    message = EmailMessage()
    message["Subject"] = subject
    message["From"] = settings.smtp_user
    message["To"] = settings.digest_recipient
    message.set_content(_html_to_text(html_body))
    message.add_alternative(html_body, subtype="html")

    sent_at = datetime.now(UTC)
    try:
        _send_smtp(settings, message)
    except smtplib.SMTPException as exc:
        logger.error("digest send failed: %s", exc, exc_info=True)
        log_error(
            conn,
            stage="email",
            exc=exc,
            source_file=__name__,
            function_name="send_digest",
        )
        log_email(conn, sent_at=sent_at, job_count=job_count, status="failed", error=str(exc))
        return False

    logger.info("digest sent: %d job(s), subject=%r", job_count, subject)
    log_email(conn, sent_at=sent_at, job_count=job_count, status="sent")
    return True
