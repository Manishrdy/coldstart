import sys

from coldstart.db import connection, init_schema
from coldstart.settings import ConfigError, load_settings


def main() -> int:
    try:
        settings = load_settings()
    except ConfigError as exc:
        print(str(exc), file=sys.stderr)
        return 1

    with connection(settings.db_path) as conn:
        init_schema(conn)

    print(f"Database ready at {settings.db_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
