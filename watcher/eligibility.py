"""Watcher-specific eligibility derived from scored jobs."""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass
from typing import Mapping

DEFAULT_TARGET_ROLES = frozenset({"swe"})
OUTSIDE_US = "outside_us"
LOCATION_US = "us"
LOCATION_AMBIGUOUS = "ambiguous"

SWE_TARGET_TRACKS = {
    "backend",
    "full_stack",
    "frontend",
    "general_swe",
    "platform_infra",
    "data_engineering",
    "ml_ai",
    "quant_dev",
    "cloud",
    "devops",
    "embedded_software",
    "firmware",
    "sdet_qa_automation",
    # Deliberate low-priority exceptions: visible, but fit-scored around 20.
    "it_support",
    "quality_test",
    "solutions_engineering",
}

_US_COUNTRY_ALIASES = frozenset(
    {
        "u s",
        "u s a",
        "united states",
        "united states of america",
        "us",
        "usa",
    }
)
_US_TERRITORY_ALIASES = frozenset(
    {
        "american samoa",
        "guam",
        "northern mariana islands",
        "puerto rico",
        "u s virgin islands",
        "united states virgin islands",
    }
)
_US_STRUCTURED_COUNTRY_CODES = frozenset({"AS", "GU", "MP", "PR", "UM", "US", "USA", "VI"})
_FOREIGN_COUNTRY_ALIASES = frozenset(
    line.strip()
    for line in """
afghanistan
albania
algeria
andorra
angola
antigua and barbuda
argentina
armenia
australia
austria
azerbaijan
bahamas
bahrain
bangladesh
barbados
belarus
belgium
belize
benin
bhutan
bolivia
bosnia and herzegovina
botswana
brazil
brunei
bulgaria
burkina faso
burma
burundi
cabo verde
cambodia
cameroon
canada
cape verde
central african republic
china
colombia
comoros
congo
costa rica
cote d ivoire
croatia
cuba
cyprus
czech republic
czechia
denmark
djibouti
dominica
dominican republic
east timor
ecuador
egypt
el salvador
england
equatorial guinea
eritrea
estonia
eswatini
ethiopia
fiji
finland
france
gabon
gambia
germany
ghana
great britain
greece
grenada
guatemala
guinea
guinea bissau
guyana
haiti
honduras
hong kong
hungary
iceland
india
indonesia
iran
iraq
ireland
israel
italy
ivory coast
jamaica
japan
kazakhstan
kenya
kiribati
kosovo
kuwait
kyrgyzstan
laos
latvia
lesotho
liberia
libya
liechtenstein
lithuania
luxembourg
macau
madagascar
malawi
malaysia
maldives
mali
malta
marshall islands
mauritania
mauritius
mexico
micronesia
moldova
monaco
mongolia
montenegro
morocco
mozambique
myanmar
namibia
nauru
nepal
netherlands
new zealand
nicaragua
niger
nigeria
north korea
north macedonia
northern ireland
norway
oman
pakistan
palau
palestine
panama
papua new guinea
paraguay
peru
philippines
poland
portugal
qatar
romania
russia
russian federation
rwanda
saint kitts and nevis
saint lucia
saint vincent and the grenadines
samoa
san marino
sao tome and principe
saudi arabia
scotland
senegal
serbia
seychelles
sierra leone
singapore
slovakia
slovenia
solomon islands
somalia
south africa
south korea
south sudan
spain
sri lanka
sudan
suriname
swaziland
sweden
switzerland
syria
taiwan
tajikistan
tanzania
thailand
timor leste
togo
tonga
trinidad and tobago
tunisia
turkey
turkiye
turkmenistan
tuvalu
uganda
uk
ukraine
united arab emirates
united kingdom
uruguay
uzbekistan
vanuatu
vatican city
venezuela
vietnam
wales
yemen
zambia
zimbabwe
""".splitlines()
    if line.strip()
)
_FOREIGN_REGION_ALIASES = frozenset(
    {
        "africa",
        "apac",
        "asia",
        "asia pacific",
        "emea",
        "europe",
        "european union",
        "latin america",
        "latam",
        "middle east",
        "oceania",
        "uae",
    }
)
_ISO_ALPHA2_CODES = frozenset(
    """
AD AE AF AG AI AL AM AO AQ AR AS AT AU AW AX AZ BA BB BD BE BF BG BH BI BJ BL
BM BN BO BQ BR BS BT BV BW BY BZ CA CC CD CF CG CH CI CK CL CM CN CO CR CU CV
CW CX CY CZ DE DJ DK DM DO DZ EC EE EG EH ER ES ET FI FJ FK FM FO FR GA GB GD
GE GF GG GH GI GL GM GN GP GQ GR GS GT GU GW GY HK HM HN HR HT HU ID IE IL IM
IN IO IQ IR IS IT JE JM JO JP KE KG KH KI KM KN KP KR KW KY KZ LA LB LC LI LK
LR LS LT LU LV LY MA MC MD ME MF MG MH MK ML MM MN MO MP MQ MR MS MT MU MV MW
MX MY MZ NA NC NE NF NG NI NL NO NP NR NU NZ OM PA PE PF PG PH PK PL PM PN PR
PS PT PW PY QA RE RO RS RU RW SA SB SC SD SE SG SH SI SJ SK SL SM SN SO SR SS
ST SV SX SY SZ TC TD TF TG TH TJ TK TL TM TN TO TR TT TV TW TZ UA UG UM US UY
UZ VA VC VE VG VI VN VU WF WS YE YT ZA ZM ZW
""".split()
)
_COUNTRY_KEYS = frozenset(
    {
        "addresscountry",
        "country",
        "countrycode",
        "country_code",
        "countryname",
        "country_name",
    }
)
_LOCATION_TEXT_KEYS = frozenset(
    {
        "address",
        "displayname",
        "display_name",
        "formatted",
        "label",
        "name",
        "region",
        "regionname",
        "region_name",
    }
)


@dataclass(frozen=True)
class LocationDecision:
    """Conservative U.S.-location decision for the watcher eligibility gate."""

    status: str
    reason: str | None
    explanation: str


def assess_us_location(job: Mapping[str, object]) -> LocationDecision:
    """Classify only explicit country evidence; ambiguous locations pass.

    Any explicit U.S. option wins for multi-location roles. State
    abbreviations are intentionally ignored because they collide with foreign
    regions such as Madrid, MD and Schiphol, NH.
    """

    text_values: list[str] = []
    structured_countries: list[str] = []
    for key in ("location", "locations", "remote_status"):
        _collect_location_value(job.get(key), text_values, structured_countries)
    extra = job.get("extra")
    if isinstance(extra, Mapping):
        for key in ("location", "locations", "remote_location"):
            _collect_location_value(extra.get(key), text_values, structured_countries)

    structured_statuses = [
        status
        for value in structured_countries
        if (status := _structured_country_status(value)) is not None
    ]
    normalized_text = [_normalize_location_text(value) for value in text_values if value]
    us_text = any(_contains_alias(value, _US_COUNTRY_ALIASES | _US_TERRITORY_ALIASES) for value in normalized_text)
    foreign_text = any(_contains_foreign_location(value) for value in normalized_text)

    if LOCATION_US in structured_statuses or us_text:
        explanation = (
            "At least one explicit United States location is available."
            if OUTSIDE_US in structured_statuses or foreign_text
            else "Explicit United States location evidence found."
        )
        return LocationDecision(LOCATION_US, None, explanation)
    if OUTSIDE_US in structured_statuses or foreign_text:
        return LocationDecision(
            OUTSIDE_US,
            OUTSIDE_US,
            "Explicit non-United States country or region evidence found.",
        )
    return LocationDecision(
        LOCATION_AMBIGUOUS,
        None,
        "Location is missing or does not clearly establish a country; retained for review.",
    )


def determine_watcher_eligibility(
    job: dict,
    target_roles: set[str] | frozenset[str] = DEFAULT_TARGET_ROLES,
) -> dict:
    """Return the watcher gate result for a scored job.

    The backend scorer owns the hard role-track decision. This wrapper applies
    the active watcher's target role set so a broad `swe` target can include
    strong software-adjacent tracks such as data engineering, ML, and quant dev.
    """

    score = job.get("score") or {}
    role_cls = job.get("role_classification") or {}
    role = role_cls.get("role")
    role_track = score.get("role_track") or role_cls.get("role_track") or role or "unknown"
    fit_score = _int_score(score.get("fit_score", score.get("total", 0)))
    location = assess_us_location(job)
    if location.reason == OUTSIDE_US:
        return {
            "watcher_eligible": False,
            "fit_score": 0,
            "eligible_reason": None,
            "ineligible_reason": OUTSIDE_US,
            "location_status": location.status,
            "location_explanation": location.explanation,
        }
    degree_eligible = job.get("degree_eligible", score.get("degree_eligible", True))
    if degree_eligible is False:
        reason = (
            job.get("degree_ineligible_reason")
            or score.get("degree_ineligible_reason")
            or "Graduate/PhD-level internship outside undergraduate target."
        )
        return {
            "watcher_eligible": False,
            "fit_score": 0,
            "eligible_reason": None,
            "ineligible_reason": reason,
            "location_status": location.status,
            "location_explanation": location.explanation,
        }
    scorer_eligible = bool(score.get("watcher_eligible", fit_score > 0))

    target_match = role in target_roles
    if "swe" in target_roles and role_track in SWE_TARGET_TRACKS:
        target_match = True

    watcher_eligible = bool(scorer_eligible and target_match and fit_score > 0)
    if watcher_eligible:
        return {
            "watcher_eligible": True,
            "fit_score": fit_score,
            "eligible_reason": score.get("fit_explanation") or f"{role_track} matches watcher target roles.",
            "ineligible_reason": None,
            "location_status": location.status,
            "location_explanation": location.explanation,
        }

    reason = score.get("watcher_ineligible_reason")
    if not reason and not target_match:
        reason = f"{role_track} does not match watcher target roles."
    if not reason:
        reason = "Role is outside the watcher target profile."
    return {
        "watcher_eligible": False,
        "fit_score": 0,
        "eligible_reason": None,
        "ineligible_reason": reason,
        "location_status": location.status,
        "location_explanation": location.explanation,
    }


def _collect_location_value(
    value: object,
    text_values: list[str],
    structured_countries: list[str],
) -> None:
    if value is None:
        return
    if isinstance(value, str):
        if value.strip():
            text_values.append(value)
        return
    if isinstance(value, Mapping):
        for key, nested in value.items():
            normalized_key = str(key).casefold().replace("-", "_")
            if normalized_key in _COUNTRY_KEYS and nested is not None:
                structured_countries.append(str(nested))
            elif normalized_key in _LOCATION_TEXT_KEYS:
                _collect_location_value(nested, text_values, structured_countries)
            elif normalized_key in {"location", "locations"}:
                _collect_location_value(nested, text_values, structured_countries)
        return
    if isinstance(value, (list, tuple, set, frozenset)):
        for nested in value:
            _collect_location_value(nested, text_values, structured_countries)


def _structured_country_status(value: object) -> str | None:
    normalized = _normalize_location_text(value)
    compact = normalized.replace(" ", "").upper()
    if normalized in _US_COUNTRY_ALIASES or normalized in _US_TERRITORY_ALIASES:
        return LOCATION_US
    if compact in _US_STRUCTURED_COUNTRY_CODES:
        return LOCATION_US
    if compact in _ISO_ALPHA2_CODES:
        return OUTSIDE_US
    if _contains_alias(normalized, _FOREIGN_COUNTRY_ALIASES):
        return OUTSIDE_US
    return None


def _contains_foreign_location(normalized: str) -> bool:
    # "New Mexico" without explicit country text is deliberately ambiguous.
    without_us_state = re.sub(r"\bnew mexico\b", " ", normalized)
    return _contains_alias(
        without_us_state,
        _FOREIGN_COUNTRY_ALIASES | _FOREIGN_REGION_ALIASES,
    )


def _contains_alias(normalized: str, aliases: frozenset[str]) -> bool:
    padded = f" {normalized} "
    return any(f" {alias} " in padded for alias in aliases)


def _normalize_location_text(value: object) -> str:
    text = unicodedata.normalize("NFKD", str(value or "").casefold())
    text = "".join(character for character in text if not unicodedata.combining(character))
    return re.sub(r"[^a-z0-9]+", " ", text).strip()


def _int_score(value) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return 0
