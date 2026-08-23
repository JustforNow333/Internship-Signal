"""Pure identity-normalization primitives shared by both layers.

These normalize a company name, a job title, or a posting URL into the
comparable form used for posting identity. They are the primitives only: the
posting-identity key policy, dedupe orchestration, and row grouping stay in
`backend.app.dedupe`, which imports these and re-exports them for existing
callers.
"""

from __future__ import annotations

import re
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

_CORP_SUFFIX = re.compile(r"\b(inc|llc|ltd|pvt|co|corp|corporation|company|gmbh)\b\.?", re.I)
_TRACKING_QUERY_KEYS = frozenset(
    {
        "gh_src",
        "lever-source",
        "ref",
        "referrer",
        "source",
        "src",
        "tracking",
        "trk",
    }
)


def _squash(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", " ", (text or "").lower()).strip()


def norm_company(name: str) -> str:
    return re.sub(r"\s+", " ", _CORP_SUFFIX.sub(" ", _squash(name))).strip()


def norm_title(title: str) -> str:
    return re.sub(r"\s+", " ", _squash(title)).strip()


def norm_url(url: str) -> str:
    url = (url or "").strip()
    if not url:
        return ""
    try:
        parts = urlsplit(url if "://" in url else "https://" + url)
    except ValueError:
        return url.lower()
    path = parts.path.rstrip("/")
    path_job_id = path.rsplit("/", 1)[-1]
    # Repeating an identical key/value pair carries no extra identity, so a feed
    # that emits `?gh_jid=1&gh_jid=1` must canonicalize exactly like `?gh_jid=1`.
    # Genuinely different values for one key (`?a=1&a=2`) are still preserved.
    query = sorted(
        {
            (k, v)
            for k, v in parse_qsl(parts.query)
            if not k.lower().startswith("utm_")
            and k.lower() not in _TRACKING_QUERY_KEYS
            and not (k.lower() == "gh_jid" and v == path_job_id)
        }
    )
    host = parts.netloc.lower()
    if host == "boards.greenhouse.io":
        host = "job-boards.greenhouse.io"
    scheme = parts.scheme.lower()
    if scheme == "http" and host.endswith(
        (
            ".ashbyhq.com",
            ".greenhouse.io",
            ".lever.co",
            ".myworkdayjobs.com",
            ".smartrecruiters.com",
            ".workable.com",
        )
    ):
        scheme = "https"
    return urlunsplit((scheme, host, path, urlencode(query), ""))
