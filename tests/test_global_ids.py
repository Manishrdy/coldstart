"""global_ids.py — the key every stage uses to tell postings apart.

Real cases from 2026-10-20 (DEVELOPMENT_PLAN.md Module 37): `R0003795` is
both OCLC's software job and Lynn University's instructor posting; one Disney
requisition is listed on two career sites of one Workday host; 25,901
successfactors postings have no id at all."""

import math

import pytest

from coldstart.global_ids import (
    PER_EMPLOYER_ATS_TYPES,
    ats_id_of,
    ats_id_text,
    make_global_id,
)

OCLC = "https://oclc.wd1.myworkdayjobs.com/oclc_careers/job/Dublin-OH/Lead-SWE_R0003795"
LYNN = "https://lynn.wd5.myworkdayjobs.com/careers/job/Freiburger/Instructor_R0003795-2"


def test_employers_sharing_a_workday_id_get_different_keys():
    assert make_global_id("workday", "R0003795", OCLC) == (
        "workday:oclc.wd1.myworkdayjobs.com:R0003795"
    )
    assert make_global_id("workday", "R0003795", LYNN) == (
        "workday:lynn.wd5.myworkdayjobs.com:R0003795"
    )


def test_one_job_on_two_career_sites_of_one_host_keeps_one_key():
    host = "https://disney.wd5.myworkdayjobs.com"
    assert make_global_id("workday", "10145073", f"{host}/disneycareer/job/NY/SWE_10145073") == (
        make_global_id("workday", "10145073", f"{host}/disneycareerdc/job/NY/SWE_10145073-1")
    )


def test_the_host_is_case_insensitive():
    assert make_global_id("icims", 2514, "https://Careers-ArcCorp.icims.com/jobs/2514/x") == (
        "icims:careers-arccorp.icims.com:2514"
    )


@pytest.mark.parametrize("ats_type", ["greenhouse", "lever", "ashby", "ycombinator", "oracle"])
def test_globally_numbered_sources_keep_their_key(ats_type):
    assert make_global_id(ats_type, "abc123", "https://example.com/jobs/1") == f"{ats_type}:abc123"


def test_oracle_ids_already_carry_their_host():
    assert make_global_id("oracle", "eeho.fa.us2.oraclecloud.com:342958", "https://x/1") == (
        "oracle:eeho.fa.us2.oraclecloud.com:342958"
    )


def test_a_whole_number_float_id_is_written_as_an_integer():
    # successfactors' id column is float only because some rows have none.
    assert ats_id_text(1288603201.0) == "1288603201"
    assert make_global_id("successfactors", 1288603201.0, "https://apply.iav.com/job/x/1") == (
        "successfactors:apply.iav.com:1288603201"
    )


@pytest.mark.parametrize("missing", [None, math.nan, ""])
def test_a_missing_id_becomes_a_hash_of_the_url(missing):
    url = "https://career5.successfactors.eu/sfcareer/jobreqcareer?jobId=22889&company=x"
    key = make_global_id("successfactors", missing, url)
    assert key.startswith("successfactors:career5.successfactors.eu:url-")
    assert "/" not in key  # the dashboard puts the key in a URL path
    assert key == make_global_id("successfactors", missing, url)  # stable
    assert key != make_global_id("successfactors", missing, url.replace("22889", "22923"))


def test_postings_without_ids_no_longer_share_one_key():
    keys = {
        make_global_id("successfactors", math.nan, f"https://x.example/job?jobId={n}")
        for n in range(50)
    }
    assert len(keys) == 50


def test_a_missing_url_still_builds_a_key():
    assert make_global_id("workday", "R1", None) == "workday:no-host:R1"


@pytest.mark.parametrize(
    "global_id,expected",
    [
        ("workday:oclc.wd1.myworkdayjobs.com:R0003795", "R0003795"),
        ("icims:careers-arccorp.icims.com:2514", "2514"),
        ("greenhouse:111", "111"),
        ("oracle:eeho.fa.us2.oraclecloud.com:342958", "eeho.fa.us2.oraclecloud.com:342958"),
        ("workday:R0003795", "R0003795"),  # the old shape still parses
    ],
)
def test_ats_id_of_reverses_make_global_id(global_id, expected):
    assert ats_id_of(global_id) == expected


def test_every_source_measured_with_shared_ids_is_per_employer():
    # Measured 2026-10-20: every slice where one ats_id belonged to more than
    # one company. Dropping one from the set brings its collisions back.
    assert {
        "workday", "icims", "cornerstone", "phenom", "bamboohr", "personio",
        "avature", "teamtailor", "taleo", "successfactors", "eightfold",
    } <= PER_EMPLOYER_ATS_TYPES
