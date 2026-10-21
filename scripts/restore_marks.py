"""Bring applied/declined marks back from the database Module 37 set aside.

Module 37 changed the key of every per-employer source (workday, icims,
successfactors, ...) from `ats_type:ats_id` to `ats_type:<host>:ats_id`, and
the operator chose to start from an empty database rather than migrate. The
old one is `data/coldstart.sqlite3.bak-20261020-pre-global-id`: 144 applied
and 1,046 declined marks, none of them in the live database. This restores
them under the new keys.

For each mark in the backup:

- the new key is rebuilt the way `global_ids.make_global_id` builds it, from
  the row's `apply_url` host (which matched the posting page's host for all
  6,650 rows checkable — DEVELOPMENT_PLAN Module 37);
- if the live database already has that job (it was re-fetched since), only
  the mark is added;
- otherwise the backup's job row is inserted under the new key, score and
  reasoning included, and the mark with it. A mark without its job row has
  no context and the dashboard can't show it.

Marks on sources now in `config/excluded_ats.json` (workday, and the sources
cut when scope was narrowed) are not restored: those sources are ignored
entirely. A live mark is never overwritten. Inserted rows never reach hemline,
which only sends a job at the moment it is scored.

    python scripts/restore_marks.py            # report only
    python scripts/restore_marks.py --apply    # write

Take a backup of the live database first.
"""

from __future__ import annotations

import argparse
import collections
import re
import sqlite3
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from coldstart.db import (  # noqa: E402
    connection,
    count_legacy_global_ids,
    init_schema,
    readonly_connection,
)
from coldstart.global_ids import ats_id_text, make_global_id  # noqa: E402
from coldstart.manifest_watch import load_excluded_ats  # noqa: E402
from coldstart.settings import load_settings  # noqa: E402

_REPO = Path(__file__).resolve().parent.parent
_DEFAULT_BACKUP = _REPO / "data" / "coldstart.sqlite3.bak-20261020-pre-global-id"
_EXCLUDED_ATS_PATH = _REPO / "config" / "excluded_ats.json"
# successfactors' old keys carry a float id ("1288603201.0"); the new ones don't.
_FLOAT_ID_RE = re.compile(r"^\d+\.0$")


def new_key(ats_type: str, old_global_id: str, apply_url: str | None) -> str:
    old_id: object = old_global_id.split(":", 1)[1]
    if old_id == "nan":
        old_id = None
    elif _FLOAT_ID_RE.match(old_id):
        old_id = ats_id_text(float(old_id))
    return make_global_id(ats_type, old_id, apply_url)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--apply", action="store_true", help="write the changes")
    parser.add_argument("--backup", type=Path, default=_DEFAULT_BACKUP)
    args = parser.parse_args()

    settings = load_settings()
    excluded = load_excluded_ats(_EXCLUDED_ATS_PATH)
    backup = sqlite3.connect(f"file:{args.backup}?mode=ro", uri=True)
    backup.row_factory = sqlite3.Row
    marked = backup.execute(
        "SELECT s.global_id AS old_id, s.state, s.updated_at, s.note, j.* "
        "FROM job_state s JOIN jobs j USING (global_id)"
    ).fetchall()
    orphans = backup.execute(
        "SELECT COUNT(*) FROM job_state WHERE global_id NOT IN (SELECT global_id FROM jobs)"
    ).fetchone()[0]

    opened = connection(settings.db_path) if args.apply else readonly_connection(settings.db_path)
    with opened as conn:
        if args.apply:
            init_schema(conn)
        live_columns = [r["name"] for r in conn.execute("PRAGMA table_info(jobs)")]
        live_ids = {r["global_id"] for r in conn.execute("SELECT global_id FROM jobs")}
        live_marks = {r["global_id"] for r in conn.execute("SELECT global_id FROM job_state")}

        rows_to_insert: list[dict] = []
        marks_to_insert: list[tuple] = []
        outcome: collections.Counter = collections.Counter()
        seen_new_ids: set[str] = set()
        for mark in marked:
            if mark["ats_type"] in excluded:
                outcome[("skipped: source excluded", mark["state"])] += 1
                continue
            key = new_key(mark["ats_type"], mark["old_id"], mark["apply_url"])
            if key in seen_new_ids:
                outcome[("skipped: two old marks map to one key", mark["state"])] += 1
                continue
            seen_new_ids.add(key)
            if key in live_marks:
                outcome[("skipped: already marked live", mark["state"])] += 1
                continue
            if key in live_ids:
                outcome[("mark added to a live job", mark["state"])] += 1
            else:
                row = {c: mark[c] for c in live_columns if c in mark.keys()}
                row["global_id"] = key
                rows_to_insert.append(row)
                outcome[("job row restored with its mark", mark["state"])] += 1
            marks_to_insert.append((key, mark["state"], mark["updated_at"], mark["note"]))

        print(
            f"backup: {args.backup.name}  "
            f"({len(marked)} marks with a job row, {orphans} without)"
        )
        for (what, state), count in sorted(outcome.items()):
            print(f"  {count:5d}  {state:8}  {what}")
        print(f"would write: {len(rows_to_insert)} job rows, {len(marks_to_insert)} marks")

        if not args.apply:
            print("\nreport only — run with --apply to write")
            return

        with conn:
            for row in rows_to_insert:
                columns = list(row)
                conn.execute(
                    f"INSERT INTO jobs ({', '.join(columns)}) "
                    f"VALUES ({', '.join('?' for _ in columns)})",
                    [row[c] for c in columns],
                )
            conn.executemany(
                "INSERT OR IGNORE INTO job_state (global_id, state, updated_at, note) "
                "VALUES (?, ?, ?, ?)",
                marks_to_insert,
            )
        # The poll refuses to start on old-shape keys; prove none came in.
        legacy = count_legacy_global_ids(conn)
        print(f"\nwrote {len(rows_to_insert)} job rows and {len(marks_to_insert)} marks")
        print(f"old-shape keys in the live database: {legacy}")


if __name__ == "__main__":
    main()
