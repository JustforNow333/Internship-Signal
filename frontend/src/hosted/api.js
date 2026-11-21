import { makeHostedFixtures } from "./fixtures.js";
import { normalizeMatch, normalizeMatchList } from "./matchModel.js";

export const MATCH_VIEWS = [
  "active",
  "saved",
  "dismissed",
  "historical",
  "applied",
  "all",
];

// Destructive requests carry this header. A cross-site page cannot add it
// without a CORS preflight, which the API grants only to the hosted frontend.
export const CSRF_HEADER = "X-FindSooner-CSRF";

function matchQuery({ view, limit, offset } = {}) {
  const query = new URLSearchParams();
  if (view) query.set("view", view);
  if (limit != null) query.set("limit", String(limit));
  if (offset != null) query.set("offset", String(offset));
  const encoded = query.toString();
  return encoded ? `?${encoded}` : "";
}

export class HostedApiError extends Error {
  constructor(message, status, details = null) {
    super(message);
    this.name = "HostedApiError";
    this.status = status;
    this.details = details;
  }
}

// Mirrors the backend's bounds in backend/app/hosted/schemas.py.
const MATCH_PAGE_LIMIT = 50;
const MATCH_PAGE_MAX_LIMIT = 100;
const MATCH_MAX_OFFSET = 10_000;

/**
 * Follow `has_more` from the first page of one view to its last.
 * `fetchPage` resolves `{ items, limit, offset, has_more }` with UI-ready items.
 */
async function loadAllPages(fetchPage, view) {
  const matches = [];
  let page = await fetchPage({ view });

  while (true) {
    const items = page?.items || [];
    matches.push(...items);
    if (page?.has_more !== true) return matches;

    const limit = page.limit;
    const offset = page.offset;
    if (
      !Number.isInteger(limit) ||
      limit < 1 ||
      !Number.isInteger(offset) ||
      offset < 0 ||
      items.length === 0
    ) {
      throw new HostedApiError("Match response pagination is invalid.", 502);
    }
    page = await fetchPage({ view, limit, offset: offset + items.length });
  }
}

function normalizeMatchPage(page) {
  if (Array.isArray(page)) return { items: normalizeMatchList(page), has_more: false };
  return { ...page, items: normalizeMatchList(page) };
}

function errorMessage(body, fallback) {
  if (typeof body?.detail === "string") return body.detail;
  if (Array.isArray(body?.detail)) {
    return (
      body.detail
        .map((item) => item.message || item.msg)
        .filter(Boolean)
        .join(" ") || fallback
    );
  }
  return body?.message || fallback;
}

async function parseResponse(response) {
  if (!response.ok) {
    const fallback = `Request failed (${response.status})`;
    let body = null;
    try {
      body = await response.json();
    } catch {
      // Keep the status-based fallback for non-JSON failures.
    }
    if (response.status === 401 && typeof window !== "undefined") {
      window.dispatchEvent(new CustomEvent("hosted-api-unauthorized"));
    }
    throw new HostedApiError(
      errorMessage(body, fallback),
      response.status,
      body?.detail || null,
    );
  }
  if (response.status === 204) return null;
  return response.json();
}

function request(baseUrl, path, options = {}) {
  const headers = options.body
    ? { "Content-Type": "application/json", ...options.headers }
    : options.headers;
  return fetch(`${baseUrl}${path}`, {
    credentials: "include",
    ...options,
    headers,
  }).then(parseResponse);
}

export function createHttpHostedApi({
  baseUrl = import.meta.env.VITE_HOSTED_API_BASE_URL || "",
} = {}) {
  const call = (path, options) =>
    request(baseUrl.replace(/\/$/, ""), path, options);
  const getMatchPage = (params) =>
    call(`/api/matches${matchQuery(params)}`).then(normalizeMatchPage);
  return {
    mode: "live",
    listCompanies: () => call("/api/companies"),
    getMe: () => call("/api/me"),
    deleteAccount: ({ password }) =>
      call("/api/me", {
        method: "DELETE",
        headers: { [CSRF_HEADER]: "1" },
        body: JSON.stringify({ password }),
      }),
    signup: (input) =>
      call("/api/auth/signup", {
        method: "POST",
        body: JSON.stringify(input),
      }),
    login: (input) =>
      call("/api/auth/login", {
        method: "POST",
        body: JSON.stringify(input),
      }),
    logout: () => call("/api/auth/logout", { method: "POST" }),
    forgotPassword: (input) =>
      call("/api/auth/forgot-password", {
        method: "POST",
        body: JSON.stringify(input),
      }),
    resetPassword: (input) =>
      call("/api/auth/reset-password", {
        method: "POST",
        body: JSON.stringify(input),
      }),
    resendVerification: (input) =>
      call("/api/auth/resend-verification", {
        method: "POST",
        body: JSON.stringify(input),
      }),
    verifyEmail: (input) =>
      call("/api/auth/verify-email", {
        method: "POST",
        body: JSON.stringify(input),
      }),
    getPreferences: () => call("/api/preferences"),
    updatePreferences: (input) =>
      call("/api/preferences", {
        method: "PUT",
        body: JSON.stringify(input),
      }),
    getWatchlist: () => call("/api/watchlist"),
    updateWatchlist: (input) =>
      call("/api/watchlist", { method: "PUT", body: JSON.stringify(input) }),
    getMatches: (params) =>
      params
        ? call(`/api/matches${matchQuery(params)}`).then(normalizeMatchList)
        : loadAllPages(getMatchPage),
    // One page of a view, keeping the pagination envelope.
    getMatchPage,
    // Every match in one view (active, saved, dismissed, ...), across pages.
    getMatchView: (view) => loadAllPages(getMatchPage, view),
    getMatch: (id) =>
      call(`/api/matches/${encodeURIComponent(id)}`).then(normalizeMatch),
    updateMatch: (id, changes) =>
      call(`/api/matches/${encodeURIComponent(id)}`, {
        method: "PATCH",
        body: JSON.stringify(changes),
      }).then(normalizeMatch),
    requestCompany: (input) =>
      call("/api/company-requests", {
        method: "POST",
        body: JSON.stringify(input),
      }),
  };
}

const clone = (value) => JSON.parse(JSON.stringify(value));

// Demo records use the normalized UI shape; these mirror the backend's
// view filters (backend/app/hosted/router.py::_match_view_filters).
function mockViewIncludes(view, match) {
  if (view === "saved") return Boolean(match.saved);
  if (view === "dismissed") return Boolean(match.dismissed);
  if (view === "historical") return Boolean(match.no_longer_matches_at);
  if (view === "applied") return Boolean(match.applied_at);
  if (view === "all") return true;
  return !match.dismissed && !match.no_longer_matches_at;
}

// ISO 8601 date-time carrying an explicit UTC offset, like the backend's
// MatchUpdate.applied_at validator (backend/app/hosted/schemas.py).
const AWARE_ISO_DATETIME =
  /^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}(:\d{2}(\.\d+)?)?(Z|[+-]\d{2}:\d{2})$/i;

// Mirrors MatchUpdate + the router's future-date check: applied is a strict
// boolean; applied_at is only allowed (and never null) with applied=true, must
// be timezone-aware, and may not be later than now. Returns an error message.
function mockAppliedError(changes, now) {
  const hasApplied = Object.prototype.hasOwnProperty.call(changes, "applied");
  const hasAppliedAt = Object.prototype.hasOwnProperty.call(
    changes,
    "applied_at",
  );
  if (hasApplied && typeof changes.applied !== "boolean")
    return "applied must be a boolean.";
  if (!hasAppliedAt) return null;
  if (changes.applied !== true)
    return "applied_at is only accepted with applied=true.";
  const value = changes.applied_at;
  if (typeof value !== "string" || !AWARE_ISO_DATETIME.test(value))
    return "applied_at must be an ISO 8601 datetime with a UTC offset.";
  const time = Date.parse(value);
  if (!Number.isFinite(time)) return "applied_at must be an ISO 8601 datetime.";
  if (time > now) return "applied_at cannot be in the future.";
  return null;
}

export function createMockHostedApi({
  latency = 0,
  failures = {},
  fixtures,
} = {}) {
  const state = clone(fixtures || makeHostedFixtures());

  // After a simulated deletion every account request fails like the server
  // would: the deleted session is no longer authenticated.
  const PUBLIC_KEYS = new Set([
    "companies",
    "signup",
    "login",
    "forgotPassword",
    "resetPassword",
    "resendVerification",
    "verifyEmail",
  ]);
  let deleted = false;
  const respond = (key, value) =>
    new Promise((resolve, reject) => {
      const finish = () => {
        if (failures[key]) reject(new Error(failures[key]));
        else if (deleted && !PUBLIC_KEYS.has(key))
          reject(new HostedApiError("Authentication required.", 401));
        else resolve(clone(value));
      };
      if (latency > 0) setTimeout(finish, latency);
      else queueMicrotask(finish);
    });

  // Same view filters, ordering, bounds, and envelope as GET /api/matches.
  // A `matches.<view>` failure targets one view; `matches` fails them all.
  const mockMatchPage = ({
    view = "active",
    limit = MATCH_PAGE_LIMIT,
    offset = 0,
  } = {}) => {
    if (!MATCH_VIEWS.includes(view))
      return Promise.reject(
        new HostedApiError("Unsupported match view.", 400),
      );
    if (
      !Number.isInteger(limit) ||
      limit < 1 ||
      limit > MATCH_PAGE_MAX_LIMIT ||
      !Number.isInteger(offset) ||
      offset < 0 ||
      offset > MATCH_MAX_OFFSET
    )
      return Promise.reject(
        new HostedApiError("Match pagination is out of range.", 422),
      );
    const selected = state.matches
      .filter((match) => mockViewIncludes(view, match))
      .sort(
        (a, b) =>
          String(b.detected_at).localeCompare(String(a.detected_at)) ||
          String(b.id).localeCompare(String(a.id)),
      );
    const items = selected.slice(offset, offset + limit);
    const key = failures[`matches.${view}`] ? `matches.${view}` : "matches";
    return respond(key, {
      items,
      limit,
      offset,
      total: selected.length,
      has_more: offset + items.length < selected.length,
    });
  };

  return {
    mode: "mock",
    listCompanies: () => respond("companies", state.companies),
    getMe: () =>
      respond("me", {
        ...state.me,
        last_successful_scan_at: state.last_successful_scan_at,
      }),
    signup: (input) =>
      respond("signup", {
        user: { ...state.me, email: input.email, email_verified: false },
        verification_email_sent: true,
      }),
    login: () => respond("login", { user: state.me }),
    deleteAccount: ({ password } = {}) => {
      if (!password)
        return Promise.reject(
          new HostedApiError("Password was not recognized.", 403),
        );
      const result = respond("deleteAccount", null);
      result.then(
        () => {
          deleted = true;
          state.preferences = null;
          state.watchlist = [];
          state.matches = [];
        },
        () => {},
      );
      return result;
    },
    logout: () => respond("logout", null),
    forgotPassword: () => respond("forgotPassword", { accepted: true }),
    resetPassword: () => respond("resetPassword", { accepted: true }),
    resendVerification: () => respond("resendVerification", { accepted: true }),
    verifyEmail: () => respond("verifyEmail", { accepted: true }),
    getPreferences: () => respond("preferences", state.preferences),
    updatePreferences: (input) => {
      state.preferences = { ...state.preferences, ...clone(input) };
      return respond("updatePreferences", state.preferences);
    },
    getWatchlist: () => respond("watchlist", state.watchlist),
    updateWatchlist: (input) => {
      state.watchlist = clone(input.companies || input.watchlist || []);
      return respond("updateWatchlist", state.watchlist);
    },
    getMatches: (params) =>
      params
        ? mockMatchPage(params).then((page) => page.items)
        : loadAllPages(mockMatchPage),
    getMatchPage: (params) => mockMatchPage(params),
    getMatchView: (view) => loadAllPages(mockMatchPage, view),
    getMatch: (id) =>
      respond(
        "match",
        state.matches.find((match) => match.id === id) || null,
      ),
    // The change is applied to demo state only once the response succeeds, so
    // a simulated failure leaves every view exactly as it was.
    updateMatch: (id, changes) => {
      const match = state.matches.find((item) => item.id === id);
      const now = Date.now();
      const appliedError = mockAppliedError(changes, now);
      if (appliedError)
        return Promise.reject(new HostedApiError(appliedError, 422));
      const stamp = new Date(now).toISOString();
      const patch = {};
      if (changes.saved != null) {
        patch.saved = changes.saved;
        patch.saved_at = changes.saved ? stamp : null;
      }
      if (changes.dismissed != null) {
        patch.dismissed = changes.dismissed;
        patch.dismissed_at = changes.dismissed ? stamp : null;
      }
      // Applied is independent of save/dismiss. Repeat marking keeps the
      // original date; a supplied applied_at (Undo) is kept as the exact string.
      if (changes.applied != null) {
        patch.applied = changes.applied;
        if (!changes.applied) patch.applied_at = null;
        else if (changes.applied_at != null) patch.applied_at = changes.applied_at;
        else patch.applied_at = match?.applied_at || stamp;
      }
      const result = respond("updateMatch", match ? { ...match, ...patch } : null);
      result.then(
        () => {
          if (match) Object.assign(match, patch);
        },
        () => {},
      );
      return result;
    },
    requestCompany: (input) =>
      respond("requestCompany", {
        id: "request-demo",
        status: "received",
        ...input,
      }),
  };
}

export const hostedApi =
  import.meta.env.VITE_HOSTED_API_MODE === "live"
    ? createHttpHostedApi()
    : createMockHostedApi();
