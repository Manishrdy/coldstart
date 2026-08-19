from __future__ import annotations

import sqlite3
from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo

from coldstart.logging_setup import get_logger
from coldstart.scoring.base import LLMUsage
from coldstart.settings import Settings

logger = get_logger(__name__)

_WARNING_THRESHOLD_FRACTION = 0.8

# Tracks the ISO date (in the configured timezone) we last warned about, so
# the 80% warning fires once per day even though check_budget runs before
# every single LLM call (potentially hundreds of times per run).
_last_warned_day: str | None = None


class BudgetExceeded(Exception):
    pass


def reset_budget_warning_state() -> None:
    global _last_warned_day
    _last_warned_day = None


def record_spend(
    conn: sqlite3.Connection,
    provider: str,
    model: str,
    usage: LLMUsage,
    cost: float,
    job_ref: str | None,
) -> None:
    conn.execute(
        """
        INSERT INTO spend_log (
            ts, provider, model, input_tokens, cached_tokens, output_tokens,
            est_cost_usd, job_ref
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            datetime.now(UTC).isoformat(),
            provider,
            model,
            usage.input_tokens,
            usage.cached_tokens,
            usage.output_tokens,
            cost,
            job_ref,
        ),
    )
    conn.commit()


def local_day_bounds_utc(tz: str) -> tuple[str, str]:
    zone = ZoneInfo(tz)
    local_now = datetime.now(UTC).astimezone(zone)
    start_of_day = local_now.replace(hour=0, minute=0, second=0, microsecond=0)
    end_of_day = start_of_day + timedelta(days=1)
    return start_of_day.astimezone(UTC).isoformat(), end_of_day.astimezone(UTC).isoformat()


def today_spend(conn: sqlite3.Connection, tz: str) -> float:
    start, end = local_day_bounds_utc(tz)
    row = conn.execute(
        "SELECT COALESCE(SUM(est_cost_usd), 0) FROM spend_log WHERE ts >= ? AND ts < ?",
        (start, end),
    ).fetchone()
    return row[0]


def check_budget(conn: sqlite3.Connection, settings: Settings) -> None:
    global _last_warned_day

    zone = ZoneInfo(settings.timezone)
    today_key = datetime.now(UTC).astimezone(zone).date().isoformat()

    spent = today_spend(conn, settings.timezone)
    ceiling = settings.daily_token_spend_ceiling_usd

    if spent >= ceiling:
        logger.critical(
            "daily spend ceiling breached: $%.4f >= $%.4f — halting run", spent, ceiling
        )
        raise BudgetExceeded(
            f"daily spend ${spent:.4f} has reached the ${ceiling:.4f} ceiling"
        )

    if ceiling > 0 and spent / ceiling >= _WARNING_THRESHOLD_FRACTION:
        if _last_warned_day != today_key:
            logger.warning(
                "daily spend at $%.4f, %.0f%% of the $%.4f ceiling",
                spent,
                spent / ceiling * 100,
                ceiling,
            )
            _last_warned_day = today_key
