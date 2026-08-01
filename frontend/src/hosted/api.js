import { makeHostedFixtures } from "./fixtures.js";

async function parseResponse(response) {
  if (!response.ok) {
    let message = `Request failed (${response.status})`;
    try {
      const body = await response.json();
      message = body.detail || body.message || message;
    } catch {
      // Keep the status-based fallback for non-JSON failures.
    }
    throw new Error(message);
  }
  if (response.status === 204) return null;
  return response.json();
}

function request(path, options = {}) {
  const headers = options.body
    ? { "Content-Type": "application/json", ...options.headers }
    : options.headers;
  return fetch(path, { credentials: "include", ...options, headers }).then(
    parseResponse,
  );
}

export function createHttpHostedApi() {
  return {
    listCompanies: () => request("/api/companies"),
    getMe: () => request("/api/me"),
    signup: (input) =>
      request("/api/auth/signup", {
        method: "POST",
        body: JSON.stringify(input),
      }),
    login: (input) =>
      request("/api/auth/login", {
        method: "POST",
        body: JSON.stringify(input),
      }),
    forgotPassword: (input) =>
      request("/api/auth/forgot-password", {
        method: "POST",
        body: JSON.stringify(input),
      }),
    getPreferences: () => request("/api/preferences"),
    updatePreferences: (input) =>
      request("/api/preferences", {
        method: "PUT",
        body: JSON.stringify(input),
      }),
    getWatchlist: () => request("/api/watchlist"),
    updateWatchlist: (input) =>
      request("/api/watchlist", { method: "PUT", body: JSON.stringify(input) }),
    getMatches: () => request("/api/matches"),
    requestCompany: (input) =>
      request("/api/company-requests", {
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

  const respond = (key, value) =>
    new Promise((resolve, reject) => {
      const finish = () => {
        if (failures[key]) reject(new Error(failures[key]));
        else resolve(clone(value));
      };
      if (latency > 0) setTimeout(finish, latency);
      else queueMicrotask(finish);
    });

  return {
    listCompanies: () => respond("companies", state.companies),
    getMe: () =>
      respond("me", {
        ...state.me,
        last_successful_scan_at: state.last_successful_scan_at,
      }),
    signup: (input) =>
      respond("signup", {
        user: { ...state.me, email: input.email, email_verified: false },
      }),
    login: () => respond("login", { user: state.me }),
    forgotPassword: () => respond("forgotPassword", { accepted: true }),
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
