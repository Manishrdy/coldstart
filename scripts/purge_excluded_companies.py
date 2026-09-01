"""Delete stored jobs whose company is now on the block list.

`filter_companies` (Module 7) drops a blocked employer's postings on the way
in, but only for postings that arrive *after* the block. Adding a name to
`config/excluded_companies.json` does nothing about the rows already stored,
so the dashboard keeps showing a company you have just said you never want to
see again. This is the retroactive half — run it after every edit to that
file.

**The rule is not re-implemented here.** It calls
`filters.company.is_excluded_company`, so this deletes exactly what the
pipeline would now refuse to ingest. That matters more than it might sound:
the matcher is deliberately exact-on-normalized-name rather than substring,
with hostname and legal-suffix handling, precisely so `apple-roofing` and
`Meta House` survive a block on `apple` and `meta`. Re-deriving that in SQL
with `LIKE '%...%'` would delete real companies.

**Rows carrying a `job_state` are kept whatever their company.** A mark is a
decision you made, and the posting record is the only context that decision
has. `job_state` has no foreign key, so a plain `DELETE FROM jobs` would leave
those marks pointing at nothing. `--include-marked` overrides it and clears
both tables together.

The block is what keeps these out, not the delete: `load_seen_keys` reads the
jobs table, so a deleted global_id stops being "seen" and the next poll
reconsiders it — `filter_companies` then drops it again, before the title,
location, eligibility and scoring stages, at no cost.

    python scripts/purge_excluded_companies.py            # report only
    python scripts/purge_excluded_companies.py --apply    # delete

Companion to `purge_stale_jobs.py`, which does the same job for postings that
have aged past MAX_POSTING_AGE_DAYS. Take a backup first.
"""

from __future__ import annotations

import argparse
import collections
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from coldstart.db import connection, init_schema  # noqa: E402
from coldstart.filters.company import is_excluded_company  # noqa: E402
from coldstart.settings import load_settings  # noqa: E402


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Delete stored jobs whose company is on the block list."
    )
    parser.add_argument("--apply", action="store_true", help="actually delete")
    parser.add_argument(
        "--include-marked",
        action="store_true",
        help="also delete jobs you have applied to or declined, and their marks",
    )
    args = parser.parse_args()

    settings = load_settings()
    with connection(settings.db_path) as conn:
        init_schema(conn)

        marked = {
            row["global_id"]: row["state"]
            for row in conn.execute("SELECT global_id, state FROM job_state")
        }
        rows = conn.execute("SELECT global_id, company, status FROM jobs").fetchall()

        doomed: list = []
        kept: collections.Counter = collections.Counter()
        per_company: collections.Counter = collections.Counter()
        matched_by: dict[str, str] = {}

        for row in rows:
            excluded, entry = is_excluded_company(row["company"])
            if not excluded:
                continue
            matched_by[row["company"]] = entry
            state = marked.get(row["global_id"])
            if state and not args.include_marked:
                kept[(row["company"], state)] += 1
                continue
            doomed.append(row)
            per_company[row["company"]] += 1

        total = conn.execute("SELECT COUNT(*) AS n FROM jobs").fetchone()["n"]
        print(f"database: {total} job(s)   block list: config/excluded_companies.json")
        print(f"blocked and deletable: {len(doomed)}")
        for company, count in per_company.most_common():
            print(f"  {count:5d}  {company}  (matched {matched_by[company]!r})")

        if kept:
            print("\nkept because you have actioned them:")
            for (company, state), count in kept.most_common():
                print(f"  {count:5d}  {company} — {state}")
            print("  (pass --include-marked to delete these too)")

        if not doomed:
            print("\nnothing to delete")
            return

        if not args.apply:
            print(f"\ndry run — pass --apply to delete these {len(doomed)} row(s)")
            return

        ids = [(row["global_id"],) for row in doomed]
        conn.executemany("DELETE FROM jobs WHERE global_id = ?", ids)
        # Only reachable with --include-marked; without it nothing here is
        # marked. Clearing the mark alongside the job is the point — a
        # job_state row whose job is gone is unreadable, not a record.
        if args.include_marked:
            conn.executemany("DELETE FROM job_state WHERE global_id = ?", ids)
        conn.commit()
        remaining = conn.execute("SELECT COUNT(*) AS n FROM jobs").fetchone()["n"]
        print(f"\ndeleted {len(doomed)} job(s) — {remaining} remain")


if __name__ == "__main__":
    main()
