import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";

import App from "../App.jsx";
import {
  createHttpHostedApi,
  createMockHostedApi,
} from "../hosted/api.js";
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
      await screen.findByText("No matching internships yet"),
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
      "Loading your Internship Signal workspace",
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
      await screen.findByText("No matching internships yet"),
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
