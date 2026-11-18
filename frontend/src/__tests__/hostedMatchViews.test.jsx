import {
  fireEvent,
  render,
  screen,
  waitFor,
  within,
} from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";

import App from "../App.jsx";
import { createHttpHostedApi, createMockHostedApi } from "../hosted/api.js";

// Match views on the Matches page, exercised through the real HTTP client so
// the requests (view, limit, offset, credentials) are the ones the API sees.

const NOW = Date.now();
const minutesAgo = (minutes) => new Date(NOW - minutes * 60_000).toISOString();

let nextId = 0;
function apiMatch(overrides = {}) {
  nextId += 1;
  const suffix = String(nextId).padStart(12, "0");
  return {
    id: `11111111-1111-4111-8111-${suffix}`,
    job_id: `22222222-2222-4222-8222-${suffix}`,
    company_id: "stripe",
    company: "Stripe",
    title: `Intern ${nextId}`,
    location: "New York, NY",
    remote: false,
    remote_status: "",
    role_id: "software_engineering",
    application_url: null,
    posting_date: null,
    deadline: null,
    is_open: true,
    match_reasons: [],
    matched_at: minutesAgo(nextId),
    last_matched_at: minutesAgo(nextId),
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
    json: async () => body,
  };
}

function page(items, { limit = 50, offset = 0, total, hasMore = false } = {}) {
  return {
    items,
    limit,
    offset,
    total: total ?? offset + items.length,
    has_more: hasMore,
  };
}

const WORKSPACE = {
  "/api/companies": [],
  "/api/me": {
    id: "user-1",
    email: "student@example.com",
    email_verified: true,
    last_successful_scan_at: null,
  },
  "/api/preferences": {
    role_ids: ["software_engineering"],
    preferred_locations: [],
    include_remote: true,
    internship_season: "Any season",
    alert_frequency: "as_detected",
    globally_paused: false,
  },
  "/api/watchlist": [],
};

/**
 * A fetch stub answering by exact request URL. A route value may be a body,
 * a function returning a response (or a promise of one), or a list consumed
 * in order.
 */
function routedFetch(routes) {
  const fetchMock = vi.fn(async (url, options = {}) => {
    if (options.method === "PATCH") {
      const route = routes[`PATCH ${url}`];
      if (!route) throw new Error(`Unexpected PATCH ${url}`);
      return route(JSON.parse(options.body));
    }
    if (url in WORKSPACE) return jsonResponse(WORKSPACE[url]);
    let route = routes[url];
    if (Array.isArray(route) && typeof route[0] === "function") {
      route = route.shift();
    }
    if (route === undefined) throw new Error(`Unexpected GET ${url}`);
    if (typeof route === "function") return route();
    return jsonResponse(route);
  });
  vi.stubGlobal("fetch", fetchMock);
  return fetchMock;
}

function deferred() {
  let resolve;
  const promise = new Promise((done) => {
    resolve = done;
  });
  return { promise, resolve };
}

const requestedUrls = (fetchMock) =>
  fetchMock.mock.calls.map(([url]) => url);

const viewButton = (name) =>
  within(screen.getByRole("group", { name: "Match view" })).getByRole(
    "button",
    { name },
  );

const list = () => screen.getByRole("region", { name: "Job matches" });

function renderMatches() {
  render(<App initialPath="/app/matches" client={createHttpHostedApi()} />);
}

describe("Matches page views", () => {
  afterEach(() => {
    vi.unstubAllGlobals();
  });

  it("defaults to Active using the existing request and no view query", async () => {
    const active = apiMatch({ title: "Active Intern" });
    const fetchMock = routedFetch({ "/api/matches": page([active]) });
    renderMatches();

    expect(await screen.findByText("Active Intern")).toBeInTheDocument();
    expect(viewButton("Active")).toHaveAttribute("aria-pressed", "true");
    expect(viewButton("Saved")).toHaveAttribute("aria-pressed", "false");
    expect(requestedUrls(fetchMock).filter((url) => url.includes("matches")))
      .toEqual(["/api/matches"]);
    expect(screen.getByRole("button", { name: "Dismiss" })).toBeInTheDocument();
  });

  it("loads every Saved page and labels closed and no-longer-matching saves", async () => {
    const savedAt = minutesAgo(1);
    const openSave = apiMatch({ title: "Open Saved Intern", saved_at: savedAt });
    const closedSave = apiMatch({
      title: "Closed Saved Intern",
      saved_at: savedAt,
      is_open: false,
    });
    const staleSave = apiMatch({
      title: "Stale Saved Intern",
      saved_at: savedAt,
      no_longer_matches_at: savedAt,
    });
    const fetchMock = routedFetch({
      "/api/matches": page([apiMatch({ title: "Active Intern" })]),
      "/api/matches?view=saved": page([openSave, closedSave], {
        limit: 2,
        total: 3,
        hasMore: true,
      }),
      "/api/matches?view=saved&limit=2&offset=2": page([staleSave], {
        limit: 2,
        offset: 2,
        total: 3,
      }),
    });
    renderMatches();
    await screen.findByText("Active Intern");

    fireEvent.click(viewButton("Saved"));

    expect(await screen.findByText("Stale Saved Intern")).toBeInTheDocument();
    expect(screen.getByText("Open Saved Intern")).toBeInTheDocument();
    expect(screen.queryByText("Active Intern")).not.toBeInTheDocument();
    expect(viewButton("Saved")).toHaveAttribute("aria-pressed", "true");
    expect(
      fetchMock.mock.calls
        .filter(([url]) => url.includes("view=saved"))
        .map(([url, options]) => [url, options.credentials]),
    ).toEqual([
      ["/api/matches?view=saved", "include"],
      ["/api/matches?view=saved&limit=2&offset=2", "include"],
    ]);

    const cards = within(list()).getAllByRole("article");
    expect(cards).toHaveLength(3);
    const card = (title) =>
      cards.find((item) => within(item).queryByText(title));
    expect(within(card("Closed Saved Intern")).getByText("Closed"))
      .toBeInTheDocument();
    expect(
      within(card("Stale Saved Intern")).getByText("No longer matches"),
    ).toBeInTheDocument();
    expect(within(card("Open Saved Intern")).queryByText("Closed")).toBeNull();
    expect(
      within(card("Open Saved Intern")).queryByText("No longer matches"),
    ).toBeNull();
    expect(document.querySelector(".results-summary")).toHaveTextContent(
      "3 matches",
    );
  });

  it("loads every Dismissed page", async () => {
    const dismissedAt = minutesAgo(1);
    routedFetch({
      "/api/matches": page([]),
      "/api/matches?view=dismissed": page(
        [apiMatch({ title: "Dismissed One", dismissed_at: dismissedAt })],
        { limit: 1, total: 2, hasMore: true },
      ),
      "/api/matches?view=dismissed&limit=1&offset=1": page(
        [apiMatch({ title: "Dismissed Two", dismissed_at: dismissedAt })],
        { limit: 1, offset: 1, total: 2 },
      ),
    });
    renderMatches();
    await screen.findByText("No matching jobs yet");

    fireEvent.click(viewButton("Dismissed"));

    expect(await screen.findByText("Dismissed Two")).toBeInTheDocument();
    expect(screen.getByText("Dismissed One")).toBeInTheDocument();
    // Dismissed matches offer Restore; dismissing again is not offered here.
    expect(within(list()).queryByRole("button", { name: "Dismiss" })).toBeNull();
    expect(
      within(list()).getAllByRole("button", { name: "Restore" }),
    ).toHaveLength(2);
  });

  it("shows loading and then a view-specific empty state", async () => {
    const pending = deferred();
    routedFetch({
      "/api/matches": page([apiMatch({ title: "Active Intern" })]),
      "/api/matches?view=saved": () => pending.promise,
    });
    renderMatches();
    await screen.findByText("Active Intern");

    fireEvent.click(viewButton("Saved"));

    expect(await screen.findByRole("status")).toHaveTextContent(
      "Loading saved matches",
    );
    pending.resolve(jsonResponse(page([])));
    expect(await screen.findByText("No saved matches yet")).toBeInTheDocument();
    expect(screen.queryByRole("status")).not.toBeInTheDocument();
  });

  it("shows an error for a failed view and recovers on retry", async () => {
    routedFetch({
      "/api/matches": page([apiMatch({ title: "Active Intern" })]),
      "/api/matches?view=dismissed": [
        () => jsonResponse({ detail: "Dismissed lookup failed." }, 500),
        () =>
          jsonResponse(
            page([
              apiMatch({ title: "Dismissed Intern", dismissed_at: minutesAgo(1) }),
            ]),
          ),
      ],
    });
    renderMatches();
    await screen.findByText("Active Intern");

    fireEvent.click(viewButton("Dismissed"));

    expect(await screen.findByRole("alert")).toHaveTextContent(
      "Dismissed lookup failed.",
    );
    expect(screen.queryByText("Active Intern")).not.toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "Try again" }));
    expect(await screen.findByText("Dismissed Intern")).toBeInTheDocument();
    expect(screen.queryByRole("alert")).not.toBeInTheDocument();
  });

  it("applies search, company, role, location and freshness filters within a view", async () => {
    const savedAt = minutesAgo(1);
    routedFetch({
      "/api/matches": page([apiMatch({ title: "Active Intern", company: "Acme" })]),
      "/api/matches?view=saved": page([
        apiMatch({ title: "Stripe Saved", saved_at: savedAt }),
        apiMatch({
          title: "Figma Saved",
          company: "Figma",
          location: "Remote",
          role_id: "product_management",
          saved_at: savedAt,
        }),
        apiMatch({
          title: "Old Saved",
          company: "Figma",
          location: "Remote",
          role_id: "product_management",
          matched_at: minutesAgo(3 * 1_440),
          saved_at: savedAt,
        }),
      ]),
    });
    renderMatches();
    await screen.findByText("Active Intern");
    fireEvent.click(viewButton("Saved"));
    await screen.findByText("Stripe Saved");

    const companyFilter = screen.getByLabelText("Company filter");
    // Options come from the selected view, not the Active list.
    expect(
      within(companyFilter).queryByRole("option", { name: "Acme" }),
    ).toBeNull();
    fireEvent.change(companyFilter, { target: { value: "Figma" } });
    expect(screen.queryByText("Stripe Saved")).not.toBeInTheDocument();
    expect(screen.getByText("Figma Saved")).toBeInTheDocument();
    expect(screen.getByText("Old Saved")).toBeInTheDocument();

    fireEvent.change(screen.getByLabelText("Freshness filter"), {
      target: { value: "older" },
    });
    expect(screen.queryByText("Figma Saved")).not.toBeInTheDocument();
    expect(screen.getByText("Old Saved")).toBeInTheDocument();

    fireEvent.click(screen.getByRole("button", { name: "Clear all filters" }));
    fireEvent.change(screen.getByLabelText("Location filter"), {
      target: { value: "New York, NY" },
    });
    expect(screen.getByText("Stripe Saved")).toBeInTheDocument();
    expect(screen.queryByText("Figma Saved")).not.toBeInTheDocument();

    fireEvent.click(screen.getByRole("button", { name: "Clear all filters" }));
    fireEvent.change(screen.getByLabelText("Role filter"), {
      target: { value: "product_management" },
    });
    expect(screen.queryByText("Stripe Saved")).not.toBeInTheDocument();
    expect(screen.getByText("Figma Saved")).toBeInTheDocument();

    fireEvent.change(screen.getByPlaceholderText(
      "Search title, company, or location",
    ), { target: { value: "old" } });
    expect(screen.queryByText("Figma Saved")).not.toBeInTheDocument();
    expect(screen.getByText("Old Saved")).toBeInTheDocument();

    fireEvent.change(screen.getByPlaceholderText(
      "Search title, company, or location",
    ), { target: { value: "nothing like this" } });
    expect(screen.getByText("No matches fit these filters")).toBeInTheDocument();
  });

  it("saves a dismissed match and unsaves from Saved, keeping Active consistent", async () => {
    const savedAt = minutesAgo(1);
    const shared = apiMatch({ title: "Shared Intern", saved_at: savedAt });
    const dismissed = apiMatch({
      title: "Dismissed Intern",
      dismissed_at: savedAt,
    });
    const patches = [];
    routedFetch({
      "/api/matches": page([shared]),
      "/api/matches?view=saved": page([shared]),
      "/api/matches?view=dismissed": page([dismissed]),
      [`PATCH /api/matches/${shared.id}`]: (body) => {
        patches.push([shared.id, body]);
        return jsonResponse({ ...shared, saved_at: body.saved ? savedAt : null });
      },
      [`PATCH /api/matches/${dismissed.id}`]: (body) => {
        patches.push([dismissed.id, body]);
        return jsonResponse({ ...dismissed, saved_at: body.saved ? savedAt : null });
      },
    });
    renderMatches();
    await screen.findByText("Shared Intern");

    fireEvent.click(viewButton("Dismissed"));
    await screen.findByText("Dismissed Intern");
    fireEvent.click(screen.getByRole("button", { name: "☆ Save" }));
    expect(
      await screen.findByRole("button", { name: "★ Saved" }),
    ).toBeInTheDocument();
    // Saving does not move it out of Dismissed.
    expect(screen.getByText("Dismissed Intern")).toBeInTheDocument();

    fireEvent.click(viewButton("Saved"));
    await screen.findByText("Shared Intern");
    fireEvent.click(screen.getByRole("button", { name: "★ Saved" }));
    expect(await screen.findByText("No saved matches yet")).toBeInTheDocument();

    fireEvent.click(viewButton("Active"));
    expect(await screen.findByText("Shared Intern")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "☆ Save" })).toBeInTheDocument();
    expect(patches).toEqual([
      [dismissed.id, { saved: true }],
      [shared.id, { saved: false }],
    ]);
  });

  it("leaves a view unchanged and shows an error when saving fails", async () => {
    const saved = apiMatch({ title: "Saved Intern", saved_at: minutesAgo(1) });
    routedFetch({
      "/api/matches": page([]),
      "/api/matches?view=saved": page([saved]),
      [`PATCH /api/matches/${saved.id}`]: () =>
        jsonResponse({ detail: "That change could not be saved." }, 500),
    });
    renderMatches();
    await screen.findByText("No matching jobs yet");
    fireEvent.click(viewButton("Saved"));
    await screen.findByText("Saved Intern");

    fireEvent.click(screen.getByRole("button", { name: "★ Saved" }));

    expect(await screen.findByRole("alert")).toHaveTextContent(
      "That change could not be saved.",
    );
    expect(screen.getByText("Saved Intern")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "★ Saved" })).toBeInTheDocument();
  });

  it("ignores a late response from a previously selected view", async () => {
    const slowSaved = deferred();
    const slowDismissed = deferred();
    routedFetch({
      "/api/matches": page([apiMatch({ title: "Active Intern" })]),
      "/api/matches?view=saved": () => slowSaved.promise,
      "/api/matches?view=dismissed": () => slowDismissed.promise,
    });
    renderMatches();
    await screen.findByText("Active Intern");

    fireEvent.click(viewButton("Saved"));
    fireEvent.click(viewButton("Dismissed"));
    slowDismissed.resolve(
      jsonResponse(
        page([apiMatch({ title: "Dismissed Intern", dismissed_at: minutesAgo(1) })]),
      ),
    );
    await screen.findByText("Dismissed Intern");
    slowSaved.resolve(
      jsonResponse(
        page([apiMatch({ title: "Late Saved Intern", saved_at: minutesAgo(1) })]),
      ),
    );
    await new Promise((resolve) => setTimeout(resolve, 0));
    expect(screen.queryByText("Late Saved Intern")).not.toBeInTheDocument();
    expect(screen.getByText("Dismissed Intern")).toBeInTheDocument();
    expect(viewButton("Dismissed")).toHaveAttribute("aria-pressed", "true");

    // Switching back to Active also discards an in-flight view request.
    fireEvent.click(viewButton("Saved"));
    fireEvent.click(viewButton("Active"));
    expect(screen.getByText("Active Intern")).toBeInTheDocument();
    await new Promise((resolve) => setTimeout(resolve, 0));
    expect(screen.getByText("Active Intern")).toBeInTheDocument();
    expect(screen.queryByText("Late Saved Intern")).not.toBeInTheDocument();
    expect(viewButton("Active")).toHaveAttribute("aria-pressed", "true");
  });
});

describe("Matches page views (demo client)", () => {
  it("shows the demo's saved and dismissed matches", async () => {
    render(<App initialPath="/app/matches" client={createMockHostedApi()} />);
    await screen.findAllByRole("article");

    fireEvent.click(viewButton("Saved"));
    const savedCard = (await within(list()).findAllByRole("article"))[0];
    expect(within(savedCard).getByText("Closed")).toBeInTheDocument();
    expect(within(savedCard).getByText("No longer matches")).toBeInTheDocument();

    fireEvent.click(viewButton("Dismissed"));
    await waitFor(() =>
      expect(within(list()).getAllByRole("article")).toHaveLength(1),
    );
  });
});
