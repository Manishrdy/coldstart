"""Before/after diff harness for the location filter (plan Phase 0).

Not part of the pipeline. Run it before a change, run it again after, and read
the `accepted -> rejected` transitions: that set must be empty or explainable.
It is the only metric that tells you whether an aggressive rule ate real US
jobs. Row group 0 of each slice is plenty (~500k rows across the default set).

    python scripts/location_baseline.py before.json
    ...make changes...
    python scripts/location_baseline.py after.json --diff before.json
"""

from __future__ import annotations

import argparse
import json
import sys
from collections import Counter
from pathlib import Path

import pyarrow.parquet as pq

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from coldstart.fetcher import RAWJOB_COLUMNS  # noqa: E402
from coldstart.filters.location import filter_locations  # noqa: E402

DEFAULT_SLICES = [
    "workday", "greenhouse", "successfactors", "smartrecruiters", "oracle",
    "icims", "lever", "ashby", "bamboohr", "jazzhr", "paycom", "recruitee",
    "personio", "phenom", "pinpoint", "avature",
]


def snapshot(data_dir: Path, slices: list[str]) -> dict:
    by_location: dict[str, list] = {}
    counts: Counter = Counter()
    for name in slices:
        path = data_dir / f"{name}.parquet"
        if not path.exists():
            continue
        pf = pq.ParquetFile(path)
        # Only project columns the slice actually has — join_com has no `region`,
        # and several slices are missing `is_remote` entirely.
        have = set(pf.schema_arrow.names)
        cols = [c for c in RAWJOB_COLUMNS if c in have]
        df = pf.read_row_group(0, columns=cols).to_pandas()
        if "global_id" not in df.columns:
            df["global_id"] = name + ":" + df.get("ats_id", df.index.to_series()).astype(str)
        df = filter_locations(df)
        for loc, iso, flag, reason in zip(
            df.get("location", [None] * len(df)),
            df.get("country_iso", [None] * len(df)),
            df["location_flag"],
            df["location_reason"],
            strict=False,
        ):
            key = f"{name}\x1f{loc}\x1f{iso}"
            by_location.setdefault(key, [flag, reason])
            counts[flag] += 1
    return {"counts": dict(counts), "locations": by_location}


def render_diff(before: dict, after: dict) -> None:
    b, a = before["locations"], after["locations"]
    transitions: dict[tuple[str, str], Counter] = {}
    for key, (aflag, areason) in a.items():
        if key not in b:
            continue
        bflag = b[key][0]
        if bflag == aflag:
            continue
        label = f"{key.split(chr(31))[1]}  [{areason}]"
        transitions.setdefault((bflag, aflag), Counter())[label] += 1

    print("=== counts ===")
    print("  before:", before["counts"])
    print("  after: ", after["counts"])
    for (bflag, aflag), items in sorted(transitions.items()):
        marker = "  <-- REGRESSION RISK" if (bflag, aflag) == ("accepted", "rejected") else ""
        print(f"\n=== {bflag} -> {aflag}: {len(items)} distinct{marker} ===")
        for label, n in items.most_common(60):
            print(f"  {n:5d}  {label}")
    if not transitions:
        print("\nno flag transitions")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("out", type=Path)
    ap.add_argument("--diff", type=Path, default=None)
    ap.add_argument("--data-dir", type=Path, default=Path("data"))
    ap.add_argument("--slices", nargs="*", default=DEFAULT_SLICES)
    args = ap.parse_args()

    snap = snapshot(args.data_dir, args.slices)
    args.out.write_text(json.dumps(snap))
    print(f"wrote {args.out}: {len(snap['locations'])} distinct locations, {snap['counts']}")
    if args.diff:
        render_diff(json.loads(args.diff.read_text()), snap)


if __name__ == "__main__":
    main()
