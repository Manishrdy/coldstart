"""Delete stored jobs whose title names a machine-learning role.

Machine-learning titles were hard-denied on 2026-10-21 ("Machine Learning",
"ML"; see filters/title.py), but the title filter only ever sees *incoming*
rows, so the ML jobs already stored stay on the dashboard. This is the
retroactive half, the same way `purge_excluded_companies.py` is for the
company block list.

**The rule is not re-implemented here.** It calls `filters.title.is_ml_title`,
the same check the title filter runs, so this deletes exactly the ML titles
the pipeline would now refuse — whole words only, so "HTML" and "MLOps"
survive. It deliberately does not re-run the whole title filter: older rows
that some other, later rule would now drop are not this script's business.

**Rows carrying a `job_state` are kept whatever their title.** A mark is a
decision you made, and the posting record is the only context that decision
has. `job_state` has no foreign key, so a plain delete would leave the mark
pointing at nothing. `--include-marked` overrides it and clears both tables
together.

A not-yet-sent hemline push for a deleted job is deleted with it, so it is
never sent later; an already-sent one is kept as the record of the send.

The deny is what keeps these out, not the delete: `load_seen_keys` reads the
jobs table, so a deleted global_id stops being "seen" and the next poll
reconsiders it — the title filter then drops it again, before any LLM call,
at no cost.

    python scripts/purge_ml_titles.py            # report only
    python scripts/purge_ml_titles.py --apply    # delete

Take a backup first.
"""

from __future__ import annotations

import argparse
import collections
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from coldstart.db import connection, init_schema, readonly_connection  # noqa: E402
from coldstart.filters.title import is_ml_title  # noqa: E402
from coldstart.settings import load_settings  # noqa: E402

_SAMPLE_TITLES = 15


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Delete stored jobs whose title names a machine-learning role."
    )
    parser.add_argument("--apply", action="store_true", help="actually delete")
    parser.add_argument(
        "--include-marked",
        action="store_true",
        help="also delete jobs you have applied to or declined, and their marks",
    )
    args = parser.parse_args()

    settings = load_settings()
    # Report mode never writes, not even the schema migration.
    opened = connection(settings.db_path) if args.apply else readonly_connection(settings.db_path)
    with opened as conn:
        if args.apply:
            init_schema(conn)

        marked = {
            row["global_id"]: row["state"]
            for row in conn.execute("SELECT global_id, state FROM job_state")
        }
        rows = conn.execute("SELECT global_id, title, status, score FROM jobs").fetchall()

        doomed: list = []
        kept: collections.Counter = collections.Counter()
        by_status: collections.Counter = collections.Counter()
        titles: collections.Counter = collections.Counter()
        for row in rows:
            if not is_ml_title(row["title"]):
                continue
            state = marked.get(row["global_id"])
            if state and not args.include_marked:
                kept[state] += 1
                continue
            doomed.append(row)
            by_status[row["status"]] += 1
            titles[row["title"]] += 1

        threshold = settings.score_threshold_strong
        shortlisted = sum(
            1 for row in doomed if row["status"] == "scored" and (row["score"] or 0) >= threshold
        )
        print(f"database: {len(rows)} job(s)   rule: filters.title.is_ml_title")
        print(f"ML titles, deletable: {len(doomed)} ({shortlisted} shortlisted)")
        for status, count in sorted(by_status.items()):
            print(f"  {count:5d}  {status}")
        if titles:
            print("\nmost common titles:")
            for title, count in titles.most_common(_SAMPLE_TITLES):
                print(f"  {count:5d}  {title}")

        if kept:
            print("\nkept because you have actioned them:")
            for state, count in kept.most_common():
                print(f"  {count:5d}  {state}")
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
