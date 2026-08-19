from __future__ import annotations

import hashlib
import sqlite3
import time
from pathlib import Path

import httpx
import pandas as pd
from tenacity import retry, retry_if_exception_type, stop_after_attempt, wait_exponential

from coldstart.errors import log_error
from coldstart.logging_setup import get_logger
from coldstart.manifest_watch import SliceInfo

logger = get_logger(__name__)

# Physical parquet columns. `ats_id` is requested only to synthesize
# global_id (no such column exists in the source data) and dropped after —
# see DEVELOPMENT_PLAN.md Module 6 for the real-schema investigation.
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


def load_slice(path: Path, columns: list[str]) -> pd.DataFrame:
    df = pd.read_parquet(path, columns=columns)
    if "ats_type" in df.columns and "ats_id" in df.columns:
        df["global_id"] = df["ats_type"].astype(str) + ":" + df["ats_id"].astype(str)
        df = df.drop(columns=["ats_id"])
    if "experience" not in df.columns:
        df["experience"] = None
    return df
