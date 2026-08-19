from __future__ import annotations

import csv
from datetime import date
from pathlib import Path

from coldstart.logging_setup import get_logger
from coldstart.models import JobRecord

logger = get_logger(__name__)

_COLUMNS = (
    "scored_at",
    "score",
    "score_band",
    "company",
    "title",
    "location",
    "resume_used",
    "matched_skills",
    "missing_skills",
    "reasoning",
    "apply_url",
    "ats_type",
    "posted_at",
    "location_flag",
    "eligibility_flag",
    "status",
    "provider_used",
)


def _job_to_row(job: JobRecord) -> list[str]:
    return [
        job.scored_at.isoformat() if job.scored_at else "",
        "" if job.score is None else str(job.score),
        job.score_band.value if job.score_band else "",
        job.company,
        job.title,
        job.location or "",
        job.resume_used.value if job.resume_used else "",
        # "; " rather than JSON — this file is read by humans in a spreadsheet,
        # not parsed back by the pipeline (contrast db._job_to_row).
        "; ".join(job.matched_skills),
        "; ".join(job.missing_skills),
        job.reasoning or "",
        job.apply_url or "",
        job.ats_type,
        job.posted_at.isoformat() if job.posted_at else "",
        job.location_flag.value,
        job.eligibility_flag.value,
        job.status.value,
        job.provider_used or "",
    ]


def export_csv(jobs: list[JobRecord], output_dir: Path, run_date: date) -> Path:
    output_dir = Path(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    path = output_dir / f"scored_{run_date.isoformat()}.csv"

    # Multiple polls/day append to the same file. utf-8-sig's BOM must only be
    # written once, at creation — opening an existing file with "utf-8-sig" in
    # append mode would prepend a second BOM as a literal character mid-file.
    is_new_file = not path.exists()
    encoding = "utf-8-sig" if is_new_file else "utf-8"

    sorted_jobs = sorted(jobs, key=lambda job: (job.score is None, -(job.score or 0)))

    with path.open("a", newline="", encoding=encoding) as f:
        writer = csv.writer(f, quoting=csv.QUOTE_ALL)
        if is_new_file:
            writer.writerow(_COLUMNS)
        for job in sorted_jobs:
            writer.writerow(_job_to_row(job))

    logger.info("wrote %d job(s) to %s (new_file=%s)", len(jobs), path, is_new_file)
    return path
