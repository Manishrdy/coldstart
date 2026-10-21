"""Delete stored jobs whose posting date is older than MAX_POSTING_AGE_DAYS.

`filter_freshness` (Module 9) stops a stale posting ever reaching the LLM, but
it only ever ran against *incoming* rows — and until it was fixed it let
through every posting whose date was written in a different format from the
first one in its slice, which is how lever postings 3,600+ days old ended up
scored and on the dashboard. This clears out what is already stored.

Every poll now runs the same prune (`pipeline._prune_stale_jobs`) for
unmarked jobs, so this script is for the backlog, for marked jobs
(`--include-marked`), and for seeing what would go before it goes.

**The rule is not re-implemented here.** `db.stale_jobs` decides with
`freshness.is_fresh` — the same function the pipeline uses — against the same
`MAX_POSTING_AGE_DAYS`, so this script deletes exactly the set the pipeline
would now refuse to score.

Two consequences of that reuse, both deliberate and both inherited rather than
chosen:

- **A row with no posting date is never deleted.** `is_fresh` returns
  "no_posted_date" -> keep, because the source omits it often enough that
  treating unknown as old would discard real opportunities. That is 7,914 of
  11,482 rows today, so this script touches far less than "everything old"
  might suggest.
- **A future-dated posting is never deleted** either. A data quirk is not a
  reason to discard a job.

**Anything you have marked is kept, whatever its age.** A row with a
`job_state` — applied or declined — is a decision you made, and the posting
record is the only context that decision has. Deleting an applied job would
throw away the evidence that you applied. Pass `--include-marked` to override
that, which also clears the corresponding `job_state` rows rather than
orphaning them (nothing enforces that link at the schema level).

Deleted rows are *not* gone for good in practice: `load_seen_keys` reads the
jobs table, so a deleted global_id is no longer "seen" and the next poll will
reconsider it. It will then be dropped again by `filter_freshness` before any
LLM call, which costs nothing. Deleting is therefore safe but not permanent
suppression — the freshness filter is what keeps these out.

    python scripts/purge_stale_jobs.py                  # report only
    python scripts/purge_stale_jobs.py --apply          # delete
    python scripts/purge_stale_jobs.py --apply --vacuum # delete and shrink the file

Take a backup first. `sqlite3.Connection.backup` is the safe way while the
dashboard is attached.
"""

from __future__ import annotations

import argparse
import collections
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from coldstart.db import connection, delete_jobs, init_schema, stale_jobs  # noqa: E402
from coldstart.filters.freshness import now_utc  # noqa: E402
from coldstart.settings import load_settings  # noqa: E402


def _bucket(age_days: float) -> str:
    """Coarse age bands, so the report reads as a shape rather than a list."""
    if age_days <= 30:
        return "15-30 days"
    if age_days <= 90:
        return "1-3 months"
    if age_days <= 365:
        return "3-12 months"
    return "over a year"


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Delete stored jobs older than MAX_POSTING_AGE_DAYS."
    )
    parser.add_argument("--apply", action="store_true", help="actually delete")
    parser.add_argument(
        "--include-marked",
        action="store_true",
        help="also delete jobs you have applied to or declined, and their marks",
    )
    parser.add_argument(
        "--vacuum",
        action="store_true",
        help="VACUUM after deleting, to shrink the file (needs exclusive access)",
    )
    parser.add_argument(
        "--max-age-days",
        type=int,
        default=None,
        help="override MAX_POSTING_AGE_DAYS for this run",
    )
    args = parser.parse_args()

    settings = load_settings()
    max_age = args.max_age_days or settings.max_posting_age_days
    now = now_utc()

    with connection(settings.db_path) as conn:
        init_schema(conn)

        stale, kept_because_marked = [], collections.Counter()
        for row, age in stale_jobs(conn, max_age, now):
            if row["mark"] and not args.include_marked:
                kept_because_marked[row["mark"]] += 1
                continue
            stale.append((row, age))

        total = conn.execute("SELECT COUNT(*) AS n FROM jobs").fetchone()["n"]
        print(f"database: {total} job(s)   cutoff: {max_age} days")
        print(f"stale and deletable: {len(stale)}")

        by_age = collections.Counter(_bucket(age) for _row, age in stale)
        for bucket in ("15-30 days", "1-3 months", "3-12 months", "over a year"):
            if by_age[bucket]:
                print(f"  {by_age[bucket]:6d}  {bucket}")

        by_status = collections.Counter(row["status"] for row, _age in stale)
        print("  by status: " + ", ".join(f"{s}={n}" for s, n in by_status.most_common()))

        if kept_because_marked:
            summary = ", ".join(f"{n} {state}" for state, n in kept_because_marked.most_common())
            print(f"\nkept because you have actioned them: {summary}")
            print("  (pass --include-marked to delete these too)")

        for row, age in stale[:10]:
            print(f"    {int(age):5d}d  {row['company'][:26]:26} {row['title'][:44]}")

        if not stale:
            print("\nnothing to delete")
            return

        if not args.apply:
            print(f"\ndry run — pass --apply to delete these {len(stale)} row(s)")
            return

        # Only --include-marked can put a marked job in `stale`, and then its
        # mark has to go with it rather than be left orphaned.
        delete_jobs(
            conn, (row["global_id"] for row, _age in stale), with_marks=args.include_marked
        )
        remaining = conn.execute("SELECT COUNT(*) AS n FROM jobs").fetchone()["n"]
        print(f"\ndeleted {len(stale)} job(s) — {remaining} remain")

        if args.vacuum:
            # Outside the transaction, and it needs the whole file: this fails
            # rather than corrupting anything if something else holds a lock.
            conn.execute("VACUUM")
            print("vacuumed")


if __name__ == "__main__":
    main()
