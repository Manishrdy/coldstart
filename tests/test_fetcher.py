import hashlib
import time

import httpx
import pandas as pd
import pytest

from coldstart.db import connection, init_schema
from coldstart.fetcher import (
    RAWJOB_COLUMNS,
    DownloadError,
    download_slice,
    load_slice,
    verify_sha256,
)
from coldstart.manifest_watch import SliceInfo


@pytest.fixture
def conn(tmp_path):
    with connection(tmp_path / "test.sqlite3") as c:
        init_schema(c)
        yield c


class _FakeStreamResponse:
    def __init__(self, chunks=(b"data",), status_error=None, block_content=True):
        self._chunks = list(chunks)
        self._status_error = status_error
        self._block_content = block_content

    def raise_for_status(self):
        if self._status_error is not None:
            raise self._status_error

    def iter_bytes(self, chunk_size=None):
        yield from self._chunks

    @property
    def content(self):
        if self._block_content:
            raise AssertionError("must stream via iter_bytes, not buffer .content")
        return b"".join(self._chunks)


class _FakeStreamCtx:
    def __init__(self, response):
        self._response = response

    def __enter__(self):
        return self._response

    def __exit__(self, exc_type, exc, tb):
        return False


def _make_stream_fn(behaviors):
    calls = {"n": 0}

    def _fn(method, url, timeout=None, follow_redirects=None):
        idx = min(calls["n"], len(behaviors) - 1)
        calls["n"] += 1
        behavior = behaviors[idx]
        if isinstance(behavior, Exception):
            raise behavior
        return _FakeStreamCtx(behavior)

    _fn.calls = calls
    return _fn


def _http_500():
    request = httpx.Request("GET", "https://example.com/jobs.parquet")
    return httpx.HTTPStatusError(
        "server error", request=request, response=httpx.Response(500, request=request)
    )


# --- verify_sha256 -----------------------------------------------------------


def test_verify_sha256_match_and_mismatch(tmp_path):
    path = tmp_path / "f.bin"
    path.write_bytes(b"hello world")
    expected = hashlib.sha256(b"hello world").hexdigest()
    assert verify_sha256(path, expected) is True
    assert verify_sha256(path, "0" * 64) is False


# --- download_slice ------------------------------------------------------------


def test_download_slice_uses_cache_no_http_call(tmp_path, conn, monkeypatch):
    content = b"cached parquet bytes"
    sha = hashlib.sha256(content).hexdigest()
    dest = tmp_path / "greenhouse.parquet"
    dest.write_bytes(content)

    def _fail_if_called(*args, **kwargs):
        raise AssertionError("should not call httpx.stream when cache is valid")

    monkeypatch.setattr(httpx, "stream", _fail_if_called)

    slice_info = SliceInfo(
        ats_type="greenhouse", parquet_url="https://x/jobs.parquet", sha256=sha, rows=10,
        size_bytes=len(content),
    )
    assert download_slice(slice_info, tmp_path, conn) == dest


def test_download_slice_downloads_fresh(tmp_path, conn, monkeypatch):
    content = b"a" * 5000
    sha = hashlib.sha256(content).hexdigest()
    response = _FakeStreamResponse(chunks=[content[:2500], content[2500:]])
    monkeypatch.setattr(httpx, "stream", _make_stream_fn([response]))

    slice_info = SliceInfo(
        ats_type="lever", parquet_url="https://x/jobs.parquet", sha256=sha, rows=5,
        size_bytes=len(content),
    )
    result = download_slice(slice_info, tmp_path, conn)
    assert result.read_bytes() == content


def test_download_slice_streams_without_buffering_full_content(tmp_path, conn, monkeypatch):
    content = b"x" * 10_000
    sha = hashlib.sha256(content).hexdigest()
    chunks = [content[i : i + 1000] for i in range(0, len(content), 1000)]
    response = _FakeStreamResponse(chunks=chunks)
    monkeypatch.setattr(httpx, "stream", _make_stream_fn([response]))

    slice_info = SliceInfo(
        ats_type="tesla", parquet_url="https://x", sha256=sha, rows=1, size_bytes=len(content)
    )
    result = download_slice(slice_info, tmp_path, conn)
    assert result.read_bytes() == content


def test_download_slice_sha256_mismatch_then_succeeds(tmp_path, conn, monkeypatch):
    good = b"correct content"
    bad = b"wrong content!!"
    sha = hashlib.sha256(good).hexdigest()
    responses = [_FakeStreamResponse(chunks=[bad]), _FakeStreamResponse(chunks=[good])]
    monkeypatch.setattr(httpx, "stream", _make_stream_fn(responses))

    slice_info = SliceInfo(
        ats_type="ashby", parquet_url="https://x", sha256=sha, rows=1, size_bytes=len(good)
    )
    result = download_slice(slice_info, tmp_path, conn)
    assert result.read_bytes() == good


def test_download_slice_sha256_mismatch_twice_raises_and_logs(tmp_path, conn, monkeypatch):
    sha = "deadbeef" * 8
    responses = [_FakeStreamResponse(chunks=[b"nope1"]), _FakeStreamResponse(chunks=[b"nope2"])]
    monkeypatch.setattr(httpx, "stream", _make_stream_fn(responses))

    slice_info = SliceInfo(
        ats_type="workable", parquet_url="https://x", sha256=sha, rows=1, size_bytes=5
    )
    with pytest.raises(DownloadError):
        download_slice(slice_info, tmp_path, conn)

    row = conn.execute("SELECT stage, job_ref FROM errors").fetchone()
    assert row["stage"] == "poll"
    assert row["job_ref"] == "workable"
    assert not (tmp_path / "workable.parquet").exists()


def test_download_slice_http_error_retried_then_succeeds(tmp_path, conn, monkeypatch):
    monkeypatch.setattr(time, "sleep", lambda s: None)
    content = b"finally works"
    sha = hashlib.sha256(content).hexdigest()
    responses = [
        _FakeStreamResponse(chunks=[], status_error=_http_500()),
        _FakeStreamResponse(chunks=[], status_error=_http_500()),
        _FakeStreamResponse(chunks=[content]),
    ]
    stream_fn = _make_stream_fn(responses)
    monkeypatch.setattr(httpx, "stream", stream_fn)

    slice_info = SliceInfo(
        ats_type="jazzhr", parquet_url="https://x", sha256=sha, rows=1, size_bytes=len(content)
    )
    result = download_slice(slice_info, tmp_path, conn)
    assert result.read_bytes() == content
    assert stream_fn.calls["n"] == 3


def test_download_slice_http_error_exhausted_raises_and_logs(tmp_path, conn, monkeypatch):
    monkeypatch.setattr(time, "sleep", lambda s: None)
    responses = [_FakeStreamResponse(chunks=[], status_error=_http_500()) for _ in range(5)]
    stream_fn = _make_stream_fn(responses)
    monkeypatch.setattr(httpx, "stream", stream_fn)

    slice_info = SliceInfo(
        ats_type="workday", parquet_url="https://x", sha256="abc", rows=1, size_bytes=1
    )
    with pytest.raises(DownloadError):
        download_slice(slice_info, tmp_path, conn)

    assert stream_fn.calls["n"] == 3
    row = conn.execute("SELECT stage, job_ref FROM errors").fetchone()
    assert row["stage"] == "poll"
    assert row["job_ref"] == "workday"


# --- load_slice ------------------------------------------------------------------


def test_load_slice_projects_columns_and_synthesizes_global_id_and_experience(tmp_path):
    df = pd.DataFrame(
        {
            "url": ["https://x/1", "https://x/2"],
            "requisition_id": ["R1", None],
            "company": ["Acme", "Acme"],
            "title": ["SWE", "SWE II"],
            "location": ["Remote", "NYC"],
            "country_iso": ["US", ""],
            "is_remote": [True, False],
            "apply_url": ["https://x/1/apply", None],
            "ats_type": ["greenhouse", "greenhouse"],
            "posted_at": ["2026-08-01T00:00:00Z", "2026-08-02T00:00:00Z"],
            "description": ["JD text 1", "JD text 2"],
            "raw": ['{"a":1}', '{"a":2}'],
            "ats_id": [111, 222],
            "salary_min": [None, None],
        }
    )
    path = tmp_path / "sample.parquet"
    df.to_parquet(path)

    result = load_slice(path, RAWJOB_COLUMNS)

    assert set(result.columns) == {
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
        "global_id",
        "experience",
    }
    assert list(result["global_id"]) == ["greenhouse:111", "greenhouse:222"]
    assert result["experience"].isna().all()


def test_load_slice_handles_string_ats_id(tmp_path):
    df = pd.DataFrame(
        {
            "url": ["https://x/1"],
            "requisition_id": ["R1"],
            "company": ["Acme"],
            "title": ["SWE"],
            "location": ["Remote"],
            "country_iso": ["US"],
            "is_remote": [True],
            "apply_url": [None],
            "ats_type": ["lever"],
            "posted_at": ["2026-08-01T00:00:00Z"],
            "description": ["JD"],
            "raw": ["{}"],
            "ats_id": ["abc123"],
        }
    )
    path = tmp_path / "sample.parquet"
    df.to_parquet(path)

    result = load_slice(path, RAWJOB_COLUMNS)
    assert result["global_id"].iloc[0] == "lever:abc123"
