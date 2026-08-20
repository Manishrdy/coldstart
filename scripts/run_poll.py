import sys
from pathlib import Path

from coldstart import exit_codes
from coldstart.budget import BudgetExceeded
from coldstart.logging_setup import new_run_id, setup_logging
from coldstart.pipeline import run_poll
from coldstart.resume_ingest import ResumesNotReady
from coldstart.settings import ConfigError, load_settings

_DEFAULT_LOG_DIR = Path("logs")  # matches Settings.log_dir's own default


def main() -> int:
    run_id = new_run_id()
    setup_logging(_DEFAULT_LOG_DIR, run_id=run_id)  # so a config failure itself gets logged

    try:
        settings = load_settings()
    except ConfigError as exc:
        print(str(exc), file=sys.stderr)
        return exit_codes.CONFIG_ERROR

    if settings.log_dir != _DEFAULT_LOG_DIR or settings.log_level != "INFO":
        setup_logging(settings.log_dir, level=settings.log_level, run_id=run_id)

    try:
        result = run_poll(settings)
    except ResumesNotReady as exc:
        print(str(exc), file=sys.stderr)
        return exit_codes.RESUMES_NOT_READY
    except BudgetExceeded as exc:
        print(f"Daily budget exceeded: {exc}", file=sys.stderr)
        return exit_codes.BUDGET_EXCEEDED

    print(
        f"run {result.run_id}: {result.slices_processed} slice(s) — "
        f"fetched={result.fetched_count} filtered={result.filtered_count} "
        f"scored={result.scored_count} failed={result.failed_count} "
        f"excluded={result.excluded_count} csv={result.csv_path or '(none)'}"
    )
    return exit_codes.OK


if __name__ == "__main__":
    raise SystemExit(main())
