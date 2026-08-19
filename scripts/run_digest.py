import sys
from pathlib import Path

from coldstart.logging_setup import new_run_id, setup_logging
from coldstart.pipeline import run_digest
from coldstart.settings import ConfigError, load_settings

_DEFAULT_LOG_DIR = Path("logs")  # matches Settings.log_dir's own default


def main() -> int:
    run_id = new_run_id()
    setup_logging(_DEFAULT_LOG_DIR, run_id=run_id)  # so a config failure itself gets logged

    try:
        settings = load_settings()
    except ConfigError as exc:
        print(str(exc), file=sys.stderr)
        return 1

    if settings.log_dir != _DEFAULT_LOG_DIR:
        setup_logging(settings.log_dir, run_id=run_id)

    sent = run_digest(settings)
    if sent:
        print("Digest sent.")
        return 0

    print("Digest send FAILED — see the errors table / logs.", file=sys.stderr)
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
