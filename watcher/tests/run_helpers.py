"""Shared fakes and canonical-row builders for the watcher run test modules.

These are the collection-time doubles the pipeline, collection, reporting, and
digest test modules all need. They deliberately produce rows through
`make_row`, so every test still exercises real canonical-row shapes.
"""

from types import SimpleNamespace

from watcher.sources.base import DirectSourceDiagnostics, make_row


class FakeSource:
    def __init__(self, rows_by_company=None, *, error=None):
        self.rows_by_company = rows_by_company or {}
        self.error = error

    def fetch(self, company):
        if self.error:
            raise self.error
        rows = self.rows_by_company.get(company.name, [])
        self.last_health_diagnostics = DirectSourceDiagnostics(
            succeeded=True,
            retained_row_count=len(rows),
            complete=True,
        )
        return rows


class DiagnosticFakeSource(FakeSource):
    def __init__(
        self,
        rows_by_company=None,
        *,
        error=None,
        requests=0,
        retries=0,
    ):
        super().__init__(rows_by_company, error=error)
        self.last_diagnostics = SimpleNamespace(
            request_attempts=requests,
            retry_attempts=retries,
        )


class FakeGithub:
    def __init__(self, rows):
        self.rows = rows

    def fetch_many(self, companies):
        return self.rows


class CountingGithub:
    def __init__(self, url, rows=None, *, error=None):
        self.feed_label = url
        self.rows = rows or []
        self.error = error
        self.calls = 0

    def fetch_many(self, companies):
        self.calls += 1
        if self.error:
            raise self.error
        return list(self.rows)


class FakeDigestSender:
    def __init__(self, *, sent=True):
        self.sent = sent
        self.calls = []

    def __call__(self, matches):
        self.calls.append(list(matches))
        return self.sent


def row(
    company,
    title,
    *,
    source="direct",
    url=None,
    deadline="",
    description="Build Python APIs with React.",
    requirements="Python, SQL, REST APIs, Git",
):
    return make_row(
        source=source,
        source_adapter="fake",
        company=company,
        title=title,
        location="New York, NY",
        description=description,
        requirements=requirements,
        source_url=url or f"https://example.com/{company}/{title}".replace(" ", "-"),
        deadline=deadline,
        internship_type="Summer",
    )


def github_row(
    company,
    title,
    *,
    source_name,
    source_format,
    priority,
    url,
    active=True,
    description="",
):
    result = row(
        company,
        title,
        source="github",
        url=url,
        description=description,
    )
    result["extra"].update(
        {
            "source_name": source_name,
            "source_format": source_format,
            "source_priority": priority,
            "active": active,
            "closed": not active,
        }
    )
    return result
