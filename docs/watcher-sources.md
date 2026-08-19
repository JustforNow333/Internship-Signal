# Source adapter catalog

Per-platform contracts for the direct ATS adapters in `watcher/sources/`. The
shared adapter protocol, malformed-payload policy, merge precedence, and posting
identity live in [`watcher.md`](watcher.md#3-source-adapters); Workday has its
own section there because of its transport, retry, and pacing rules.

Read this file when adding or changing one adapter. You do not need it for
scoring, eligibility, health, or notification work.

---

## Supported `ats` values

`watcher/sources/registry.py` is the single source of truth for direct
adapters. Its `DIRECT_SOURCE_SPECS` table names each adapter and how to build
it, `DIRECT_ATS` exposes those names, and `build_direct_sources()` constructs
the runtime set. `watcher/config.py::supported_ats()` returns `DIRECT_ATS`
plus the non-direct configuration modes in `NON_DIRECT_ATS`.

Direct: `bain`, `epic`, `ibm`, `greenhouse`, `lever`, `ashby`,
`smartrecruiters`, `workable`, `workday`, `oracle_hcm`, `talentbrew`, `icims`,
`successfactors`, `paylocity`. Non-direct modes: `bespoke`, `github_only`.

**To add a direct source**, append one `DirectSourceSpec` to
`DIRECT_SOURCE_SPECS`. Configuration validation and runtime construction both
follow automatically; set `needs_workday_pacer=True` only for adapters whose
constructor takes the shared `WorkdayPacer`.

---

## Contracts

- **Greenhouse** — `boards-api.greenhouse.io/v1/boards/<token>/jobs?content=true`.
- **Lever** — `api.lever.co/v0/postings/<token>?mode=json`.
- **Ashby** — public posting API per board token. Hosted-board changes require a
  verified bounded contract, stable posting identity, and preserved
  empty-versus-failure semantics before any code change.
- **SmartRecruiters** — `api.smartrecruiters.com/v1/companies/<token>/postings`.
- **Workable** — company subdomain jobs endpoint with cursor pagination that
  fails closed on repeated or missing cursors or totals; strict completeness is
  preserved.
- **Oracle HCM** — Candidate Experience listing API.
- **TalentBrew/Radancy** — public careers-site listing. Contract audits require
  bounded sanitized structure comparisons, posting-specific URLs and identity,
  and preserved all-malformed failure behavior.
- **iCIMS** — one adapter with an explicit `icims_variant` and hostname-only
  `icims_host`. `jibe_json` enumerates `GET /api/jobs?limit=100&page=N`, requires
  a stable nonnegative `totalCount`, validates nested `jobs[].data`, and uses the
  portal-namespaced `req_id` plus the posting-specific same-host canonical URL.
  `classic` parses only `GET /jobs/search?ss=1&in_iframe=1&pr=N`, derives
  identity from the numeric `/jobs/{id}/.../job` path, and rejects the outer
  iframe shell. Optional `icims_portals` is a complete ordered host list; every
  portal must enumerate successfully, though an explicitly empty portal may
  coexist with populated siblings. No per-job enrichment.
- **SuccessFactors** — anonymous Career Site Builder search,
  `GET /{optional-site-prefix}/search/?q=&locationsearch=&startrow=N`. The
  adapter derives page size and complete range from explicit result/page
  metadata, rejects repeated pages and changing or inconsistent totals, and uses
  the same-host numeric ID in each posting-specific `/job/.../{id}` URL. Root
  search sites may link to one same-host brand segment before `/job`; sites with
  an explicit prefix require that exact prefix. `successfactors_host` is
  required; `successfactors_site_prefix` and `successfactors_locale` are
  optional. Retries cover only `retryable` fetch failures: at most three attempts
  per page under one five-retry crawl budget that resets each `fetch()`.
  Exhausting either bound fails the crawl with no partial rows. A retry
  re-requests the identical `startrow`; total stability, page ordering, and
  exact-count enumeration never relax. A crawl that recovered without record
  loss reports `failed_request_count`, `request_retry_recovered`,
  `degraded=True`, and `complete=True`, so it is filed as minor degradation;
  mixed record loss stays incomplete.
- **Paylocity** — the official server-rendered complete `window.pageData.Jobs`
  array. Configured company/module identity and the fixed posting-detail path
  must both validate. Missing or malformed contracts fail; a valid empty array
  is an empty board.
- **Bain & Company** — dedicated `ats: bain` source over the official
  referer-gated `GET /en/api/jobsearch/keyword/get` proxy. `start` is a
  zero-based page number, `totalResults` must be stable, and numeric `JobId`
  values are namespaced as native identity. Only posting-specific Bain detail or
  internship-program URLs are accepted. It is not a generic Avature adapter.
- **Epic** — dedicated `ats: epic` source over the official `careers.epic.com`
  Next.js flow. One bounded HTML request extracts `allOpenJobs` and
  `avaturePositions` from server-rendered Flight data; one bounded
  `GET /cached-api/jobs/search/` independently supplies the complete published ID
  list. Collection succeeds only when those normalized ID sets match **exactly**.
  Identity is the stable numeric Avature ID inside a posting-specific
  `epic.avature.net/Careers/FolderDetail` URL. The incomplete standard Avature
  `Careers/SearchJobs` board is never authoritative input.
- **IBM** — dedicated `ats: ibm` source over the anonymous `www-api.ibm.com`
  search index (`appid=careers`, `scope=careers2`, pagination through `fr`,
  `nr`, one-based `page`, `sortby=url`). A result is trusted only after two
  consecutive complete passes agree on totals, sanitized page membership,
  canonical rows, duplicates, and parse diagnostics. At most three passes of at
  most 100 pages; passes are never unioned. Only equivalent repeated index
  documents collapse, and conflicting numeric `jobId` identities fail. URLs must
  be posting-specific `careers.ibm.com/careers/JobDetail?jobId=...`; the legacy
  challenged Avature flow is never requested.
- **Avature (generic)** — audit-gated. A shared verified anonymous contract with
  complete pagination and stable posting-specific identity is required before
  any implementation.
- **Bespoke** (`sources/bespoke/*.py`) — one module per custom site; prefer an
  internal JSON endpoint over HTML parsing. If a site uses Cloudflare or another
  anti-bot challenge, do **not** escalate to headless-browser evasion: mark the
  company `github_only` and document the decision in the module.


---

## New-ATS readiness

Verify every official tenant flow with bounded read-only probes and compare
reusable contracts, complete enumeration, native identity, anti-bot risk, and
GitHub Actions reliability *before* writing adapter code. Verify live endpoints
manually before adding or changing an adapter, then save a safe fixture.

---

## Shared record parsing

`sources/direct.py` shares only invariant record parsing and diagnostic
plumbing. Greenhouse and Lever share the single-payload lifecycle
(`SinglePayloadDirectAdapter`); SmartRecruiters and Workable keep their
provider-specific pagination visible in their own adapters. Adopt
`DirectRecordAdapter` selectively — expand it only where it removes invariant
parser plumbing, and never add hooks or obscure provider orchestration merely to
increase inheritance coverage.
