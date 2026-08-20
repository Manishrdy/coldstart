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


# --- rendering -------------------------------------------------------------
#
# Email HTML is not web HTML. Flexbox, grid, external stylesheets and <style>
# blocks are unreliable across Gmail/Outlook/Apple Mail, so the layout is
# table-based with inline styles and a 600px shell — the format every bulk
# sender converges on for the same reasons. No external images either: mail
# clients block remote content by default, so the brand mark is type, not a
# logo file.

_BRAND = "Coldstart"
_WIDTH = 600

_INK = "#16181d"
_MUTED = "#6b7280"
_FAINT = "#9aa1ad"
_BORDER = "#e5e7eb"
_PAGE_BG = "#f4f5f7"
_CARD_BG = "#ffffff"
_ACCENT = "#2563eb"

_FONT = (
    "-apple-system,BlinkMacSystemFont,'Segoe UI',Roboto,Helvetica,Arial,sans-serif"
)

# (badge background, badge text) per band.
_TONES = {
    "strong": ("#047857", "#ffffff"),
    "consider": ("#b45309", "#ffffff"),
    "review": ("#475569", "#ffffff"),
}


def _chip(text: str, *, muted: bool = False) -> str:
    bg = "#f1f5f9" if muted else "#ecfdf5"
    fg = _MUTED if muted else "#047857"
    return (
        f'<span style="display:inline-block;background:{bg};color:{fg};'
        f'border-radius:4px;padding:2px 7px;margin:0 4px 4px 0;'
        f'font-size:12px;line-height:18px;">{html.escape(text)}</span>'
    )


def _render_job_row(job: JobRecord, *, tone: str = "strong") -> str:
    """One job, as a card.

    Was a row in a 9-column table, which is unreadable on a phone — the
    reasoning column alone is a paragraph. A card stacks instead of
    overflowing."""
    badge_bg, badge_fg = _TONES.get(tone, _TONES["review"])

    meta_bits = [job.company]
    if job.location:
        meta_bits.append(job.location)
    meta = " &middot; ".join(html.escape(bit) for bit in meta_bits)

    sub_bits = []
    if job.resume_used:
        sub_bits.append(f"Resume {html.escape(job.resume_used.value)}")
    if job.ats_type:
        sub_bits.append(html.escape(job.ats_type))
    if job.posted_at:
        sub_bits.append(f"posted {job.posted_at:%b %-d}")
    sub = " &middot; ".join(sub_bits)

    matched = "".join(_chip(skill) for skill in job.matched_skills[:8])
    missing = "".join(_chip(skill, muted=True) for skill in job.missing_skills[:5])

    skills_block = ""
    if matched:
        skills_block += f'<div style="margin-top:10px;">{matched}</div>'
    if missing:
        skills_block += (
            f'<div style="margin-top:2px;"><span style="font-size:11px;color:{_FAINT};'
            f'text-transform:uppercase;letter-spacing:.04em;">Gaps</span><br>{missing}</div>'
        )

    reasoning = (
        f'<div style="margin-top:10px;font-size:13px;line-height:20px;color:{_MUTED};'
        f'word-break:break-word;">'
        f"{html.escape(job.reasoning)}</div>"
        if job.reasoning
        else ""
    )

    # A bulletproof-ish button: a padded anchor, which degrades to a plain
    # link anywhere the styling is stripped.
    apply_block = (
        f'<div style="margin-top:14px;">'
        f'<a href="{html.escape(job.apply_url, quote=True)}" '
        f'style="display:inline-block;background:{_ACCENT};color:#ffffff;'
        f'text-decoration:none;font-size:13px;font-weight:600;'
        f'padding:9px 18px;border-radius:6px;">Apply &rarr;</a></div>'
        if job.apply_url
        else f'<div style="margin-top:14px;font-size:12px;color:{_FAINT};">'
        f"No application link published for this posting</div>"
    )

    return (
        f'<table role="presentation" width="100%" cellpadding="0" cellspacing="0" '
        f'style="border-collapse:separate;margin:0 0 12px 0;">'
        f'<tr><td style="background:{_CARD_BG};border:1px solid {_BORDER};'
        f'border-radius:10px;padding:18px 20px;">'
        f'<table role="presentation" width="100%" cellpadding="0" cellspacing="0">'
        f'<tr>'
        f'<td width="46" valign="top" style="padding-right:14px;">'
        f'<div style="background:{badge_bg};color:{badge_fg};border-radius:8px;'
        f'width:46px;text-align:center;padding:8px 0;font-size:18px;'
        f'font-weight:700;line-height:22px;">{job.score}</div>'
        f"</td>"
        f'<td valign="top">'
        f'<div style="font-size:16px;font-weight:600;color:{_INK};line-height:22px;'
        f'word-break:break-word;">'
        f"{html.escape(job.title)}</div>"
        f'<div style="margin-top:3px;font-size:13px;color:{_MUTED};">{meta}</div>'
        f'<div style="margin-top:2px;font-size:12px;color:{_FAINT};">{sub}</div>'
        f"{reasoning}{skills_block}{apply_block}"
        f"</td></tr></table>"
        f"</td></tr></table>"
    )


def _render_job_section(
    title: str, jobs: list[JobRecord], *, deemphasize: bool, tone: str = "strong"
) -> str:
    ordered = sorted(jobs, key=lambda job: job.score, reverse=True)
    cards = "".join(_render_job_row(job, tone=tone) for job in ordered)
    count = f'<span style="color:{_FAINT};font-weight:400;"> ({len(ordered)})</span>'
    return (
        f'<div style="margin:26px 0 12px 0;font-size:12px;font-weight:700;'
        f"letter-spacing:.09em;text-transform:uppercase;"
        f'color:{_MUTED if deemphasize else _INK};">{html.escape(title)}{count}</div>'
        f"{cards}"
    )


def _stat(label: str, value: str) -> str:
    return (
        f'<td align="center" style="padding:0 6px;">'
        f'<div style="font-size:22px;font-weight:700;color:{_INK};line-height:26px;">{value}</div>'
        f'<div style="font-size:11px;color:{_FAINT};text-transform:uppercase;'
        f'letter-spacing:.06em;margin-top:2px;">{html.escape(label)}</div></td>'
    )


def _render_summary(sections: DigestSections) -> str:
    scored = sections.strong + sections.consider
    top = max((job.score for job in scored if job.score is not None), default=None)
    return (
        f'<table role="presentation" width="100%" cellpadding="0" cellspacing="0" '
        f'style="background:{_CARD_BG};border:1px solid {_BORDER};border-radius:10px;'
        f'padding:18px 12px;margin-bottom:4px;"><tr>'
        f"{_stat('Strong', str(len(sections.strong)))}"
        f"{_stat('Considering', str(len(sections.consider)))}"
        f"{_stat('Top score', str(top) if top is not None else '—')}"
        f"</tr></table>"
    )


def _render_footer(sections: DigestSections) -> str:
    providers = ", ".join(sections.providers_used) if sections.providers_used else "none"
    covered = (
        f"Covering everything since the last digest "
        f"({sections.window_start:%Y-%m-%d %H:%M} UTC)<br>"
        if sections.window_start is not None
        else ""
    )
    errors_line = (
        f'<span style="color:#b45309;">Unresolved errors: '
        f"{sections.unresolved_errors_count}</span>"
        if sections.unresolved_errors_count
        else f"Unresolved errors: {sections.unresolved_errors_count}"
    )
    return (
        f'<div style="margin-top:26px;padding-top:16px;border-top:1px solid {_BORDER};'
        f'font-size:12px;line-height:19px;color:{_FAINT};">'
        f"{covered}"
        f"Fetched: {sections.fetched_count} &middot; "
        f"Filtered: {sections.filtered_count} &middot; "
        f"Scored: {sections.scored_count} &middot; "
        f"Failed: {sections.failed_count}<br>"
        f"Spend today: ${sections.spend_today_usd:.2f}<br>"
        f"Provider(s) used: {html.escape(providers)}<br>"
        f"CSV: {html.escape(sections.csv_path)}<br>"
        f"{errors_line}"
        f"</div>"
        f'<div style="margin-top:18px;font-size:11px;color:{_FAINT};">'
        f"{_BRAND} &middot; automated job sourcing, running on your machine."
        f"</div>"
    )


def _preheader(sections: DigestSections) -> str:
    """The grey preview line next to the subject in an inbox list. Hidden in
    the body itself — if it isn't set, clients scrape whatever text comes
    first, which would be the word 'Strong'."""
    if not (sections.strong or sections.consider):
        text = "No new matches in this period."
    else:
        text = (
            f"{len(sections.strong)} strong match(es), "
            f"{len(sections.consider)} worth considering."
        )
    return (
        f'<div style="display:none;max-height:0;overflow:hidden;opacity:0;'
        f'mso-hide:all;">{html.escape(text)}</div>'
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
        body = (
            f'<table role="presentation" width="100%" cellpadding="0" cellspacing="0" '
            f'style="background:{_CARD_BG};border:1px solid {_BORDER};border-radius:10px;">'
            f'<tr><td style="padding:34px 24px;text-align:center;">'
            f'<div style="font-size:15px;font-weight:600;color:{_INK};">'
            f"No new matches today.</div>"
            f'<div style="margin-top:6px;font-size:13px;color:{_MUTED};">'
            f"Everything upstream was already seen, or nothing cleared the bar."
            f"</div></td></tr></table>"
        )
    else:
        parts = [_render_summary(sections)]
        if sections.strong:
            parts.append(
                _render_job_section("Strong matches", sections.strong, deemphasize=False)
            )
        if sections.consider:
            parts.append(
                _render_job_section(
                    "Worth considering", sections.consider, deemphasize=True, tone="consider"
                )
            )
        if sections.location_uncertain:
            parts.append(
                _render_job_section(
                    "Location uncertain — needs a manual eyeball",
                    sections.location_uncertain,
                    deemphasize=False,
                    tone="review",
                )
            )
        if sections.eligibility_uncertain:
            parts.append(
                _render_job_section(
                    "Eligibility uncertain — needs a manual eyeball",
                    sections.eligibility_uncertain,
                    deemphasize=False,
                    tone="review",
                )
            )
        body = "".join(parts)

    return (
        "<!DOCTYPE html><html><head>"
        '<meta charset="utf-8">'
        '<meta name="viewport" content="width=device-width,initial-scale=1">'
        '<meta name="color-scheme" content="light">'
        f"<title>{_BRAND} Digest</title>"
        "</head>"
        f'<body style="margin:0;padding:0;background:{_PAGE_BG};'
        f'font-family:{_FONT};-webkit-font-smoothing:antialiased;">'
        f"{_preheader(sections)}"
        f'<table role="presentation" width="100%" cellpadding="0" cellspacing="0" '
        f'style="background:{_PAGE_BG};padding:24px 12px;"><tr><td align="center">'
        f'<table role="presentation" width="100%" cellpadding="0" cellspacing="0" '
        f'style="width:100%;max-width:{_WIDTH}px;text-align:left;">'
        f'<tr><td style="padding-bottom:18px;">'
        f'<div style="font-size:19px;font-weight:700;letter-spacing:-.01em;color:{_INK};">'
        f"{_BRAND}</div>"
        f'<div style="width:34px;height:3px;background:{_ACCENT};'
        f'border-radius:2px;margin-top:7px;font-size:0;line-height:0;">&nbsp;</div>'
        f'<div style="margin-top:9px;font-size:13px;color:{_MUTED};">'
        f"Daily match digest &middot; {run_date:%A, %B %-d, %Y}</div>"
        f"</td></tr>"
        f"<tr><td>{body}{_render_footer(sections)}</td></tr>"
        f"</table></td></tr></table></body></html>"
    )


def render_digest_text(sections: DigestSections, run_date: date) -> str:
    """The text/plain alternative, written rather than scraped.

    Regex-stripping the HTML used to be fine when the HTML was a plain table;
    against a card layout it produces soup. Some clients (and every screen
    reader on a text-only setting) show this part, so it deserves to be
    readable on its own."""
    lines = [f"{_BRAND} — daily match digest", f"{run_date:%A, %B %-d, %Y}", ""]

    def section(title: str, jobs: list[JobRecord]) -> None:
        if not jobs:
            return
        lines.append(f"{title.upper()} ({len(jobs)})")
        lines.append("-" * 60)
        for job in sorted(jobs, key=lambda j: j.score, reverse=True):
            where = f" — {job.location}" if job.location else ""
            lines.append(f"[{job.score}] {job.title}")
            lines.append(f"       {job.company}{where}")
            if job.resume_used:
                lines.append(f"       Resume {job.resume_used.value}")
            if job.reasoning:
                lines.append(f"       {job.reasoning}")
            if job.matched_skills:
                lines.append(f"       Matched: {'; '.join(job.matched_skills)}")
            if job.missing_skills:
                lines.append(f"       Gaps: {'; '.join(job.missing_skills)}")
            lines.append(f"       Apply: {job.apply_url or '(no link published)'}")
            lines.append("")
        lines.append("")

    if not any(
        (
            sections.strong,
            sections.consider,
            sections.location_uncertain,
            sections.eligibility_uncertain,
        )
    ):
        lines.append("No new matches today.")
        lines.append("")
    else:
        section("Strong matches", sections.strong)
        section("Worth considering", sections.consider)
        section("Location uncertain — needs a manual eyeball", sections.location_uncertain)
        section(
            "Eligibility uncertain — needs a manual eyeball", sections.eligibility_uncertain
        )

    if sections.window_start is not None:
        lines.append(
            f"Covering everything since the last digest "
            f"({sections.window_start:%Y-%m-%d %H:%M} UTC)"
        )
    lines.append(
        f"Fetched: {sections.fetched_count} · Filtered: {sections.filtered_count} · "
        f"Scored: {sections.scored_count} · Failed: {sections.failed_count}"
    )
    lines.append(f"Spend today: ${sections.spend_today_usd:.2f}")
    lines.append(
        f"Provider(s) used: {', '.join(sections.providers_used) or 'none'}"
    )
    lines.append(f"CSV: {sections.csv_path}")
    lines.append(f"Unresolved errors: {sections.unresolved_errors_count}")
    return "\n".join(lines)


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
