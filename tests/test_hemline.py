from __future__ import annotations

import json
import uuid
from datetime import UTC, datetime

import httpx
import pytest

from coldstart import hemline
from coldstart.db import (
    clear_slice_state,
    connection,
    get_hemline_event,
    init_schema,
    set_slice_state,
    upsert_job,
)
from coldstart.models import (
    EligibilityFlag,
    JobRecord,
    JobStatus,
    LocationFlag,
    ResumeId,
    ScoreBand,
    SliceState,
)
from coldstart.settings import Settings


@pytest.fixture(autouse=True)
def _no_retry_delay(monkeypatch):
    monkeypatch.setattr(hemline, "_INLINE_RETRY_DELAY_SECONDS", 0)


def _mark_processed_before(conn, ats_type: str = "greenhouse") -> None:
    set_slice_state(
        conn,
        SliceState(ats_type=ats_type, last_sha256="b" * 64, last_processed_at=datetime.now(UTC)),
    )


@pytest.fixture
def conn(tmp_path):
    # greenhouse counts as processed on an earlier poll, so its jobs are new
    # postings; the refill tests below start from a source with no history.
    with connection(tmp_path / "coldstart.sqlite3") as c:
        init_schema(c)
        _mark_processed_before(c)
        yield c


def _settings(**overrides) -> Settings:
    kwargs = dict(
        experience_years=3.5,
        smtp_user="user@example.com",
        smtp_app_password="app-pw",
        digest_recipient="user@example.com",
        hemline_token="tok-123",
    )
    kwargs.update(overrides)
    return Settings(**kwargs)


def _record(**overrides) -> JobRecord:
    kwargs = dict(
        global_id="greenhouse:1",
        company="Stripe",
        title="Senior Backend Engineer",
        apply_url="https://boards.greenhouse.io/stripe/jobs/1",
        ats_type="greenhouse",
        resume_used=ResumeId.A,
        score=90,
        score_band=ScoreBand.STRONG,
        eligible=True,
        status=JobStatus.SCORED,
        location_flag=LocationFlag.ACCEPTED,
        eligibility_flag=EligibilityFlag.PASSED,
        first_seen_at=datetime.now(UTC),
        scored_at=datetime.now(UTC),
    )
    kwargs.update(overrides)
    return JobRecord(**kwargs)


def _poster(*responses):
    """Fake httpx.post. Each response is a status code (int) or an exception
    to raise; the last one repeats once the queue runs out."""
    calls: list[dict] = []
    queue = list(responses)

    def _post(url, json=None, headers=None, timeout=None):
        calls.append({"url": url, "json": json, "headers": headers, "timeout": timeout})
        outcome = queue.pop(0) if len(queue) > 1 else queue[0]
        if isinstance(outcome, Exception):
            raise outcome
        return httpx.Response(outcome, text="nope" if outcome >= 400 else "ok")

    _post.calls = calls
    return _post


def _error_count(conn) -> int:
    return conn.execute("SELECT COUNT(*) FROM errors WHERE stage = 'hemline'").fetchone()[0]


# --- the payload -----------------------------------------------------------------


def test_every_resume_maps_to_its_hemline_profile():
    assert hemline.PROFILE_IDS == {
        ResumeId.A: "software-engineer",
        ResumeId.B: "ai-engineer",
        ResumeId.C: "forward-deployed-engineer-swe",
        ResumeId.D: "forward-deployed-engineer-ai",
    }


def test_event_id_is_a_uuid_stable_per_job():
    first = hemline.event_id_for("greenhouse:1")
    assert first == hemline.event_id_for("greenhouse:1")
    assert first != hemline.event_id_for("greenhouse:2")
    uuid.UUID(first)


def test_payload_has_exactly_the_agreed_fields():
    payload = hemline.build_payload(_record(resume_used=ResumeId.D))
    assert payload == {
        "id": hemline.event_id_for("greenhouse:1"),
        "job_title": "Senior Backend Engineer",
        "profile_id": "forward-deployed-engineer-ai",
        "company": "Stripe",
        "job_link": "https://boards.greenhouse.io/stripe/jobs/1",
    }


def test_blank_company_is_sent_as_null():
    assert hemline.build_payload(_record(company="  "))["company"] is None


def test_no_link_means_no_payload():
    assert hemline.build_payload(_record(apply_url=None)) is None


# --- job_scored: who gets sent ---------------------------------------------------


def test_strong_job_is_posted_once_with_bearer_token(conn, monkeypatch):
    post = _poster(201)
    monkeypatch.setattr(httpx, "post", post)

    hemline.job_scored(conn, _record(), _settings())

    assert len(post.calls) == 1
    call = post.calls[0]
    assert call["url"] == "https://hemline.botta.in/jobs"
    assert call["headers"] == {"Authorization": "Bearer tok-123"}
    assert call["json"]["job_title"] == "Senior Backend Engineer"
    assert call["timeout"] is not None
    row = get_hemline_event(conn, "greenhouse:1")
    assert row["status"] == "sent"
    assert row["sent_at"] is not None


def test_no_token_means_nothing_sent_and_nothing_queued(conn, monkeypatch):
    # Nothing queued matters as much as nothing sent: a queue built up while
    # the token was missing would all go out the moment it was added.
    post = _poster(201)
    monkeypatch.setattr(httpx, "post", post)

    hemline.job_scored(conn, _record(), _settings(hemline_token=None))
    hemline.job_scored(conn, _record(global_id="greenhouse:2"), _settings(hemline_token="  "))

    assert post.calls == []
    assert conn.execute("SELECT COUNT(*) FROM hemline_events").fetchone()[0] == 0


@pytest.mark.parametrize("score, sent", [(84, False), (85, True), (100, True)])
def test_cutoff_is_inclusive_at_85(conn, monkeypatch, score, sent):
    post = _poster(201)
    monkeypatch.setattr(httpx, "post", post)

    hemline.job_scored(conn, _record(score=score), _settings())

    assert (len(post.calls) == 1) is sent


def test_cutoff_follows_the_setting(conn, monkeypatch):
    post = _poster(201)
    monkeypatch.setattr(httpx, "post", post)

    hemline.job_scored(conn, _record(score=90), _settings(hemline_min_score=95))

    assert post.calls == []


def test_a_failed_score_is_never_sent(conn, monkeypatch):
    post = _poster(201)
    monkeypatch.setattr(httpx, "post", post)

    hemline.job_scored(conn, _record(status=JobStatus.FAILED, score=None), _settings())

    assert post.calls == []


def test_a_manual_score_is_never_sent_or_queued(conn, monkeypatch):
    # Module 40: the extra postings at a company that used its LLM slots for
    # the day. However high the keyword score, hemline never hears of them.
    post = _poster(201)
    monkeypatch.setattr(httpx, "post", post)

    hemline.job_scored(conn, _record(score=99, provider_used="manual"), _settings())

    assert post.calls == []
    assert conn.execute("SELECT COUNT(*) FROM hemline_events").fetchone()[0] == 0


def test_same_job_scored_twice_is_sent_once(conn, monkeypatch):
    post = _poster(201)
    monkeypatch.setattr(httpx, "post", post)

    hemline.job_scored(conn, _record(), _settings())
    hemline.job_scored(conn, _record(title="Renamed"), _settings())
    hemline.retry_unsent(conn, _settings())

    assert len(post.calls) == 1


def test_409_counts_as_delivered(conn, monkeypatch):
    monkeypatch.setattr(httpx, "post", _poster(409))

    hemline.job_scored(conn, _record(), _settings())

    assert get_hemline_event(conn, "greenhouse:1")["status"] == "sent"


# --- job_scored: a refill is not news ---------------------------------------------


def test_a_source_processed_for_the_first_time_sends_nothing(conn, monkeypatch):
    # An emptied database, or a source newly let in: every posting in its window
    # is scored for the first time, and none of it is new.
    post = _poster(201)
    monkeypatch.setattr(httpx, "post", post)

    hemline.job_scored(
        conn, _record(global_id="lever:1", ats_type="lever", score=99), _settings()
    )

    assert post.calls == []
    assert get_hemline_event(conn, "lever:1") is None


def test_a_refill_is_not_queued_for_later_either(conn, monkeypatch):
    post = _poster(201)
    monkeypatch.setattr(httpx, "post", post)
    hemline.job_scored(conn, _record(global_id="lever:1", ats_type="lever"), _settings())

    _mark_processed_before(conn, "lever")
    hemline.retry_unsent(conn, _settings())

    assert post.calls == []


def test_once_a_source_has_been_processed_its_new_jobs_are_sent(conn, monkeypatch):
    post = _poster(201)
    monkeypatch.setattr(httpx, "post", post)
    _mark_processed_before(conn, "lever")

    hemline.job_scored(conn, _record(global_id="lever:2", ats_type="lever"), _settings())

    assert len(post.calls) == 1


def test_a_re_run_from_the_analytics_page_sends_nothing(conn, monkeypatch):
    # "Re-run" forgets the source was processed so old postings get another
    # pass through the filters. Whatever that turns up is old, not new.
    post = _poster(201)
    monkeypatch.setattr(httpx, "post", post)
    clear_slice_state(conn, "greenhouse")

    hemline.job_scored(conn, _record(), _settings())

    assert post.calls == []


# --- job_scored: when hemline misbehaves ----------------------------------------


def test_unreachable_is_retried_once_then_left_owed(conn, monkeypatch):
    post = _poster(httpx.ConnectError("refused"))
    monkeypatch.setattr(httpx, "post", post)

    hemline.job_scored(conn, _record(), _settings())

    assert len(post.calls) == 2
    row = get_hemline_event(conn, "greenhouse:1")
    assert row["status"] == "pending"
    assert row["attempts"] == 1
    assert "ConnectError" in row["last_error"]


def test_a_blip_recovers_on_the_inline_retry(conn, monkeypatch):
    post = _poster(503, 201)
    monkeypatch.setattr(httpx, "post", post)

    hemline.job_scored(conn, _record(), _settings())

    assert len(post.calls) == 2
    assert get_hemline_event(conn, "greenhouse:1")["status"] == "sent"


def test_a_refusal_is_not_retried_inline(conn, monkeypatch):
    post = _poster(401)
    monkeypatch.setattr(httpx, "post", post)

    hemline.job_scored(conn, _record(), _settings())

    assert len(post.calls) == 1
    row = get_hemline_event(conn, "greenhouse:1")
    assert row["status"] == "pending"
    assert row["last_error"].startswith("HTTP 401")


def test_an_unexpected_error_never_reaches_the_pipeline(conn, monkeypatch):
    monkeypatch.setattr(httpx, "post", _poster(RuntimeError("boom")))

    hemline.job_scored(conn, _record(), _settings())  # must not raise

    assert _error_count(conn) == 1


# --- retry_unsent ----------------------------------------------------------------


def test_retry_sends_what_failed_earlier(conn, monkeypatch):
    monkeypatch.setattr(httpx, "post", _poster(httpx.ConnectError("refused")))
    hemline.job_scored(conn, _record(), _settings())

    post = _poster(201)
    monkeypatch.setattr(httpx, "post", post)
    assert hemline.retry_unsent(conn, _settings()) == 1

    assert len(post.calls) == 1
    assert post.calls[0]["json"]["id"] == hemline.event_id_for("greenhouse:1")
    assert get_hemline_event(conn, "greenhouse:1")["status"] == "sent"


def test_retry_stops_at_the_first_sign_hemline_is_down(conn, monkeypatch):
    monkeypatch.setattr(httpx, "post", _poster(httpx.ConnectError("refused")))
    for n in range(3):
        hemline.job_scored(conn, _record(global_id=f"greenhouse:{n}"), _settings())

    post = _poster(httpx.ConnectError("refused"))
    monkeypatch.setattr(httpx, "post", post)
    assert hemline.retry_unsent(conn, _settings()) == 0

    assert len(post.calls) == 1


def test_retry_moves_past_a_refused_job_to_the_next(conn, monkeypatch):
    monkeypatch.setattr(httpx, "post", _poster(httpx.ConnectError("refused")))
    for n in range(2):
        hemline.job_scored(conn, _record(global_id=f"greenhouse:{n}"), _settings())

    post = _poster(422, 201)
    monkeypatch.setattr(httpx, "post", post)
    assert hemline.retry_unsent(conn, _settings()) == 1

    assert len(post.calls) == 2


def test_gives_up_loudly_after_max_attempts(conn, monkeypatch):
    post = _poster(500)
    monkeypatch.setattr(httpx, "post", post)
    hemline.job_scored(conn, _record(), _settings())
    for _ in range(hemline._MAX_ATTEMPTS + 3):
        hemline.retry_unsent(conn, _settings())

    row = get_hemline_event(conn, "greenhouse:1")
    assert row["status"] == "failed"
    assert row["attempts"] == hemline._MAX_ATTEMPTS
    assert _error_count(conn) == 1
    # Two requests for the first round (inline retry), one per round after.
    assert len(post.calls) == hemline._MAX_ATTEMPTS + 1


def test_retry_does_nothing_with_the_token_removed(conn, monkeypatch):
    monkeypatch.setattr(httpx, "post", _poster(httpx.ConnectError("refused")))
    hemline.job_scored(conn, _record(), _settings())

    post = _poster(201)
    monkeypatch.setattr(httpx, "post", post)
    assert hemline.retry_unsent(conn, _settings(hemline_token=None)) == 0
    assert post.calls == []


def test_jobs_scored_before_hemline_existed_are_never_sent(conn, monkeypatch):
    # The operator's rule: new jobs only. A strong job already in `jobs` has no
    # hemline_events row, and nothing ever creates one after the fact.
    upsert_job(conn, _record(score=97))
    post = _poster(201)
    monkeypatch.setattr(httpx, "post", post)

    assert hemline.retry_unsent(conn, _settings()) == 0
    assert post.calls == []


def test_retry_sends_the_payload_frozen_at_score_time(conn, monkeypatch):
    monkeypatch.setattr(httpx, "post", _poster(httpx.ConnectError("refused")))
    hemline.job_scored(conn, _record(), _settings())
    upsert_job(conn, _record(title="Changed Later"))

    post = _poster(201)
    monkeypatch.setattr(httpx, "post", post)
    hemline.retry_unsent(conn, _settings())

    assert post.calls[0]["json"]["job_title"] == "Senior Backend Engineer"
    stored = json.loads(get_hemline_event(conn, "greenhouse:1")["payload"])
    assert stored == post.calls[0]["json"]
