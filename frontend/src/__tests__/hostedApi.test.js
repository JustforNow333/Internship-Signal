import { afterEach, describe, expect, it, vi } from "vitest";

import { HostedApiError, createHttpHostedApi } from "../hosted/api.js";

function jsonResponse(body, status = 200) {
  return {
    ok: status >= 200 && status < 300,
    status,
    json: vi.fn().mockResolvedValue(body),
  };
}

describe("hosted HTTP API adapter", () => {
  afterEach(() => {
    vi.unstubAllGlobals();
  });

  it("submits signup and login with credentialed JSON requests", async () => {
    const fetchMock = vi
      .fn()
      .mockResolvedValueOnce(jsonResponse({ user: { id: "signup" } }))
      .mockResolvedValueOnce(jsonResponse({ user: { id: "login" } }));
    vi.stubGlobal("fetch", fetchMock);
    const api = createHttpHostedApi({ baseUrl: "http://localhost:8000" });

    await api.signup({ email: "student@example.com", password: "password123" });
    await api.login({ email: "student@example.com", password: "password123" });

    expect(fetchMock).toHaveBeenNthCalledWith(
      1,
      "http://localhost:8000/api/auth/signup",
      expect.objectContaining({
        method: "POST",
        credentials: "include",
        body: JSON.stringify({
          email: "student@example.com",
          password: "password123",
        }),
      }),
    );
    expect(fetchMock).toHaveBeenNthCalledWith(
      2,
      "http://localhost:8000/api/auth/login",
      expect.objectContaining({ method: "POST", credentials: "include" }),
    );
  });

  it("supports logout, verification, password reset, and recovery", async () => {
    const fetchMock = vi
      .fn()
      .mockResolvedValue(jsonResponse({ accepted: true }));
    vi.stubGlobal("fetch", fetchMock);
    const api = createHttpHostedApi();

    await api.logout();
    await api.resendVerification({ email: "student@example.com" });
    await api.verifyEmail({ token: "verification-token" });
    await api.forgotPassword({ email: "student@example.com" });
    await api.resetPassword({ token: "reset-token", password: "password123" });

    expect(fetchMock.mock.calls.map(([path]) => path)).toEqual([
      "/api/auth/logout",
      "/api/auth/resend-verification",
      "/api/auth/verify-email",
      "/api/auth/forgot-password",
      "/api/auth/reset-password",
    ]);
  });

  it("loads and replaces preferences and watchlists", async () => {
    const fetchMock = vi.fn().mockResolvedValue(jsonResponse([]));
    vi.stubGlobal("fetch", fetchMock);
    const api = createHttpHostedApi();
    const preferences = {
      role_ids: ["software_engineering"],
      preferred_locations: ["Boston, MA"],
      include_remote: true,
      internship_season: "Summer 2027",
      alert_frequency: "as_detected",
      globally_paused: false,
    };
    const watchlist = {
      companies: [{ company_id: "stripe", paused: false }],
    };

    await api.getPreferences();
    await api.updatePreferences(preferences);
    await api.getWatchlist();
    await api.updateWatchlist(watchlist);

    expect(fetchMock.mock.calls.map(([path]) => path)).toEqual([
      "/api/preferences",
      "/api/preferences",
      "/api/watchlist",
      "/api/watchlist",
    ]);
    expect(fetchMock.mock.calls[1][1].body).toBe(JSON.stringify(preferences));
    expect(fetchMock.mock.calls[3][1].body).toBe(JSON.stringify(watchlist));
  });

  it("dispatches unauthorized handling for HTTP 401", async () => {
    const unauthorized = vi.fn();
    window.addEventListener("hosted-api-unauthorized", unauthorized, {
      once: true,
    });
    vi.stubGlobal(
      "fetch",
      vi
        .fn()
        .mockResolvedValue(
          jsonResponse({ detail: "Authentication required." }, 401),
        ),
    );
    const api = createHttpHostedApi();

    await expect(api.getPreferences()).rejects.toEqual(
      expect.objectContaining({
        name: "HostedApiError",
        status: 401,
        message: "Authentication required.",
      }),
    );
    expect(unauthorized).toHaveBeenCalledOnce();
  });

  it("renders safe backend validation details", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn().mockResolvedValue(
        jsonResponse(
          {
            detail: [
              {
                field: "role_ids",
                message: "Unsupported role ID.",
                type: "value_error",
              },
            ],
          },
          422,
        ),
      ),
    );
    const api = createHttpHostedApi();

    await expect(api.getPreferences()).rejects.toEqual(
      expect.objectContaining({
        name: "HostedApiError",
        status: 422,
        message: "Unsupported role ID.",
      }),
    );
  });

  it("returns a structured error type for non-JSON failures", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn().mockResolvedValue({
        ok: false,
        status: 503,
        json: vi.fn().mockRejectedValue(new Error("not JSON")),
      }),
    );
    const api = createHttpHostedApi();

    await expect(api.getMe()).rejects.toBeInstanceOf(HostedApiError);
  });
});
