from __future__ import annotations

import logging
import logging.handlers
import uuid
from pathlib import Path

_LOG_FORMAT = (
    "%(asctime)s | %(levelname)-8s | %(run_id)s | "
    "%(name)s:%(funcName)s:%(lineno)d | %(message)s"
)
_MAX_BYTES = 10 * 1024 * 1024
_BACKUP_COUNT = 5

_current_run_id = "-"


class _RunIdFilter(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        record.run_id = _current_run_id
        return True


def new_run_id() -> str:
    return uuid.uuid4().hex[:8]


def setup_logging(
    log_dir: Path,
    level: str = "INFO",
    run_id: str | None = None,
    filename: str = "coldstart.log",
) -> None:
    """Configure the root logger with a rotating file handler and a console
    handler, both tagged with a run_id. Safe to call more than once (e.g.
    between test runs) — replaces any handlers from a previous call.

    `filename` exists so the daemon (Module 20) can log to its own file. Two
    processes sharing one RotatingFileHandler corrupt each other's rollover,
    and the daemon runs concurrently with the run_poll/run_digest children it
    spawns — so the daemon takes `daemon.log` and children keep the default."""
    global _current_run_id
    _current_run_id = run_id or new_run_id()

    log_dir = Path(log_dir)
    log_dir.mkdir(parents=True, exist_ok=True)

    formatter = logging.Formatter(_LOG_FORMAT)
    run_id_filter = _RunIdFilter()

    file_handler = logging.handlers.RotatingFileHandler(
        log_dir / filename,
        maxBytes=_MAX_BYTES,
        backupCount=_BACKUP_COUNT,
    )
    file_handler.setFormatter(formatter)
    file_handler.addFilter(run_id_filter)

    console_handler = logging.StreamHandler()
    console_handler.setFormatter(formatter)
    console_handler.addFilter(run_id_filter)

    root = logging.getLogger()
    for handler in root.handlers[:]:
        root.removeHandler(handler)
        handler.close()
    root.setLevel(level.upper())
    root.addHandler(file_handler)
    root.addHandler(console_handler)


def get_logger(name: str) -> logging.Logger:
    return logging.getLogger(name)
