"""The one entrypoint: run Coldstart round the clock until stopped.

Polls upstream every POLL_INTERVAL_MINUTES, ingests when it actually changed,
emails the digest once a day at DIGEST_TIME_PDT, and serves the dashboard on
DASHBOARD_PORT. Ctrl-C stops it cleanly.

Equivalent to running `scripts/run_poll.py` on a 30-minute cron and
`scripts/run_digest.py` on a daily one — both of those still work standalone.
"""

import sys
from pathlib import Path

from coldstart import exit_codes
from coldstart.daemon import AlreadyRunning, run_daemon
from coldstart.logging_setup import new_run_id, setup_logging
from coldstart.settings import ConfigError, load_settings

_DEFAULT_LOG_DIR = Path("logs")  # matches Settings.log_dir's own default

# The daemon gets its own log file. Its run_poll/run_digest children write to
# coldstart.log, and two processes sharing one RotatingFileHandler corrupt
# each other's rollover.
_LOG_FILENAME = "daemon.log"


def main() -> int:
    run_id = new_run_id()
    setup_logging(  # so a config failure itself gets logged
        _DEFAULT_LOG_DIR, run_id=run_id, filename=_LOG_FILENAME
    )

    try:
        settings = load_settings()
    except ConfigError as exc:
        print(str(exc), file=sys.stderr)
        return exit_codes.CONFIG_ERROR

    if settings.log_dir != _DEFAULT_LOG_DIR or settings.log_level != "INFO":
        setup_logging(
            settings.log_dir, level=settings.log_level, run_id=run_id, filename=_LOG_FILENAME
        )

    try:
        return run_daemon(settings)
    except AlreadyRunning as exc:
        print(str(exc), file=sys.stderr)
        return exit_codes.FAILURE


if __name__ == "__main__":
    raise SystemExit(main())
