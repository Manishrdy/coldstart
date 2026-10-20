"""How a posting's `global_id` is built.

It is the one key every stage leans on: dedupe's "have I seen this posting",
the upsert's "which row is this", and the operator's applied/declined marks
in `job_state`. Upstream has no such column, so it is synthesized here from
`ats_type`, `ats_id` and the posting page `url`. DEVELOPMENT_PLAN.md
Module 37 has the investigation behind every rule below.
"""

from __future__ import annotations

import hashlib
from urllib.parse import urlsplit

# Sources that number postings per employer, so one `ats_id` names unrelated
# jobs at different employers: 70,164 Workday ids and 42,221 iCIMS ids belong
# to more than one company (measured 2026-10-20) — `R0003795` is both OCLC's
# "Lead Software Engineer" and Lynn University's instructor posting. Their key
# carries the posting site's host, which is the employer's own site
# (`oclc.wd1.myworkdayjobs.com`, `careers-arccorp.icims.com`); one employer's
# several career sites on one Workday host share it, so the same job listed
# twice there stays one row. Every other source numbers globally (0 shared ids
# measured) and keeps `ats_type:ats_id`; `oracle`'s upstream ids already carry
# their host.
PER_EMPLOYER_ATS_TYPES = frozenset(
    {
        "avature",
        "bamboohr",
        "cornerstone",
        "eightfold",
        "icims",
        "personio",
        "phenom",
        "successfactors",
        "taleo",
        "teamtailor",
        "workday",
    }
)


def ats_id_text(value: object) -> str | None:
    """`ats_id` as key text, or None when the posting has none.

    A whole-number float is written as an integer: successfactors' id column
    reads as float only because 25,901 of its rows have no id, which turned
    every key into `1288603201.0` — a shape that would change the day
    upstream fills the blanks, making every stored job look new."""
    if value is None:
        return None
    if isinstance(value, float):
        if value != value:  # NaN
            return None
        if value.is_integer():
            return str(int(value))
    text = str(value)
    return text or None


def _host(url: object) -> str:
    host = urlsplit(url).hostname if isinstance(url, str) and url else None
    return host or "no-host"


def make_global_id(ats_type: str, ats_id: object, url: object) -> str:
    """`ats_type[:host]:id`.

    A posting with no id gets a short hash of its URL in place of one, rather
    than the text "nan" all 25,901 such successfactors postings used to share.
    A hash and not the URL itself: the dashboard puts the key in
    `/api/jobs/{global_id}/state`, and a URL's slashes would break that route."""
    parts = [ats_type]
    if ats_type in PER_EMPLOYER_ATS_TYPES:
        parts.append(_host(url))
    ident = ats_id_text(ats_id)
    if ident is None:
        ident = "url-" + hashlib.sha1(str(url or "").encode()).hexdigest()[:16]
    parts.append(ident)
    return ":".join(parts)


def ats_id_of(global_id: str) -> str:
    """The `ats_id` text a key was built from — for scripts that look a stored
    job back up in its snapshot. `url-…` for a posting that had none, which
    no snapshot row carries."""
    ats_type, rest = global_id.split(":", 1)
    if ats_type in PER_EMPLOYER_ATS_TYPES and ":" in rest:
        return rest.split(":", 1)[1]
    return rest
