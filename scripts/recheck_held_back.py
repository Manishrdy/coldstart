"""Re-run the location filter over jobs it previously held back.

Held-back rows are persisted, which means `load_seen_keys` marks their
global_ids as seen and the pipeline never reconsiders them. That is the right
default — it stops the same Bengaluru posting being re-evaluated every poll —
but it also means **a lexicon fix does not retroactively rescue anything**.
Add "Redondo Beach" to config/us_cities.json and the eleven Redondo Beach jobs
already sitting in the held-back bucket stay there forever.

This script closes that gap. It re-classifies every `excluded_location` row
against the current lexicons and flips the ones that now come out ACCEPTED
back to `pending`, so the next poll picks them up and scores them.

Only `location` and `location_flag` survive in the database — `country_iso`,
`is_remote` and `raw` are never persisted — so a row that was held back on a
`raw`-derived signal cannot be re-derived here. Those stay put, which is the
safe direction: this script only ever *rescues*, never newly rejects.

    python scripts/recheck_held_back.py            # report only
    python scripts/recheck_held_back.py --apply    # flip the rescued rows
"""

from __future__ import annotations

import argparse
import collections
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from coldstart.db import connection, init_schema  # noqa: E402
from coldstart.filters.location import classify_location  # noqa: E402
from coldstart.models import JobStatus, LocationFlag  # noqa: E402
from coldstart.settings import load_settings  # noqa: E402


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--apply", action="store_true", help="write the changes")
    parser.add_argument("--limit", type=int, default=0, help="only look at N rows")
    args = parser.parse_args()

    settings = load_settings()
    with connection(settings.db_path) as conn:
        init_schema(conn)
        sql = (
            "SELECT global_id, company, title, location, location_reason "
            "FROM jobs WHERE status = ? ORDER BY first_seen_at DESC"
        )
        params: tuple = (JobStatus.EXCLUDED_LOCATION.value,)
        if args.limit:
            sql += " LIMIT ?"
            params += (args.limit,)
        rows = conn.execute(sql, params).fetchall()

        rescued = []
        for row in rows:
            # country_iso/is_remote/raw are not persisted; None is the honest
            # input, and it can only make the classifier more conservative.
            flag, reason = classify_location(row["location"], None, None, None)
            if flag is LocationFlag.ACCEPTED:
                rescued.append((row, reason))

        print(f"held back: {len(rows)}   now accepted: {len(rescued)}")
        by_reason = collections.Counter(reason for _row, reason in rescued)
        for reason, count in by_reason.most_common():
            print(f"  {count:5d}  {reason}")
        for row, reason in rescued[:25]:
            print(f"    {row['location']!r}  ({row['location_reason']} -> {reason})")

        if not args.apply:
            print("\ndry run — pass --apply to requeue these as pending")
            return

        for row, reason in rescued:
            conn.execute(
                "UPDATE jobs SET status = ?, location_flag = ?, location_reason = ? "
                "WHERE global_id = ?",
                (
                    JobStatus.PENDING.value,
                    LocationFlag.ACCEPTED.value,
                    reason,
                    row["global_id"],
                ),
            )
        conn.commit()
        print(f"\nrequeued {len(rescued)} job(s) as pending — the next poll will score them")


if __name__ == "__main__":
    main()
