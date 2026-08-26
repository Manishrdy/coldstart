import json
from pathlib import Path

import httpx
import pytest

from coldstart.db import connection, init_schema, set_slice_state
from coldstart.manifest_watch import (
    SliceInfo,
    changed_slices,
    fetch_manifest,
    load_excluded_ats,
    relevant_slices,
)
from coldstart.models import SliceState

FIXTURES = Path(__file__).parent / "fixtures"
EXCLUDED_ATS_PATH = Path(__file__).parent.parent / "config" / "excluded_ats.json"


@pytest.fixture
def conn(tmp_path):
    with connection(tmp_path / "test.sqlite3") as c:
        init_schema(c)
        yield c


@pytest.fixture
def real_manifest() -> dict:
    return json.loads((FIXTURES / "manifest_sample.json").read_text())


@pytest.fixture
def excluded() -> set[str]:
    return load_excluded_ats(EXCLUDED_ATS_PATH)


def test_load_excluded_ats_has_all_55(excluded):
    assert len(excluded) == 55
    assert {"eures", "bundesagentur", "wanted", "jobbankca"} <= excluded
    # single-employer companies and job-board aggregators, added after a
    # real-data volume complaint (Amazon: 33k+ rows/poll, whole org)
    assert {"amazon", "tesla", "apple", "tiktok", "google", "uber", "meta"} <= excluded
    assert {
        "weworkremotely",
        "builtin",
        "remoteok",
        "thehub",
        "manfred",
    } <= excluded


def test_ycombinator_and_wellfound_are_not_excluded(excluded):
    """Both were dropped from the aggregator list deliberately (§3.1).

    They are aggregators, but small ones that carry startup postings which
    never reach this pipeline any other way: ycombinator is 3,419 rows and
    wellfound 348, against 33k+ for a single excluded employer. Volume was
    the reason the other aggregators went, and it does not apply here."""
    assert {"ycombinator", "wellfound"}.isdisjoint(excluded)
    # scope narrowed to ashby/greenhouse/icims/lever/mercor/oracle/rippling/workday
    assert {"bamboohr", "smartrecruiters", "workable", "successfactors"} <= excluded
    assert {"ashby", "greenhouse", "icims", "lever", "mercor", "oracle", "rippling", "workday"}.isdisjoint(
        excluded
    )


def test_relevant_slices_excludes_configured_sources(real_manifest, excluded, conn):
    slices = relevant_slices(real_manifest, excluded, conn)
    ats_types = {s.ats_type for s in slices}
    assert "eures" not in ats_types


def test_relevant_slices_skips_zero_row_slices(real_manifest, excluded, conn):
    slices = relevant_slices(real_manifest, excluded, conn)
    ats_types = {s.ats_type for s in slices}
    assert "meta" not in ats_types


def test_relevant_slices_uses_parquet_hash_not_csv_hash(real_manifest, excluded, conn):
    slices = relevant_slices(real_manifest, excluded, conn)
    greenhouse = next(s for s in slices if s.ats_type == "greenhouse")
    raw = real_manifest["by_ats"]["greenhouse"]
    assert greenhouse.sha256 == raw["parquet_sha256"]
    assert greenhouse.sha256 != raw["sha256"]
    assert greenhouse.size_bytes == raw["parquet_size_bytes"]
    assert greenhouse.parquet_url == raw["parquet"]


def test_relevant_slices_returns_both_included_sources(real_manifest, excluded, conn):
    slices = relevant_slices(real_manifest, excluded, conn)
    assert {s.ats_type for s in slices} == {"greenhouse", "lever"}


def test_relevant_slices_falls_back_to_csv_when_parquet_absent(conn):
    manifest = {
        "by_ats": {
            "greenhouse": {
                "csv": "https://example.com/greenhouse/jobs.csv",
                "sha256": "csv-hash-123",
                "size_bytes": 1000,
                "rows": 50,
            }
        }
    }
    slices = relevant_slices(manifest, set(), conn)
    assert len(slices) == 1
    assert slices[0].parquet_url == "https://example.com/greenhouse/jobs.csv"
    assert slices[0].sha256 == "csv-hash-123"
    assert slices[0].size_bytes == 1000


def test_relevant_slices_skips_entry_with_no_url_or_hash(conn):
    manifest = {"by_ats": {"broken_source": {"rows": 100}}}
    assert relevant_slices(manifest, set(), conn) == []


def test_relevant_slices_malformed_manifest_returns_empty_and_logs_error(conn):
    result = relevant_slices({"not_by_ats": {}}, set(), conn)
    assert result == []
    row = conn.execute("SELECT stage FROM errors").fetchone()
    assert row["stage"] == "poll"


def test_relevant_slices_manifest_not_a_dict_returns_empty(conn):
    assert relevant_slices({"by_ats": "not-a-dict"}, set(), conn) == []


def test_changed_slices_unchanged_sha_not_returned(conn):
    slice_info = SliceInfo(
        ats_type="greenhouse",
        parquet_url="https://x/jobs.parquet",
        sha256="abc",
        rows=10,
        size_bytes=100,
    )
    set_slice_state(conn, SliceState(ats_type="greenhouse", last_sha256="abc"))
    assert changed_slices(conn, [slice_info]) == []


def test_changed_slices_changed_sha_is_returned(conn):
    slice_info = SliceInfo(
        ats_type="greenhouse",
        parquet_url="https://x/jobs.parquet",
        sha256="new-hash",
        rows=10,
        size_bytes=100,
    )
    set_slice_state(conn, SliceState(ats_type="greenhouse", last_sha256="old-hash"))
    result = changed_slices(conn, [slice_info])
    assert result == [slice_info]


def test_changed_slices_new_ats_type_is_returned(conn):
    slice_info = SliceInfo(
        ats_type="lever",
        parquet_url="https://x/jobs.parquet",
        sha256="hash",
        rows=10,
        size_bytes=100,
    )
    assert changed_slices(conn, [slice_info]) == [slice_info]


def test_two_consecutive_runs_yield_empty_on_second(real_manifest, excluded, conn):
    slices = relevant_slices(real_manifest, excluded, conn)
    first_run = changed_slices(conn, slices)
    assert len(first_run) == 2

    for s in first_run:
        set_slice_state(
            conn, SliceState(ats_type=s.ats_type, last_sha256=s.sha256, row_count=s.rows)
        )

    second_run = changed_slices(conn, relevant_slices(real_manifest, excluded, conn))
    assert second_run == []


def test_fetch_manifest_success(monkeypatch):
    payload = {"by_ats": {}}

    class _FakeResponse:
        def raise_for_status(self):
            pass

        def json(self):
            return payload

    def _fake_get(url, timeout=None, follow_redirects=None):
        assert url == "https://example.com/manifest.json"
        return _FakeResponse()

    monkeypatch.setattr(httpx, "get", _fake_get)
    assert fetch_manifest("https://example.com/manifest.json") == payload


def test_fetch_manifest_raises_on_http_error(monkeypatch):
    def _fake_get(url, timeout=None, follow_redirects=None):
        raise httpx.ConnectError("connection refused")

    monkeypatch.setattr(httpx, "get", _fake_get)
    with pytest.raises(httpx.ConnectError):
        fetch_manifest("https://example.com/manifest.json")
