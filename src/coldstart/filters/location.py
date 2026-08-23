from __future__ import annotations

import json
import re
import unicodedata

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
_UNRELIABLE_COUNTRY_ISO = {"CA"}

# Case-sensitive on purpose: "us"/"usa" lowercase collide with the pronoun/word
# ("join us", "usable") far too often to match case-insensitively.
_US_ABBR_RE = re.compile(r"\bUSA\b|\bUS\b|\bU\.S\.A\.(?![A-Za-z])|\bU\.S\.(?![A-Za-z])")
_UNITED_STATES_RE = re.compile(r"\bUnited States\b", re.IGNORECASE)
_NORTH_AMERICA_RE = re.compile(r"\bNorth America\b", re.IGNORECASE)


_US_STATES: dict[str, str] = load_config_json("us_states.json")  # abbr -> full name
_US_CITIES: dict[str, str] = load_config_json("us_cities.json")  # city/alias -> state abbr
_FOREIGN_COUNTRIES: list[str] = load_config_json("foreign_countries.json")
_FOREIGN_CITIES: list[str] = load_config_json("foreign_cities.json")
_ALL_ISO2: frozenset[str] = frozenset(load_config_json("foreign_iso2.json"))

_STATE_FULLNAME_RE = build_word_boundary_alternation(list(_US_STATES.values()))

# A country marker is decisive. A city marker is not: dozens of US towns are
# named after foreign cities (Paris TX, Vienna VA, Athens GA, Dublin OH,
# Rome NY, Moscow ID). Splitting the old single foreign_markers.json in two
# (removed in Module 26) is what lets a city marker stand down when the string
# also carries a US state signal — before it, "Vienna, VA" and "Paris, TX"
# were both REJECTED.
_FOREIGN_COUNTRY_RE = build_word_boundary_alternation(_FOREIGN_COUNTRIES)
_FOREIGN_CITY_RE = build_word_boundary_alternation(_FOREIGN_CITIES)

# build_word_boundary_alternation forces re.IGNORECASE, which is fine for
# "San Francisco" but catastrophic for the 2-3 character aliases: a
# case-insensitive "LA" matches the French "Pays de la Loire", so
# "Nantes, Pays de la Loire, fr" was ACCEPTED with reason "city_match". The
# short aliases are therefore matched case-sensitively, the rest are not.
_SHORT_CITY_ALIASES = [c for c in _US_CITIES if len(c) <= 3]
_CITY_RE = build_word_boundary_alternation([c for c in _US_CITIES if len(c) > 3])
_CITY_ALIAS_RE = re.compile(
    r"\b(?:"
    + "|".join(sorted((re.escape(c) for c in _SHORT_CITY_ALIASES), key=len, reverse=True))
    + r")\b"
)

# Derived, never hand-typed: omitting a single code here (GA, IN) silently
# reinstates the bug class this module exists to fix. PR is a US territory
# that also holds an ISO2 code, so it is a US signal, not a foreign one.
_AMBIGUOUS_ISO2 = frozenset(_US_STATES) & _ALL_ISO2 - {"PR"}

# The ambiguous codes where the foreign country appears at real volume *and*
# often without a recognisable city name attached. For every other ambiguous
# code the US state reading is overwhelmingly correct in job postings — there
# are no Vatican (VA), Gabon (GA), Moldova (MD) or Seychelles (SC) software
# jobs in this corpus, but there are thousands in Virginia, Georgia, Maryland
# and South Carolina. AR/CO/ID/MA stay out deliberately: Bentonville AR,
# Denver CO, Boise ID and Boston MA dwarf their country twins, and Buenos
# Aires / Bogota / Jakarta / Rabat are caught by the city lexicon instead.
_CONTESTED_ABBRS = frozenset({"CA", "DE", "IN", "IL"})

# State abbreviations with no ISO2 twin at all — the strongest abbreviation-
# level US evidence available.
_US_ONLY_ABBRS = frozenset(_US_STATES) - _ALL_ISO2

# US territories carry ISO2 codes but are US locations, not foreign ones.
_US_TERRITORIES = frozenset({"PR", "VI", "GU", "AS", "MP", "UM"})
_US_SIGNAL_ABBRS = frozenset(_US_STATES) - _CONTESTED_ABBRS

# "Georgia" is both a US state and a country. Keeping it as a US signal is the
# right trade (Atlanta volume dwarfs Tbilisi), but it must not be able to
# *rescue* a foreign city marker, or "Tbilisi, Georgia" would be accepted.
_UNAMBIGUOUS_STATE_FULLNAMES = [n for n in _US_STATES.values() if n != "Georgia"]
_STATE_FULLNAME_RESCUE_RE = build_word_boundary_alternation(_UNAMBIGUOUS_STATE_FULLNAMES)

assert "GA" in _AMBIGUOUS_ISO2, "ISO2 lexicon lost a US-state collision"
assert "PR" not in _AMBIGUOUS_ISO2, "Puerto Rico must stay a US signal"
assert "TX" in _US_SIGNAL_ABBRS and "DE" not in _US_SIGNAL_ABBRS

_SPLIT_RE = re.compile(r"[,;|]")
_ISO_SLOT_RE = re.compile(r"^[A-Za-z]{2,3}$")
# A foreign address puts a short region code before the country ("Herzliya,
# HA, IL"); a US address that reached three parts did so via a street, so its
# middle slot is a city name ("... 777 Hemlock St, Macon, GA").
_SHORT_REGION_RE = re.compile(r"^[A-Za-z0-9]{1,3}$")
_US_ZIP_RE = re.compile(r"^\d{5}(?:-\d{4})?$")
_POSTAL_RE = re.compile(
    r"^(?:"
    r"\d{4,6}(?:-\d{3,4})?"          # US 5-digit, DE/PL/ES/IT 4-6 digit
    r"|[A-Z]\d[A-Z]\s*\d[A-Z]\d"     # Canada  M1L 4S2
    r"|[A-Z]{1,2}\d{1,2}[A-Z]?\s*\d[A-Z]{2}"  # UK  OX14 4RW
    r"|\d{4}\s?[A-Z]{2}"             # Netherlands  2597 AK
    r")$"
)
# "RS-Belgrade", "RO-Cluj-Napoca", "BIH-Tuzla". Anchored, case-sensitive, and
# only when a capital follows, so "US-Remote" and "Winston-Salem" are untouched.
_ISO_PREFIX_RE = re.compile(r"^([A-Z]{2,3})-(?=[A-Z])")

# ISO3 aliases seen in the wild in the country slot.
_ISO3_FOREIGN = frozenset({"ISR", "DEU", "IND", "CAN", "GBR", "FRA", "ESP", "MEX", "BIH", "AUS"})


def _parts(text: str) -> list[str]:
    return [p.strip() for p in _SPLIT_RE.split(text) if p.strip()]


def _to_bool(value: object) -> bool:
    """`bool("false")` is True, and phenom.parquet stores is_remote as the
    strings 'false'/'true' (1,050 rows). Every other slice is either a real
    bool or entirely null, so a plain bool() cast silently marked those rows
    remote."""
    if value is None:
        return False
    try:
        if pd.isna(value):
            return False
    except (TypeError, ValueError):
        pass
    if isinstance(value, str):
        return value.strip().lower() not in {"", "false", "0", "no", "f", "n", "none", "null"}
    return bool(value)


def _match_state_abbr(text: str, abbrs: frozenset[str] | None = None) -> bool:
    for abbr in _US_STATES if abbrs is None else abbrs:
        if abbr in _COLLISION_ABBRS:
            pattern = r",\s*" + abbr + r"\b"
        else:
            pattern = r"(?:,\s*|\b)" + abbr + r"\b"
        if re.search(pattern, text):
            return True
    return False


def _has_us_marker(text: str) -> bool:
    return bool(_US_ABBR_RE.search(text) or _UNITED_STATES_RE.search(text))


def _has_us_city(text: str) -> bool:
    return bool(_CITY_RE.search(text) or _CITY_ALIAS_RE.search(text))


def _has_us_signal(text: str) -> bool:
    """Broad veto for the aggressive signals. Deliberately excludes the
    contested abbreviations — otherwise "Herzliya, HA, IL" vetoes itself."""
    return (
        _has_us_marker(text)
        or _STATE_FULLNAME_RE.search(text) is not None
        or _has_us_city(text)
        or _match_state_abbr(text, _US_SIGNAL_ABBRS)
    )


def _has_strong_us_signal(text: str) -> bool:
    """Narrow veto for the trailing-ISO rule. A bare state abbreviation cannot
    count here: "Bhilai, CT, in" is Chhattisgarh, India, and treating CT as
    Connecticut vetoes the very rule meant to catch it."""
    return (
        _has_us_marker(text)
        or _STATE_FULLNAME_RE.search(text) is not None
        or _has_us_city(text)
    )


def _has_us_rescue_signal(text: str) -> bool:
    """What it takes for a foreign *city* marker to stand down.

    Three tiers, loosest evidence last:
      - a US country marker or an unambiguous state name always wins;
      - so does a recognised US city ("1737 NE Alberta Street, Portland, OR");
      - a bare state abbreviation is weaker. One with no ISO2 twin at all
        (NY, TX, OH) counts anywhere, which rescues "Jamaica Ave, Jamaica, NY".
        One that merely isn't contested (AR, GA, VA) counts only in a simple
        two-part "City, ST" string, because "Casablanca, CAS, MA" is Morocco
        while "Stuttgart, AR" is Arkansas.

    Diacritics veto the abbreviation tiers: a US town spelled with non-ASCII
    letters is rare, a foreign city beside an ambiguous code is not. That is
    what keeps "Medellín, CO" (Colombia) rejected while leaving "Cañon City,
    CO" and "Española, NM" alone — neither carries a foreign city marker."""
    if _has_us_marker(text) or _STATE_FULLNAME_RESCUE_RE.search(text) is not None:
        return True
    if _has_us_city(text):
        return True
    if _has_non_ascii_letter(text):
        return False
    if _match_state_abbr(text, _US_ONLY_ABBRS):
        return True
    return len(_parts(text)) <= 2 and _match_state_abbr(text, _US_SIGNAL_ABBRS)


def _has_non_ascii_letter(text: str) -> bool:
    """Category-based on purpose. Testing `ord(c) > 127` also fires on the
    en-dash in "Remote – California Bay Area" and "Location:Carlsbad –
    California, USA", both of which are US jobs."""
    return any(ord(c) > 127 and unicodedata.category(c).startswith("L") for c in text)


def _external_path_slug(raw: object) -> str:
    """Workday puts the real city in raw.externalPath even when `location` is
    empty or the literal string "2 Locations" — 558 of the uncertain jobs in
    one run, every single one of which had a usable slug. Note raw arrives as
    a JSON *string* here, not a dict.

    "/job/Xiamen-Fujian-China/Junior-Software-Engineer_JR00043012" -> "Xiamen Fujian China"
    """
    if not isinstance(raw, str) or not raw.startswith("{"):
        return ""
    try:
        payload = json.loads(raw)
    except (ValueError, TypeError):
        return ""
    if not isinstance(payload, dict):
        return ""
    path = payload.get("externalPath")
    if not isinstance(path, str):
        return ""
    segments = [s for s in path.split("/") if s]
    if len(segments) < 2:
        return ""
    return segments[1].replace("-", " ").strip()


def _postal_anchored_country(parts: list[str]) -> str | None:
    """In "City, Region, ISO, Postal" / "City, ISO, Postal" the token before a
    postal code is the country. Gated on the ISO2 lexicon because the same
    shape is used by US data with a state there — "Fate, TX, 75189",
    "Enfield, CT, 06082" — and an ungated rule rejects those outright."""
    if len(parts) < 2 or not _POSTAL_RE.match(parts[-1]):
        return None
    code = parts[-2].upper()
    if not _ISO_SLOT_RE.fullmatch(code) or code in {"US", "USA"} or code in _US_TERRITORIES:
        return None
    if code == "CA":
        # Canadian postcodes are always alphanumeric (M1L 4S2); a bare 5-digit
        # ZIP in this slot means California. Checked before the ambiguous
        # branch below, which would otherwise swallow CA entirely.
        return None if _US_ZIP_RE.match(parts[-1]) else "CA"
    if code in _AMBIGUOUS_ISO2:
        # Measured: DE occupies this slot 19,064 times with zero Delaware
        # cities alongside it, matching the long-standing note that DE is
        # reliably Germany. Every other ambiguous code skews US here, so the
        # state reading wins ("Fate, TX, 75189", "Madison, IN, 47250").
        return "DE" if code == "DE" else None
    return code if code in _ALL_ISO2 or code in _ISO3_FOREIGN else None


def _trailing_foreign_iso2(parts: list[str]) -> str | None:
    """Last-slot country code, for the "City, Region, ISO" shape with no
    postal. Requires >= 3 parts: "San Carlos, CA" and "Somerville, MA" are US
    cities absent from us_cities.json, and a 2-part rule deletes them."""
    if len(parts) < 3:
        return None
    code = parts[-1].upper()
    if not _ISO_SLOT_RE.fullmatch(code) or code in {"US", "USA"} or code in _US_TERRITORIES:
        return None
    if code not in _ALL_ISO2 and code not in _ISO3_FOREIGN:
        return None
    if code in _AMBIGUOUS_ISO2:
        # Only the contested codes can outrank the US-state reading here. For
        # CO/MA/ID/PA/VA/GA/AL/MO/MD/KY the state is right essentially always:
        # an ungated rule read "Longmont, CO" as Colombia and "Macon, GA" as
        # Gabon. The short-region guard is a second gate on top of that.
        if code not in _CONTESTED_ABBRS or not _SHORT_REGION_RE.fullmatch(parts[-2]):
            return None
    return code


def _leading_iso_prefix(text: str) -> str | None:
    match = _ISO_PREFIX_RE.match(text)
    if not match:
        return None
    code = match.group(1)
    if code in {"US", "USA"} or code in _US_TERRITORIES:
        return None
    return code if code in _ALL_ISO2 or code in _ISO3_FOREIGN else None


def _has_trailing_us_country(loc: str) -> bool:
    """Is the final comma-part a bare "us"/"usa"?

    _US_ABBR_RE is case-sensitive because lowercase "us" collides with the
    pronoun ("join us", "about us") far too often to match safely. That
    collision cannot happen in the country slot: nobody writes "join us" as a
    standalone trailing comma-part of a location. Restricting the
    case-insensitive read to that one position rescues 928 rows measured
    across the corpus — "Vienna, VA, us", "Warsaw, IN, us", "Ontario, CA, us",
    "Melbourne, FL, us" — every one of them plainly a US job.

    Deliberately NOT folded into _has_us_marker: that would also veto the
    non-ASCII rule, and "Wilhelmstraße 118, us" is a Berlin address that
    upstream mislabels as US."""
    parts = _parts(loc)
    return bool(parts) and parts[-1].strip().lower() in {"us", "usa"}


def _country_marker_is_trailing(loc: str) -> bool:
    """Is the country name the *last* comma-part?

    "Perth, WA, Australia" and "Bhubaneswar, OR, India" name a real country in
    the final slot. "New Brunswick, NJ", "Ontario, OR", "Nederland, TX" and
    "1737 NE Alberta Street, Portland, OR" only collide with one earlier in the
    string. Position is what separates the two, and it is the difference
    between rejecting ~30 Australian rows and keeping ~40 New Jersey ones."""
    parts = _parts(loc)
    if not parts:
        return False
    return _FOREIGN_COUNTRY_RE.search(parts[-1]) is not None


def _foreign_marker_fires(loc: str, remote: bool) -> bool:
    # A spelled-out, unambiguous state name outranks even a country marker:
    # "Ontario, California" is a real US city, while "Toronto, Ontario" is not.
    # Canadian provinces outnumber Ontario-California ~19:1 in this corpus, so
    # provinces stay in the country list and only the full state name rescues.
    override = (
        _has_us_marker(loc)
        or _has_trailing_us_country(loc)
        or _STATE_FULLNAME_RESCUE_RE.search(loc) is not None
        or (remote and _NORTH_AMERICA_RE.search(loc))
    )
    if _FOREIGN_COUNTRY_RE.search(loc) and not override:
        # A non-trailing country name beside an abbreviation with no ISO2 twin
        # is a US place ("New Brunswick, NJ"), not a country.
        if not (
            not _country_marker_is_trailing(loc)
            and _match_state_abbr(loc, _US_ONLY_ABBRS)
        ):
            return True
    if _FOREIGN_CITY_RE.search(loc) and not (override or _has_us_rescue_signal(loc)):
        return True
    return False


def classify_location(
    location: str | None,
    country_iso: str | None,
    is_remote: bool | None,
    raw: object = None,
) -> tuple[LocationFlag, str]:
    iso = _clean_str(country_iso).upper()
    if iso and iso != "US" and iso not in _UNRELIABLE_COUNTRY_ISO:
        return LocationFlag.REJECTED, "country_iso_foreign"

    loc = _clean_str(location)
    remote = _to_bool(is_remote)
    # Workday's own location field is frequently empty or the literal string
    # "2 Locations"; raw.externalPath carries the real city either way.
    slug = _external_path_slug(raw)

    if not loc:
        if slug:
            loc = slug
        elif iso == "US":
            return LocationFlag.ACCEPTED, "country_iso"
        else:
            return LocationFlag.UNCERTAIN, "no_location_data"

    has_us_marker = _has_us_marker(loc)

    # Foreign signals run before the acceptance paths — including before the
    # country_iso == "US" shortcut, because upstream stamps country_iso='US'
    # on "Wilhelmstraße 118" (Berlin) and "Fabryczna 20A" (Wrocław) — and
    # before state-abbr matching, because the entire bug class being fixed
    # here is a foreign ISO code read as a US state ("München, BY, DE, 80809"
    # matching Delaware, "Herzliya, HA, IL" matching Illinois).
    if slug and not _has_us_signal(loc) and not _has_us_marker(slug):
        if _FOREIGN_COUNTRY_RE.search(slug) or _FOREIGN_CITY_RE.search(slug):
            return LocationFlag.REJECTED, "workday_path_foreign"

    if _foreign_marker_fires(loc, remote):
        return LocationFlag.REJECTED, "foreign_country_marker"

    parts = _parts(loc)

    code = _postal_anchored_country(parts)
    if code:
        return LocationFlag.REJECTED, f"postal_country_{code.lower()}"

    if not _has_strong_us_signal(loc):
        code = _trailing_foreign_iso2(parts)
        if code:
            return LocationFlag.REJECTED, f"trailing_iso_{code.lower()}"

        code = _leading_iso_prefix(loc)
        if code:
            return LocationFlag.REJECTED, f"iso_prefix_{code.lower()}"

    if _has_non_ascii_letter(loc) and not _has_us_signal(loc):
        return LocationFlag.REJECTED, "non_ascii_letter"

    if iso == "US":
        return LocationFlag.ACCEPTED, "country_iso"
    if has_us_marker:
        return LocationFlag.ACCEPTED, "us_marker"
    if remote and _NORTH_AMERICA_RE.search(loc):
        return LocationFlag.ACCEPTED, "us_marker_remote_na"

    if _match_state_abbr(loc) or _STATE_FULLNAME_RE.search(loc):
        return LocationFlag.ACCEPTED, "state_match"

    if _has_us_city(loc):
        return LocationFlag.ACCEPTED, "city_match"

    if _BARE_REMOTE_RE.match(loc):
        return LocationFlag.UNCERTAIN, "bare_remote"

    return LocationFlag.UNCERTAIN, "unresolved"


def _clean_str(value: object) -> str:
    # Values sourced from a pandas DataFrame use NaN (a float), not None,
    # for missing data — pd.isna() handles both uniformly.
    if value is None:
        return ""
    try:
        if pd.isna(value):
            return ""
    except (TypeError, ValueError):
        pass
    return str(value).strip()


def filter_locations(df: pd.DataFrame) -> pd.DataFrame:
    results = df.apply(
        lambda row: classify_location(
            row.get("location"),
            row.get("country_iso"),
            row.get("is_remote"),
            row.get("raw"),
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
    # Under default-deny, REJECTED is thrown away outright, so a rule that
    # starts eating a whole slice needs to announce itself rather than quietly
    # emptying the pipeline.
    rejected_pct = (counts.get(LocationFlag.REJECTED.value, 0) / total * 100) if total else 0
    if rejected_pct > 90:
        logger.warning(
            "location filter: %.1f%% of batch REJECTED — check for an over-broad rule", rejected_pct
        )

    return df
