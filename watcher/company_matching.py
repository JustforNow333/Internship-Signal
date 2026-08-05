"""Exact normalization for watchlist company-label matching only."""

from __future__ import annotations

import re
import unicodedata
from typing import Iterable, Protocol, TypeVar


LEGAL_ENTITY_SUFFIXES = frozenset(
    {
        "inc",
        "incorporated",
        "corp",
        "corporation",
        "company",
        "co",
        "llc",
        "llp",
        "lp",
        "ltd",
        "limited",
        "plc",
        "gmbh",
        "ag",
        "nv",
        "bv",
        "sa",
        "pte",
        "pty",
    }
)

_TRANSLITERATION = str.maketrans(
    {
        "Ø": "O",
        "ø": "o",
        "Ł": "L",
        "ł": "l",
        "Æ": "AE",
        "æ": "ae",
        "Œ": "OE",
        "œ": "oe",
        "Ð": "D",
        "ð": "d",
        "Þ": "TH",
        "þ": "th",
    }
)
_CAMEL_ACRONYM_BOUNDARY = re.compile(r"(?<=[A-Z])(?=[A-Z][a-z])")
_CAMEL_WORD_BOUNDARY = re.compile(r"(?<=[a-z0-9])(?=[A-Z])")
_CAMEL_CASED_LEGAL_SUFFIX = re.compile(r"\bGmbH\b", re.IGNORECASE)


class CompanyConfigLike(Protocol):
    name: str
    aliases: Iterable[str]


CompanyT = TypeVar("CompanyT", bound=CompanyConfigLike)


def company_matching_key(value: object) -> str:
    """Return a conservative exact-match key for a company label.

    This function is deliberately separate from backend company normalization:
    it is used only to map external labels onto configured watchlist companies.
    Meaningful descriptive words remain part of the key, while only allowlisted
    terminal legal-entity suffixes are discarded.
    """

    text = str(value or "")
    text = _CAMEL_CASED_LEGAL_SUFFIX.sub(lambda match: match.group(0).casefold(), text)
    text = _CAMEL_ACRONYM_BOUNDARY.sub(" ", text)
    text = _CAMEL_WORD_BOUNDARY.sub(" ", text)
    text = text.translate(_TRANSLITERATION).replace("&", " and ")
    decomposed = unicodedata.normalize("NFKD", text)

    normalized_characters: list[str] = []
    for character in decomposed:
        if unicodedata.category(character) == "Mn":
            continue
        normalized_characters.append(character if character.isalnum() else " ")

    tokens = "".join(normalized_characters).casefold().split()
    removed_suffix = False
    while tokens and tokens[-1] in LEGAL_ENTITY_SUFFIXES:
        tokens.pop()
        removed_suffix = True
    if removed_suffix and tokens and tokens[-1] == "and":
        tokens.pop()
    return " ".join(tokens)


def company_matches(source_company: object, company: CompanyConfigLike) -> bool:
    """Return whether a feed label exactly claims a canonical name or alias."""

    source_key = company_matching_key(source_company)
    if not source_key:
        return False
    return source_key in {
        company_matching_key(label)
        for label in (company.name, *tuple(company.aliases))
    }


def match_watchlist_company(
    source_company: object,
    companies: Iterable[CompanyT],
) -> CompanyT | None:
    """Return the sole exact watchlist match, or ``None`` when unmatched.

    Configuration validation prevents ambiguous claims. The defensive runtime
    check still refuses to choose a first match if an invalid injected company
    collection contains an ambiguity.
    """

    matches = [
        company
        for company in companies
        if company_matches(source_company, company)
    ]
    return matches[0] if len(matches) == 1 else None


def matched_company_label(
    source_company: object,
    company: CompanyConfigLike,
) -> str | None:
    """Return the canonical name or alias responsible for an exact match."""

    source_key = company_matching_key(source_company)
    if not source_key:
        return None
    for label in (company.name, *tuple(company.aliases)):
        if company_matching_key(label) == source_key:
            return label
    return None
