"""Module 27 — is this posting actually still there?

Trigger: a Genpact/Workday posting scored 95 (strong) and was dead by the
time the operator clicked it, minutes later. `apply_url` is useless for this
— Workday's `myworkdayjobs.com` job pages are a client-rendered SPA shell
that returns HTTP 200 with the same ~6.8KB of markup whether the requisition
exists or not (confirmed live, 2026-08-24, on the exact dead URL). A `curl`
or `ping` against it tells you nothing.

What each ATS actually exposes is the JSON endpoint its own frontend calls,
which *does* distinguish the two cases. Confirmed live against real rows
pulled from the production database, both directions, same day:

- **Workday**: the CXS endpoint is `apply_url` with `/wday/cxs/{tenant}`
  spliced in right after the domain (`{tenant}` = the subdomain's first
  label). A live posting returns 200 with a `jobPostingInfo` object; the
  dead Genpact posting returned `403 {"errorCode":"S22","message":"permission
  denied"}`. No extra data needed — it's a pure transform of what's already
  in `jobs.apply_url`.
- **Greenhouse**: `https://boards-api.greenhouse.io/v1/boards/{token}/jobs/{id}`.
  A live posting (`aircallioinc` #4356975009) returns 200; a nonexistent one
  returns `404 {"status":404,"error":"Job not found"}`.
- **Lever**: `https://api.lever.co/v0/postings/{company}/{postingId}`. A live
  posting (`Ketch` posting) returns 200 with the full JD; a nonexistent one
  returns `404 {"ok":false,"error":"Document not found"}`.

**The Greenhouse trust asymmetry is deliberate.** Most `apply_url`s carry the
board token directly (`job-boards.greenhouse.io/{token}/jobs/{id}`), but some
sources embed the widget behind a custom company domain instead
(`coinbase.com/careers/positions/8113286?gh_jid=8113286` — no token visible
in the URL at all). For those, the token is *guessed* from the `company`
field, which is right often enough to be worth trying but is a guess, not
data. A 200 from a guessed token positively confirms both "the guess was
right" and "the posting is live" — trust it. A 404 from a guessed token is
ambiguous — wrong guess and dead posting look identical — so it is UNKNOWN,
not DEAD. Getting this backwards would silently delist live postings behind
custom career-page domains, exactly the failure mode this project exists to
avoid (scope.md §10).

**UNKNOWN is the default for everything else**: an ats_type this module
doesn't cover, a URL shape that doesn't match any known pattern, a network
error, a timeout, an unexpected status code. None of those are evidence the
posting is gone — only a confirmed not-found response is. A job that comes
back UNKNOWN is left exactly as it was.

**Bonus signal, same call: Workday's real posting age.** The CXS response
for a live posting also carries `jobPostingInfo.postedOn`, a human string
Workday's own site renders directly — `"Posted 24 Days Ago"`,
`"Posted Today"`, `"Posted 30+ Days Ago"`. That's a live number straight
from the source, which matters because the snapshot's own `posted_at` can
be stale: scope.md §3.2 measured the manifest sitting unchanged for 13+
days, so the freshness filter's "today minus posted_at" can under-count a
posting's true age by that much. Since a checkable workday job already
costs one CXS call for the liveness check, parsing `postedOn` out of the
same response is free — no second request. `check_still_live` surfaces it
as `posted_days_ago` on a LIVE result; the caller (pipeline.py) treats a
posting that's live but revealed as older than `MAX_POSTING_AGE_DAYS` the
same way filters/freshness.py treats any other stale posting — dropped
before scoring, not persisted.
"""

from __future__ import annotations

import re
from urllib.parse import urlparse

import httpx

from coldstart.logging_setup import get_logger
from coldstart.models import LivenessCheck, LivenessFlag

logger = get_logger(__name__)

# Checked ats_types only — every other source (greenhouse-alikes with no
# public API, iCIMS, SuccessFactors, Oracle, ...) is out of scope for now and
# always comes back UNKNOWN via the dispatcher's fallthrough.
CHECKED_ATS_TYPES: frozenset[str] = frozenset({"workday", "greenhouse", "lever"})

# Short — this runs inline in the scoring loop and in a periodic sweep over
# many rows; a slow/hanging ATS endpoint must not stall either one for long.
# A single attempt, no retry: a false UNKNOWN from one bad request just gets
# re-tried on the next sweep, which is cheap and correct by construction.
_TIMEOUT = httpx.Timeout(10.0, connect=5.0)
_HEADERS = {"Accept": "application/json", "User-Agent": "coldstart-liveness-check/1.0"}

_WORKDAY_HOST_RE = re.compile(r"^([a-z0-9-]+)\.wd\d+\.myworkdayjobs\.com$", re.IGNORECASE)

_GREENHOUSE_TOKEN_URL_RE = re.compile(
    r"^https?://(?:job-boards|boards)\.greenhouse\.io/([^/]+)/jobs/(\d+)", re.IGNORECASE
)
_GREENHOUSE_SUBDOMAIN_RE = re.compile(
    r"^https?://([a-z0-9-]+)\.greenhouse\.io/jobs/(\d+)", re.IGNORECASE
)
_GREENHOUSE_GH_JID_RE = re.compile(r"[?&]gh_jid=(\d+)")
_NON_TOKEN_CHARS_RE = re.compile(r"[^a-z0-9]")

_LEVER_URL_RE = re.compile(
    r"^https?://jobs\.lever\.co/([^/]+)/([0-9a-fA-F-]{36})", re.IGNORECASE
)

# Workday's own rendering of postedOn, seen in the wild: "Posted Today",
# "Posted Yesterday", "Posted 24 Days Ago", "Posted 30+ Days Ago". The "+" is
# swallowed by the optional literal, so "30+" parses as 30 — a deliberate
# lower-bound approximation (see _parse_workday_posted_days_ago).
_WORKDAY_POSTED_TODAY_RE = re.compile(r"posted\s+today", re.IGNORECASE)
_WORKDAY_POSTED_YESTERDAY_RE = re.compile(r"posted\s+yesterday", re.IGNORECASE)
_WORKDAY_POSTED_N_DAYS_RE = re.compile(r"posted\s+(\d+)\+?\s+days?\s+ago", re.IGNORECASE)


def _parse_workday_posted_days_ago(posted_on: object) -> int | None:
    """None means "couldn't parse", never "zero" — an absent or unrecognized
    string must not be silently read as a fresh posting.

    "30+ Days Ago" becomes 30, a lower bound, not an exact age: real age
    could be higher. That only matters if MAX_POSTING_AGE_DAYS is raised
    above 30 — at the default (15) any "N+" string already exceeds it."""
    if not isinstance(posted_on, str):
        return None
    text = posted_on.strip()
    if _WORKDAY_POSTED_TODAY_RE.search(text):
        return 0
    if _WORKDAY_POSTED_YESTERDAY_RE.search(text):
        return 1
    match = _WORKDAY_POSTED_N_DAYS_RE.search(text)
    return int(match.group(1)) if match else None


def _get(url: str) -> httpx.Response | None:
    try:
        return httpx.get(url, headers=_HEADERS, timeout=_TIMEOUT)
    except httpx.HTTPError as exc:
        logger.debug("liveness check request failed for %s: %s", url, exc)
        return None


def _check_workday(apply_url: str) -> tuple[LivenessFlag, str, int | None]:
    parsed = urlparse(apply_url)
    match = _WORKDAY_HOST_RE.match(parsed.netloc)
    if not match or not parsed.path:
        return LivenessFlag.UNKNOWN, "workday_unrecognized_url", None

    tenant = match.group(1)
    cxs_url = f"https://{parsed.netloc}/wday/cxs/{tenant}{parsed.path}"
    response = _get(cxs_url)
    if response is None:
        return LivenessFlag.UNKNOWN, "workday_check_error", None

    if response.status_code == 200:
        try:
            body = response.json()
        except ValueError:
            return LivenessFlag.UNKNOWN, "workday_200_unparseable", None
        posting_info = body.get("jobPostingInfo") if isinstance(body, dict) else None
        if isinstance(posting_info, dict):
            posted_days_ago = _parse_workday_posted_days_ago(posting_info.get("postedOn"))
            return LivenessFlag.LIVE, "workday_cxs_200", posted_days_ago
        return LivenessFlag.UNKNOWN, "workday_200_unexpected_shape", None

    if response.status_code in (403, 404):
        return LivenessFlag.DEAD, f"workday_cxs_{response.status_code}", None

    return LivenessFlag.UNKNOWN, f"workday_cxs_http_{response.status_code}", None


def _guess_greenhouse_token(company: str) -> str:
    # Real greenhouse board tokens are lowercase alnum, no spaces/punctuation
    # ("aircallioinc"). Best-effort only — see the trust-asymmetry note above.
    return _NON_TOKEN_CHARS_RE.sub("", company.lower())


def _check_greenhouse(apply_url: str, company: str) -> tuple[LivenessFlag, str]:
    match = _GREENHOUSE_TOKEN_URL_RE.match(apply_url) or _GREENHOUSE_SUBDOMAIN_RE.match(apply_url)
    if match:
        token, job_id = match.group(1), match.group(2)
        trust_404 = True
    else:
        jid_match = _GREENHOUSE_GH_JID_RE.search(apply_url)
        token = _guess_greenhouse_token(company) if company else ""
        if not jid_match or not token:
            return LivenessFlag.UNKNOWN, "greenhouse_unrecognized_url"
        job_id = jid_match.group(1)
        trust_404 = False

    api_url = f"https://boards-api.greenhouse.io/v1/boards/{token}/jobs/{job_id}"
    response = _get(api_url)
    if response is None:
        return LivenessFlag.UNKNOWN, "greenhouse_check_error"

    if response.status_code == 200:
        return LivenessFlag.LIVE, "greenhouse_200"

    if response.status_code == 404:
        if trust_404:
            return LivenessFlag.DEAD, "greenhouse_404"
        return LivenessFlag.UNKNOWN, "greenhouse_404_guessed_token"

    return LivenessFlag.UNKNOWN, f"greenhouse_http_{response.status_code}"


def _check_lever(apply_url: str) -> tuple[LivenessFlag, str]:
    match = _LEVER_URL_RE.match(apply_url)
    if not match:
        return LivenessFlag.UNKNOWN, "lever_unrecognized_url"

    company, posting_id = match.group(1), match.group(2)
    api_url = f"https://api.lever.co/v0/postings/{company}/{posting_id}"
    response = _get(api_url)
    if response is None:
        return LivenessFlag.UNKNOWN, "lever_check_error"

    if response.status_code == 200:
        return LivenessFlag.LIVE, "lever_200"
    if response.status_code == 404:
        return LivenessFlag.DEAD, "lever_404"

    return LivenessFlag.UNKNOWN, f"lever_http_{response.status_code}"


def check_still_live(
    ats_type: str, apply_url: str | None, company: str = ""
) -> LivenessCheck:
    """Never raises — a check that can crash the pipeline is worse than one
    that just says UNKNOWN."""
    if not apply_url or ats_type not in CHECKED_ATS_TYPES:
        return LivenessCheck(flag=LivenessFlag.UNKNOWN, reason="unsupported_ats")

    try:
        if ats_type == "workday":
            flag, reason, posted_days_ago = _check_workday(apply_url)
            return LivenessCheck(flag=flag, reason=reason, posted_days_ago=posted_days_ago)
        if ats_type == "greenhouse":
            flag, reason = _check_greenhouse(apply_url, company)
        else:
            flag, reason = _check_lever(apply_url)
        return LivenessCheck(flag=flag, reason=reason)
    except Exception as exc:  # noqa: BLE001 - see docstring; this must never propagate
        logger.warning("liveness check crashed for %s (%s): %s", apply_url, ats_type, exc)
        return LivenessCheck(flag=LivenessFlag.UNKNOWN, reason="check_crashed")
