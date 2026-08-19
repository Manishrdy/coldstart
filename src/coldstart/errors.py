from __future__ import annotations

import functools
import sqlite3
from collections.abc import Callable
from datetime import UTC, datetime
from typing import ParamSpec, TypeVar

from coldstart.logging_setup import get_logger

logger = get_logger(__name__)

P = ParamSpec("P")
T = TypeVar("T")


def log_error(
    conn: sqlite3.Connection,
    *,
    stage: str,
    exc: BaseException | None = None,
    message: str | None = None,
    source_file: str,
    function_name: str,
    provider: str | None = None,
    job_ref: str | None = None,
    retry_count: int = 0,
) -> None:
    error_type = type(exc).__name__ if exc is not None else None
    error_message = message or (str(exc) if exc is not None else None)

    logger.error(
        "stage=%s source_file=%s function_name=%s provider=%s job_ref=%s: %s",
        stage,
        source_file,
        function_name,
        provider,
        job_ref,
        error_message,
        exc_info=exc is not None,
    )

    try:
        conn.execute(
            """
            INSERT INTO errors (
                ts, stage, source_file, function_name, provider, job_ref,
                error_type, error_message, retry_count, resolved
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 0)
            """,
            (
                datetime.now(UTC).isoformat(),
                stage,
                source_file,
                function_name,
                provider,
                job_ref,
                error_type,
                error_message,
                retry_count,
            ),
        )
        conn.commit()
    except Exception:
        logger.error("failed to write error row to the errors table", exc_info=True)


def capture_errors(stage: str) -> Callable[[Callable[P, T]], Callable[P, T | None]]:
    """Wraps a function taking `conn` as its first argument: on any exception,
    logs it via log_error(stage=...) and returns None instead of propagating —
    so one bad slice/job can't abort the whole pipeline run."""

    def decorator(func: Callable[P, T]) -> Callable[P, T | None]:
        @functools.wraps(func)
        def wrapper(conn: sqlite3.Connection, *args: P.args, **kwargs: P.kwargs) -> T | None:
            try:
                return func(conn, *args, **kwargs)
            except Exception as exc:
                log_error(
                    conn,
                    stage=stage,
                    exc=exc,
                    source_file=func.__module__,
                    function_name=func.__qualname__,
                )
                return None

        return wrapper

    return decorator
