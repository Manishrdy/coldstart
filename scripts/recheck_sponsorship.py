"""Run the sponsorship filter over jobs that were scored before it existed.

The filter (Module 33, filters/sponsorship.py) only sees postings as they come
through a poll, and a poll never touches a row it has already scored — so the
shortlist built up before the filter keeps every posting that says it won't
sponsor. This closes that gap, the same way recheck_held_back.py does for the
location filter.

JD text is never persisted to `jobs`, so the description is read back from the
local snapshots in DATA_DIR. A job whose posting is no longer in its snapshot
can't be re-checked and is left exactly as it is.

What it changes, per scored job it can re-check:

- records `sponsorship` / `sponsorship_reason` (always — the stance is a
  ranking signal for every job, not just a gate);
- if the posting says it won't sponsor, NEEDS_VISA_SPONSORSHIP is on, and
  you haven't applied to or declined it, moves it out of the scored list to
  `excluded_sponsorship`. **The score and reasoning are kept** — this is an
  explicit UPDATE, not the pipeline's upsert, so nothing paid for is lost.

Jobs you have applied to or declined are annotated but never moved.

    python scripts/recheck_sponsorship.py            # report only
    python scripts/recheck_sponsorship.py --apply    # write the changes
"""

from __future__ import annotations

import argparse
import collections
import sys
from pathlib import Path

import pyarrow as pa
import pyarrow.parquet as pq

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from coldstart.db import connection, init_schema, job_states, readonly_connection  # noqa: E402
from coldstart.filters.sponsorship import classify_sponsorship  # noqa: E402
from coldstart.models import JobStatus, SponsorshipStance  # noqa: E402
from coldstart.settings import load_settings  # noqa: E402


def _load_descriptions(data_dir: Path, ats_type: str, ats_ids: set[str]) -> dict[str, tuple]:
    """{ats_id: (description, raw)} for the ids still in the local snapshot."""
    path = data_dir / f"{ats_type}.parquet"
    if not path.exists():
        return {}
    id_type = pq.read_schema(path).field("ats_id").type
    # ycombinator's ats_id is an integer column; a string filter against it
    # raises rather than matching nothing.
    if pa.types.is_integer(id_type):
        wanted: list = [int(i) for i in ats_ids if i.isdigit()]
    else:
        wanted = list(ats_ids)
    if not wanted:
        return {}
    table = pq.read_table(
        path, columns=["ats_id", "description", "raw"], filters=[("ats_id", "in", wanted)]
    )
    return {
        str(ats_id): (description, raw)
        for ats_id, description, raw in zip(
            table.column("ats_id").to_pylist(),
            table.column("description").to_pylist(),
            table.column("raw").to_pylist(),
            strict=True,
        )
    }


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--apply", action="store_true", help="write the changes")
    args = parser.parse_args()

    settings = load_settings()
    # Report mode never writes, not even the schema migration: a read-only
    # connection makes that structural rather than a promise.
    opened = connection(settings.db_path) if args.apply else readonly_connection(settings.db_path)
    with opened as conn:
        if args.apply:
            init_schema(conn)
        rows = conn.execute(
            "SELECT global_id, ats_type, company, title, score FROM jobs WHERE status = ?",
            (JobStatus.SCORED.value,),
        ).fetchall()
        states = job_states(conn)

        by_source: dict[str, set[str]] = collections.defaultdict(set)
        for row in rows:
            by_source[row["ats_type"]].add(row["global_id"].split(":", 1)[1])
        snapshots = {
            ats_type: _load_descriptions(settings.data_dir, ats_type, ids)
            for ats_type, ids in by_source.items()
        }

        annotate: list[tuple] = []
        move: list = []
        stances: collections.Counter = collections.Counter()
        missing = 0
        kept_decided = 0
        for row in rows:
            found = snapshots[row["ats_type"]].get(row["global_id"].split(":", 1)[1])
            if found is None:
                missing += 1
                continue
            stance, reason = classify_sponsorship(*found)
            stances[stance.value if stance else "unstated"] += 1
            annotate.append((stance.value if stance else None, reason, row["global_id"]))
            if stance is SponsorshipStance.WONT_SPONSOR and settings.needs_visa_sponsorship:
                if row["global_id"] in states:
                    kept_decided += 1
                else:
                    move.append(row)

        shortlisted = sum(
            1 for row in move if (row["score"] or 0) >= settings.score_threshold_strong
        )
        print(f"scored jobs: {len(rows)}")
        print(f"  re-checkable (posting still in its local snapshot): {len(annotate)}")
        print(f"  not re-checkable, left alone: {missing}")
        print(f"  what they say: {dict(stances.most_common())}")
        if not settings.needs_visa_sponsorship:
            print("NEEDS_VISA_SPONSORSHIP is off — annotating only, nothing moves.")
        print(
            f"  would move to the won't-sponsor list: {len(move)} "
            f"({shortlisted} of them currently shortlisted)"
        )
        print(f"  won't sponsor, but you already applied/declined (left in place): {kept_decided}")
        for row in sorted(move, key=lambda r: -(r["score"] or 0))[:25]:
            print(f"    {row['score']:>3}  {row['company'][:28]:28}  {row['title'][:60]}")

        if not args.apply:
            print("\nreport only — run with --apply to write")
            return

        with conn:
            conn.executemany(
                "UPDATE jobs SET sponsorship = ?, sponsorship_reason = ? WHERE global_id = ?",
                annotate,
            )
            conn.executemany(
                "UPDATE jobs SET status = ? WHERE global_id = ? AND status = ?",
                [
                    (JobStatus.EXCLUDED_SPONSORSHIP.value, row["global_id"], JobStatus.SCORED.value)
                    for row in move
                ],
            )
        print(f"\nwrote: annotated {len(annotate)}, moved {len(move)}")


if __name__ == "__main__":
    main()
