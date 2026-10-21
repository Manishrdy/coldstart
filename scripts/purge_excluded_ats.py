"""Delete stored jobs from a source that is now in config/excluded_ats.json.

Excluding a source stops it being downloaded from the next poll on, but the
rows already stored stay — so after `workday` was excluded on 2026-10-20 its
scored jobs were still on the dashboard. This is the retroactive half, the
same way `purge_excluded_companies.py` is for config/excluded_companies.json.
Run it after every edit to excluded_ats.json.

**Rows carrying a `job_state` are kept whatever their source.** A mark is a
decision you made, and the posting record is the only context that decision
has. `job_state` has no foreign key, so a plain delete would leave the mark
pointing at nothing. `--include-marked` overrides it and clears both tables
together.

A not-yet-sent hemline push for a deleted job is deleted with it, so it is
never sent later; an already-sent one is kept as the record of the send.

The exclusion is what keeps these out, not the delete: an excluded source is
never downloaded, so a deleted row cannot come back.

    python scripts/purge_excluded_ats.py            # report only
    python scripts/purge_excluded_ats.py --apply    # delete

Take a backup first.
"""

from __future__ import annotations

import argparse
import collections
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from coldstart.db import connection, init_schema, readonly_connection  # noqa: E402
from coldstart.manifest_watch import load_excluded_ats  # noqa: E402
from coldstart.settings import load_settings  # noqa: E402

_EXCLUDED_ATS_PATH = Path(__file__).resolve().parent.parent / "config" / "excluded_ats.json"


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Delete stored jobs from sources on the ATS exclusion list."
    )
    parser.add_argument("--apply", action="store_true", help="actually delete")
    parser.add_argument(
        "--include-marked",
        action="store_true",
        help="also delete jobs you have applied to or declined, and their marks",
    )
    args = parser.parse_args()

    settings = load_settings()
    excluded = load_excluded_ats(_EXCLUDED_ATS_PATH)
    # Report mode never writes, not even the schema migration.
    opened = connection(settings.db_path) if args.apply else readonly_connection(settings.db_path)
    with opened as conn:
        if args.apply:
            init_schema(conn)

        marked = {
            row["global_id"]: row["state"]
            for row in conn.execute("SELECT global_id, state FROM job_state")
        }
        rows = conn.execute("SELECT global_id, ats_type, status, score FROM jobs").fetchall()

        doomed: list = []
        kept: collections.Counter = collections.Counter()
        by_source: collections.Counter = collections.Counter()
        for row in rows:
            if row["ats_type"] not in excluded:
                continue
            state = marked.get(row["global_id"])
            if state and not args.include_marked:
                kept[(row["ats_type"], state)] += 1
                continue
            doomed.append(row)
            by_source[(row["ats_type"], row["status"])] += 1

        threshold = settings.score_threshold_strong
        shortlisted = sum(
            1 for row in doomed if row["status"] == "scored" and (row["score"] or 0) >= threshold
        )
        print(f"database: {len(rows)} job(s)   exclusion list: config/excluded_ats.json")
        print(f"from excluded sources, deletable: {len(doomed)} ({shortlisted} shortlisted)")
        for (ats_type, status), count in sorted(by_source.items()):
            print(f"  {count:5d}  {ats_type}  {status}")

        if kept:
            print("\nkept because you have actioned them:")
            for (ats_type, state), count in kept.most_common():
                print(f"  {count:5d}  {ats_type} — {state}")
            print("  (pass --include-marked to delete these too)")

        if not doomed:
            print("\nnothing to delete")
            return

        if not args.apply:
            print(f"\ndry run — pass --apply to delete these {len(doomed)} row(s)")
            return

        ids = [(row["global_id"],) for row in doomed]
        with conn:
            conn.executemany("DELETE FROM jobs WHERE global_id = ?", ids)
            conn.executemany(
                "DELETE FROM hemline_events WHERE global_id = ? AND status = 'pending'", ids
            )
            # Only reachable with --include-marked; without it nothing here
            # is marked.
            if args.include_marked:
                conn.executemany("DELETE FROM job_state WHERE global_id = ?", ids)
        remaining = conn.execute("SELECT COUNT(*) AS n FROM jobs").fetchone()["n"]
        print(f"\ndeleted {len(doomed)} job(s) — {remaining} remain")


if __name__ == "__main__":
    main()
