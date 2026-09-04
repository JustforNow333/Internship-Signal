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

from internship_signal.domain.identity import norm_url
from watcher.company_matching import company_matches
from watcher.config import CompanyCfg
from watcher.season_terms import terms_match
from watcher.sources.contracts import SourceError, SourceSchemaError
from watcher.sources.github_listings import _safe_feed_url
from watcher.sources.rows import make_row
from watcher.sources.transport import fetch_text

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


@dataclass(frozen=True)
class _InternshipTable:
    header_indexes: dict[str, int]
    candidate_lines: tuple[str, ...]


@dataclass(frozen=True)
class MarkdownParseDiagnostics:
    candidate_tables: int = 0
    valid_tables: int = 0
    malformed_tables: int = 0
    candidate_rows: int = 0
    malformed_rows: int = 0
    rows_by_table: tuple[int, ...] = ()
    duplicates_removed: int = 0


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
        self.last_diagnostics = MarkdownParseDiagnostics()

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
        self.last_diagnostics = MarkdownParseDiagnostics()
        if not isinstance(markdown, str):
            raise SourceSchemaError("GitHub Markdown payload must be UTF-8 text")
        tables, candidate_table_count, malformed_tables = _find_internship_tables(
            markdown
        )
        if candidate_table_count == 0:
            self._schema_problem(
                "GitHub Markdown payload is missing the expected "
                "Company/Role/Location/Apply/Added table"
            )
        if not tables:
            self.last_diagnostics = MarkdownParseDiagnostics(
                candidate_tables=candidate_table_count,
                malformed_tables=malformed_tables,
            )
            self._schema_problem(
                "GitHub Markdown candidate tables had an invalid separator row"
            )

        postings: list[_Posting] = []
        skipped: Counter[str] = Counter()
        candidate_rows = 0
        duplicates_removed = 0
        rows_by_table: list[int] = []
        seen_urls: set[str] = set()
        for table in tables:
            contributed = 0
            candidate_rows += len(table.candidate_lines)
            for line in table.candidate_lines:
                posting, reason = _parse_candidate(line, table.header_indexes)
                if posting is None:
                    skipped[reason or "malformed_row"] += 1
                    continue
                url_key = norm_url(posting.source_url)
                if url_key in seen_urls:
                    duplicates_removed += 1
                    continue
                seen_urls.add(url_key)
                postings.append(posting)
                contributed += 1
            rows_by_table.append(contributed)

        self.last_diagnostics = MarkdownParseDiagnostics(
            candidate_tables=candidate_table_count,
            valid_tables=len(tables),
            malformed_tables=malformed_tables,
            candidate_rows=candidate_rows,
            malformed_rows=sum(skipped.values()),
            rows_by_table=tuple(rows_by_table),
            duplicates_removed=duplicates_removed,
        )
        if candidate_rows and not postings:
            self._schema_problem(
                f"GitHub Markdown tables had {candidate_rows} candidate row(s), all malformed"
            )
        if skipped or malformed_tables:
            reasons = ", ".join(f"{key}={skipped[key]}" for key in sorted(skipped))
            parts = []
            if malformed_tables:
                parts.append(f"ignored {malformed_tables} malformed candidate table(s)")
            if skipped:
                parts.append(
                    f"skipped {sum(skipped.values())} malformed row(s): {reasons[:180]}"
                )
            LOGGER.warning(
                "GitHub Markdown source %s %s.",
                self.source_name,
                "; ".join(parts),
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
            if not any(
                terms_match((self.default_term,), company.terms)
                for company in matching_companies
            ):
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


def _find_internship_tables(
    markdown: str,
) -> tuple[list[_InternshipTable], int, int]:
    lines = markdown.splitlines()
    tables: list[_InternshipTable] = []
    candidate_tables = 0
    malformed_tables = 0
    index = 0
    while index < len(lines):
        line = lines[index]
        cells = _split_markdown_row(line)
        normalized = [_normalize_header(cell) for cell in cells]
        if not all(header in normalized for header in EXPECTED_HEADERS):
            index += 1
            continue
        candidate_tables += 1
        if index + 1 >= len(lines):
            malformed_tables += 1
            index += 1
            continue
        separators = _split_markdown_row(lines[index + 1])
        if len(separators) != len(cells) or not all(
            SEPARATOR_RE.fullmatch(cell.strip()) for cell in separators
        ):
            malformed_tables += 1
            index += 1
            continue
        header_indexes = {
            header: normalized.index(header) for header in EXPECTED_HEADERS
        }
        candidates = []
        row_index = index + 2
        while row_index < len(lines):
            row_line = lines[row_index]
            if not row_line.strip():
                break
            if "|" not in row_line:
                break
            candidates.append(row_line)
            row_index += 1
        tables.append(
            _InternshipTable(
                header_indexes=header_indexes,
                candidate_lines=tuple(candidates),
            )
        )
        index = max(row_index, index + 2)
    return tables, candidate_tables, malformed_tables


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
