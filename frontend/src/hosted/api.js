import { makeHostedFixtures } from "./fixtures.js";
import { normalizeMatch, normalizeMatchList } from "./matchModel.js";

export const MATCH_VIEWS = ["active", "saved", "dismissed", "historical", "all"];

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

async function loadAllMatches(call) {
  const matches = [];
  let page = await call("/api/matches");

  while (true) {
    const items = normalizeMatchList(page);
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
    page = await call(
      `/api/matches${matchQuery({ limit, offset: offset + items.length })}`,
    );
  }
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
        : loadAllMatches(call),
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
    getMatches: () => respond("matches", state.matches),
    getMatch: (id) =>
      respond(
        "match",
        state.matches.find((match) => match.id === id) || null,
      ),
    updateMatch: (id, changes) => {
      const match = state.matches.find((item) => item.id === id);
      if (match) {
        const stamp = new Date().toISOString();
        if (changes.saved != null) {
          match.saved = changes.saved;
          match.saved_at = changes.saved ? stamp : null;
        }
        if (changes.dismissed != null) {
          match.dismissed = changes.dismissed;
          match.dismissed_at = changes.dismissed ? stamp : null;
        }
      }
      return respond("updateMatch", match);
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
