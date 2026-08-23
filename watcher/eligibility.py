"""Watcher-specific eligibility derived from scored jobs."""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass
from typing import Mapping

from internship_signal.domain.eligibility import CATEGORICAL_EXCLUSION_REASONS

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
    "technical_product",
    # Deliberate low-priority exceptions: visible, but fit-scored around 20.
    "it_support",
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
_US_STRUCTURED_COUNTRY_CODES = frozenset(
    {"AS", "ASM", "GU", "GUM", "MP", "MNP", "PR", "PRI", "UM", "UMI", "US", "USA", "VI", "VIR"}
)
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
chile
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
_ISO_ALPHA3_CODES = frozenset(
    """
ABW AFG AGO AIA ALA ALB AND ARE ARG ARM ASM ATA ATF ATG AUS AUT AZE BDI BEL
BEN BES BFA BGD BGR BHR BHS BIH BLM BLR BLZ BMU BOL BRA BRB BRN BTN BVT BWA
CAF CAN CCK CHE CHL CHN CIV CMR COD COG COK COL COM CPV CRI CUB CUW CXR CYM
CYP CZE DEU DJI DMA DNK DOM DZA ECU EGY ERI ESH ESP EST ETH FIN FJI FLK FRA
FRO FSM GAB GBR GEO GGY GHA GIB GIN GLP GMB GNB GNQ GRC GRD GRL GTM GUF GUM
GUY HKG HMD HND HRV HTI HUN IDN IMN IND IOT IRL IRN IRQ ISL ISR ITA JAM JEY
JOR JPN KAZ KEN KGZ KHM KIR KNA KOR KWT LAO LBN LBR LBY LCA LIE LKA LSO LTU
LUX LVA MAC MAF MAR MCO MDA MDG MDV MEX MHL MKD MLI MLT MMR MNE MNG MNP MOZ
MRT MSR MTQ MUS MWI MYS MYT NAM NCL NER NFK NGA NIC NIU NLD NOR NPL NRU NZL
OMN PAK PAN PCN PER PHL PLW PNG POL PRI PRK PRT PRY PSE PYF QAT REU ROU RUS
RWA SAU SDN SEN SGP SGS SHN SJM SLB SLE SLV SMR SOM SPM SRB SSD STP SUR SVK
SVN SWE SWZ SXM SYC SYR TCA TCD TGO THA TJK TKL TKM TLS TON TTO TUN TUR TUV
TWN TZA UGA UKR UMI URY USA UZB VAT VCT VEN VGB VIR VNM VUT WLF WSM YEM ZAF
ZMB ZWE
""".split()
)
_COUNTRY_KEYS = frozenset(
    {
        "addresscountry",
        "country",
        "countrycode",
        "countryname",
    }
)
_LOCATION_CONTAINER_KEYS = frozenset(
    {
        "joblocation",
        "joblocations",
        "location",
        "locationdata",
        "locations",
        "normalizedlocation",
        "normalizedlocations",
        "primarylocation",
        "rawlocation",
        "rawlocations",
        "remotelocation",
        "remotelocations",
        "remotestatus",
        "workplacelocation",
        "workplacelocations",
    }
)
_LOCATION_TEXT_KEYS = frozenset(
    {
        "address",
        "city",
        "displayname",
        "formatted",
        "label",
        "name",
        "region",
        "regionname",
    }
)
_METADATA_CONTAINER_KEYS = frozenset(
    {
        "extra",
        "jobmetadata",
        "metadata",
        "postingmetadata",
        "sourcedetails",
        "sourcemetadata",
    }
)
_DESCRIPTION_LOCATION_CONTEXT_RE = re.compile(
    r"\b(?:based|located|location|office|offices|on site|onsite|team in|work in)\b"
)
_DESCRIPTION_SPLIT_RE = re.compile(r"[\n.!?;]+")
_US_DESCRIPTION_ALIASES = _US_COUNTRY_ALIASES - {"us"}


@dataclass(frozen=True)
class LocationDecision:
    """Conservative U.S.-location decision for the watcher eligibility gate."""

    status: str
    reason: str | None
    explanation: str


@dataclass(frozen=True)
class _LocationEvidence:
    source: str
    text_values: tuple[str, ...] = ()
    structured_countries: tuple[str, ...] = ()
    allow_bare_us: bool = True


@dataclass(frozen=True)
class _EvidenceMatch:
    status: str
    source: str
    value: str


def assess_us_location(job: Mapping[str, object]) -> LocationDecision:
    """Classify only reliable country evidence; ambiguous locations pass.

    Structured country values take precedence over text within the same
    location object. Any separate explicit U.S. option still wins for a
    multi-location role. State abbreviations are intentionally ignored because
    they collide with foreign regions such as Madrid, MD and Schiphol, NH.
    """

    evidence: list[_LocationEvidence] = []
    _collect_metadata_evidence(job, "", evidence, recurse=False)
    evidence.extend(_description_location_evidence(job.get("description")))

    matches = [
        match
        for item in evidence
        if (match := _classify_evidence(item)) is not None
    ]
    us_matches = [match for match in matches if match.status == LOCATION_US]
    foreign_matches = [match for match in matches if match.status == OUTSIDE_US]

    if us_matches:
        detail = _format_evidence_match(us_matches[0])
        explanation = (
            f"At least one explicit United States location is available ({detail}); "
            "other non-U.S. evidence does not override it."
            if foreign_matches
            else f"Explicit United States location evidence found ({detail})."
        )
        return LocationDecision(LOCATION_US, None, explanation)
    if foreign_matches:
        detail = _format_evidence_match(foreign_matches[0])
        return LocationDecision(
            OUTSIDE_US,
            OUTSIDE_US,
            f"Explicit non-United States country or region evidence found ({detail}).",
        )
    return LocationDecision(
        LOCATION_AMBIGUOUS,
        None,
        "Location is missing or does not clearly establish a country; retained for review.",
    )


def determine_watcher_eligibility(
    job: dict,
    target_roles: set[str] | frozenset[str] = DEFAULT_TARGET_ROLES,
    *,
    apply_student_restrictions: bool = True,
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
    student = job.get("student_eligibility") or score.get("student_eligibility") or {}
    if not isinstance(student, Mapping):
        student = {}
    raw_student_reason = student.get("exclusion_reason")
    student_reason = (
        raw_student_reason
        if apply_student_restrictions
        and raw_student_reason in CATEGORICAL_EXCLUSION_REASONS
        else None
    )
    student_fields = {
        "categorical_evaluation_applied": apply_student_restrictions,
        "eligibility_exclusion_reason": (
            str(student_reason) if student_reason in CATEGORICAL_EXCLUSION_REASONS else None
        ),
        "eligibility_evidence_source": (
            student.get("evidence_source")
            if apply_student_restrictions
            else None
        ),
        "eligibility_evidence": (
            student.get("evidence")
            if apply_student_restrictions
            else None
        ),
        "eligibility_explanation": (
            student.get("explanation")
            if apply_student_restrictions
            else (
                "Categorical student restrictions were not evaluated because "
                "the posting is not an open internship or student program."
            )
        ),
        "eligibility_mandatory_language_detected": (
            bool(student.get("mandatory_language_detected"))
            if apply_student_restrictions
            else False
        ),
        "eligibility_negation_detected": (
            bool(student.get("negation_detected"))
            if apply_student_restrictions
            else False
        ),
        "eligibility_mixed_eligibility_detected": (
            bool(student.get("mixed_eligibility_detected"))
            if apply_student_restrictions
            else False
        ),
    }
    location = assess_us_location(job)
    if student_reason in CATEGORICAL_EXCLUSION_REASONS:
        return {
            "watcher_eligible": False,
            "fit_score": 0,
            "eligible_reason": None,
            "ineligible_reason": student_reason,
            "location_status": location.status,
            "location_explanation": location.explanation,
            **student_fields,
        }
    if location.reason == OUTSIDE_US:
        return {
            "watcher_eligible": False,
            "fit_score": 0,
            "eligible_reason": None,
            "ineligible_reason": OUTSIDE_US,
            "location_status": location.status,
            "location_explanation": location.explanation,
            **student_fields,
        }
    degree_eligible = job.get("degree_eligible", score.get("degree_eligible", True))
    if degree_eligible is False and apply_student_restrictions:
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
            **student_fields,
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
            **student_fields,
        }

    reason = score.get("watcher_ineligible_reason")
    if (
        not apply_student_restrictions
        and (
            raw_student_reason in CATEGORICAL_EXCLUSION_REASONS
            or reason in CATEGORICAL_EXCLUSION_REASONS
        )
    ):
        reason = None
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
        **student_fields,
    }


def _collect_metadata_evidence(
    value: object,
    path: str,
    evidence: list[_LocationEvidence],
    *,
    recurse: bool,
) -> None:
    if not isinstance(value, Mapping):
        return

    country_values: list[str] = []
    location_items: list[tuple[str, object]] = []
    for key, nested in value.items():
        token = _key_token(key)
        nested_path = f"{path}.{key}" if path else str(key)
        if token in _COUNTRY_KEYS and nested is not None:
            country_values.extend(_string_values(nested))
        elif token in _LOCATION_CONTAINER_KEYS:
            location_items.append((nested_path, nested))

    if country_values:
        text_values = [
            text
            for _nested_path, nested in location_items
            for text in _location_text_values(nested)
        ]
        evidence.append(
            _LocationEvidence(
                source=path or "job metadata",
                text_values=tuple(text_values),
                structured_countries=tuple(country_values),
            )
        )
    else:
        for nested_path, nested in location_items:
            _append_location_evidence(nested, nested_path, evidence)

    for key, nested in value.items():
        token = _key_token(key)
        if token in _COUNTRY_KEYS or token in _LOCATION_CONTAINER_KEYS:
            continue
        if not isinstance(nested, (Mapping, list, tuple, set, frozenset)):
            continue
        if recurse or token in _METADATA_CONTAINER_KEYS:
            nested_path = f"{path}.{key}" if path else str(key)
            _walk_metadata_value(nested, nested_path, evidence)


def _walk_metadata_value(
    value: object,
    path: str,
    evidence: list[_LocationEvidence],
) -> None:
    if isinstance(value, (list, tuple, set, frozenset)):
        for index, nested in enumerate(value):
            _walk_metadata_value(nested, f"{path}[{index}]", evidence)
    elif isinstance(value, Mapping):
        _collect_metadata_evidence(value, path, evidence, recurse=True)


def _append_location_evidence(
    value: object,
    source: str,
    evidence: list[_LocationEvidence],
) -> None:
    if value is None:
        return
    if isinstance(value, str):
        if value.strip():
            evidence.append(_LocationEvidence(source=source, text_values=(value,)))
        return
    if isinstance(value, Mapping):
        countries: list[str] = []
        texts: list[str] = []
        nested_locations: list[tuple[str, object]] = []
        for key, nested in value.items():
            token = _key_token(key)
            nested_source = f"{source}.{key}"
            if token in _COUNTRY_KEYS and nested is not None:
                countries.extend(_string_values(nested))
            elif token in _LOCATION_TEXT_KEYS:
                texts.extend(_location_text_values(nested))
            elif token in _LOCATION_CONTAINER_KEYS:
                nested_locations.append((nested_source, nested))
        if countries or texts:
            evidence.append(
                _LocationEvidence(
                    source=source,
                    text_values=tuple(texts),
                    structured_countries=tuple(countries),
                )
            )
        for nested_source, nested in nested_locations:
            _append_location_evidence(nested, nested_source, evidence)
        for key, nested in value.items():
            token = _key_token(key)
            if token in _COUNTRY_KEYS | _LOCATION_TEXT_KEYS | _LOCATION_CONTAINER_KEYS:
                continue
            if isinstance(nested, (Mapping, list, tuple, set, frozenset)):
                _walk_metadata_value(nested, f"{source}.{key}", evidence)
        return
    if isinstance(value, (list, tuple, set, frozenset)):
        for index, nested in enumerate(value):
            _append_location_evidence(nested, f"{source}[{index}]", evidence)


def _location_text_values(value: object) -> list[str]:
    if isinstance(value, str):
        return [value] if value.strip() else []
    if isinstance(value, Mapping):
        values: list[str] = []
        for key, nested in value.items():
            if _key_token(key) in _LOCATION_TEXT_KEYS | _LOCATION_CONTAINER_KEYS:
                values.extend(_location_text_values(nested))
        return values
    if isinstance(value, (list, tuple, set, frozenset)):
        return [text for nested in value for text in _location_text_values(nested)]
    return []


def _string_values(value: object) -> list[str]:
    if isinstance(value, str):
        return [value] if value.strip() else []
    if isinstance(value, (list, tuple, set, frozenset)):
        return [text for nested in value for text in _string_values(nested)]
    if value is None or isinstance(value, Mapping):
        return []
    text = str(value).strip()
    return [text] if text else []


def _description_location_evidence(value: object) -> list[_LocationEvidence]:
    if not isinstance(value, str) or not value.strip():
        return []
    evidence: list[_LocationEvidence] = []
    for segment in _DESCRIPTION_SPLIT_RE.split(value):
        normalized = _normalize_location_text(segment)
        if not normalized or not _DESCRIPTION_LOCATION_CONTEXT_RE.search(normalized):
            continue
        has_us = _contains_alias(
            normalized,
            _US_DESCRIPTION_ALIASES | _US_TERRITORY_ALIASES,
        )
        if not has_us and not _contains_foreign_location(normalized):
            continue
        evidence.append(
            _LocationEvidence(
                source="description location context",
                text_values=(segment.strip(),),
                allow_bare_us=False,
            )
        )
    return evidence


def _classify_evidence(evidence: _LocationEvidence) -> _EvidenceMatch | None:
    structured_matches = [
        _EvidenceMatch(status, evidence.source, value)
        for value in evidence.structured_countries
        if (status := _structured_country_status(value)) is not None
    ]
    us_match = next(
        (match for match in structured_matches if match.status == LOCATION_US),
        None,
    )
    if us_match is not None:
        return us_match
    foreign_match = next(
        (match for match in structured_matches if match.status == OUTSIDE_US),
        None,
    )
    if foreign_match is not None:
        return foreign_match

    for value in evidence.text_values:
        status = _text_location_status(value, allow_bare_us=evidence.allow_bare_us)
        if status == LOCATION_US:
            return _EvidenceMatch(status, evidence.source, value)
    for value in evidence.text_values:
        status = _text_location_status(value, allow_bare_us=evidence.allow_bare_us)
        if status == OUTSIDE_US:
            return _EvidenceMatch(status, evidence.source, value)
    return None


def _text_location_status(value: object, *, allow_bare_us: bool) -> str | None:
    normalized = _normalize_location_text(value)
    us_aliases = _US_COUNTRY_ALIASES if allow_bare_us else _US_DESCRIPTION_ALIASES
    if _contains_alias(normalized, us_aliases | _US_TERRITORY_ALIASES):
        return LOCATION_US
    prefix_match = re.match(r"^\s*([A-Za-z]{3})\s*(?:[-\u2013\u2014|:/])", str(value or ""))
    if prefix_match:
        prefix_status = _structured_country_status(prefix_match.group(1))
        if prefix_status is not None:
            return prefix_status
    if _contains_foreign_location(normalized):
        return OUTSIDE_US
    return None


def _format_evidence_match(match: _EvidenceMatch) -> str:
    value = re.sub(r"\s+", " ", match.value).strip()
    if len(value) > 140:
        if match.source == "description location context":
            value = "..." + value[-137:].lstrip()
        else:
            value = value[:137].rstrip() + "..."
    return f'{match.source}: "{value}"'


def _key_token(value: object) -> str:
    return re.sub(r"[^a-z0-9]+", "", str(value or "").casefold())


def _structured_country_status(value: object) -> str | None:
    normalized = _normalize_location_text(value)
    compact = normalized.replace(" ", "").upper()
    if normalized in _US_COUNTRY_ALIASES or normalized in _US_TERRITORY_ALIASES:
        return LOCATION_US
    if compact in _US_STRUCTURED_COUNTRY_CODES:
        return LOCATION_US
    if compact in _ISO_ALPHA2_CODES or compact in _ISO_ALPHA3_CODES:
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
