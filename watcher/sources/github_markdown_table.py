"""GitHub Markdown-table internship backstop adapter."""

from __future__ import annotations

import logging
import re
from collections import Counter
from dataclasses import dataclass
from datetime import date
from html import unescape
from typing import Iterable
from urllib.parse import urlsplit

from watcher.config import CompanyCfg
from watcher.sources.base import SourceError, SourceSchemaError, fetch_text, make_row
from watcher.sources.github_listings import _normalize_term, _safe_feed_url, company_matches

LOGGER = logging.getLogger(__name__)

EXPECTED_HEADERS = ("company", "role", "location", "apply", "added")
MARKERS = {
    "🔒": "closed",
    "🛂": "no_sponsorship",
    "🇺🇸": "us_citizenship_required",
}
MISSING_DATE_VALUES = {"", "-", "—", "–"}
SEPARATOR_RE = re.compile(r"^:?-{3,}:?$")
MARKDOWN_ESCAPE_RE = re.compile(r"\\([\\`*_{}\[\]()#+.!|<>-])")
MARKDOWN_LINK_LABEL_RE = re.compile(r"\[([^\]]*)\]\([^)]*\)")
HTML_TAG_RE = re.compile(r"<[^>]+>")


@dataclass(frozen=True)
class _Posting:
    company: str
    title: str
    location: str
    source_url: str
    source_added_date: str
    closed: bool
    no_sponsorship: bool
    us_citizenship_required: bool


class GitHubMarkdownTableSource:
    """Parse a configured five-column GitHub README table."""

    name = "github_markdown_table"
    format = "github_markdown_table"
    priority = 20

    def __init__(self, url: str, *, source_name: str, default_term: str):
        self.url = str(url).strip()
        self.source_name = str(source_name).strip()
        self.default_term = " ".join(str(default_term).split())
        if not self.url:
            raise ValueError("GitHub Markdown source requires a URL")
        if not self.source_name:
            raise ValueError("GitHub Markdown source requires a name")
        if not self.default_term:
            raise ValueError("GitHub Markdown source requires a default term")
        self.feed_label = _safe_feed_url(self.url)

    def fetch_payload(self) -> str:
        try:
            return fetch_text(self.url, self.name)
        except SourceError as exc:
            message = str(exc).replace(self.url, self.feed_label)
            raise type(exc)(message) from exc

    def fetch(self, company: CompanyCfg) -> list[dict]:
        return self.parse(self.fetch_payload(), (company,))

    def fetch_many(self, companies: Iterable[CompanyCfg]) -> list[dict]:
        return self.parse(self.fetch_payload(), companies)

    def parse(self, markdown: str, companies: Iterable[CompanyCfg]) -> list[dict]:
        if not isinstance(markdown, str):
            raise SourceSchemaError("GitHub Markdown payload must be UTF-8 text")
        header_indexes, candidate_lines = _find_internship_table(markdown)
        postings: list[_Posting] = []
        skipped: Counter[str] = Counter()
        for line in candidate_lines:
            posting, reason = _parse_candidate(line, header_indexes)
            if posting is None:
                skipped[reason or "malformed_row"] += 1
            else:
                postings.append(posting)

        if candidate_lines and not postings:
            self._schema_problem(
                f"GitHub Markdown table had {len(candidate_lines)} candidate row(s), all malformed"
            )
        if skipped:
            reasons = ", ".join(f"{key}={skipped[key]}" for key in sorted(skipped))
            LOGGER.warning(
                "GitHub Markdown source %s skipped %d malformed row(s): %s",
                self.source_name,
                sum(skipped.values()),
                reasons[:240],
            )

        configured_companies = tuple(companies)
        rows = []
        for posting in postings:
            if not any(company_matches(posting.company, company) for company in configured_companies):
                continue
            matching_companies = [
                company
                for company in configured_companies
                if company_matches(posting.company, company)
            ]
            if not any(_term_matches(self.default_term, company.terms) for company in matching_companies):
                continue
            rows.append(self._make_row(posting))
        return rows

    def _make_row(self, posting: _Posting) -> dict:
        return make_row(
            source="github",
            source_adapter=self.name,
            company=posting.company,
            title=posting.title,
            location=posting.location,
            source_url=posting.source_url,
            date_posted="",
            internship_type=self.default_term,
            extra={
                "feed_url": self.feed_label,
                "source_name": self.source_name,
                "source_format": self.format,
                "source_priority": self.priority,
                "source_added_date": posting.source_added_date,
                "active": not posting.closed,
                "closed": posting.closed,
                "no_sponsorship": posting.no_sponsorship,
                "us_citizenship_required": posting.us_citizenship_required,
            },
        )

    def _schema_problem(self, message: str) -> None:
        LOGGER.warning("GitHub Markdown schema problem for %s: %s", self.source_name, message)
        raise SourceSchemaError(message)


def _find_internship_table(markdown: str) -> tuple[dict[str, int], list[str]]:
    lines = markdown.splitlines()
    for index, line in enumerate(lines):
        cells = _split_markdown_row(line)
        normalized = [_normalize_header(cell) for cell in cells]
        if not all(header in normalized for header in EXPECTED_HEADERS):
            continue
        if index + 1 >= len(lines):
            raise SourceSchemaError("GitHub Markdown internship table is missing its separator row")
        separators = _split_markdown_row(lines[index + 1])
        if len(separators) != len(cells) or not all(
            SEPARATOR_RE.fullmatch(cell.strip()) for cell in separators
        ):
            raise SourceSchemaError("GitHub Markdown internship table has an invalid separator row")
        header_indexes = {header: normalized.index(header) for header in EXPECTED_HEADERS}
        candidates = []
        for row_line in lines[index + 2 :]:
            if not row_line.strip():
                break
            if "|" not in row_line:
                break
            candidates.append(row_line)
        return header_indexes, candidates
    raise SourceSchemaError(
        "GitHub Markdown payload is missing the expected Company/Role/Location/Apply/Added table"
    )


def _parse_candidate(
    line: str,
    header_indexes: dict[str, int],
) -> tuple[_Posting | None, str | None]:
    cells = _split_markdown_row(line)
    if len(cells) <= max(header_indexes.values()):
        return None, "column_count"

    raw_company = cells[header_indexes["company"]]
    raw_title = cells[header_indexes["role"]]
    raw_location = cells[header_indexes["location"]]
    marker_text = " ".join((raw_company, raw_title, raw_location))
    flags = {flag: marker in marker_text for marker, flag in MARKERS.items()}
    company = _clean_markdown_text(_remove_markers(raw_company))
    title = _clean_markdown_text(_remove_markers(raw_title))
    location = _clean_markdown_text(_remove_markers(raw_location))
    if not company:
        return None, "missing_company"
    if not title:
        return None, "missing_role"

    source_url = _extract_markdown_url(cells[header_indexes["apply"]])
    if not source_url:
        return None, "invalid_apply_url"
    added, valid_added = _source_added_date(cells[header_indexes["added"]])
    if not valid_added:
        return None, "invalid_added_date"
    return (
        _Posting(
            company=company,
            title=title,
            location=location,
            source_url=source_url,
            source_added_date=added,
            closed=flags["closed"],
            no_sponsorship=flags["no_sponsorship"],
            us_citizenship_required=flags["us_citizenship_required"],
        ),
        None,
    )


def _split_markdown_row(line: str) -> list[str]:
    text = line.strip()
    if text.startswith("|"):
        text = text[1:]
    if text.endswith("|") and not text.endswith("\\|"):
        text = text[:-1]
    cells = []
    current = []
    index = 0
    while index < len(text):
        char = text[index]
        if char == "\\" and index + 1 < len(text):
            current.extend((char, text[index + 1]))
            index += 2
            continue
        if char == "|":
            cells.append("".join(current).strip())
            current = []
        else:
            current.append(char)
        index += 1
    cells.append("".join(current).strip())
    return cells


def _normalize_header(value: str) -> str:
    return re.sub(r"[^a-z]+", "", _clean_markdown_text(value).casefold())


def _remove_markers(value: str) -> str:
    for marker in MARKERS:
        value = value.replace(marker, " ")
    return value


def _clean_markdown_text(value: str) -> str:
    text = re.sub(r"(?i)<br\s*/?>", " / ", str(value or ""))
    text = MARKDOWN_LINK_LABEL_RE.sub(r"\1", text)
    text = HTML_TAG_RE.sub(" ", text)
    text = MARKDOWN_ESCAPE_RE.sub(r"\1", text)
    text = unescape(text).replace("\xa0", " ")
    text = text.replace("**", "").replace("__", "").replace("~~", "").replace("`", "")
    return re.sub(r"\s+", " ", text).strip()


def _extract_markdown_url(value: str) -> str:
    text = str(value or "").strip()
    link_start = re.search(r"\[[^\]]*\]\(", text)
    candidate = ""
    if link_start:
        open_index = link_start.end() - 1
        depth = 1
        escaped = False
        for index in range(open_index + 1, len(text)):
            char = text[index]
            if escaped:
                escaped = False
                continue
            if char == "\\":
                escaped = True
            elif char == "(":
                depth += 1
            elif char == ")":
                depth -= 1
                if depth == 0:
                    candidate = text[open_index + 1 : index].strip()
                    break
    if not candidate:
        raw_match = re.search(r"https?://\S+", text)
        candidate = raw_match.group(0).rstrip(".,;") if raw_match else ""
    if candidate.startswith("<") and candidate.endswith(">"):
        candidate = candidate[1:-1].strip()
    if " " in candidate:
        candidate = candidate.split(None, 1)[0]
    try:
        parsed = urlsplit(candidate)
    except ValueError:
        return ""
    if parsed.scheme.lower() not in {"http", "https"} or not parsed.hostname:
        return ""
    if parsed.username or parsed.password:
        return ""
    return candidate


def _source_added_date(value: str) -> tuple[str, bool]:
    cleaned = _clean_markdown_text(value)
    if cleaned in MISSING_DATE_VALUES:
        return "", True
    try:
        return date.fromisoformat(cleaned).isoformat(), True
    except ValueError:
        return "", False


def _term_matches(default_term: str, configured_terms: Iterable[str]) -> bool:
    wanted = {_normalize_term(term) for term in configured_terms if str(term).strip()}
    return not wanted or _normalize_term(default_term) in wanted
