import {
  act,
  fireEvent,
  render,
  screen,
  waitFor,
  within,
} from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";

import App from "../App.jsx";
import { createHttpHostedApi, createMockHostedApi } from "../hosted/api.js";

// Restore from Dismissed and Undo after a dismissal, through the real HTTP
// client so PATCH bodies and GET requests are the ones the API sees.

const NOW = Date.now();
const minutesAgo = (minutes) => new Date(NOW - minutes * 60_000).toISOString();

let nextId = 0;
function apiMatch(overrides = {}) {
  nextId += 1;
  const suffix = String(nextId).padStart(12, "0");
  return {
    id: `33333333-3333-4333-8333-${suffix}`,
    job_id: `44444444-4444-4444-8444-${suffix}`,
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
  return { ok: status >= 200 && status < 300, status, json: async () => body };
}

const page = (items) => ({
  items,
  limit: 50,
  offset: 0,
  total: items.length,
  has_more: false,
});

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
 * Answers GETs by exact URL (a body, a function, or a list of functions used
 * in order) and PATCHes through `PATCH <url>` handlers. Records every PATCH.
 */
function routedFetch(routes) {
  const patches = [];
  const fetchMock = vi.fn(async (url, options = {}) => {
    if (options.method === "PATCH") {
      const body = JSON.parse(options.body);
      patches.push([url, body]);
      const route = routes[`PATCH ${url}`];
      if (!route) throw new Error(`Unexpected PATCH ${url}`);
      return route(body);
    }
    if (url in WORKSPACE) return jsonResponse(WORKSPACE[url]);
    let route = routes[url];
    if (Array.isArray(route)) route = route.shift();
    if (route === undefined) throw new Error(`Unexpected GET ${url}`);
    if (typeof route === "function") return route();
    return jsonResponse(route);
  });
  vi.stubGlobal("fetch", fetchMock);
  return { fetchMock, patches };
}

function deferred() {
  let resolve;
  const promise = new Promise((done) => {
    resolve = done;
  });
  return { promise, resolve };
}

const flush = () => new Promise((resolve) => setTimeout(resolve, 0));

const viewButton = (name) =>
  within(screen.getByRole("group", { name: "Match view" })).getByRole(
    "button",
    { name },
  );
const list = () => screen.getByRole("region", { name: "Job matches" });
const cardFor = (title) =>
  within(list())
    .getAllByRole("article")
    .find((card) => within(card).queryByText(title));
const activeGets = (fetchMock) =>
  fetchMock.mock.calls.filter(([url]) => url === "/api/matches").length;

function renderMatches() {
  render(<App initialPath="/app/matches" client={createHttpHostedApi()} />);
}

// The PATCH response for a record after `changes`, as the API returns it.
const patched = (record, changes) => ({
  ...record,
  ...("dismissed" in changes && {
    dismissed_at: changes.dismissed ? minutesAgo(0) : null,
  }),
  ...("saved" in changes && {
    saved_at: changes.saved ? minutesAgo(0) : null,
  }),
});

describe("Restoring dismissed matches", () => {
  afterEach(() => {
    vi.unstubAllGlobals();
  });

  it("restores a match absent from Active into Active, keeping its save, without a reload", async () => {
    // A closed posting still belongs in Active: backend eligibility ignores is_open.
    const hidden = apiMatch({
      title: "Hidden Intern",
      saved_at: minutesAgo(5),
      dismissed_at: minutesAgo(4),
      is_open: false,
    });
    const { fetchMock, patches } = routedFetch({
      "/api/matches": page([apiMatch({ title: "Active Intern" })]),
      "/api/matches?view=dismissed": page([hidden]),
      [`PATCH /api/matches/${hidden.id}`]: (body) =>
        jsonResponse(patched(hidden, body)),
    });
    renderMatches();
    await screen.findByText("Active Intern");
    fireEvent.click(viewButton("Dismissed"));
    await screen.findByText("Hidden Intern");

    fireEvent.click(
      within(cardFor("Hidden Intern")).getByRole("button", { name: "Restore" }),
    );

    expect(await screen.findByText("No dismissed matches")).toBeInTheDocument();
    expect(patches).toEqual([
      [`/api/matches/${hidden.id}`, { dismissed: false }],
    ]);
    fireEvent.click(viewButton("Active"));
    const restored = cardFor("Hidden Intern");
    expect(restored).toBeTruthy();
    expect(
      within(restored).getByRole("button", { name: "★ Saved" }),
    ).toBeInTheDocument();
    expect(screen.getByText("Active Intern")).toBeInTheDocument();
    expect(activeGets(fetchMock)).toBe(1);
  });

  it("does not add a restored match that no longer matches to Active", async () => {
    const stale = apiMatch({
      title: "Stale Intern",
      dismissed_at: minutesAgo(4),
      no_longer_matches_at: minutesAgo(3),
    });
    routedFetch({
      "/api/matches": page([apiMatch({ title: "Active Intern" })]),
      "/api/matches?view=dismissed": page([stale]),
      [`PATCH /api/matches/${stale.id}`]: (body) =>
        jsonResponse(patched(stale, body)),
    });
    renderMatches();
    await screen.findByText("Active Intern");
    fireEvent.click(viewButton("Dismissed"));
    await screen.findByText("Stale Intern");

    fireEvent.click(screen.getByRole("button", { name: "Restore" }));

    expect(await screen.findByText("No dismissed matches")).toBeInTheDocument();
    expect(screen.getByText(/no longer matches your preferences/i))
      .toBeInTheDocument();
    fireEvent.click(viewButton("Active"));
    expect(screen.getByText("Active Intern")).toBeInTheDocument();
    expect(screen.queryByText("Stale Intern")).not.toBeInTheDocument();
  });

  it("keeps both lists unchanged and shows an error when restoring fails", async () => {
    const dismissed = apiMatch({
      title: "Dismissed Intern",
      dismissed_at: minutesAgo(4),
    });
    routedFetch({
      "/api/matches": page([apiMatch({ title: "Active Intern" })]),
      "/api/matches?view=dismissed": page([dismissed]),
      [`PATCH /api/matches/${dismissed.id}`]: () =>
        jsonResponse({ detail: "Restore failed upstream." }, 500),
    });
    renderMatches();
    await screen.findByText("Active Intern");
    fireEvent.click(viewButton("Dismissed"));
    await screen.findByText("Dismissed Intern");

    fireEvent.click(screen.getByRole("button", { name: "Restore" }));

    expect(await screen.findByRole("alert")).toHaveTextContent(
      "Restore failed upstream.",
    );
    expect(screen.getByText("Dismissed Intern")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Restore" })).toBeEnabled();
    fireEvent.click(viewButton("Active"));
    expect(screen.getByText("Active Intern")).toBeInTheDocument();
    expect(screen.queryByText("Dismissed Intern")).not.toBeInTheDocument();
  });

  it("sends one restore while it is pending", async () => {
    const dismissed = apiMatch({
      title: "Dismissed Intern",
      dismissed_at: minutesAgo(4),
    });
    const pending = deferred();
    const { patches } = routedFetch({
      "/api/matches": page([]),
      "/api/matches?view=dismissed": page([dismissed]),
      [`PATCH /api/matches/${dismissed.id}`]: () => pending.promise,
    });
    renderMatches();
    await screen.findByText("No matching jobs yet");
    fireEvent.click(viewButton("Dismissed"));
    await screen.findByText("Dismissed Intern");

    const restore = screen.getByRole("button", { name: "Restore" });
    fireEvent.click(restore);
    fireEvent.click(restore);
    await flush();

    expect(restore).toBeDisabled();
    expect(
      within(cardFor("Dismissed Intern")).getByRole("button", {
        name: "☆ Save",
      }),
    ).toBeDisabled();
    expect(patches).toHaveLength(1);
    pending.resolve(jsonResponse(patched(dismissed, { dismissed: false })));
    expect(await screen.findByText("No dismissed matches")).toBeInTheDocument();
  });

  it("does not let a stale Dismissed response bring back a restored match", async () => {
    const dismissed = apiMatch({
      title: "Dismissed Intern",
      dismissed_at: minutesAgo(4),
    });
    const patch = deferred();
    const staleReload = deferred();
    routedFetch({
      "/api/matches": page([]),
      "/api/matches?view=dismissed": [
        () => jsonResponse(page([dismissed])),
        () => staleReload.promise,
      ],
      "/api/matches?view=saved": page([]),
      [`PATCH /api/matches/${dismissed.id}`]: () => patch.promise,
    });
    renderMatches();
    await screen.findByText("No matching jobs yet");
    fireEvent.click(viewButton("Dismissed"));
    await screen.findByText("Dismissed Intern");

    fireEvent.click(screen.getByRole("button", { name: "Restore" }));
    fireEvent.click(viewButton("Saved"));
    await screen.findByText("No saved matches yet");
    fireEvent.click(viewButton("Dismissed"));
    // The PATCH succeeds while the reload is in flight; the reload was read
    // before the restore landed and still lists the match.
    await act(async () => {
      patch.resolve(jsonResponse(patched(dismissed, { dismissed: false })));
      await flush();
    });
    staleReload.resolve(jsonResponse(page([dismissed])));

    expect(await screen.findByText("No dismissed matches")).toBeInTheDocument();
    fireEvent.click(viewButton("Active"));
    expect(screen.getByText("Dismissed Intern")).toBeInTheDocument();
  });
});

describe("Undoing a dismissal", () => {
  afterEach(() => {
    vi.unstubAllGlobals();
  });

  it("undoes a dismissal from the notice, keeping the save", async () => {
    const saved = apiMatch({ title: "Saved Intern", saved_at: minutesAgo(5) });
    const { fetchMock, patches } = routedFetch({
      "/api/matches": page([saved]),
      [`PATCH /api/matches/${saved.id}`]: (body) =>
        jsonResponse(patched(saved, body)),
    });
    renderMatches();
    await screen.findByText("Saved Intern");

    fireEvent.click(screen.getByRole("button", { name: "Dismiss" }));
    const notice = await screen.findByRole("status");
    expect(notice).toHaveTextContent("Dismissed “Saved Intern”.");
    expect(screen.queryByRole("article")).not.toBeInTheDocument();

    fireEvent.click(within(notice).getByRole("button", { name: "Undo" }));

    const card = await within(list()).findByRole("article");
    expect(within(card).getByText("Saved Intern")).toBeInTheDocument();
    expect(
      within(card).getByRole("button", { name: "★ Saved" }),
    ).toBeInTheDocument();
    await waitFor(() =>
      expect(screen.queryByRole("button", { name: "Undo" })).toBeNull(),
    );
    expect(patches).toEqual([
      [`/api/matches/${saved.id}`, { dismissed: true }],
      [`/api/matches/${saved.id}`, { dismissed: false }],
    ]);
    expect(activeGets(fetchMock)).toBe(1);
  });

  it("shows no notice and keeps the match when dismissing fails", async () => {
    const match = apiMatch({ title: "Kept Intern" });
    routedFetch({
      "/api/matches": page([match]),
      [`PATCH /api/matches/${match.id}`]: () =>
        jsonResponse({ detail: "Dismiss failed upstream." }, 500),
    });
    renderMatches();
    await screen.findByText("Kept Intern");

    fireEvent.click(screen.getByRole("button", { name: "Dismiss" }));

    expect(await screen.findByRole("alert")).toHaveTextContent(
      "Dismiss failed upstream.",
    );
    expect(screen.getByText("Kept Intern")).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Undo" })).toBeNull();
  });

  it("keeps the match dismissed and the notice available when undo fails", async () => {
    const match = apiMatch({ title: "Gone Intern" });
    routedFetch({
      "/api/matches": page([match]),
      "/api/matches?view=dismissed": page([patched(match, { dismissed: true })]),
      [`PATCH /api/matches/${match.id}`]: (body) =>
        body.dismissed
          ? jsonResponse(patched(match, body))
          : jsonResponse({ detail: "Undo failed upstream." }, 500),
    });
    renderMatches();
    await screen.findByText("Gone Intern");
    fireEvent.click(screen.getByRole("button", { name: "Dismiss" }));
    const undo = await screen.findByRole("button", { name: "Undo" });

    fireEvent.click(undo);

    expect(await screen.findByRole("alert")).toHaveTextContent(
      "Undo failed upstream.",
    );
    expect(screen.queryByRole("article")).not.toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Undo" })).toBeEnabled();
    fireEvent.click(viewButton("Dismissed"));
    expect(await screen.findByText("Gone Intern")).toBeInTheDocument();
  });

  it("does not let a stale Dismissed response hide a dismissal made during the load", async () => {
    const match = apiMatch({ title: "Racing Intern" });
    const patch = deferred();
    const staleLoad = deferred();
    routedFetch({
      "/api/matches": page([match]),
      "/api/matches?view=dismissed": () => staleLoad.promise,
      [`PATCH /api/matches/${match.id}`]: () => patch.promise,
    });
    renderMatches();
    await screen.findByText("Racing Intern");

    fireEvent.click(screen.getByRole("button", { name: "Dismiss" }));
    fireEvent.click(viewButton("Dismissed"));
    await act(async () => {
      patch.resolve(jsonResponse(patched(match, { dismissed: true })));
      await flush();
    });
    // Read before the dismissal landed.
    staleLoad.resolve(jsonResponse(page([])));

    expect(await screen.findByText("Racing Intern")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Restore" })).toBeInTheDocument();
  });
});

describe("Match details drawer", () => {
  afterEach(() => {
    vi.unstubAllGlobals();
  });

  const drawer = () => screen.queryByRole("dialog");
  const openDetails = (title) =>
    fireEvent.click(
      within(cardFor(title)).getByRole("button", { name: "View details" }),
    );

  it("closes when an unsave removes its match from Saved", async () => {
    const saved = apiMatch({ title: "Saved Intern", saved_at: minutesAgo(5) });
    const { patches } = routedFetch({
      "/api/matches": page([saved]),
      "/api/matches?view=saved": page([saved]),
      [`PATCH /api/matches/${saved.id}`]: (body) =>
        jsonResponse(patched(saved, body)),
    });
    renderMatches();
    await screen.findByText("Saved Intern");
    fireEvent.click(viewButton("Saved"));
    await within(list()).findByText("Saved Intern");
    openDetails("Saved Intern");
    expect(drawer()).toHaveTextContent("Saved Intern");

    fireEvent.click(
      within(cardFor("Saved Intern")).getByRole("button", { name: "★ Saved" }),
    );

    expect(await screen.findByText("No saved matches yet")).toBeInTheDocument();
    expect(drawer()).toBeNull();
    expect(patches).toEqual([[`/api/matches/${saved.id}`, { saved: false }]]);
  });

  it("stays open on the updated match after a save, sending one request while pending", async () => {
    const match = apiMatch({ title: "Open Intern" });
    const pending = deferred();
    const { patches } = routedFetch({
      "/api/matches": page([match]),
      [`PATCH /api/matches/${match.id}`]: () => pending.promise,
    });
    renderMatches();
    await screen.findByText("Open Intern");
    openDetails("Open Intern");

    const save = within(cardFor("Open Intern")).getByRole("button", {
      name: "☆ Save",
    });
    fireEvent.click(save);
    fireEvent.click(save);
    await flush();
    expect(save).toBeDisabled();
    expect(patches).toHaveLength(1);

    pending.resolve(jsonResponse(patched(match, { saved: true })));
    await waitFor(() =>
      expect(
        within(cardFor("Open Intern")).getByRole("button", {
          name: "★ Saved",
        }),
      ).toBeEnabled(),
    );
    expect(drawer()).toHaveTextContent("Open Intern");
    expect(patches).toHaveLength(1);
  });

  it("keeps the drawer and Saved list unchanged when an unsave fails", async () => {
    const saved = apiMatch({ title: "Saved Intern", saved_at: minutesAgo(5) });
    routedFetch({
      "/api/matches": page([saved]),
      "/api/matches?view=saved": page([saved]),
      [`PATCH /api/matches/${saved.id}`]: () =>
        jsonResponse({ detail: "Unsave failed upstream." }, 500),
    });
    renderMatches();
    await screen.findByText("Saved Intern");
    fireEvent.click(viewButton("Saved"));
    await within(list()).findByText("Saved Intern");
    openDetails("Saved Intern");

    fireEvent.click(
      within(cardFor("Saved Intern")).getByRole("button", { name: "★ Saved" }),
    );

    expect(await screen.findByRole("alert")).toHaveTextContent(
      "Unsave failed upstream.",
    );
    expect(drawer()).toHaveTextContent("Saved Intern");
    expect(
      within(cardFor("Saved Intern")).getByRole("button", { name: "★ Saved" }),
    ).toBeEnabled();
  });
});

describe("Restore and undo with the demo client", () => {
  it("leaves demo state unchanged when an update is rejected", async () => {
    const failures = {};
    const api = createMockHostedApi({ failures });
    failures.updateMatch = "Demo update failed.";

    await expect(
      api.updateMatch("match-stripe-001", { dismissed: true, saved: true }),
    ).rejects.toThrow("Demo update failed.");

    delete failures.updateMatch;
    const active = await api.getMatches();
    expect(active.map((match) => match.id)).toContain("match-stripe-001");
    expect(active.find((match) => match.id === "match-stripe-001")).toMatchObject(
      { saved: false, saved_at: null, dismissed: false, dismissed_at: null },
    );
    expect(
      (await api.getMatchView("dismissed")).map((match) => match.id),
    ).not.toContain("match-stripe-001");
    expect(
      (await api.getMatchView("saved")).map((match) => match.id),
    ).not.toContain("match-stripe-001");
  });

  it("restores the demo's dismissed match into Active", async () => {
    render(<App initialPath="/app/matches" client={createMockHostedApi()} />);
    await screen.findAllByRole("article");
    expect(screen.queryByText("Data Science Intern, Risk")).toBeNull();

    fireEvent.click(viewButton("Dismissed"));
    await screen.findByText("Data Science Intern, Risk");
    fireEvent.click(screen.getByRole("button", { name: "Restore" }));
    expect(await screen.findByText("No dismissed matches")).toBeInTheDocument();

    fireEvent.click(viewButton("Active"));
    expect(screen.getByText("Data Science Intern, Risk")).toBeInTheDocument();
  });
});
