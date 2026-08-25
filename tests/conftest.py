from __future__ import annotations

import subprocess
from pathlib import Path

import httpx
import pytest

from coldstart import digest
from coldstart.scoring.base import LLMProvider, LLMResponse, LLMUsage
from coldstart.settings import Settings


@pytest.fixture(autouse=True)
def _no_real_subprocesses(monkeypatch, request):
    """Make it impossible for a test to launch a real pipeline entrypoint.

    This exists because of a real incident. `daemon.run_child` spawns
    `scripts/run_digest.py` as a subprocess, and that script calls
    `load_settings()` itself — so it reads the REAL .env, not whatever
    Settings a test constructed. Test isolation stops at the process
    boundary.

    On 2026-08-20 a digest loop was added inside `run_daemon()`; four tests
    that called `run_daemon` mocked `_tick` but not the new digest path, so
    the suite spawned the real run_digest.py and sent **seven real emails to
    the real recipient**, writing seven rows to the real database.

    Mocking each call site is not enough — the next un-mocked path does it
    again. So the boundary itself is closed: any attempt to spawn one of
    these scripts fails loudly. A test that genuinely needs a subprocess
    (run_child's own tests use harmless throwaway scripts) is unaffected,
    and a test can opt in with @pytest.mark.allow_real_subprocess."""
    if request.node.get_closest_marker("allow_real_subprocess"):
        return

    real_popen = subprocess.Popen
    guarded = {
        "run_poll.py",
        "run_digest.py",
        "run_daemon.py",
        "ingest_resumes.py",
        "run_liveness_sweep.py",
    }

    def _blocked(args, *rest, **kwargs):
        names = {Path(str(a)).name for a in (args if isinstance(args, (list, tuple)) else [args])}
        if names & guarded:
            raise AssertionError(
                f"test tried to launch a real pipeline entrypoint: {sorted(names & guarded)}. "
                "These re-read the real .env and would hit the real database, SMTP and LLM. "
                "Mock daemon.run_child (or daemon._digest_tick) instead."
            )
        return real_popen(args, *rest, **kwargs)

    monkeypatch.setattr(subprocess, "Popen", _blocked)


@pytest.fixture(autouse=True)
def _no_real_smtp(monkeypatch):
    """No test may open a real SMTP connection.

    The subprocess guard above closed one route to the outside world; this
    closes the other. Found the same day, by a test of mine that forgot a
    `mocker.patch` and went straight to Gmail — it failed on credentials
    rather than sending, which is luck, not design.

    Tests that patch `coldstart.digest.smtplib.SMTP` themselves override this
    and work normally. Tests that forget get a loud failure instead of
    reaching the internet."""

    def _blocked(*args, **kwargs):
        raise AssertionError(
            "test tried to open a real SMTP connection. "
            'Patch it: mocker.patch("coldstart.digest.smtplib.SMTP")'
        )

    monkeypatch.setattr(digest.smtplib, "SMTP", _blocked)


@pytest.fixture(autouse=True)
def _no_real_outbound_get(monkeypatch, request):
    """No test may make a real `httpx.get`.

    verify.py (Module 27) calls httpx.get directly against real third-party
    ATS endpoints (workday/greenhouse/lever) to check whether a posting is
    still live. Without this guard, any test that exercises the liveness
    check without remembering to mock it — pipeline tests, sweep tests,
    anything that touches _process_slice — would silently hit the real
    internet. Same lesson as _no_real_smtp: don't rely on every call site
    remembering to mock, close the boundary itself.

    daemon.upstream_changed and manifest_watch.fetch_manifest also call
    httpx.get, and their own tests already monkeypatch it directly — that
    still works, since a test's own monkeypatch.setattr simply overrides this
    one within that test."""
    if request.node.get_closest_marker("allow_real_network"):
        return

    def _blocked(*args, **kwargs):
        raise AssertionError(
            "test tried to make a real httpx.get call. Patch it: "
            'monkeypatch.setattr(httpx, "get", fake_get)'
        )

    monkeypatch.setattr(httpx, "get", _blocked)


@pytest.fixture(autouse=True)
def _isolate_settings_from_real_dotenv(monkeypatch):
    # Settings() reads the repo's real .env (via model_config's env_file) as a
    # fallback whenever a test constructs Settings without passing every
    # field explicitly. Once a real .env exists (with real provider keys,
    # ollama_model, etc.), that silently defeats every test asserting
    # "missing X raises" behavior. Tests should only ever see the fields they
    # explicitly pass in.
    monkeypatch.setitem(Settings.model_config, "env_file", None)


class FakeProvider(LLMProvider):
    """Test double satisfying the LLMProvider ABC. Queue up text responses
    (or exception instances) and it returns/raises them in order, one per
    .complete() call, wrapping plain strings in an LLMResponse automatically
    so callers can keep supplying just the text content they want back."""

    def __init__(self, responses: list[str | Exception], *, name: str = "fake"):
        self._responses = list(responses)
        self.calls = 0
        self.name = name

    def complete(
        self, system: str, user: str, *, json_schema: dict | None = None
    ) -> LLMResponse:
        self.calls += 1
        response = self._responses.pop(0)
        if isinstance(response, Exception):
            raise response
        return LLMResponse(
            text=response,
            usage=LLMUsage(input_tokens=10, cached_tokens=0, output_tokens=5),
            provider=self.name,
            model="fake-model",
        )

    def estimate_cost(self, usage: LLMUsage) -> float:
        return 0.0
