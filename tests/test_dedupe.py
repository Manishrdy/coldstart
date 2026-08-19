import pandas as pd

from coldstart.dedupe import dedupe


def _df(rows: list[dict]) -> pd.DataFrame:
    defaults = {"company": "Acme", "location": "Remote — US"}
    return pd.DataFrame([{**defaults, **row} for row in rows])


def test_known_global_id_dropped():
    df = _df(
        [
            {"global_id": "a:1", "requisition_id": None, "posted_at": None},
            {"global_id": "a:2", "requisition_id": None, "posted_at": None},
        ]
    )
    result = dedupe(df, seen_global={"a:1"}, seen_req=set())
    assert list(result["global_id"]) == ["a:2"]


def test_known_req_key_dropped_even_with_new_global_id():
    df = _df(
        [
            {
                "global_id": "b:1",
                "company": "Acme",
                "requisition_id": "REQ-1",
                "location": "NYC",
                "posted_at": None,
            },
            {
                "global_id": "b:2",
                "company": "Acme",
                "requisition_id": "REQ-2",
                "location": "NYC",
                "posted_at": None,
            },
        ]
    )
    result = dedupe(df, seen_global=set(), seen_req={("Acme", "REQ-1", "NYC")})
    assert list(result["global_id"]) == ["b:2"]


def test_null_requisition_id_never_collides_with_another_null():
    df = _df(
        [
            {"global_id": "c:1", "requisition_id": None, "posted_at": None},
            {"global_id": "c:2", "requisition_id": None, "posted_at": None},
            {"global_id": "c:3", "requisition_id": None, "posted_at": None},
        ]
    )
    result = dedupe(df, seen_global=set(), seen_req=set())
    assert len(result) == 3


def test_same_requisition_id_different_company_not_collapsed():
    # Regression: requisition_id="1" collided across unrelated companies on
    # real data (DEVELOPMENT_PLAN.md Module 10) — bare requisition_id must
    # never be trusted as a dedup key on its own.
    df = _df(
        [
            {
                "global_id": "x:1",
                "company": "companyA",
                "requisition_id": "1",
                "location": "NYC",
                "posted_at": None,
            },
            {
                "global_id": "x:2",
                "company": "companyB",
                "requisition_id": "1",
                "location": "NYC",
                "posted_at": None,
            },
        ]
    )
    result = dedupe(df, seen_global=set(), seen_req=set())
    assert len(result) == 2


def test_same_company_and_requisition_id_different_location_not_collapsed():
    # Regression: one real company reused requisition_id="1" as a default
    # across thousands of genuinely different (same-title) postings for
    # different cities — location must be part of the dedup key.
    df = _df(
        [
            {
                "global_id": "y:1",
                "company": "svetness",
                "requisition_id": "1",
                "location": "Tyler, TX",
                "posted_at": None,
            },
            {
                "global_id": "y:2",
                "company": "svetness",
                "requisition_id": "1",
                "location": "Troy, TX",
                "posted_at": None,
            },
        ]
    )
    result = dedupe(df, seen_global=set(), seen_req=set())
    assert len(result) == 2


def test_intra_batch_duplicate_collapsed_prefers_non_null_posted_at():
    df = _df(
        [
            {"global_id": "d:1", "requisition_id": "REQ-9", "posted_at": None},
            {"global_id": "d:2", "requisition_id": "REQ-9", "posted_at": "2026-08-01"},
            {"global_id": "d:3", "requisition_id": "REQ-9", "posted_at": None},
        ]
    )
    result = dedupe(df, seen_global=set(), seen_req=set())
    assert len(result) == 1
    assert result["global_id"].iloc[0] == "d:2"


def test_intra_batch_duplicate_keeps_first_when_no_posted_at_present():
    df = _df(
        [
            {"global_id": "e:1", "requisition_id": "REQ-5", "posted_at": None},
            {"global_id": "e:2", "requisition_id": "REQ-5", "posted_at": None},
        ]
    )
    result = dedupe(df, seen_global=set(), seen_req=set())
    assert len(result) == 1
    assert result["global_id"].iloc[0] == "e:1"


def test_intra_batch_duplicate_keeps_first_when_multiple_have_posted_at():
    df = _df(
        [
            {"global_id": "f:1", "requisition_id": "REQ-7", "posted_at": "2026-08-01"},
            {"global_id": "f:2", "requisition_id": "REQ-7", "posted_at": "2026-08-02"},
        ]
    )
    result = dedupe(df, seen_global=set(), seen_req=set())
    assert len(result) == 1
    assert result["global_id"].iloc[0] == "f:1"


def test_empty_seen_sets_drop_nothing():
    df = _df(
        [
            {"global_id": "g:1", "requisition_id": "REQ-1", "posted_at": None},
            {"global_id": "g:2", "requisition_id": None, "posted_at": None},
        ]
    )
    result = dedupe(df, seen_global=set(), seen_req=set())
    assert len(result) == 2


def test_no_requisition_ids_at_all_skips_intra_batch_dedup():
    df = _df(
        [
            {"global_id": "h:1", "requisition_id": None, "posted_at": None},
            {"global_id": "h:2", "requisition_id": None, "posted_at": "2026-08-01"},
        ]
    )
    result = dedupe(df, seen_global=set(), seen_req=set())
    assert len(result) == 2


def test_result_preserves_relative_order():
    df = _df(
        [
            {"global_id": "i:1", "requisition_id": None, "posted_at": None},
            {"global_id": "i:2", "requisition_id": "REQ-A", "posted_at": None},
            {"global_id": "i:3", "requisition_id": None, "posted_at": None},
        ]
    )
    result = dedupe(df, seen_global=set(), seen_req=set())
    assert list(result["global_id"]) == ["i:1", "i:2", "i:3"]


def test_running_same_slice_twice_yields_zero_new_jobs():
    df = _df(
        [
            {
                "global_id": "j:1",
                "company": "Acme",
                "requisition_id": "REQ-1",
                "location": "NYC",
                "posted_at": "2026-08-01",
            },
            {"global_id": "j:2", "requisition_id": None, "posted_at": None},
        ]
    )
    first_pass = dedupe(df, seen_global=set(), seen_req=set())
    assert len(first_pass) == 2

    seen_global = set(first_pass["global_id"])
    seen_req = {
        (row.company, row.requisition_id, row.location)
        for row in first_pass.itertuples()
        if pd.notna(row.requisition_id)
    }

    second_pass = dedupe(df, seen_global=seen_global, seen_req=seen_req)
    assert len(second_pass) == 0
