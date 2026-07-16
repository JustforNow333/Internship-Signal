"""Convenience ATS detector for building watcher watchlists.

This module is intentionally separate from the scheduled watcher run path. It
fetches public careers/search pages, extracts ATS URLs from those real pages,
and writes a review-oriented report/watchlist for manual cleanup.
"""

from __future__ import annotations

import argparse
import html
import json
import re
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterable, Sequence
from urllib.error import HTTPError, URLError
from urllib.parse import parse_qs, quote_plus, unquote, urljoin, urlparse
from urllib.request import Request, urlopen

USER_AGENT = "internship-signal-watchlist-detector/0.1"
HTTP_USER_AGENT = f"Mozilla/5.0 (compatible; {USER_AGENT})"
DEFAULT_TIMEOUT_SECONDS = 15
DEFAULT_MAX_PAGES = 16
DEFAULT_FETCH_DELAY_SECONDS = 0.2
DEFAULT_COMPANY_DELAY_SECONDS = 0.8

RESOLVED = "resolved"
BESPOKE = "bespoke"
UNRESOLVED = "unresolved"

SUPPORTED_ATS = {
    "greenhouse",
    "lever",
    "ashby",
    "smartrecruiters",
    "workable",
    "workday",
}

ATS_HOSTS = {
    "greenhouse.io",
    "lever.co",
    "ashbyhq.com",
    "myworkdayjobs.com",
    "smartrecruiters.com",
    "workable.com",
}

BLOCKED_DISCOVERY_HOSTS = {
    "crunchbase.com",
    "facebook.com",
    "glassdoor.com",
    "instagram.com",
    "levels.fyi",
    "reddit.com",
    "simplify.jobs",
    "teamblind.com",
    "theladders.com",
    "wikipedia.org",
    "x.com",
    "youtube.com",
}

UNSUPPORTED_ATS_HOSTS = {
    "8fold.ai": "eightfold",
    "avature.net": "avature",
    "brassring.com": "brassring",
    "dayforcehcm.com": "dayforce",
    "eightfold.ai": "eightfold",
    "icims.com": "icims",
    "jibeapply.com": "jibe",
    "my.site.com": "salesforce_experience",
    "oraclecloud.com": "oracle_hcm",
    "phenompeople.com": "phenom",
    "pinpointhq.com": "pinpoint",
    "successfactors.com": "successfactors",
    "taleo.net": "taleo",
    "ultipro.com": "ultipro",
}

BESPOKE_URL_PATTERNS = (
    re.compile(r"^https?://(?:www\.)?amazon\.jobs(?:/|$)", re.I),
    re.compile(r"^https?://(?:www\.)?bloomberg\.com/company/careers(?:/|$)", re.I),
    re.compile(r"^https?://careers\.google\.com(?:/|$)", re.I),
    re.compile(r"^https?://(?:www\.)?google\.com/about/careers(?:/|$)", re.I),
    re.compile(r"^https?://(?:www\.)?ibm\.com/(?:[^/]+/)?careers(?:/|$)", re.I),
    re.compile(r"^https?://(?:www\.)?indeed\.jobs(?:/|$)", re.I),
    re.compile(r"^https?://(?:www\.)?linkedin\.com/(?:jobs|careers)(?:/|$)", re.I),
    re.compile(r"^https?://(?:www\.)?oracle\.com/(?:[^/]+/)?careers(?:/|$)", re.I),
    re.compile(r"^https?://(?:www\.)?uber\.com/(?:[^/]+/)?careers(?:/|$)", re.I),
)

TOKEN_EXCLUDES = {
    "api",
    "apply",
    "career",
    "careers",
    "embed",
    "en",
    "en-us",
    "job",
    "job_board",
    "jobs",
    "posting-api",
    "search",
    "v1",
    "view",
}

DOMAIN_SUFFIX_WORDS = {
    "ai",
    "analytics",
    "asset",
    "capital",
    "dao",
    "group",
    "holdings",
    "industries",
    "investments",
    "labs",
    "laboratories",
    "laboratory",
    "management",
    "software",
    "solutions",
    "systems",
    "technologies",
    "technology",
}

ALIAS_SUFFIX_WORDS = {
    "analytics",
    "asset management",
    "holdings",
    "industries",
    "investments",
    "laboratory",
    "labs",
    "software",
    "solutions",
    "systems",
    "technologies",
    "technology",
}


@dataclass(frozen=True)
class AtsMatch:
    ats: str
    token: str
    source_url: str
    workday_shard: str = ""
    workday_site: str = ""


@dataclass(frozen=True)
class PortalMatch:
    source_url: str
    kind: str = "company-hosted careers portal"


@dataclass
class DetectionResult:
    company: str
    status: str
    ats: str = ""
    token: str = ""
    workday_shard: str = ""
    workday_site: str = ""
    source_url: str = ""
    reason: str = ""
    errors: list[str] = field(default_factory=list)

    @classmethod
    def resolved(cls, company: str, match: AtsMatch) -> "DetectionResult":
        return cls(
            company=company,
            status=RESOLVED,
            ats=match.ats,
            token=match.token,
            workday_shard=match.workday_shard,
            workday_site=match.workday_site,
            source_url=match.source_url,
        )

    @classmethod
    def bespoke(cls, company: str, match: PortalMatch, errors: list[str]) -> "DetectionResult":
        return cls(
            company=company,
            status=BESPOKE,
            ats="bespoke",
            source_url=match.source_url,
            reason=match.kind,
            errors=errors,
        )

    @classmethod
    def unresolved(cls, company: str, reason: str, errors: list[str], source_url: str = "") -> "DetectionResult":
        return cls(company=company, status=UNRESOLVED, reason=reason, errors=errors, source_url=source_url)


@dataclass(frozen=True)
class FetchedPage:
    requested_url: str
    final_url: str
    text: str
    content_type: str


class Detector:
    def __init__(
        self,
        *,
        timeout: int = DEFAULT_TIMEOUT_SECONDS,
        max_pages: int = DEFAULT_MAX_PAGES,
        fetch_delay: float = DEFAULT_FETCH_DELAY_SECONDS,
        use_search: bool = True,
    ) -> None:
        self.timeout = timeout
        self.max_pages = max_pages
        self.fetch_delay = fetch_delay
        self.use_search = use_search

    def detect(self, company: str) -> DetectionResult:
        company = company.strip()
        if not company:
            return DetectionResult.unresolved(company, "empty company name", [])

        errors: list[str] = []
        queue: list[str] = []
        bespoke_match: PortalMatch | None = None
        unsupported_match: tuple[str, str] | None = None

        if self.use_search:
            for search_url in _search_urls(company):
                try:
                    page = self._fetch(search_url)
                except FetchError as exc:
                    errors.append(str(exc))
                    continue
                search_match = first_ats_match(page.text, page.final_url)
                if search_match:
                    return DetectionResult.resolved(company, search_match)
                queue.extend(_extract_urls(page.text, page.final_url))

        queue.extend(_company_url_candidates(company))
        queue = _prioritized_unique_urls(queue, company)
        visited: set[str] = set()

        while queue and len(visited) < self.max_pages:
            url = queue.pop(0)
            normalized_url = _normalize_url(url)
            if normalized_url in visited or not _should_visit(normalized_url, company):
                continue
            visited.add(normalized_url)

            direct_match = ats_match_from_url(normalized_url)
            if direct_match:
                return DetectionResult.resolved(company, direct_match)

            try:
                page = self._fetch(normalized_url)
            except FetchError as exc:
                errors.append(str(exc))
                continue

            match = first_ats_match(page.text, page.final_url)
            if match:
                return DetectionResult.resolved(company, match)
            probed_match = self._probe_from_platform_evidence(company, page)
            if probed_match:
                return DetectionResult.resolved(company, probed_match)

            unsupported = first_unsupported_ats(page.text, page.final_url)
            if unsupported and unsupported_match is None:
                unsupported_match = unsupported

            page_bespoke = first_bespoke_portal(page.text, page.final_url, company)
            if page_bespoke and bespoke_match is None:
                bespoke_match = page_bespoke

            discovered = _prioritized_unique_urls(_extract_urls(page.text, page.final_url), company)
            for discovered_url in discovered:
                if len(queue) + len(visited) >= self.max_pages * 2:
                    break
                if _looks_high_value_link(discovered_url, company) and _normalize_url(discovered_url) not in visited:
                    queue.append(discovered_url)

        if unsupported_match and (not bespoke_match or bespoke_match.kind != "known company-hosted careers portal"):
            ats_name, source_url = unsupported_match
            return DetectionResult.unresolved(
                company,
                f"found unsupported ATS '{ats_name}'",
                errors,
                source_url=source_url,
            )
        if bespoke_match:
            return DetectionResult.bespoke(company, bespoke_match, errors)
        if unsupported_match:
            ats_name, source_url = unsupported_match
            return DetectionResult.unresolved(
                company,
                f"found unsupported ATS '{ats_name}'",
                errors,
                source_url=source_url,
            )
        return DetectionResult.unresolved(company, "no supported ATS URL or bespoke portal found", errors)

    def _fetch(self, url: str) -> FetchedPage:
        request = Request(
            url,
            headers={
                "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,application/json;q=0.8,*/*;q=0.5",
                "User-Agent": HTTP_USER_AGENT,
            },
        )
        try:
            with urlopen(request, timeout=self.timeout) as response:
                body = response.read(2_000_000)
                final_url = response.geturl()
                content_type = response.headers.get("Content-Type", "")
        except HTTPError as exc:
            raise FetchError(f"HTTP {exc.code}: {url}") from exc
        except (TimeoutError, URLError, OSError) as exc:
            raise FetchError(f"fetch failed: {url} ({exc})") from exc
        finally:
            if self.fetch_delay > 0:
                time.sleep(self.fetch_delay)

        charset = "utf-8"
        match = re.search(r"charset=([\w.-]+)", content_type, re.I)
        if match:
            charset = match.group(1)
        try:
            text = body.decode(charset, errors="replace")
        except LookupError:
            text = body.decode("utf-8", errors="replace")
        return FetchedPage(requested_url=url, final_url=final_url, text=text, content_type=content_type)

    def _probe_from_platform_evidence(self, company: str, page: FetchedPage) -> AtsMatch | None:
        text = _decode_text(f"{page.final_url}\n{page.text}").lower()
        if "greenhouse.io" in text:
            return self._probe_greenhouse(company)
        return None

    def _probe_greenhouse(self, company: str) -> AtsMatch | None:
        for token in _company_token_candidates(company):
            url = f"https://boards-api.greenhouse.io/v1/boards/{token}/jobs?content=false"
            request = Request(url, headers={"Accept": "application/json", "User-Agent": HTTP_USER_AGENT})
            try:
                with urlopen(request, timeout=self.timeout) as response:
                    payload = json.loads(response.read().decode("utf-8"))
            except (HTTPError, json.JSONDecodeError, TimeoutError, URLError, OSError, UnicodeDecodeError, ValueError):
                continue
            finally:
                if self.fetch_delay > 0:
                    time.sleep(self.fetch_delay)
            jobs = payload.get("jobs") if isinstance(payload, dict) else None
            if not isinstance(jobs, list) or not jobs:
                continue
            company_names = [
                str(job.get("company_name") or "")
                for job in jobs[:25]
                if isinstance(job, dict) and job.get("company_name")
            ]
            urls = [
                str(job.get("absolute_url") or "")
                for job in jobs[:25]
                if isinstance(job, dict) and job.get("absolute_url")
            ]
            if _company_names_match(company, company_names) or any(f"/{token}/" in url for url in urls):
                source_url = next((url for url in urls if f"/{token}/" in url), url)
                return AtsMatch("greenhouse", token, _normalize_url(source_url))
        return None


class FetchError(Exception):
    """Catchable fetch failure for detector discovery pages."""


def first_ats_match(text: str, source_url: str = "") -> AtsMatch | None:
    urls = []
    if source_url:
        urls.append(source_url)
    urls.extend(_extract_urls(text, source_url))
    for url in _unique_urls(urls):
        match = ats_match_from_url(url)
        if match:
            return match
    return None


def ats_match_from_url(url: str) -> AtsMatch | None:
    url = _normalize_url(url)
    parsed = urlparse(url)
    host = (parsed.hostname or "").lower()
    if not host:
        return None
    path_parts = [unquote(part) for part in parsed.path.split("/") if part]
    query = parse_qs(parsed.query)

    if host in {"boards.greenhouse.io", "job-boards.greenhouse.io", "boards-api.greenhouse.io"} or (
        host.endswith(".greenhouse.io") and host.split(".")[0] in {"boards", "job-boards", "boards-api"}
    ):
        token = ""
        if host == "boards-api.greenhouse.io" and len(path_parts) >= 3 and path_parts[:2] == ["v1", "boards"]:
            token = path_parts[2]
        elif "for" in query:
            token = query["for"][0]
        elif path_parts:
            token = path_parts[0]
        if _valid_token(token):
            return AtsMatch("greenhouse", token, url)

    if host == "jobs.lever.co" and path_parts and _valid_token(path_parts[0]):
        return AtsMatch("lever", path_parts[0], url)

    if host == "jobs.ashbyhq.com" and path_parts and _valid_token(path_parts[0]):
        return AtsMatch("ashby", path_parts[0], url)
    if host == "api.ashbyhq.com" and len(path_parts) >= 3 and path_parts[:2] == ["posting-api", "job-board"]:
        token = path_parts[2]
        if _valid_token(token):
            return AtsMatch("ashby", token, url)

    if host == "jobs.smartrecruiters.com" and path_parts and _valid_token(path_parts[0]):
        return AtsMatch("smartrecruiters", path_parts[0], url)
    if host == "api.smartrecruiters.com" and len(path_parts) >= 3 and path_parts[:2] == ["v1", "companies"]:
        token = path_parts[2]
        if _valid_token(token):
            return AtsMatch("smartrecruiters", token, url)

    if host == "apply.workable.com" and path_parts and _valid_token(path_parts[0]):
        return AtsMatch("workable", path_parts[0], url)

    if host.endswith(".myworkdayjobs.com"):
        workday = _workday_match(url, host, path_parts)
        if workday:
            return workday

    return None


def first_bespoke_portal(text: str, source_url: str, company: str = "") -> PortalMatch | None:
    urls = []
    if source_url:
        urls.append(source_url)
    urls.extend(_extract_urls(text, source_url))
    for url in _unique_urls(urls):
        normalized = _normalize_url(url)
        if any(pattern.search(normalized) for pattern in BESPOKE_URL_PATTERNS):
            return PortalMatch(source_url=normalized, kind="known company-hosted careers portal")
        if _looks_company_hosted_careers_portal(normalized, company):
            return PortalMatch(source_url=normalized)
    return None


def first_unsupported_ats(text: str, source_url: str = "") -> tuple[str, str] | None:
    urls = []
    if source_url:
        urls.append(source_url)
    urls.extend(_extract_urls(text, source_url))
    for url in _unique_urls(urls):
        host = (urlparse(url).hostname or "").lower()
        for suffix, name in UNSUPPORTED_ATS_HOSTS.items():
            if host == suffix or host.endswith(f".{suffix}"):
                return name, _normalize_url(url)
    return None


def write_report(results: Iterable[DetectionResult], path: Path) -> None:
    results = list(results)
    lines = ["# ATS detection report", ""]
    for status, title in ((RESOLVED, "Resolved"), (BESPOKE, "Bespoke"), (UNRESOLVED, "Unresolved")):
        section = [result for result in results if result.status == status]
        lines.append(f"## {title} ({len(section)})")
        lines.append("")
        if not section:
            lines.append("_None._")
            lines.append("")
            continue
        for result in section:
            if status == RESOLVED:
                detail = f"{result.company} - {result.ats} / {_display_token(result)}"
                lines.append(f"- {detail} - {result.source_url}")
            elif status == BESPOKE:
                lines.append(f"- {result.company} - {result.reason} - {result.source_url}")
            else:
                source = f" - {result.source_url}" if result.source_url else ""
                lines.append(f"- {result.company} - {result.reason}{source}")
        lines.append("")
    path.write_text("\n".join(lines).rstrip() + "\n", encoding="utf-8")


def write_watchlist(
    results: Iterable[DetectionResult],
    path: Path,
    *,
    terms: Sequence[str],
    github_listing_urls: Sequence[str] = (),
) -> None:
    results = list(results)
    terms = tuple(str(term).strip() for term in terms if str(term).strip())
    if not terms:
        raise ValueError("generated watchlists require at least one explicit internship term")
    lines = [
        "defaults:",
        f"  terms: {_inline_list(terms)}",
        f"  github_listing_urls: {_inline_list(github_listing_urls)}",
        '  target_roles: ["swe"]',
        "  min_score:",
        "",
        "companies:",
    ]
    for result in results:
        lines.extend(_watchlist_entry_lines(result))
        lines.append("")
    path.write_text("\n".join(lines).rstrip() + "\n", encoding="utf-8")


def print_text_report(results: Iterable[DetectionResult]) -> None:
    results = list(results)
    counts = _counts(results)
    print(f"Resolved: {counts[RESOLVED]} | Bespoke: {counts[BESPOKE]} | Unresolved: {counts[UNRESOLVED]}")
    for status, label in ((RESOLVED, "Resolved"), (BESPOKE, "Bespoke"), (UNRESOLVED, "Unresolved")):
        print(f"\n{label}:")
        section = [result for result in results if result.status == status]
        if not section:
            print("  (none)")
            continue
        for result in section:
            if status == RESOLVED:
                print(f"  {result.company}: {result.ats} {_display_token(result)} ({result.source_url})")
            elif status == BESPOKE:
                print(f"  {result.company}: {result.reason} ({result.source_url})")
            else:
                suffix = f" ({result.source_url})" if result.source_url else ""
                print(f"  {result.company}: {result.reason}{suffix}")


def result_to_dict(result: DetectionResult) -> dict:
    return {
        "company": result.company,
        "status": result.status,
        "ats": result.ats,
        "token": result.token,
        "workday_shard": result.workday_shard,
        "workday_site": result.workday_site,
        "source_url": result.source_url,
        "reason": result.reason,
        "errors": result.errors,
    }


def _display_token(result: DetectionResult) -> str:
    if result.ats == "workday":
        parts = [result.token, result.workday_shard, result.workday_site]
        return "/".join(part for part in parts if part)
    return result.token


def _watchlist_entry_lines(result: DetectionResult) -> list[str]:
    aliases = _seed_aliases(result.company)
    alumni_match = _seed_alumni_match(result.company, aliases)
    lines = [f'  - name: "{_yaml_escape(result.company)}"']
    if result.status == RESOLVED:
        lines.append(f"    ats: {result.ats}")
        lines.append(f'    token: "{_yaml_escape(result.token)}"')
        if result.workday_shard:
            lines.append(f'    workday_shard: "{_yaml_escape(result.workday_shard)}"')
        if result.workday_site:
            lines.append(f'    workday_site: "{_yaml_escape(result.workday_site)}"')
        if aliases:
            lines.append(f"    aliases: {_inline_list(aliases)}")
        lines.append(f"    alumni_match: {_inline_list(alumni_match)}")
        lines.append(f'    source_url: "{_yaml_escape(result.source_url)}"')
        return lines

    if result.status == BESPOKE:
        module = _module_name(result.company)
        lines.append("    ats: bespoke")
        lines.append(f'    module: "{module}"')
        if aliases:
            lines.append(f"    aliases: {_inline_list(aliases)}")
        lines.append(f"    alumni_match: {_inline_list(alumni_match)}")
        lines.append(f'    source_url: "{_yaml_escape(result.source_url)}"')
        lines.append(f'    note: "{_yaml_escape(result.reason or "custom adapter needed")}"')
        return lines

    lines.append("    ats: github_only")
    if aliases:
        lines.append(f"    aliases: {_inline_list(aliases)}")
    lines.append(f"    alumni_match: {_inline_list(alumni_match)}")
    lines.append(f'    note: "{_yaml_escape(result.reason or "manual check needed")}"')
    if result.source_url:
        lines.append(f'    source_url: "{_yaml_escape(result.source_url)}"')
    return lines


def _search_urls(company: str) -> list[str]:
    queries = [
        f"{company} careers jobs",
        f"{company} jobs greenhouse lever ashby workday",
    ]
    urls: list[str] = []
    for query in queries:
        encoded = quote_plus(query)
        urls.append(f"https://duckduckgo.com/html/?q={encoded}")
        urls.append(f"https://www.bing.com/search?q={encoded}")
    return urls


def _company_url_candidates(company: str) -> list[str]:
    domains: list[str] = []
    for stem in _domain_stems(company):
        for tld in ("com", "edu"):
            domains.append(f"{stem}.{tld}")
    paths = ("", "/careers", "/jobs", "/open-roles", "/positions", "/careers/jobs", "/company/careers")
    urls: list[str] = []
    for domain in _unique_strings(domains):
        for host in (domain, f"www.{domain}"):
            for path in paths:
                urls.append(f"https://{host}{path}")
    return urls


def _domain_stems(company: str) -> list[str]:
    words = _words(company)
    stems = []
    trimmed = [word for word in words if word not in DOMAIN_SUFFIX_WORDS]
    if trimmed and trimmed != words:
        stems.append("".join(trimmed))
    if len(words) > 1 and len(words[0]) >= 3:
        stems.append(words[0])
    stems.append("".join(words))
    return [stem for stem in _unique_strings(stems) if stem]


def _company_token_candidates(company: str) -> list[str]:
    words = _words(company)
    candidates = ["".join(words)]
    candidates.extend(_domain_stems(company))
    if words:
        candidates.append("-".join(words))
    trimmed = [word for word in words if word not in DOMAIN_SUFFIX_WORDS]
    if trimmed and trimmed != words:
        candidates.append("-".join(trimmed))
    return [candidate for candidate in _unique_strings(candidates) if _valid_token(candidate)]


def _company_names_match(company: str, names: Iterable[str]) -> bool:
    company_key = _compact(company)
    company_stems = {_compact(stem) for stem in _seed_alumni_match(company, _seed_aliases(company))}
    company_stems.add(company_key)
    for name in names:
        name_key = _compact(name)
        if not name_key:
            continue
        if name_key in company_stems or company_key in name_key or name_key in company_key:
            return True
    return False


def _extract_urls(text: str, base_url: str = "") -> list[str]:
    normalized = _decode_text(text)
    urls: list[str] = []
    for match in re.finditer(r"https?://[^\s\"'<>\\]+", normalized):
        urls.append(_clean_extracted_url(match.group(0)))
    for match in re.finditer(r"""(?i)(?:href|src|data-url|data-href)\s*=\s*["']([^"']+)["']""", normalized):
        href = _decode_text(match.group(1).strip())
        if not href or href.startswith(("mailto:", "tel:", "javascript:")):
            continue
        urls.append(_clean_extracted_url(urljoin(base_url, href)))
    decoded_urls = []
    for url in urls:
        decoded_urls.append(_unwrap_search_redirect(url))
    return [url for url in decoded_urls if url.startswith(("http://", "https://"))]


def _decode_text(text: str) -> str:
    decoded = html.unescape(str(text or ""))
    for _ in range(2):
        decoded = decoded.replace("\\/", "/")
        decoded = decoded.replace("\\u002F", "/").replace("\\u002f", "/")
        decoded = decoded.replace("\\u003A", ":").replace("\\u003a", ":")
        decoded = decoded.replace("\\u0026", "&")
        decoded = unquote(decoded)
        decoded = html.unescape(decoded)
    return decoded


def _clean_extracted_url(url: str) -> str:
    url = _decode_text(url).strip()
    url = re.sub(r"[),.;\]]+$", "", url)
    return _normalize_url(url)


def _normalize_url(url: str) -> str:
    url = _decode_text(url).strip().replace(" ", "%20")
    parsed = urlparse(url)
    if not parsed.scheme or not parsed.netloc:
        return url
    scheme = parsed.scheme.lower()
    host = (parsed.netloc or "").lower()
    return parsed._replace(scheme=scheme, netloc=host, fragment="").geturl()


def _unwrap_search_redirect(url: str) -> str:
    parsed = urlparse(url)
    host = (parsed.hostname or "").lower()
    query = parse_qs(parsed.query)
    if "duckduckgo.com" in host and "uddg" in query:
        return _normalize_url(query["uddg"][0])
    if "bing.com" in host and "u" in query:
        return _normalize_url(query["u"][0])
    if "google.com" in host and "url" in query:
        return _normalize_url(query["url"][0])
    return _normalize_url(url)


def _workday_match(url: str, host: str, path_parts: list[str]) -> AtsMatch | None:
    tenant = host.split(".", 1)[0]
    host_parts = host.split(".")
    shard = host_parts[1] if len(host_parts) > 2 else ""
    site = ""
    if len(path_parts) >= 4 and path_parts[:2] == ["wday", "cxs"]:
        tenant = path_parts[2]
        site = path_parts[3]
    else:
        filtered = [
            part
            for part in path_parts
            if part.lower() not in {"en-us", "en", "fr-ca", "es", "de", "wday", "cxs"}
        ]
        if filtered:
            site = filtered[0]
    if _valid_token(tenant) and _valid_workday_shard(shard) and _valid_workday_site(site):
        return AtsMatch("workday", tenant, url, workday_shard=shard, workday_site=site)
    return None


def _valid_workday_shard(shard: str) -> bool:
    return bool(re.fullmatch(r"wd\d+", str(shard or "").strip(), re.I))


def _valid_workday_site(site: str) -> bool:
    site = str(site or "").strip()
    return bool(site and re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]*", site))


def _valid_token(token: str) -> bool:
    token = str(token or "").strip()
    return bool(
        token
        and token.lower() not in TOKEN_EXCLUDES
        and re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]*", token)
    )


def _prioritized_unique_urls(urls: Iterable[str], company: str) -> list[str]:
    scored: list[tuple[int, int, str]] = []
    for index, url in enumerate(_unique_urls(urls)):
        if not url.startswith(("http://", "https://")):
            continue
        score = 50
        parsed = urlparse(url)
        host = (parsed.hostname or "").lower()
        path = parsed.path.lower()
        if any(host == suffix or host.endswith(f".{suffix}") for suffix in ATS_HOSTS):
            score = 0
        elif _has_career_signal(host, path):
            score = 10
        elif _host_looks_like_company(host, company):
            score = 20
        scored.append((score, index, url))
    scored.sort()
    return [url for _, _, url in scored]


def _unique_urls(urls: Iterable[str]) -> list[str]:
    seen: set[str] = set()
    result = []
    for url in urls:
        normalized = _normalize_url(url)
        if normalized and normalized not in seen:
            seen.add(normalized)
            result.append(normalized)
    return result


def _unique_strings(values: Iterable[str]) -> list[str]:
    seen: set[str] = set()
    result = []
    for value in values:
        if value and value not in seen:
            seen.add(value)
            result.append(value)
    return result


def _should_visit(url: str, company: str) -> bool:
    parsed = urlparse(url)
    host = (parsed.hostname or "").lower()
    if not host:
        return False
    if parsed.scheme not in {"http", "https"}:
        return False
    if any(host == suffix or host.endswith(f".{suffix}") for suffix in ATS_HOSTS):
        return True
    if any(host == suffix or host.endswith(f".{suffix}") for suffix in UNSUPPORTED_ATS_HOSTS):
        return True
    if _is_blocked_host(host, company):
        return False
    return _host_looks_like_company(host, company) or _looks_high_value_link(url, company)


def _is_blocked_host(host: str, company: str) -> bool:
    company_key = _compact(company)
    for suffix in BLOCKED_DISCOVERY_HOSTS:
        if host == suffix or host.endswith(f".{suffix}"):
            if company_key and company_key in _compact(suffix):
                return False
            return True
    return False


def _looks_high_value_link(url: str, company: str) -> bool:
    parsed = urlparse(url)
    host = (parsed.hostname or "").lower()
    path = parsed.path.lower()
    if any(host == suffix or host.endswith(f".{suffix}") for suffix in ATS_HOSTS):
        return True
    if any(host == suffix or host.endswith(f".{suffix}") for suffix in UNSUPPORTED_ATS_HOSTS):
        return True
    if _is_blocked_host(host, company):
        return False
    return _has_career_signal(host, path)


def _has_career_signal(host: str, path: str) -> bool:
    combined = f"{host} {path}".lower()
    return any(
        signal in combined
        for signal in (
            "career",
            "jobs",
            "job-search",
            "open-roles",
            "openroles",
            "open_positions",
            "open-positions",
            "positions",
            "work-with-us",
            "join-us",
        )
    )


def _looks_company_hosted_careers_portal(url: str, company: str) -> bool:
    parsed = urlparse(url)
    host = (parsed.hostname or "").lower()
    if not host:
        return False
    if any(host == suffix or host.endswith(f".{suffix}") for suffix in ATS_HOSTS):
        return False
    if any(host == suffix or host.endswith(f".{suffix}") for suffix in UNSUPPORTED_ATS_HOSTS):
        return False
    if _is_blocked_host(host, company):
        return False
    high_value = _looks_high_value_link(url, company)
    return high_value and _host_looks_like_company(host, company)


def _host_looks_like_company(host: str, company: str) -> bool:
    compact_host = _compact(host.replace("www.", "", 1))
    stems = _domain_stems(company)
    return any(stem and stem in compact_host for stem in stems)


def _words(value: str) -> list[str]:
    return re.findall(r"[a-z0-9]+", value.lower())


def _compact(value: str) -> str:
    return "".join(_words(value))


def _seed_aliases(company: str) -> list[str]:
    aliases: list[str] = []
    lower = company.lower()
    for suffix in sorted(ALIAS_SUFFIX_WORDS, key=len, reverse=True):
        pattern = rf"\s+{re.escape(suffix)}$"
        alias = re.sub(pattern, "", lower, flags=re.I).strip()
        if alias and alias != lower:
            aliases.append(_title_like(alias))
            break
    if company == "JHU Applied Physics Laboratory":
        aliases.extend(["JHU APL", "Applied Physics Laboratory"])
    if company == "MIT Lincoln Laboratory":
        aliases.append("MIT Lincoln Lab")
    return _unique_strings(alias for alias in aliases if alias != company)


def _seed_alumni_match(company: str, aliases: Iterable[str]) -> list[str]:
    values = [company.lower(), *(alias.lower() for alias in aliases)]
    return _unique_strings(values)


def _title_like(value: str) -> str:
    keep_upper = {"apl", "dao", "ibm", "iqvia", "jhu", "mit", "zs"}
    parts = []
    for word in value.split():
        parts.append(word.upper() if word.lower() in keep_upper else word.capitalize())
    return " ".join(parts)


def _module_name(company: str) -> str:
    return "_".join(_words(company))


def _inline_list(values: Iterable[str]) -> str:
    return "[" + ", ".join(f'"{_yaml_escape(value)}"' for value in values) + "]"


def _yaml_escape(value: str) -> str:
    return str(value).replace("\\", "\\\\").replace('"', '\\"')


def _counts(results: Iterable[DetectionResult]) -> dict[str, int]:
    counts = {RESOLVED: 0, BESPOKE: 0, UNRESOLVED: 0}
    for result in results:
        counts[result.status] += 1
    return counts


def _read_company_file(path: Path) -> list[str]:
    return [line.strip() for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Detect ATS tokens for watcher watchlist construction.")
    parser.add_argument("companies", nargs="*", help="Company names to detect")
    parser.add_argument("--company-file", type=Path, help="UTF-8 text file with one company name per line")
    parser.add_argument("--json-out", type=Path, help="Write raw detection results as JSON")
    parser.add_argument("--report-out", type=Path, help="Write a Markdown three-way report")
    parser.add_argument("--watchlist-out", type=Path, help="Write a starter watchlist.yml")
    parser.add_argument(
        "--term",
        action="append",
        default=[],
        help="Explicit internship term for --watchlist-out; repeat for multiple terms",
    )
    parser.add_argument(
        "--github-listing-url",
        action="append",
        default=[],
        help="Verified structured listings URL for --watchlist-out; repeat for multiple feeds",
    )
    parser.add_argument("--max-pages", type=int, default=DEFAULT_MAX_PAGES)
    parser.add_argument("--timeout", type=int, default=DEFAULT_TIMEOUT_SECONDS)
    parser.add_argument("--fetch-delay", type=float, default=DEFAULT_FETCH_DELAY_SECONDS)
    parser.add_argument("--company-delay", type=float, default=DEFAULT_COMPANY_DELAY_SECONDS)
    parser.add_argument("--no-search", action="store_true", help="Skip search engines and only try common URLs")
    args = parser.parse_args(argv)
    if args.watchlist_out and not any(str(term).strip() for term in args.term):
        parser.error("--watchlist-out requires at least one explicit --term")

    companies = list(args.companies)
    if args.company_file:
        companies.extend(_read_company_file(args.company_file))
    companies = _unique_strings(company.strip() for company in companies if company.strip())
    if not companies:
        parser.error("provide at least one company or --company-file")

    detector = Detector(
        timeout=args.timeout,
        max_pages=args.max_pages,
        fetch_delay=args.fetch_delay,
        use_search=not args.no_search,
    )
    results: list[DetectionResult] = []
    for index, company in enumerate(companies, start=1):
        print(f"[{index}/{len(companies)}] {company}", file=sys.stderr)
        try:
            result = detector.detect(company)
        except Exception as exc:  # defensive convenience-tool boundary
            result = DetectionResult.unresolved(company, f"unexpected {type(exc).__name__}: {exc}", [])
        results.append(result)
        if args.company_delay > 0 and index < len(companies):
            time.sleep(args.company_delay)

    if args.json_out:
        args.json_out.write_text(
            json.dumps([result_to_dict(result) for result in results], indent=2, ensure_ascii=False) + "\n",
            encoding="utf-8",
        )
    if args.report_out:
        write_report(results, args.report_out)
    if args.watchlist_out:
        write_watchlist(
            results,
            args.watchlist_out,
            terms=args.term,
            github_listing_urls=args.github_listing_url,
        )

    print_text_report(results)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
