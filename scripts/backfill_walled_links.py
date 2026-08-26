"""Repoint already-persisted jobs whose stored link is a login wall.

`_best_link` now skips an `apply_url` that leads to a sign-in form and stores
the public posting page from `url` instead (pipeline.py). That only governs
rows written *after* the fix — every ycombinator job already in the database
keeps the `account.ycombinator.com/authenticate?continue=...` link it was
persisted with, and the dashboard and digest both read that column, so the
existing shortlist stays unclickable until it is rewritten here.

Nothing else can rewrite it. `load_seen_keys` marks these global_ids as seen,
so the pipeline will not revisit them, and `url` is not a persisted column —
it has to be re-read from the parquet slice the row came from, matched on
`ats_id` (the half of `global_id` after the colon).

A row whose parquet no longer carries its `ats_id` is left exactly as it was:
a stale wall link is worse than nothing to click, but silently blanking a
column is worse than both.

    python scripts/backfill_walled_links.py            # report only
    python scripts/backfill_walled_links.py --apply    # rewrite the rows
"""

from __future__ import annotations

import argparse
import collections
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

import pyarrow.parquet as pq  # noqa: E402

from coldstart.db import connection  # noqa: E402
from coldstart.pipeline import _LOGIN_WALLED_APPLY_RE  # noqa: E402
from coldstart.settings import load_settings  # noqa: E402


def _listing_urls(data_dir: Path, ats_type: str, wanted: set[str]) -> dict[str, str]:
    """ats_id -> public posting url, for the ids we actually need."""
    path = data_dir / f"{ats_type}.parquet"
    if not path.exists():
        print(f"  no local slice at {path} — skipping {ats_type}")
        return {}

    found: dict[str, str] = {}
    parquet = pq.ParquetFile(path)
    for batch in parquet.iter_batches(batch_size=20_000, columns=["ats_id", "url"]):
        ids = batch.column("ats_id").to_pylist()
        urls = batch.column("url").to_pylist()
        for ats_id, url in zip(ids, urls, strict=True):
            key = str(ats_id)
            if key in wanted and key not in found and url:
                found[key] = url
    return found


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--apply", action="store_true", help="write the rewritten links")
    args = parser.parse_args()

    settings = load_settings()
    data_dir = Path(settings.data_dir)

    with connection(settings.db_path) as conn:
        rows = conn.execute(
            "SELECT global_id, ats_type, apply_url FROM jobs WHERE apply_url IS NOT NULL"
        ).fetchall()

        walled = [r for r in rows if _LOGIN_WALLED_APPLY_RE.match(r["apply_url"])]
        if not walled:
            print("no login-walled links in the database")
            return 0

        by_ats: dict[str, list] = collections.defaultdict(list)
        for row in walled:
            by_ats[row["ats_type"]].append(row)

        print(f"{len(walled)} login-walled links across {len(by_ats)} source(s)\n")

        updates: list[tuple[str, str]] = []
        for ats_type, source_rows in sorted(by_ats.items()):
            wanted = {r["global_id"].split(":", 1)[1] for r in source_rows}
            urls = _listing_urls(data_dir, ats_type, wanted)

            resolved = 0
            for row in source_rows:
                url = urls.get(row["global_id"].split(":", 1)[1])
                if url:
                    updates.append((url, row["global_id"]))
                    resolved += 1
            missing = len(source_rows) - resolved
            print(f"  {ats_type}: {resolved} resolved, {missing} not found in the local slice")

        print(f"\n{len(updates)} row(s) would be rewritten")
        if updates:
            print("\nsample:")
            for url, global_id in updates[:5]:
                print(f"  {global_id}  ->  {url}")

        if not args.apply:
            print("\nreport only — re-run with --apply to write")
            return 0

        conn.executemany("UPDATE jobs SET apply_url = ? WHERE global_id = ?", updates)
        conn.commit()
        print(f"\nrewrote {len(updates)} row(s)")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
