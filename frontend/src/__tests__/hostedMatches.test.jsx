import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";

import App from "../App.jsx";
import {
  createHttpHostedApi,
  createMockHostedApi,
} from "../hosted/api.js";
import { makeHostedFixtures } from "../hosted/fixtures.js";
import { normalizeMatch } from "../hosted/matchModel.js";

const DEMO_BANNER =
  "Demo data — these listings are examples and are not live detections.";

const MATCHED_AT = new Date(Date.now() - 60_000).toISOString();

function apiMatch(overrides = {}) {
  return {
    id: "11111111-1111-4111-8111-111111111111",
    job_id: "22222222-2222-4222-8222-222222222222",
    company_id: "stripe",
    company: "Stripe",
    title: "Software Engineering Intern, Payments",
    location: "New York, NY",
    remote: false,
    remote_status: "",
    role_id: "software_engineering",
    application_url: "https://stripe.com/jobs/1",
    posting_date: "2026-08-01",
    deadline: null,
    is_open: true,
    match_reasons: [
      { code: "company_watched", value: "stripe" },
      { code: "role_selected", value: "software_engineering" },
    ],
    matched_at: MATCHED_AT,
    last_matched_at: MATCHED_AT,
    no_longer_matches_at: null,
    saved_at: null,
    dismissed_at: null,
    ...overrides,
  };
}

function jsonResponse(body, status = 200) {
  return {
    ok: status >= 200 && status < 300,
    status,
    json: vi.fn().mockResolvedValue(body),
  };
}

/** A live client whose non-match calls are stubbed with minimal valid data. */
function liveClientWith(matchesResult, updateResult) {
  const live = createHttpHostedApi();
  return {
    ...live,
    mode: "live",
    listCompanies: async () => [],
    getMe: async () => ({
      id: "user-1",
      email: "student@example.com",
      email_verified: true,
      last_successful_scan_at: null,
    }),
    getPreferences: async () => ({
      role_ids: ["software_engineering"],
      preferred_locations: [],
      include_remote: true,
      internship_season: "Any season",
      alert_frequency: "as_detected",
      globally_paused: false,
    }),
    getWatchlist: async () => [],
    getMatches: matchesResult,
    updateMatch: updateResult || live.updateMatch,
  };
}

describe("hosted match API adapter", () => {
  afterEach(() => {
    vi.unstubAllGlobals();
  });

  it("requests the live match list and normalizes it for the UI", async () => {
    const fetchMock = vi
      .fn()
      .mockResolvedValue(
        jsonResponse({
          items: [apiMatch()],
          limit: 50,
          offset: 0,
          total: 1,
          has_more: false,
        }),
      );
    vi.stubGlobal("fetch", fetchMock);
    const api = createHttpHostedApi({ baseUrl: "http://localhost:8000" });

    const matches = await api.getMatches();

    expect(fetchMock).toHaveBeenCalledWith(
      "http://localhost:8000/api/matches",
      expect.objectContaining({ credentials: "include" }),
    );
    expect(matches).toHaveLength(1);
    expect(matches[0].company).toBe("Stripe");
    expect(matches[0].source_url).toBe("https://stripe.com/jobs/1");
    expect(matches[0].detected_at).toBe(apiMatch().matched_at);
    expect(matches[0].why[0]).toBe("Stripe is on your watchlist");
  });

  it("loads every page when the workspace requests its complete match list", async () => {
    const secondMatch = apiMatch({
      id: "33333333-3333-4333-8333-333333333333",
      title: "Machine Learning Intern",
    });
    const fetchMock = vi
      .fn()
      .mockResolvedValueOnce(
        jsonResponse({
          items: [apiMatch()],
          limit: 1,
          offset: 0,
          total: 2,
          has_more: true,
        }),
      )
      .mockResolvedValueOnce(
        jsonResponse({
          items: [secondMatch],
          limit: 1,
          offset: 1,
          total: 2,
          has_more: false,
        }),
      );
    vi.stubGlobal("fetch", fetchMock);
    const api = createHttpHostedApi();

    const matches = await api.getMatches();

    expect(fetchMock).toHaveBeenCalledTimes(2);
    expect(fetchMock.mock.calls[0][0]).toBe("/api/matches");
    expect(fetchMock.mock.calls[1][0]).toBe(
      "/api/matches?limit=1&offset=1",
    );
    expect(matches.map((match) => match.title)).toEqual([
      "Software Engineering Intern, Payments",
      "Machine Learning Intern",
    ]);
  });

  it("sends bounded view and pagination parameters", async () => {
    const fetchMock = vi.fn().mockResolvedValue(jsonResponse({ items: [] }));
    vi.stubGlobal("fetch", fetchMock);
    const api = createHttpHostedApi();

    await api.getMatches({ view: "saved", limit: 10, offset: 20 });

    expect(fetchMock.mock.calls[0][0]).toBe(
      "/api/matches?view=saved&limit=10&offset=20",
    );
  });

  it("reads a single match and patches only save or dismiss state", async () => {
    const fetchMock = vi.fn().mockResolvedValue(jsonResponse(apiMatch()));
    vi.stubGlobal("fetch", fetchMock);
    const api = createHttpHostedApi();

    await api.getMatch("abc");
    await api.updateMatch("abc", { saved: true });

    expect(fetchMock.mock.calls[0][0]).toBe("/api/matches/abc");
    expect(fetchMock.mock.calls[1][0]).toBe("/api/matches/abc");
    expect(fetchMock.mock.calls[1][1]).toEqual(
      expect.objectContaining({
        method: "PATCH",
        body: JSON.stringify({ saved: true }),
      }),
    );
  });

  it("normalizes a match without reasons or an application URL", () => {
    const normalized = normalizeMatch(
      apiMatch({ match_reasons: [], application_url: null }),
    );
    expect(normalized.why).toHaveLength(1);
    expect(normalized.source_url).toBe("");
  });

  it("marks live and mock clients so the UI can label demo data", () => {
    expect(createHttpHostedApi().mode).toBe("live");
    expect(createMockHostedApi().mode).toBe("mock");
  });
});

describe("hosted match view pagination (live client)", () => {
  afterEach(() => {
    vi.unstubAllGlobals();
  });

  const SECOND_ID = "33333333-3333-4333-8333-333333333333";

  it("loads every saved page, keeping the view and closed saves", async () => {
    const savedAt = new Date().toISOString();
    const closedSave = apiMatch({
      id: SECOND_ID,
      title: "Closed Saved Intern",
      is_open: false,
      no_longer_matches_at: savedAt,
      saved_at: savedAt,
    });
    const fetchMock = vi
      .fn()
      .mockResolvedValueOnce(
        jsonResponse({
          items: [apiMatch({ saved_at: savedAt })],
          limit: 1,
          offset: 0,
          total: 2,
          has_more: true,
        }),
      )
      .mockResolvedValueOnce(
        jsonResponse({
          items: [closedSave],
          limit: 1,
          offset: 1,
          total: 2,
          has_more: false,
        }),
      );
    vi.stubGlobal("fetch", fetchMock);
    const api = createHttpHostedApi({ baseUrl: "http://localhost:8000" });

    const saved = await api.getMatchView("saved");

    expect(fetchMock.mock.calls.map(([url]) => url)).toEqual([
      "http://localhost:8000/api/matches?view=saved",
      "http://localhost:8000/api/matches?view=saved&limit=1&offset=1",
    ]);
    for (const [, options] of fetchMock.mock.calls) {
      expect(options).toEqual(
        expect.objectContaining({ credentials: "include" }),
      );
    }
    expect(saved.map((match) => match.id)).toEqual([apiMatch().id, SECOND_ID]);
    expect(saved.every((match) => match.saved)).toBe(true);
    expect(saved[1]).toMatchObject({
      is_open: false,
      no_longer_matches_at: savedAt,
    });
  });

  it("loads every dismissed page", async () => {
    const dismissedAt = new Date().toISOString();
    const fetchMock = vi
      .fn()
      .mockResolvedValueOnce(
        jsonResponse({
          items: [apiMatch({ dismissed_at: dismissedAt })],
          limit: 1,
          offset: 0,
          total: 2,
          has_more: true,
        }),
      )
      .mockResolvedValueOnce(
        jsonResponse({
          items: [apiMatch({ id: SECOND_ID, dismissed_at: dismissedAt })],
          limit: 1,
          offset: 1,
          total: 2,
          has_more: false,
        }),
      );
    vi.stubGlobal("fetch", fetchMock);

    const dismissed = await createHttpHostedApi().getMatchView("dismissed");

    expect(fetchMock.mock.calls[1][0]).toBe(
      "/api/matches?view=dismissed&limit=1&offset=1",
    );
    expect(dismissed).toHaveLength(2);
    expect(dismissed.every((match) => match.dismissed)).toBe(true);
  });

  it("returns one normalized page with its pagination envelope", async () => {
    const fetchMock = vi.fn().mockResolvedValue(
      jsonResponse({
        items: [apiMatch()],
        limit: 10,
        offset: 20,
        total: 31,
        has_more: true,
      }),
    );
    vi.stubGlobal("fetch", fetchMock);

    const page = await createHttpHostedApi().getMatchPage({
      view: "dismissed",
      limit: 10,
      offset: 20,
    });

    expect(fetchMock.mock.calls[0][0]).toBe(
      "/api/matches?view=dismissed&limit=10&offset=20",
    );
    expect(page).toMatchObject({
      limit: 10,
      offset: 20,
      total: 31,
      has_more: true,
    });
    expect(page.items[0].company).toBe("Stripe");
  });

  it("rejects a view whose pagination cannot advance", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn().mockResolvedValue(
        jsonResponse({
          items: [],
          limit: 50,
          offset: 0,
          total: 3,
          has_more: true,
        }),
      ),
    );

    await expect(
      createHttpHostedApi().getMatchView("saved"),
    ).rejects.toMatchObject({ name: "HostedApiError", status: 502 });
  });

  it("fails the whole view when a later page fails", async () => {
    vi.stubGlobal(
      "fetch",
      vi
        .fn()
        .mockResolvedValueOnce(
          jsonResponse({
            items: [apiMatch()],
            limit: 1,
            offset: 0,
            total: 2,
            has_more: true,
          }),
        )
        .mockResolvedValueOnce(
          jsonResponse({ detail: "Match lookup failed." }, 500),
        ),
    );

    await expect(
      createHttpHostedApi().getMatchView("dismissed"),
    ).rejects.toMatchObject({ status: 500, message: "Match lookup failed." });
  });
});

describe("hosted match view pagination (demo client)", () => {
  it("keeps the default list to active matches only", async () => {
    const api = createMockHostedApi();

    const active = await api.getMatches();
    const ids = active.map((match) => match.id);

    expect(active).toHaveLength(6);
    expect(ids).not.toContain("match-figma-007");
    expect(ids).not.toContain("match-stripe-008");
    expect(await api.getMatchView("active")).toEqual(active);
  });

  it("lists saved (including closed, no-longer-matching) and dismissed views", async () => {
    const api = createMockHostedApi();

    const saved = await api.getMatchView("saved");
    const dismissed = await api.getMatchView("dismissed");

    expect(saved.map((match) => match.id)).toEqual(["match-figma-007"]);
    expect(saved[0]).toMatchObject({ is_open: false, saved: true });
    expect(saved[0].no_longer_matches_at).toBeTruthy();
    expect(dismissed.map((match) => match.id)).toEqual(["match-stripe-008"]);
  });

  it("honours limit, offset, and has_more newest first", async () => {
    const api = createMockHostedApi();
    const everything = await api.getMatchView("all");

    const first = await api.getMatchPage({ view: "all", limit: 3 });
    const last = await api.getMatchPage({ view: "all", limit: 3, offset: 6 });

    expect(everything).toHaveLength(8);
    const times = everything.map((match) => match.detected_at);
    expect([...times].sort().reverse()).toEqual(times);
    expect(first).toMatchObject({
      limit: 3,
      offset: 0,
      total: 8,
      has_more: true,
    });
    expect(first.items.map((m) => m.id)).toEqual(
      everything.slice(0, 3).map((m) => m.id),
    );
    expect(last).toMatchObject({ offset: 6, total: 8, has_more: false });
    expect(last.items).toHaveLength(2);
    expect(
      await api.getMatches({ view: "all", limit: 2, offset: 7 }),
    ).toHaveLength(1);
  });

  it("follows has_more across pages for a view larger than one page", async () => {
    const fixtures = makeHostedFixtures();
    const template = fixtures.matches[0];
    fixtures.matches = Array.from({ length: 120 }, (_, index) => ({
      ...template,
      id: `match-saved-${String(index).padStart(3, "0")}`,
      detected_at: new Date(Date.UTC(2026, 0, 1, 0, index)).toISOString(),
      saved: index % 2 === 0,
    }));
    const api = createMockHostedApi({ fixtures });

    const saved = await api.getMatchView("saved");

    expect(saved).toHaveLength(60);
    expect(new Set(saved.map((match) => match.id)).size).toBe(60);
    expect(saved[0].id).toBe("match-saved-118");
    const firstPage = await api.getMatchPage({ view: "saved" });
    expect(firstPage).toMatchObject({ limit: 50, total: 60, has_more: true });
  });

  it("moves a match between views without losing its save", async () => {
    const api = createMockHostedApi();

    await api.updateMatch("match-stripe-001", { saved: true });
    await api.updateMatch("match-stripe-001", { dismissed: true });
    const dismissedIds = (await api.getMatchView("dismissed")).map((m) => m.id);
    const savedIds = (await api.getMatchView("saved")).map((m) => m.id);
    expect(dismissedIds).toContain("match-stripe-001");
    expect(savedIds).toContain("match-stripe-001");
    expect((await api.getMatches()).map((m) => m.id)).not.toContain(
      "match-stripe-001",
    );

    const restored = await api.updateMatch("match-stripe-001", {
      dismissed: false,
    });
    expect(restored).toMatchObject({ dismissed: false, saved: true });
    expect((await api.getMatches()).map((m) => m.id)).toContain(
      "match-stripe-001",
    );
    expect((await api.getMatchView("dismissed")).map((m) => m.id)).not.toContain(
      "match-stripe-001",
    );
  });

  it("rejects unsupported views and out-of-range pagination like the API", async () => {
    const api = createMockHostedApi();

    await expect(api.getMatchView("applied")).rejects.toMatchObject({
      status: 400,
    });
    await expect(api.getMatchPage({ limit: 0 })).rejects.toMatchObject({
      status: 422,
    });
    await expect(api.getMatchPage({ limit: 101 })).rejects.toMatchObject({
      status: 422,
    });
    await expect(api.getMatchPage({ offset: -1 })).rejects.toMatchObject({
      status: 422,
    });
  });

  it("can fail one view, or every view, for error-state previews", async () => {
    const one = createMockHostedApi({
      failures: { "matches.saved": "Saved matches failed." },
    });
    await expect(one.getMatchView("saved")).rejects.toThrow(
      "Saved matches failed.",
    );
    await expect(one.getMatchView("dismissed")).resolves.toHaveLength(1);
    await expect(one.getMatches()).resolves.toHaveLength(6);

    const all = createMockHostedApi({ failures: { matches: "Down." } });
    await expect(all.getMatchView("dismissed")).rejects.toThrow("Down.");
  });

  it("requires the demo session for every view after account deletion", async () => {
    const api = createMockHostedApi();
    await api.deleteAccount({ password: "secure password" });

    await expect(api.getMatchView("saved")).rejects.toMatchObject({
      status: 401,
    });
  });
});

describe("hosted match views", () => {
  afterEach(() => {
    vi.unstubAllGlobals();
  });

  it("renders live match data returned by the API", async () => {
    const client = liveClientWith(async () => [normalizeMatch(apiMatch())]);
    render(<App initialPath="/app/matches" client={client} />);

    expect(
      await screen.findByText("Software Engineering Intern, Payments"),
    ).toBeInTheDocument();
    expect(screen.getByText("Stripe is on your watchlist")).toBeInTheDocument();
    expect(
      screen.getByRole("link", { name: "Apply now ↗" }),
    ).toHaveAttribute("href", "https://stripe.com/jobs/1");
  });

  it("shows a genuine empty state when a live user has no matches", async () => {
    const client = liveClientWith(async () => []);
    render(<App initialPath="/app/matches" client={client} />);

    expect(
      await screen.findByText("No matching jobs yet"),
    ).toBeInTheDocument();
    expect(
      screen.queryByText("Software Engineering Intern, Payments"),
    ).not.toBeInTheDocument();
    expect(screen.queryByText(DEMO_BANNER)).not.toBeInTheDocument();
  });

  it("renders a loading state and then an error state on failure", async () => {
    const client = liveClientWith(async () => {
      throw new Error("Match lookup failed.");
    });
    render(<App initialPath="/app/matches" client={client} />);

    expect(screen.getByRole("status")).toHaveTextContent(
      "Loading your FindSooner workspace",
    );
    expect(await screen.findByRole("alert")).toHaveTextContent(
      "Match lookup failed.",
    );
  });

  it("persists a save through the API and reflects it in the UI", async () => {
    const updateMatch = vi.fn(async (id, changes) =>
      normalizeMatch(
        apiMatch({
          saved_at: changes.saved ? new Date().toISOString() : null,
        }),
      ),
    );
    const client = liveClientWith(
      async () => [normalizeMatch(apiMatch())],
      updateMatch,
    );
    render(<App initialPath="/app/matches" client={client} />);
    await screen.findByText("Software Engineering Intern, Payments");

    fireEvent.click(screen.getByRole("button", { name: "☆ Save" }));

    expect(
      await screen.findByRole("button", { name: "★ Saved" }),
    ).toBeInTheDocument();
    expect(updateMatch).toHaveBeenCalledWith(apiMatch().id, { saved: true });
  });

  it("dismisses a match through the API and removes it from the list", async () => {
    const updateMatch = vi.fn(async () =>
      normalizeMatch(apiMatch({ dismissed_at: new Date().toISOString() })),
    );
    const client = liveClientWith(
      async () => [normalizeMatch(apiMatch())],
      updateMatch,
    );
    render(<App initialPath="/app/matches" client={client} />);
    await screen.findByText("Software Engineering Intern, Payments");

    fireEvent.click(screen.getByRole("button", { name: "Dismiss" }));

    await waitFor(() =>
      expect(
        screen.queryByText("Software Engineering Intern, Payments"),
      ).not.toBeInTheDocument(),
    );
    expect(updateMatch).toHaveBeenCalledWith(apiMatch().id, {
      dismissed: true,
    });
    expect(
      await screen.findByText("No matching jobs yet"),
    ).toBeInTheDocument();
  });

  it("surfaces an error when a save cannot be persisted", async () => {
    const client = liveClientWith(
      async () => [normalizeMatch(apiMatch())],
      async () => {
        throw new Error("That change could not be saved.");
      },
    );
    render(<App initialPath="/app/matches" client={client} />);
    await screen.findByText("Software Engineering Intern, Payments");

    fireEvent.click(screen.getByRole("button", { name: "☆ Save" }));

    expect(await screen.findByRole("alert")).toHaveTextContent(
      "That change could not be saved.",
    );
  });

  it("shows the demo-data banner in mock mode", async () => {
    render(
      <App initialPath="/app/dashboard" client={createMockHostedApi()} />,
    );
    await screen.findByRole("heading", { name: "Good morning." });
    expect(screen.getByText(DEMO_BANNER)).toBeInTheDocument();
  });

  it("does not show the demo-data banner in live mode", async () => {
    const client = liveClientWith(async () => [normalizeMatch(apiMatch())]);
    render(<App initialPath="/app/dashboard" client={client} />);
    await screen.findByRole("heading", { name: "Good morning." });
    expect(screen.queryByText(DEMO_BANNER)).not.toBeInTheDocument();
  });
});
