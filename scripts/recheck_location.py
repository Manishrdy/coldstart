"""Run the current location filter over jobs that are already scored.

The filter only sees postings as they come through a poll, and a poll never
touches a row it has already scored — so the shortlist keeps every foreign job
scored before a rule existed. On 2026-10-20 that was 191 of 1,308 open
shortlisted jobs, nearly all scored before the Module 26 overhaul (Aug 23),
when "can't tell" still meant "send it to the LLM": "München, BY, DE",
"Sofia, BG", "Cambridge" (a UK employer), "Tirana, AL". This closes that gap,
the same way recheck_sponsorship.py does for the sponsorship filter.

`country_iso`, `is_remote` and `raw` are never persisted to `jobs`, so they
are read back from the local snapshots in DATA_DIR, and the job is classified
exactly as a poll would classify it today. A job whose posting is no longer in
its snapshot is classified on its stored location text alone, and then only a
REJECTED verdict moves it — text alone can't see a `country_iso='US'` that may
have been what accepted it. The one exception is a row that was itself stored
as `uncertain`: those were scored under the old "uncertain goes to the LLM"
rule, so their own flag already says nothing US was found.

What it changes, per scored job that now comes out REJECTED or UNCERTAIN and
that you haven't applied to or declined:

- `status` becomes `excluded_location` (the dashboard's "Held back" view),
  with `location_flag` / `location_reason` set to today's verdict. **The score
  and reasoning are kept** — an explicit UPDATE, not the pipeline's upsert.
- Nothing else. `scored_at` is untouched, so these never land in a digest.

Jobs you have applied to or declined are reported but never moved.

    python scripts/recheck_location.py            # report only
    python scripts/recheck_location.py --apply    # write the changes
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
from coldstart.filters.location import classify_location  # noqa: E402
from coldstart.global_ids import ats_id_of, make_global_id  # noqa: E402
from coldstart.models import JobStatus, LocationFlag  # noqa: E402
from coldstart.settings import load_settings  # noqa: E402

_COLUMNS = ("location", "country_iso", "is_remote", "raw")


def _load_inputs(data_dir: Path, ats_type: str, ats_ids: set[str]) -> dict[str, list[tuple]]:
    """{global_id: [(company, location, country_iso, is_remote, raw), ...]} for
    ids still in the snapshot, keyed the way load_slice keys them
    (global_ids.make_global_id, Module 37). A list, because a few keys are
    still shared — one Taleo host serves many employers — so `_match` checks
    the company as well."""
    path = data_dir / f"{ats_type}.parquet"
    if not path.exists():
        return {}
    schema = pq.read_schema(path)
    # ycombinator's ats_id is an integer column; a string filter against it
    # raises rather than matching nothing.
    if pa.types.is_integer(schema.field("ats_id").type):
        wanted: list = [int(i) for i in ats_ids if i.isdigit()]
    else:
        wanted = list(ats_ids)
    if not wanted:
        return {}
    # Several slices have no is_remote or country_iso column at all.
    columns = [c for c in _COLUMNS if c in schema.names]
    table = pq.read_table(
        path, columns=["ats_id", "url", "company", *columns], filters=[("ats_id", "in", wanted)]
    )
    values = {c: table.column(c).to_pylist() for c in columns}
    companies = table.column("company").to_pylist()
    urls = table.column("url").to_pylist()
    found: dict[str, list[tuple]] = collections.defaultdict(list)
    for i, ats_id in enumerate(table.column("ats_id").to_pylist()):
        found[make_global_id(ats_type, ats_id, urls[i])].append(
            (companies[i], *(values[c][i] if c in values else None for c in _COLUMNS))
        )
    return found


def _match(candidates: list[tuple], company: str, location: str | None) -> tuple | None:
    """The snapshot posting this row was scored from: same company *and* the
    same location text. None otherwise, and the caller falls back to the
    stored text. A posting can change after it is scored — Capital One's
    "McLean, VA" is "2 Locations" in the current snapshot — and the stored
    text is what was scored and what the dashboard shows."""
    chosen = {c[1:] for c in candidates if c[0] == company and (c[1] or None) == (location or None)}
    return chosen.pop() if len(chosen) == 1 else None


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
            "SELECT global_id, ats_type, company, title, location, location_flag, score "
            "FROM jobs WHERE status = ?",
            (JobStatus.SCORED.value,),
        ).fetchall()
        states = job_states(conn)

        by_source: dict[str, set[str]] = collections.defaultdict(set)
        for row in rows:
            by_source[row["ats_type"]].add(ats_id_of(row["global_id"]))
        snapshots = {
            ats_type: _load_inputs(settings.data_dir, ats_type, ids)
            for ats_type, ids in by_source.items()
        }

        move: list[tuple] = []
        kept_decided: list[tuple] = []
        verdicts: collections.Counter = collections.Counter()
        from_snapshot = 0
        for row in rows:
            found = _match(
                snapshots[row["ats_type"]].get(row["global_id"], []),
                row["company"],
                row["location"],
            )
            if found is not None:
                from_snapshot += 1
                flag, reason = classify_location(*found)
                acts = flag is not LocationFlag.ACCEPTED
            else:
                flag, reason = classify_location(row["location"], None, None, None)
                acts = flag is LocationFlag.REJECTED or (
                    flag is LocationFlag.UNCERTAIN
                    and row["location_flag"] == LocationFlag.UNCERTAIN.value
                )
            verdicts[flag.value] += 1
            if not acts:
                continue
            if row["global_id"] in states:
                kept_decided.append((row, flag, reason))
            else:
                move.append((row, flag, reason))

        threshold = settings.score_threshold_strong
        shortlisted = [m for m in move if (m[0]["score"] or 0) >= threshold]
        print(f"scored jobs: {len(rows)}")
        print(f"  classified from the local snapshot: {from_snapshot}")
        print(f"  classified from stored location text: {len(rows) - from_snapshot}")
        print(f"  today's verdicts: {dict(verdicts.most_common())}")
        print(
            f"  would move to Held back: {len(move)} "
            f"({len(shortlisted)} of them currently shortlisted, score >= {threshold})"
        )
        by_flag = collections.Counter(flag.value for _row, flag, _reason in move)
        print(f"    by verdict: {dict(by_flag.most_common())}")
        # "bare_remote" and "no_location_data" say nothing about the country;
        # today's rules hold those back too, but they are the ones worth a look.
        by_reason = collections.Counter(
            reason.rsplit("_", 1)[0]
            if reason.startswith(("country_code_", "region_country_", "postal_country_"))
            else reason
            for _row, _flag, reason in move
        )
        print(f"    by reason: {dict(by_reason.most_common())}")
        print(f"  not US, but you already applied/declined (left in place): {len(kept_decided)}")
        for row, _flag, reason in sorted(shortlisted, key=lambda m: -(m[0]["score"] or 0))[:30]:
            print(
                f"    {row['score']:>3}  {row['company'][:24]:24}  "
                f"{(row['location'] or '')[:40]!r:44}  {reason}"
            )

        if not args.apply:
            print("\nreport only — run with --apply to write")
            return

        with conn:
            conn.executemany(
                "UPDATE jobs SET status = ?, location_flag = ?, location_reason = ? "
                "WHERE global_id = ? AND status = ?",
                [
                    (
                        JobStatus.EXCLUDED_LOCATION.value,
                        flag.value,
                        reason,
                        row["global_id"],
                        JobStatus.SCORED.value,
                    )
                    for row, flag, reason in move
                ],
            )
        print(f"\nwrote: moved {len(move)} to Held back")


if __name__ == "__main__":
    main()
