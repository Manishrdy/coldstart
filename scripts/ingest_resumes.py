import sys

from coldstart.db import connection, init_schema
from coldstart.resume_ingest import ResumesNotReady, check_resumes_ready, ingest_resumes
from coldstart.settings import ConfigError, load_settings


def main() -> int:
    try:
        settings = load_settings()
    except ConfigError as exc:
        print(str(exc), file=sys.stderr)
        return 1

    resumes_dir = settings.resume_manifest.parent

    with connection(settings.db_path) as conn:
        init_schema(conn)

        if check_resumes_ready(resumes_dir, settings.resume_manifest):
            print("Resumes already ingested and unchanged — nothing to do.")
            return 0

        try:
            resolved = ingest_resumes(
                resumes_dir,
                settings.resume_manifest,
                chain=[],
                conn=conn,
                experience_start_date=settings.experience_start_date,
            )
        except ResumesNotReady as exc:
            print(str(exc), file=sys.stderr)
            return 1

    for slot in sorted(resolved):
        record = resolved[slot]
        print(f"{slot.value}: {record.source_filename} -> {record.description}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
