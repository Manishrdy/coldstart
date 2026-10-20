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

# Foreign city names that are also real US towns, found by scanning every
# location string in the local snapshots (Module 36: 388k distinct) for the
# "City, ST" shape. Only these can be rescued by a bare state abbreviation
# that is also a country code. Before the list existed every foreign city
# could be, and the same shape is exactly how a foreign address names its own
# country: "Jakarta, ID" (390 rows), "Bogota, CO" (160), "Buenos Aires, AR"
# (70), "Casablanca, MA" (59), "Chennai, TN" (41), "Tirana, AL" were all
# ACCEPTED as Idaho, Colorado, Arkansas, Massachusetts, Tennessee, Alabama.
_US_NAMESAKES = frozenset(c.casefold() for c in load_config_json("us_namesake_cities.json"))
assert _US_NAMESAKES <= {c.casefold() for c in _FOREIGN_CITIES}, "namesake not a foreign city"

# US city names that are just as often a foreign place. Before Module 36 a
# bare one both accepted a string and vetoed the foreign signal beside it:
# "Richmond, BC", "Cambridge, ON", "Columbia - Bogotá", "Richmond, Melbourne
# VIC", "Edinburgh, Leeds, Manchester or London" were all ACCEPTED. Now none
# of them can overrule a foreign signal unless its own state is right beside
# it ("Cambridge, MA", "Birmingham AL", "MA-Worcester").
#
# Alone, they split two ways — measured on the operator's own scored jobs as
# well as the corpus:
#   - foreign_leaning: a bare "Cambridge" was Healx and Red Gate (both UK),
#     scored 91 and 0; every bare "Manchester" and "Richmond" scored was UK.
#     Held back.
#   - us_leaning: 298 Workday postings say only "Columbia"; "LSU -
#     Alexandria", "Burlington Hospital". Accepted, as before.
_AMBIGUOUS = load_config_json("ambiguous_us_cities.json")
_FOREIGN_LEANING_CITIES: list[str] = _AMBIGUOUS["foreign_leaning"]
_AMBIGUOUS_CITIES: list[str] = _FOREIGN_LEANING_CITIES + _AMBIGUOUS["us_leaning"]
assert set(_AMBIGUOUS_CITIES) <= set(_US_CITIES), "ambiguous city missing from us_cities.json"
_FOREIGN_LEANING_CITY_RE = build_word_boundary_alternation(_FOREIGN_LEANING_CITIES)
_AMBIGUOUS_CITY_RE = build_word_boundary_alternation(_AMBIGUOUS_CITIES)
_AMBIGUOUS_CITY_ALT = r"(?i:\b(?:" + "|".join(re.escape(c) for c in _AMBIGUOUS_CITIES) + r")\b)"
_STATE_ALT = r"(?:" + "|".join(sorted(_US_STATES)) + r")"
_AMBIGUOUS_CITY_STATE_RE = re.compile(
    # "Columbia SC", "Columbia (MO)", "Cambridge-MA"
    _AMBIGUOUS_CITY_ALT + r"(?:\s+|\s*\(\s*|\s*-\s*)" + _STATE_ALT + r"\b"
    # "MA-Worcester", "IN - Richmond", "AL, Birmingham", "IN-North Manchester"
    + r"|^" + _STATE_ALT + r"\s*[-,]\s*(?:North\s+|South\s+|East\s+|West\s+)?"
    + _AMBIGUOUS_CITY_ALT
)

# build_word_boundary_alternation forces re.IGNORECASE, which is fine for
# "San Francisco" but catastrophic for the 2-3 character aliases: a
# case-insensitive "LA" matches the French "Pays de la Loire", so
# "Nantes, Pays de la Loire, fr" was ACCEPTED with reason "city_match". The
# short aliases are therefore matched case-sensitively, the rest are not.
_SHORT_CITY_ALIASES = [c for c in _US_CITIES if len(c) <= 3]
_CITY_RE = build_word_boundary_alternation(
    [c for c in _US_CITIES if len(c) > 3 and c not in _AMBIGUOUS_CITIES]
)
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

# A no-ISO2-twin abbreviation in a state slot: after a comma or a space, and
# not followed by a short number ("FL 7,8,9,11" is a floor; "NJ 08901" is not).
_US_ONLY_STATE_SLOT_RE = re.compile(
    r"(?:,\s*|\s)(?:" + "|".join(sorted(_US_ONLY_ABBRS)) + r")\b(?!\s*\d{1,4}\b)"
)

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
# The same "XX-City" shape is how Workday-style boards write a US state:
# "NC-Winston-Salem", "IL-Downers Grove", "GA-Marietta", "TN-Murfreesboro".
# Until Module 36 every one of those was REJECTED as New Caledonia, Israel,
# Gabon, Tunisia. Only these prefixes really are split between the two
# readings in the corpus ("DE-Leverkusen", "CO-Barranquilla", "CA-Markham",
# "IN-Gujarat", "ID-Jakarta", "MA-Le Siege"), plus WA, which one Workday
# tenant uses for Western Australia ("WA - Mandurah Area", "WA - Busselton");
# every other state code in that position is the state. It is weak evidence
# all the same — icims writes Israel as "IL-Kefar Sava" and Brazil's Mato
# Grosso as "MT-CUIABA, Mato Grosso, BR" — so it only ever *accepts* once
# every foreign rule has had its say; it never overrules one.
_FOREIGN_LEANING_PREFIXES = frozenset({"CA", "CO", "DE", "ID", "IN", "MA", "WA"})
_US_STATE_PREFIX_RE = re.compile(
    r"^(?:"
    + "|".join(sorted(frozenset(_US_STATES) - _FOREIGN_LEANING_PREFIXES))
    + r")\s*-\s*(?=[A-Z])"  # "NC-Durham", "AL - Columbiana"
)
# "Vienna, VA 22182", "Edmonton, KY 42129": a state followed by a ZIP is a
# US mailing address, whatever city it names. DE is left out — German
# postcodes are five digits too.
_STATE_ZIP_RE = re.compile(
    r"\b(?:" + "|".join(sorted(frozenset(_US_STATES) - {"DE"})) + r")\s+\d{5}(?:-\d{4})?\b"
)

# ISO3 aliases seen in the wild in the country slot ("Birmingham, GBR",
# "Lincoln, Canterbury, NZL, 7674"). Only ever read from that slot. Left out
# on purpose: EST (Eastern time), PER, ARE, FIN — ordinary words or time zones
# a US posting could plausibly end with.
_ISO3_FOREIGN = frozenset({
    "ISR", "DEU", "IND", "CAN", "GBR", "FRA", "ESP", "MEX", "BIH", "AUS",
    "NZL", "BRA", "ARG", "CHN", "JPN", "SGP", "POL", "NLD", "IRL", "CHE", "SWE",
    "ITA", "PRT", "ROU", "BGR", "HUN", "CZE", "PHL", "MYS", "IDN", "THA", "VNM",
    "ZAF", "EGY", "SAU", "TUR", "UKR", "KOR", "TWN", "HKG", "COL", "CHL", "CRI",
    "BEL", "DNK", "AUT", "GRC", "PAK", "BGD", "LKA", "NGA", "KEN", "URY", "SRB",
    "HRV", "SVK", "SVN", "LTU", "LVA",
})

# Two-letter codes that are real ISO2 but also how a US posting abbreviates
# something else in the same slot: North America, Eastern/Pacific time, AM/PM.
_SLOT_LOOKALIKES = frozenset({"NA", "ET", "PT", "AM", "PM"})

# Trailing parts that carry no place name: postcodes in any national format
# ("80.240-031", "113/114"), and the "#", "_", "-----" placeholders some ATSes
# leave in the postcode slot.
_POSTALISH_RE = re.compile(r"^[\d\s.\-/#_]+$")

# Region codes from the three countries that leak through most: Canadian
# provinces, Australian states, Indian states. None of them is a US state
# abbreviation, so "Richmond, BC" or "Bangaluru, KA, IN" cannot be US.
# Deliberately absent: WA (Washington), SA (also how US postings shorten San
# Antonio), AP (the US military's "APO AP" mail code), UP (Michigan's Upper
# Peninsula), HP, HR, OR, TN.
_FOREIGN_REGION_CODES = frozenset(
    code for codes in load_config_json("foreign_region_codes.json").values() for code in codes
)
assert _FOREIGN_REGION_CODES.isdisjoint(_US_STATES), "a foreign region code collides with a state"
_FOREIGN_REGION_PART_RE = re.compile(
    r"^(?:" + "|".join(sorted(_FOREIGN_REGION_CODES, key=len, reverse=True)) + r")"
    r"(?:\s+[A-Z]\d[A-Z]\s*\d[A-Z]\d|\s+[-–]\s.*)?$"  # "ON L4S 0B2", "ON - Birmingham St"
)
# How a multi-location posting separates its entries.
_SEGMENT_RE = re.compile(r"[;|]|\s/\s|\s+or\s+")
# Uppercase tokens that sit in a US region slot without being a state.
_US_REGION_TOKENS = frozenset({"US", "USA", "AFB", "ARB", "NAS", "HQ", "DMV", "NA"})


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


def _has_us_address(text: str) -> bool:
    """A state + ZIP: "Ontario, CA 91764", "Paris, TN 38242", "Vienna, VA
    22182". Before Module 36 a foreign city marker beat these unless a US
    city name happened to sit beside it ("Lincoln Family Medical Center ...
    Edmonton, KY 42129"), and those without one were REJECTED."""
    return _STATE_ZIP_RE.search(text) is not None


def _has_us_city(text: str) -> bool:
    return bool(
        _CITY_RE.search(text)
        or _CITY_ALIAS_RE.search(text)
        or _AMBIGUOUS_CITY_STATE_RE.search(text)
    )


def _has_us_signal(text: str) -> bool:
    """Broad veto for the aggressive signals. Deliberately excludes the
    contested abbreviations — otherwise "Herzliya, HA, IL" vetoes itself."""
    return (
        _has_us_marker(text)
        or _has_us_address(text)
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
        or _has_us_address(text)
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
        while "Stuttgart, AR" is Arkansas — and only for a city on the US
        namesake list, because "Casablanca, MA" is Morocco too.

    Diacritics veto the abbreviation tiers: a US town spelled with non-ASCII
    letters is rare, a foreign city beside an ambiguous code is not. That is
    what keeps "Medellín, CO" (Colombia) rejected while leaving "Cañon City,
    CO" and "Española, NM" alone — neither carries a foreign city marker."""
    if _has_us_marker(text) or _STATE_FULLNAME_RESCUE_RE.search(text) is not None:
        return True
    if _has_us_address(text):
        return True
    if _has_us_city(text):
        return True
    if _has_non_ascii_letter(text):
        return False
    if _match_state_abbr(text, _US_ONLY_ABBRS):
        return True
    return (
        len(_parts(text)) <= 2
        and _match_state_abbr(text, _US_SIGNAL_ABBRS)
        and all(m.casefold() in _US_NAMESAKES for m in _FOREIGN_CITY_RE.findall(text))
    )


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


def _strip_postal(parts: list[str]) -> list[str]:
    core = list(parts)
    while core and (_POSTAL_RE.match(core[-1]) or _POSTALISH_RE.match(core[-1])):
        core.pop()
    return core


def _explicit_foreign_country(parts: list[str]) -> str | None:
    """A foreign country code with no US-state twin, in the country slot:
    "Richardson, GD, CN", "San Jose, CR", "Alexandria, EG", "Birmingham, GBR",
    "Água Verde, PR, BR, 80.240-031".

    Two parts are enough. _trailing_foreign_iso2's 3-part minimum protects
    "San Carlos, CA", and CA is a state twin, which this rule never reads. A
    US city name does not veto it either: every one of the examples above
    was ACCEPTED before Module 36 on the strength of the city alone."""
    core = _strip_postal(parts)
    if len(core) < 2:
        return None
    code = core[-1].upper()
    if not _ISO_SLOT_RE.fullmatch(code) or code in {"US", "USA"} or code in _US_TERRITORIES:
        return None
    if code in _AMBIGUOUS_ISO2 or code in _SLOT_LOOKALIKES:
        return None
    return code if code in _ALL_ISO2 or code in _ISO3_FOREIGN else None


def _region_coded_foreign_country(parts: list[str]) -> str | None:
    """A state-twin code in the country slot with a foreign region code in
    front of it: "Tanger, TNG, MA, 90000" (Morocco, not Massachusetts),
    "DUITAMA, COL, CO" (Colombia), "Bekasi, JB, ID" (Indonesia), "Jamnagar,
    GJ, IN, 361004". A US address never puts an uppercase non-state code in
    the slot before its state.

    Case-sensitive and 3+ parts on purpose: real US towns with 2-3 letter
    names ("Ord, NE", "Ava, MO", "Ajo, AZ") are title case and 2-part."""
    core = _strip_postal(parts)
    if len(core) < 3:
        return None
    code = core[-1].upper()
    if code not in _AMBIGUOUS_ISO2:
        return None
    region = core[-2]
    if not re.fullmatch(r"[A-Z]{1,3}", region):
        return None
    if region in _US_STATES or region in _US_REGION_TOKENS or region in _US_TERRITORIES:
        return None
    return code


def _foreign_region_without_us_option(loc: str) -> bool:
    """A foreign region code, and nothing that makes the posting US.

    The province code is decisive for its own address — "Durham Region, ON"
    and "Hudson's Hope, BC, CA" are Canadian even though Durham and Hudson
    are in us_cities.json — so a US city name only counts when it is not
    the part right beside the code: "Hybrid Atlanta, GA | Hybrid Halifax, NS"
    and "Washington D.C, Sarasota, Miami, Toronto, ON" are open in the US.
    Explicit US evidence ("Remote Employee US, ON, CA", "Edmonton, AB,
    Dallas, TX") counts anywhere."""
    parts = [p for seg in _SEGMENT_RE.split(loc) for p in _parts(seg)]
    hits = [i for i, p in enumerate(parts) if _FOREIGN_REGION_PART_RE.match(p)]
    if len(parts) < 2 or not hits or _has_explicit_us_presence(loc):
        return False
    # The code's own address: the city before it, and a country code or
    # postcode after it ("Hudson's Hope, BC, CA"), never a further city.
    own = {j for i in hits for j in (i - 1, i)}
    own |= {
        i + 1
        for i in hits
        if i + 1 < len(parts)
        and (_ISO_SLOT_RE.fullmatch(parts[i + 1]) or _POSTALISH_RE.match(parts[i + 1]))
    }
    return not any(_has_strong_us_signal(p) for j, p in enumerate(parts) if j not in own)


def _has_explicit_us_presence(text: str) -> bool:
    """Something only a US location says: a US country marker, a state +
    ZIP, or a state abbreviation with no ISO2 twin. The last is what keeps a
    multi-location posting open — "Greensboro, NC; Long Beach, CA; Montreal,
    CAN" is available in Greensboro."""
    return (
        _has_us_marker(text)
        or _has_us_address(text)
        or _has_trailing_us_country(text)
        or _match_state_abbr(text, _US_ONLY_ABBRS)
    )


def _leading_iso_prefix(text: str) -> str | None:
    match = _ISO_PREFIX_RE.match(text)
    if not match:
        return None
    code = match.group(1)
    if code in {"US", "USA"} or code in _US_TERRITORIES or _US_STATE_PREFIX_RE.match(text):
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
        or _has_us_address(loc)
        or _has_trailing_us_country(loc)
        or _STATE_FULLNAME_RESCUE_RE.search(loc) is not None
        or (remote and _NORTH_AMERICA_RE.search(loc))
    )
    if _FOREIGN_COUNTRY_RE.search(loc) and not override:
        # A non-trailing country name beside an abbreviation with no ISO2 twin
        # is a US place ("New Brunswick, NJ"), not a country. The abbreviation
        # has to sit in a state slot: "India, Hyderabad, SEZ-1; FL 7,8,9,11"
        # is a floor number.
        if not (
            not _country_marker_is_trailing(loc)
            and _US_ONLY_STATE_SLOT_RE.search(loc)
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

    code = _explicit_foreign_country(parts)
    if code and not _has_explicit_us_presence(loc):
        return LocationFlag.REJECTED, f"country_code_{code.lower()}"

    if not _has_strong_us_signal(loc):
        code = _trailing_foreign_iso2(parts)
        if code:
            return LocationFlag.REJECTED, f"trailing_iso_{code.lower()}"

        code = _leading_iso_prefix(loc)
        if code:
            return LocationFlag.REJECTED, f"iso_prefix_{code.lower()}"

        if not _has_explicit_us_presence(loc):
            code = _region_coded_foreign_country(parts)
            if code:
                return LocationFlag.REJECTED, f"region_country_{code.lower()}"

    if _foreign_region_without_us_option(loc):
        return LocationFlag.REJECTED, "foreign_region_code"

    if _has_non_ascii_letter(loc) and not _has_us_signal(loc):
        return LocationFlag.REJECTED, "non_ascii_letter"

    if iso == "US":
        return LocationFlag.ACCEPTED, "country_iso"
    if has_us_marker:
        return LocationFlag.ACCEPTED, "us_marker"
    if remote and _NORTH_AMERICA_RE.search(loc):
        return LocationFlag.ACCEPTED, "us_marker_remote_na"

    if (
        _match_state_abbr(loc)
        or _STATE_FULLNAME_RE.search(loc)
        or _has_us_address(loc)
        or _US_STATE_PREFIX_RE.match(loc)
    ):
        return LocationFlag.ACCEPTED, "state_match"

    if _has_us_city(loc):
        return LocationFlag.ACCEPTED, "city_match"

    # "Manchester", "Cambridge (Hybrid)", "Birmingham HQ": held back rather
    # than rejected, because some of them are the US one.
    if _FOREIGN_LEANING_CITY_RE.search(loc):
        return LocationFlag.UNCERTAIN, "ambiguous_city"
    if _AMBIGUOUS_CITY_RE.search(loc):
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
