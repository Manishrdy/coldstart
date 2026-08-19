from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import httpx
from pydantic import BaseModel

from coldstart.db import get_slice_state
from coldstart.errors import log_error
from coldstart.logging_setup import get_logger

logger = get_logger(__name__)


class SliceInfo(BaseModel):
    ats_type: str
    parquet_url: str
    sha256: str
    rows: int
    size_bytes: int


def load_excluded_ats(path: Path) -> set[str]:
    entries = json.loads(Path(path).read_text())
    return {entry["name"] for entry in entries}


def fetch_manifest(url: str) -> dict:
    response = httpx.get(url, timeout=30.0, follow_redirects=True)
    response.raise_for_status()
    return response.json()


def relevant_slices(
    manifest: dict, excluded: set[str], conn: sqlite3.Connection
) -> list[SliceInfo]:
    by_ats = manifest.get("by_ats") if isinstance(manifest, dict) else None
    if not isinstance(by_ats, dict):
        log_error(
            conn,
            stage="poll",
            message="manifest missing or malformed 'by_ats' field",
            source_file=__name__,
            function_name="relevant_slices",
        )
        return []

    slices: list[SliceInfo] = []
    excluded_count = 0
    excluded_bytes = 0
    skipped_zero_rows = 0

    for ats_type, entry in by_ats.items():
        if ats_type in excluded:
            excluded_count += 1
            excluded_bytes += entry.get("parquet_size_bytes") or entry.get("size_bytes") or 0
            continue

        rows = entry.get("rows") or 0
        if not rows:
            skipped_zero_rows += 1
            logger.debug("skipping zero-row slice: %s", ats_type)
            continue

        parquet_url = entry.get("parquet")
        if parquet_url:
            url, sha256, size_bytes = parquet_url, entry.get("parquet_sha256"), entry.get(
                "parquet_size_bytes", 0
            )
        else:
            url, sha256, size_bytes = entry.get("csv"), entry.get("sha256"), entry.get(
                "size_bytes", 0
            )

        if not url or not sha256:
            logger.warning("skipping %s: no usable url/sha256 in manifest entry", ats_type)
            continue

        slices.append(
            SliceInfo(
                ats_type=ats_type,
                parquet_url=url,
                sha256=sha256,
                rows=rows,
                size_bytes=size_bytes,
            )
        )

    logger.info(
        "excluded %d sources (%.1f MB), skipped %d zero-row slices, %d relevant slices",
        excluded_count,
        excluded_bytes / 1e6,
        skipped_zero_rows,
        len(slices),
    )
    return slices


def changed_slices(conn: sqlite3.Connection, slices: list[SliceInfo]) -> list[SliceInfo]:
    changed = []
    for slice_info in slices:
        state = get_slice_state(conn, slice_info.ats_type)
        if state is None or state.last_sha256 != slice_info.sha256:
            changed.append(slice_info)

    total_mb = sum(s.size_bytes for s in changed) / 1e6
    logger.info("%d/%d slices changed, %.1f MB to download", len(changed), len(slices), total_mb)
    return changed
