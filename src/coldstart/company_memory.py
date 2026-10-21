"""Module 34 — remember decisions about an employer, not just a posting.

Measured on the operator's own 1,190 decisions (2026-10-20): above score 80
the score barely predicted what got applied to, while the company did — 63
companies declined 3+ times and never applied to (a third of all declines),
151 declines came after already applying at that company, and companies with
10+ shortlisted roles were applied to 8% of the time against 44% for
single-role ones. So the shortlist forgets three things it now remembers:

- **"Not this company"** (`company_state`, state `blocked`): hidden until
  undone.
- **"Rejected me"** (a `rejected` mark on an applied job): that company is
  hidden for COMPANY_COOLDOWN_DAYS (90 — the operator's choice) from the day
  it was marked. Many employers let you reapply after a waiting period, so
  this is a cooldown, not a block.
- **Applications per company.** An `applied` mark counts for
  APPLICATION_WINDOW_DAYS, or until it is marked rejected. At one, the
  company's other roles are still scored but collapse under "Applied here"
  on the dashboard; at APPLICATIONS_PER_COMPANY (2) they are hidden — the
  operator can't send more than two tailored applications to one employer.

Hidden companies' new postings never reach the LLM: `filter_company_memory`
drops them right before scoring. They are dropped, not persisted — like the
company block list — so the day a hide lifts, the next poll scores them with
no requeue step. Everything here is derived on read from `job_state` and
`company_state`; there is no third copy of a decision to fall out of sync.
"""

from __future__ import annotations

import sqlite3
from collections import Counter
from dataclasses import asdict, dataclass
from datetime import UTC, datetime, timedelta

import pandas as pd

from coldstart.db import company_states
from coldstart.filters.company import company_key
from coldstart.logging_setup import get_logger
from coldstart.settings import Settings

logger = get_logger(__name__)

BLOCKED = "blocked"      # "not this company"
COOLDOWN = "cooldown"    # rejected you recently
CAPPED = "capped"        # you've used your applications there
APPLIED = "applied"      # applied, below the cap — roles still scored, collapsed

HIDING_KINDS = frozenset({BLOCKED, COOLDOWN, CAPPED})


@dataclass(frozen=True)
class CompanyStatus:
    kind: str
    company_key: str
    company: str
    # When the deciding mark was made (date), and when a cooldown ends.
    since: str | None = None
    until: str | None = None
    # Applications still counting toward the cap.
    applications: int = 0
    cap: int = 0

    @property
    def hides(self) -> bool:
        return self.kind in HIDING_KINDS

    def as_dict(self) -> dict:
        return asdict(self)


def _parse(stamp: str) -> datetime:
    when = datetime.fromisoformat(stamp)
    return when if when.tzinfo else when.replace(tzinfo=UTC)


class CompanyMemory:
    def __init__(self, statuses: dict[str, CompanyStatus]):
        self._statuses = statuses

    def status(self, company: object) -> CompanyStatus | None:
        return self._statuses.get(company_key(company))

    def hidden(self, company: object) -> CompanyStatus | None:
        status = self.status(company)
        return status if status is not None and status.hides else None

    def all(self) -> list[CompanyStatus]:
        return list(self._statuses.values())


def load(
    conn: sqlite3.Connection, settings: Settings, now: datetime | None = None
) -> CompanyMemory:
    now = now or datetime.now(UTC)
    cooldown = timedelta(days=settings.company_cooldown_days)
    window_start = now - timedelta(days=settings.application_window_days)
    cap = settings.applications_per_company

    rejected_at: dict[str, tuple[datetime, str]] = {}
    applications: dict[str, list[datetime]] = {}
    names: dict[str, str] = {}
    for row in conn.execute(
        "SELECT j.company, s.state, s.updated_at FROM job_state s "
        "JOIN jobs j USING (global_id) WHERE s.state IN ('applied', 'rejected')"
    ):
        key = company_key(row["company"])
        if not key:
            continue
        names.setdefault(key, row["company"])
        when = _parse(row["updated_at"])
        if row["state"] == "rejected":
            if key not in rejected_at or when > rejected_at[key][0]:
                rejected_at[key] = (when, row["company"])
        elif when >= window_start:
            applications.setdefault(key, []).append(when)

    statuses: dict[str, CompanyStatus] = {}
    # Lowest precedence first, so a stronger reason overwrites a weaker one.
    for key, dates in applications.items():
        statuses[key] = CompanyStatus(
            kind=CAPPED if len(dates) >= cap else APPLIED,
            company_key=key,
            company=names[key],
            since=max(dates).date().isoformat(),
            applications=len(dates),
            cap=cap,
        )
    for key, (when, company) in rejected_at.items():
        if now < when + cooldown:
            statuses[key] = CompanyStatus(
                kind=COOLDOWN,
                company_key=key,
                company=company,
                since=when.date().isoformat(),
                until=(when + cooldown).date().isoformat(),
                applications=len(applications.get(key, [])),
                cap=cap,
            )
    for row in company_states(conn):
        if row["state"] == BLOCKED:
            statuses[row["company_key"]] = CompanyStatus(
                kind=BLOCKED,
                company_key=row["company_key"],
                company=row["company"],
                since=_parse(row["updated_at"]).date().isoformat(),
                applications=len(applications.get(row["company_key"], [])),
                cap=cap,
            )
    return CompanyMemory(statuses)


def filter_company_memory(df: pd.DataFrame, memory: CompanyMemory) -> pd.DataFrame:
    """Drop postings from companies the operator has hidden, before any LLM
    call. Logged per company, so the drop is never silent."""
    if df.empty or "company" not in df.columns:
        return df
    verdicts = [memory.hidden(company) for company in df["company"]]
    keep = pd.Series([v is None for v in verdicts], index=df.index, dtype=bool)
    dropped = Counter(
        (v.company, v.kind) for v in verdicts if v is not None
    )
    for (company, kind), count in dropped.most_common():
        logger.info("company memory: %d posting(s) from %r not scored (%s)", count, company, kind)
    if dropped:
        logger.info(
            "company memory: %d of %d posting(s) skipped the LLM", sum(dropped.values()), len(df)
        )
    return df[keep].reset_index(drop=True)
