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
from coldstart.email_template import (
    HTML_TEMPLATE,
    TEXT_TEMPLATE,
    TemplateProblem,
)
from coldstart.email_template import render as render_template
from coldstart.errors import log_error
from coldstart.logging_setup import get_logger
from coldstart.models import EligibilityFlag, JobRecord, JobStatus
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


# The held-back list is unbounded in the database; an email is not.
_MAX_HELD_BACK_IN_DIGEST = 25


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
    # Sourced from held-back rows, NOT from `scored`. Under default-deny no
    # scored row can ever be location-uncertain any more, so the old
    # `scored`-derived list would sit permanently empty with no test failing —
    # "empty section" is a valid state. These rows never reached an LLM, so
    # they have no score; the templates render them without one. Capped
    # because this bucket can run to thousands and it is going into an email.
    location_uncertain = [job for job in jobs if job.status == JobStatus.EXCLUDED_LOCATION][
        :_MAX_HELD_BACK_IN_DIGEST
    ]
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


# --- the fallback layout ---------------------------------------------------
#
# The real layout lives in config/email/digest.html.j2 (Module 24). What's
# here is the safety net for when that template is missing or broken: a
# deliberately plain rendering whose only job is to get the information out.
#
# It used to be a full duplicate of the styled layout, which is worse than
# useless — two layouts to keep in sync, and the copy nobody looks at rots
# quietly until the day it's needed. Keeping it visibly unstyled also means a
# broken template announces itself in the inbox instead of going unnoticed.


def _fallback_notice() -> str:
    return (
        "<p style=\"background:#fef3c7;border:1px solid #f59e0b;padding:10px;\">"
        "<strong>Rendered with the built-in fallback layout.</strong> The email "
        "template in <code>config/email/</code> could not be rendered — see the "
        "logs, or open /preview/email on the dashboard for the exact error."
        "</p>"
    )


def _fallback_html(sections: DigestSections, run_date: date) -> str:
    def rows(jobs: list[JobRecord]) -> str:
        out = []
        for job in sorted(jobs, key=lambda j: j.score or 0, reverse=True):
            link = (
                f'<a href="{html.escape(job.apply_url, quote=True)}">Apply</a>'
                if job.apply_url
                else "no link"
            )
            out.append(
                f"<li><strong>{'&mdash;' if job.score is None else job.score}</strong> &middot; "
                f"{html.escape(job.title)} &mdash; {html.escape(job.company)}"
                f"{' &middot; ' + html.escape(job.location) if job.location else ''}"
                f" &middot; {link}"
                f"<br><span style=\"color:#555;\">{html.escape(job.reasoning or '')}</span></li>"
            )
        return "".join(out)

    def block(title: str, jobs: list[JobRecord]) -> str:
        return f"<h2>{html.escape(title)} ({len(jobs)})</h2><ul>{rows(jobs)}</ul>" if jobs else ""

    body = (
        block("Strong matches", sections.strong)
        + block("Worth considering", sections.consider)
        + block("Held back by the location filter", sections.location_uncertain)
        + block("Eligibility uncertain", sections.eligibility_uncertain)
    ) or "<p><strong>No new matches today.</strong></p>"

    return (
        "<html><body style=\"font-family:sans-serif;\">"
        f"{_fallback_notice()}"
        f"<h1>Coldstart Digest — {run_date.isoformat()}</h1>"
        f"{body}"
        f"<hr><p style=\"color:#555;font-size:12px;\">"
        f"Fetched: {sections.fetched_count} &middot; Filtered: {sections.filtered_count} "
        f"&middot; Scored: {sections.scored_count} &middot; Failed: {sections.failed_count}<br>"
        f"Spend today: ${sections.spend_today_usd:.2f}<br>"
        f"Provider(s) used: {html.escape(', '.join(sections.providers_used) or 'none')}<br>"
        f"CSV: {html.escape(sections.csv_path)}<br>"
        f"Unresolved errors: {sections.unresolved_errors_count}</p>"
        "</body></html>"
    )


def _fallback_text(sections: DigestSections, run_date: date) -> str:
    lines = [
        "Rendered with the built-in fallback layout — the email template "
        "in config/email/ could not be rendered.",
        "",
        f"Coldstart Digest — {run_date.isoformat()}",
        "",
    ]

    def block(title: str, jobs: list[JobRecord]) -> None:
        if not jobs:
            return
        lines.append(f"{title.upper()} ({len(jobs)})")
        for job in sorted(jobs, key=lambda j: j.score or 0, reverse=True):
            where = f" — {job.location}" if job.location else ""
            mark = "  --" if job.score is None else f"[{job.score}]"
            lines.append(f"  {mark} {job.title} — {job.company}{where}")
            lines.append(f"        {job.apply_url or '(no link published)'}")
        lines.append("")

    block("Strong matches", sections.strong)
    block("Worth considering", sections.consider)
    block("Held back by the location filter", sections.location_uncertain)
    block("Eligibility uncertain", sections.eligibility_uncertain)
    if len(lines) <= 4:
        lines.append("No new matches today.")
        lines.append("")

    lines.append(
        f"Fetched: {sections.fetched_count} · Filtered: {sections.filtered_count} · "
        f"Scored: {sections.scored_count} · Failed: {sections.failed_count}"
    )
    lines.append(f"Spend today: ${sections.spend_today_usd:.2f}")
    lines.append(f"Provider(s) used: {', '.join(sections.providers_used) or 'none'}")
    lines.append(f"CSV: {sections.csv_path}")
    lines.append(f"Unresolved errors: {sections.unresolved_errors_count}")
    return "\n".join(lines)


def build_digest_html(sections: DigestSections, run_date: date) -> str:
    """Render the email, preferring the operator-editable template.

    `config/email/digest.html.j2` is read fresh on every call, so an edit
    takes effect on the next digest with no restart. If it is missing or
    broken we log loudly and fall back to the built-in layout — a template
    typo must never cost a day's matches."""
    return _render_or_fallback(HTML_TEMPLATE, sections, run_date, _fallback_html)


def render_digest_text(sections: DigestSections, run_date: date) -> str:
    """The text/plain half, same template-first rule as the HTML."""
    return _render_or_fallback(TEXT_TEMPLATE, sections, run_date, _fallback_text)


def _render_or_fallback(template_name, sections, run_date, fallback):
    try:
        return render_template(template_name, sections=sections, run_date=run_date)
    except TemplateProblem as exc:
        logger.error(
            "email template %s failed, falling back to the built-in layout: %s",
            template_name,
            exc,
        )
        return fallback(sections, run_date)


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
    settings: Settings,
    html_body: str,
    subject: str,
    job_count: int,
    conn: sqlite3.Connection,
    *,
    text_body: str | None = None,
) -> bool:
    """`text_body` is the text/plain alternative. Callers should pass a
    purpose-written one (`render_digest_text`); the regex-stripped fallback
    only exists for callers that have HTML and nothing else."""
    message = EmailMessage()
    message["Subject"] = subject
    message["From"] = settings.smtp_user
    message["To"] = settings.digest_recipient
    message.set_content(text_body if text_body is not None else _html_to_text(html_body))
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
