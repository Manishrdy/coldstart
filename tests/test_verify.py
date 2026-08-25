from __future__ import annotations

import httpx
import pytest

from coldstart.models import LivenessFlag
from coldstart.verify import CHECKED_ATS_TYPES, check_still_live


def _responder(status_code: int, json: object = None, text: str | None = None):
    calls: list[str] = []

    def _get(url: str, headers=None, timeout=None):
        calls.append(url)
        if text is not None:
            return httpx.Response(status_code, text=text)
        return httpx.Response(status_code, json=json)

    _get.calls = calls
    return _get


def _raiser(exc: Exception):
    def _get(url: str, headers=None, timeout=None):
        raise exc

    return _get


# --- dispatcher --------------------------------------------------------------


def test_checked_ats_types_is_exactly_workday_greenhouse_lever():
    assert CHECKED_ATS_TYPES == {"workday", "greenhouse", "lever"}


def test_unsupported_ats_type_never_makes_a_request(monkeypatch):
    get = _responder(200)
    monkeypatch.setattr(httpx, "get", get)

    result = check_still_live("icims", "https://icims.example.com/job/1", "Acme")

    assert (result.flag, result.reason) == (LivenessFlag.UNKNOWN, "unsupported_ats")
    assert result.posted_days_ago is None
    assert get.calls == []


def test_missing_apply_url_never_makes_a_request(monkeypatch):
    get = _responder(200)
    monkeypatch.setattr(httpx, "get", get)

    result = check_still_live("workday", None, "Acme")

    assert (result.flag, result.reason) == (LivenessFlag.UNKNOWN, "unsupported_ats")
    assert get.calls == []


def test_a_crashing_checker_never_propagates(monkeypatch):
    import coldstart.verify as verify

    def _boom(url):
        raise RuntimeError("unexpected shape")

    monkeypatch.setattr(verify, "_check_workday", _boom)

    result = check_still_live("workday", "https://x.wd1.myworkdayjobs.com/j/1", "Acme")

    assert (result.flag, result.reason) == (LivenessFlag.UNKNOWN, "check_crashed")


# --- workday -------------------------------------------------------------------

_WORKDAY_URL = (
    "https://genpact.wd108.myworkdayjobs.com/External_Careers/job/"
    "3409-GLLC-1155-Perimeter-Center-West-Atlanta-GA/AI-Engineer-4A_JR10018694"
)
_WORKDAY_CXS_URL = (
    "https://genpact.wd108.myworkdayjobs.com/wday/cxs/genpact/External_Careers/job/"
    "3409-GLLC-1155-Perimeter-Center-West-Atlanta-GA/AI-Engineer-4A_JR10018694"
)


def test_workday_live_posting_hits_the_cxs_endpoint_and_returns_live(monkeypatch):
    # Real shape confirmed live, 2026-08-24, against a real still-open Genpact
    # posting from the same tenant/pod.
    get = _responder(
        200,
        json={
            "jobPostingInfo": {
                "canApply": True,
                "posted": True,
                "postedOn": "Posted 3 Days Ago",
            }
        },
    )
    monkeypatch.setattr(httpx, "get", get)

    result = check_still_live("workday", _WORKDAY_URL, "Genpact")

    assert (result.flag, result.reason) == (LivenessFlag.LIVE, "workday_cxs_200")
    assert result.posted_days_ago == 3
    assert get.calls == [_WORKDAY_CXS_URL]


@pytest.mark.parametrize("status", [403, 404])
def test_workday_dead_posting_is_detected(monkeypatch, status):
    # 403 {"errorCode":"S22","message":"permission denied"} is the exact real
    # response for the dead Genpact JR10018694 posting that triggered this
    # module; 404 is treated the same way for tenants that respond that way.
    get = _responder(status, json={"errorCode": "S22", "message": "permission denied"})
    monkeypatch.setattr(httpx, "get", get)

    result = check_still_live("workday", _WORKDAY_URL, "Genpact")

    assert result.flag == LivenessFlag.DEAD
    assert result.reason == f"workday_cxs_{status}"
    assert result.posted_days_ago is None


def test_workday_apply_url_that_returns_200_but_is_the_spa_shell_is_unknown(monkeypatch):
    # The original bug: myworkdayjobs.com job pages 200 unconditionally. This
    # only matters if someone points check_still_live at apply_url directly
    # instead of the CXS transform — confirms the shape guard catches it.
    get = _responder(200, text="<html>...</html>")
    monkeypatch.setattr(httpx, "get", get)

    result = check_still_live("workday", _WORKDAY_URL, "Genpact")

    assert (result.flag, result.reason) == (LivenessFlag.UNKNOWN, "workday_200_unparseable")


def test_workday_200_with_unexpected_json_shape_is_unknown(monkeypatch):
    get = _responder(200, json={"unexpected": "shape"})
    monkeypatch.setattr(httpx, "get", get)

    result = check_still_live("workday", _WORKDAY_URL, "Genpact")

    assert (result.flag, result.reason) == (LivenessFlag.UNKNOWN, "workday_200_unexpected_shape")


def test_workday_server_error_is_unknown_not_dead(monkeypatch):
    get = _responder(500)
    monkeypatch.setattr(httpx, "get", get)

    result = check_still_live("workday", _WORKDAY_URL, "Genpact")

    assert (result.flag, result.reason) == (LivenessFlag.UNKNOWN, "workday_cxs_http_500")


def test_workday_network_error_is_unknown(monkeypatch):
    monkeypatch.setattr(httpx, "get", _raiser(httpx.ConnectTimeout("timed out")))

    result = check_still_live("workday", _WORKDAY_URL, "Genpact")

    assert (result.flag, result.reason) == (LivenessFlag.UNKNOWN, "workday_check_error")


def test_workday_url_not_matching_the_myworkdayjobs_host_pattern_is_unknown(monkeypatch):
    get = _responder(200, json={"jobPostingInfo": {}})
    monkeypatch.setattr(httpx, "get", get)

    result = check_still_live("workday", "https://careers.example.com/job/JR1", "Acme")

    assert (result.flag, result.reason) == (LivenessFlag.UNKNOWN, "workday_unrecognized_url")
    assert get.calls == []


# --- workday: postedOn parsing (bonus signal, same CXS call) ----------------


@pytest.mark.parametrize(
    ("posted_on", "expected_days"),
    [
        ("Posted Today", 0),
        ("Posted Yesterday", 1),
        ("Posted 3 Days Ago", 3),
        ("Posted 24 Days Ago", 24),
        ("Posted 30+ Days Ago", 30),  # lower bound, not exact — see verify.py
        (None, None),  # field genuinely absent on some postings
        ("", None),
        ("Some unrecognized future format", None),
    ],
)
def test_workday_posted_on_text_is_parsed_into_days_ago(monkeypatch, posted_on, expected_days):
    posting_info = {"canApply": True, "posted": True}
    if posted_on is not None:
        posting_info["postedOn"] = posted_on
    get = _responder(200, json={"jobPostingInfo": posting_info})
    monkeypatch.setattr(httpx, "get", get)

    result = check_still_live("workday", _WORKDAY_URL, "Genpact")

    assert result.flag == LivenessFlag.LIVE
    assert result.posted_days_ago == expected_days


def test_workday_posted_days_ago_is_none_when_the_posting_is_dead(monkeypatch):
    # A dead posting has no jobPostingInfo at all — nothing to parse.
    get = _responder(403, json={"errorCode": "S22"})
    monkeypatch.setattr(httpx, "get", get)

    result = check_still_live("workday", _WORKDAY_URL, "Genpact")

    assert result.posted_days_ago is None


# --- greenhouse ------------------------------------------------------------


def test_greenhouse_direct_token_url_live(monkeypatch):
    # Real shape confirmed live against aircallioinc #4356975009.
    get = _responder(200, json={"id": 4356975009, "title": "Staff Software Engineer, AI"})
    monkeypatch.setattr(httpx, "get", get)

    result = check_still_live(
        "greenhouse",
        "https://job-boards.greenhouse.io/aircallioinc/jobs/4356975009",
        "Aircall",
    )

    assert (result.flag, result.reason) == (LivenessFlag.LIVE, "greenhouse_200")
    assert result.posted_days_ago is None  # bonus signal is workday-only
    assert get.calls == ["https://boards-api.greenhouse.io/v1/boards/aircallioinc/jobs/4356975009"]


def test_greenhouse_direct_token_url_dead_is_trusted(monkeypatch):
    # Real shape confirmed live: {"status":404,"error":"Job not found"}.
    get = _responder(404, json={"status": 404, "error": "Job not found"})
    monkeypatch.setattr(httpx, "get", get)

    result = check_still_live(
        "greenhouse", "https://boards.greenhouse.io/aircallioinc/jobs/0000000000", "Aircall"
    )

    assert (result.flag, result.reason) == (LivenessFlag.DEAD, "greenhouse_404")


def test_greenhouse_subdomain_url_style_is_recognized(monkeypatch):
    get = _responder(200, json={"id": 1})
    monkeypatch.setattr(httpx, "get", get)

    result = check_still_live(
        "greenhouse", "https://aircallioinc.greenhouse.io/jobs/4356975009", "Aircall"
    )

    assert (result.flag, result.reason) == (LivenessFlag.LIVE, "greenhouse_200")


def test_greenhouse_custom_domain_guessed_token_live_is_trusted(monkeypatch):
    # Real shape: coinbase's own careers page embeds Greenhouse via gh_jid,
    # no board token visible in the URL at all. Confirmed live: guessing the
    # company name as the token happened to work for coinbase.
    get = _responder(200, json={"id": 8113286})
    monkeypatch.setattr(httpx, "get", get)

    result = check_still_live(
        "greenhouse",
        "https://www.coinbase.com/careers/positions/8113286?gh_jid=8113286",
        "Coinbase",
    )

    assert (result.flag, result.reason) == (LivenessFlag.LIVE, "greenhouse_200")
    assert get.calls == ["https://boards-api.greenhouse.io/v1/boards/coinbase/jobs/8113286"]


def test_greenhouse_custom_domain_guessed_token_404_is_unknown_not_dead(monkeypatch):
    # A 404 here can't be trusted — it's indistinguishable from "the guessed
    # token was simply wrong". Treating it as DEAD would silently delist live
    # postings behind any custom career-page domain.
    get = _responder(404, json={"status": 404, "error": "Job not found"})
    monkeypatch.setattr(httpx, "get", get)

    result = check_still_live(
        "greenhouse",
        "https://www.example-corp.com/careers/9999?gh_jid=9999",
        "Example Corp, Inc.",
    )

    assert (result.flag, result.reason) == (LivenessFlag.UNKNOWN, "greenhouse_404_guessed_token")


def test_greenhouse_unrecognized_url_and_no_company_never_makes_a_request(monkeypatch):
    get = _responder(200)
    monkeypatch.setattr(httpx, "get", get)

    result = check_still_live("greenhouse", "https://www.example.com/careers/some-job", "")

    assert (result.flag, result.reason) == (LivenessFlag.UNKNOWN, "greenhouse_unrecognized_url")
    assert get.calls == []


# --- lever -------------------------------------------------------------------

_LEVER_URL = "https://jobs.lever.co/Ketch/9835fb73-d8c3-4b96-b499-2ce10bc70e57/apply"
_LEVER_API_URL = "https://api.lever.co/v0/postings/Ketch/9835fb73-d8c3-4b96-b499-2ce10bc70e57"


def test_lever_live_posting(monkeypatch):
    get = _responder(200, json={"text": "Backend Software Engineer"})
    monkeypatch.setattr(httpx, "get", get)

    result = check_still_live("lever", _LEVER_URL, "Ketch")

    assert (result.flag, result.reason) == (LivenessFlag.LIVE, "lever_200")
    assert result.posted_days_ago is None  # bonus signal is workday-only
    assert get.calls == [_LEVER_API_URL]


def test_lever_dead_posting(monkeypatch):
    # Real shape confirmed live: {"ok":false,"error":"Document not found"}.
    get = _responder(404, json={"ok": False, "error": "Document not found"})
    monkeypatch.setattr(httpx, "get", get)

    result = check_still_live("lever", _LEVER_URL, "Ketch")

    assert (result.flag, result.reason) == (LivenessFlag.DEAD, "lever_404")


def test_lever_unrecognized_url_never_makes_a_request(monkeypatch):
    get = _responder(200)
    monkeypatch.setattr(httpx, "get", get)

    result = check_still_live("lever", "https://jobs.lever.co/Ketch", "Ketch")

    assert (result.flag, result.reason) == (LivenessFlag.UNKNOWN, "lever_unrecognized_url")
    assert get.calls == []
