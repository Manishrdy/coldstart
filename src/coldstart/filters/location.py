from __future__ import annotations

import re

import pandas as pd

from coldstart.filters._shared import build_word_boundary_alternation, load_config_json
from coldstart.logging_setup import get_logger
from coldstart.models import LocationFlag

logger = get_logger(__name__)

# Abbreviations that double as ordinary English words / common tokens in prose
# (scope.md §4.1 / DEVELOPMENT_PLAN.md Module 7) — these only count as a state
# match when comma-preceded, never on a bare word-boundary match.
_COLLISION_ABBRS = {
    "OR", "IN", "ME", "OK", "HI", "PA", "DE", "LA", "MA", "AL", "AR",
    "MS", "MT", "MD", "MN", "MO", "SC", "WA",
}

_BARE_REMOTE_RE = re.compile(r"^(?:fully\s+|100%\s+)?remote(?:\s+only)?$", re.IGNORECASE)

# Empirically discovered on live data (DEVELOPMENT_PLAN.md Module 7): "CA" as
# country_iso is genuinely ambiguous in this dataset — it's the real ISO code
# for Canada, but also a frequent data-quality bug where "California" ends up
# in country_iso instead of "US". On a real greenhouse slice this was ~11.6k
# rows, roughly 2:1 California:real-Canada by content. Trusting it outright
# would silently reject thousands of legitimate California jobs, so it's
# deferred to the location-text cascade instead of accepted as authoritative.
# Other state/country abbreviation collisions (DE=Delaware/Germany, IN=Indiana/
# India, PA=Pennsylvania/Panama, MT=Montana/Malta, MD=Maryland/Moldova,
# MO=Missouri/Macao, SC=South Carolina/Seychelles) were checked on the same
# data and found to be reliably the real country at any meaningful volume —
# "DE" was consistently Germany, not Delaware — so only "CA" needs this.
_UNRELIABLE_COUNTRY_ISO = {"CA"}

# Case-sensitive on purpose: "us"/"usa" lowercase collide with the pronoun/word
# ("join us", "usable") far too often to match case-insensitively.
_US_ABBR_RE = re.compile(r"\bUSA\b|\bUS\b|\bU\.S\.A\.(?![A-Za-z])|\bU\.S\.(?![A-Za-z])")
_UNITED_STATES_RE = re.compile(r"\bUnited States\b", re.IGNORECASE)
_NORTH_AMERICA_RE = re.compile(r"\bNorth America\b", re.IGNORECASE)


_US_STATES: dict[str, str] = load_config_json("us_states.json")  # abbr -> full name
_US_CITIES: dict[str, str] = load_config_json("us_cities.json")  # city/alias -> state abbr
_FOREIGN_MARKERS: list[str] = load_config_json("foreign_markers.json")

_STATE_FULLNAME_RE = build_word_boundary_alternation(list(_US_STATES.values()))
_CITY_RE = build_word_boundary_alternation(list(_US_CITIES.keys()))
_FOREIGN_RE = build_word_boundary_alternation(_FOREIGN_MARKERS)


def _match_state_abbr(text: str) -> bool:
    for abbr in _US_STATES:
        if abbr in _COLLISION_ABBRS:
            pattern = r",\s*" + abbr + r"\b"
        else:
            pattern = r"(?:,\s*|\b)" + abbr + r"\b"
        if re.search(pattern, text):
            return True
    return False


def _has_us_marker(text: str) -> bool:
    return bool(_US_ABBR_RE.search(text) or _UNITED_STATES_RE.search(text))


def _clean_str(value: object) -> str:
    # Values sourced from a pandas DataFrame use NaN (a float), not None,
    # for missing data — pd.isna() handles both uniformly.
    if value is None or pd.isna(value):
        return ""
    return str(value).strip()


def classify_location(
    location: str | None, country_iso: str | None, is_remote: bool | None
) -> tuple[LocationFlag, str]:
    iso = _clean_str(country_iso).upper()
    if iso and iso not in _UNRELIABLE_COUNTRY_ISO:
        if iso == "US":
            return LocationFlag.ACCEPTED, "country_iso"
        return LocationFlag.REJECTED, "country_iso_foreign"

    loc = _clean_str(location)
    if not loc:
        return LocationFlag.UNCERTAIN, "no_location_data"

    is_remote = False if (is_remote is None or pd.isna(is_remote)) else bool(is_remote)

    # Foreign veto must run before state matching (scope.md §4.1 step 2) —
    # only a strong US-country marker (step 3) can override it; a state
    # abbreviation is too ambiguous (e.g. "CA" in "Ontario, CA" could mean
    # California or Canada) to be trusted at this stage.
    if _FOREIGN_RE.search(loc) and not (
        _has_us_marker(loc) or (is_remote and _NORTH_AMERICA_RE.search(loc))
    ):
        return LocationFlag.REJECTED, "foreign_country_marker"

    if _has_us_marker(loc):
        return LocationFlag.ACCEPTED, "us_marker"
    if is_remote and _NORTH_AMERICA_RE.search(loc):
        return LocationFlag.ACCEPTED, "us_marker_remote_na"

    if _match_state_abbr(loc) or _STATE_FULLNAME_RE.search(loc):
        return LocationFlag.ACCEPTED, "state_match"

    if _CITY_RE.search(loc):
        return LocationFlag.ACCEPTED, "city_match"

    if _BARE_REMOTE_RE.match(loc):
        return LocationFlag.UNCERTAIN, "bare_remote"

    return LocationFlag.UNCERTAIN, "unresolved"


def filter_locations(df: pd.DataFrame) -> pd.DataFrame:
    results = df.apply(
        lambda row: classify_location(
            row.get("location"), row.get("country_iso"), row.get("is_remote")
        ),
        axis=1,
    )
    df = df.copy()
    df["location_flag"] = [r[0].value for r in results]
    df["location_reason"] = [r[1] for r in results]

    total = len(df)
    global_ids = df["global_id"] if "global_id" in df.columns else ["unknown"] * total
    for global_id, flag, reason in zip(
        global_ids, df["location_flag"], df["location_reason"], strict=True
    ):
        logger.debug("location: %s -> %s (%s)", global_id, flag, reason)

    counts = df["location_flag"].value_counts().to_dict()
    logger.info("location filter: %s (total=%d)", counts, total)

    uncertain_pct = (counts.get(LocationFlag.UNCERTAIN.value, 0) / total * 100) if total else 0
    if uncertain_pct > 15:
        logger.warning(
            "location filter: %.1f%% of batch is UNCERTAIN — possible lexicon gap", uncertain_pct
        )

    return df
