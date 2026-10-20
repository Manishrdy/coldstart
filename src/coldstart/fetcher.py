from __future__ import annotations

import hashlib
import sqlite3
import time
from pathlib import Path

import httpx
import pandas as pd
from tenacity import retry, retry_if_exception_type, stop_after_attempt, wait_exponential

from coldstart.errors import log_error
from coldstart.global_ids import make_global_id
from coldstart.logging_setup import get_logger
from coldstart.manifest_watch import SliceInfo

logger = get_logger(__name__)

# Physical parquet columns. `ats_id` is requested only to synthesize
# global_id (no such column exists in the source data) and dropped after —
# see DEVELOPMENT_PLAN.md Module 6 for the real-schema investigation, and
# global_ids.py for how the key is built (`url` is part of it).
RAWJOB_COLUMNS = [
    "url",
    "requisition_id",
    "company",
    "title",
    "location",
    "country_iso",
    "is_remote",
    "apply_url",
    "ats_type",
    "posted_at",
    "description",
    "raw",
    "ats_id",
]

_CHUNK_SIZE = 1 << 20  # 1 MiB
_STREAM_TIMEOUT = httpx.Timeout(30.0, read=300.0)


class DownloadError(Exception):
    pass


def verify_sha256(path: Path, expected: str) -> bool:
    digest = hashlib.sha256()
    with Path(path).open("rb") as f:
        for chunk in iter(lambda: f.read(_CHUNK_SIZE), b""):
            digest.update(chunk)
    return digest.hexdigest() == expected


@retry(
    retry=retry_if_exception_type(httpx.HTTPError),
    stop=stop_after_attempt(3),
    wait=wait_exponential(multiplier=1, min=1, max=10),
    reraise=True,
)
def _stream_to_disk(url: str, dest: Path) -> None:
    dest.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = dest.with_suffix(dest.suffix + ".part")
    with httpx.stream("GET", url, timeout=_STREAM_TIMEOUT, follow_redirects=True) as response:
        response.raise_for_status()
        with tmp_path.open("wb") as f:
            for chunk in response.iter_bytes(chunk_size=_CHUNK_SIZE):
                f.write(chunk)
    tmp_path.replace(dest)


def download_slice(slice_info: SliceInfo, data_dir: Path, conn: sqlite3.Connection) -> Path:
    data_dir = Path(data_dir)
    dest = data_dir / f"{slice_info.ats_type}.parquet"

    if dest.exists() and verify_sha256(dest, slice_info.sha256):
        logger.info(
            "using cached slice: %s (%.1f MB, %d rows)",
            slice_info.ats_type,
            dest.stat().st_size / 1e6,
            slice_info.rows,
        )
        return dest

    for attempt in range(2):
        start = time.monotonic()
        try:
            _stream_to_disk(slice_info.parquet_url, dest)
        except httpx.HTTPError as exc:
            log_error(
                conn,
                stage="poll",
                exc=exc,
                source_file=__name__,
                function_name="download_slice",
                job_ref=slice_info.ats_type,
                retry_count=attempt,
            )
            raise DownloadError(
                f"failed to download {slice_info.ats_type}: {exc}"
            ) from exc

        if verify_sha256(dest, slice_info.sha256):
            elapsed = time.monotonic() - start
            size_mb = dest.stat().st_size / 1e6
            logger.info(
                "downloaded %s: %.1f MB in %.1fs, %d rows",
                slice_info.ats_type,
                size_mb,
                elapsed,
                slice_info.rows,
            )
            return dest

        logger.warning(
            "sha256 mismatch for %s on attempt %d, retrying", slice_info.ats_type, attempt + 1
        )
        dest.unlink(missing_ok=True)

    message = f"sha256 verification failed twice for {slice_info.ats_type}"
    log_error(
        conn,
        stage="poll",
        message=message,
        source_file=__name__,
        function_name="download_slice",
        job_ref=slice_info.ats_type,
        retry_count=2,
    )
    raise DownloadError(message)


def _warn_shared_keys(df: pd.DataFrame, path: Path) -> None:
    """Say so when one key still names different postings.

    Different titles is the test, not different company names: one employer's
    several career sites on one Workday host list the same job under two
    labels ("Disney", "Disney (Disneycareerdc)") — 18k such keys, all one job
    each, which sharing a key correctly collapses.

    Known and accepted (DEVELOPMENT_PLAN.md Module 37, measured over every
    local slice 2026-10-20): 12 Taleo keys, because one Taleo host serves
    many employers; 94 Workday keys, mostly one employer's two career sites
    titling the same opening differently (CoStar's "HR Advisor" / "Associate
    HR Advisor") plus a few tenants with junk ids (Capri's "Barcelona",
    AIA's "Permanent"). None of the 106 passes the title filter. A jump
    means a source has started numbering per employer and belongs in
    global_ids.PER_EMPLOYER_ATS_TYPES. Dedupe keeps one posting per key, so
    the others are dropped; this is where that stops being silent."""
    if "title" not in df.columns or df.empty:
        return
    titles = df.groupby("global_id", sort=False)["title"].nunique()
    shared = titles[titles > 1]
    if shared.empty:
        return
    logger.warning(
        "%s: %d key(s) name postings with different titles — only one posting per key "
        "is kept (e.g. %s)",
        Path(path).stem,
        len(shared),
        ", ".join(shared.index[:3]),
    )


def load_slice(path: Path, columns: list[str]) -> pd.DataFrame:
    df = pd.read_parquet(path, columns=columns)
    if "ats_type" in df.columns and "ats_id" in df.columns:
        ats_ids = df["ats_id"].astype(object).where(df["ats_id"].notna(), None)
        urls = df["url"] if "url" in df.columns else [None] * len(df)
        df["global_id"] = [
            make_global_id(str(ats_type), ats_id, url)
            for ats_type, ats_id, url in zip(df["ats_type"], ats_ids, urls, strict=True)
        ]
        df = df.drop(columns=["ats_id"])
        _warn_shared_keys(df, path)
    if "experience" not in df.columns:
        df["experience"] = None
    return df
