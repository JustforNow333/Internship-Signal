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
import { makeHostedFixtures } from "../hosted/fixtures.js";

// The Applied toggle, badge, drawer status and Applied view on the Matches
// page: success-only updates, pending clicks, exact-date Undo and focus.

// Noon UTC, so the calendar day is the same in every common time zone.
const APPLIED_AT = "2026-03-14T12:00:00.000Z";
const APPLIED_LABEL = "Applied Mar 14, 2026";

const ACTIVE_APPLIED = "Software Engineering Intern, Payments"; // stripe-001
const ACTIVE_OTHER = "Machine Learning Systems Intern"; // nvidia-002
const CLOSED = "Software Engineering Intern, Editor"; // closed, saved, stale
const DISMISSED = "Data Science Intern, Risk"; // dismissed

// The demo client with three applied matches (active, closed/no-longer-
// matching/saved, dismissed) and updateMatch calls held until settled.
function heldClient() {
  const fixtures = makeHostedFixtures();
  for (const id of ["match-stripe-001", "match-figma-007", "match-stripe-008"]) {
    Object.assign(
      fixtures.matches.find((match) => match.id === id),
      { applied: true, applied_at: APPLIED_AT },
    );
  }
  const client = createMockHostedApi({ fixtures });
  const held = [];
  const updateMatch = client.updateMatch;
  client.updateMatch = (id, changes) =>
    new Promise((resolve, reject) => {
      held.push({
        id,
        changes,
        succeed: () => updateMatch(id, changes).then(resolve, reject),
        fail: () => reject(new Error("The change could not be saved.")),
      });
    });
  return { client, held };
}

const list = () => screen.getByRole("region", { name: "Job matches" });
const viewButton = (name) =>
  within(screen.getByRole("group", { name: "Match view" })).getByRole(
    "button",
    { name },
  );
const card = (title) =>
  within(list())
    .getAllByRole("article")
    .find((item) => within(item).queryByRole("heading", { name: title }));
const titles = () =>
  within(list())
    .queryAllByRole("article")
    .map((item) => within(item).getByRole("heading").textContent);
const titleButton = (title) =>
  within(card(title)).getByRole("button", { name: title });
const appliedToggle = (title) =>
  within(card(title)).getByRole("button", { name: /^(Mark applied|Applied)$/ });
const press = (button) => {
  button.focus();
  fireEvent.click(button);
};
const settle = async (call, how = "succeed") => {
  await act(async () => {
    await call[how]();
  });
};

async function openView(name) {
  const { client, held } = heldClient();
  render(<App initialPath="/app/matches" client={client} />);
  await screen.findAllByRole("article");
  if (name !== "Active") {
    fireEvent.click(viewButton(name));
    await waitFor(() => expect(titles().length).toBeGreaterThan(0));
  }
  return { held, client };
}

describe("Applied toggle on match cards", () => {
  it("shows a pressed Applied toggle and dated badge on applied Active cards", async () => {
    await openView("Active");
    // Applied matches stay in Active.
    expect(titles()).toContain(ACTIVE_APPLIED);
    const toggle = appliedToggle(ACTIVE_APPLIED);
    expect(toggle).toHaveTextContent(/^Applied$/);
    expect(toggle).toHaveAttribute("aria-pressed", "true");
    expect(within(card(ACTIVE_APPLIED)).getByText(APPLIED_LABEL))
      .toBeInTheDocument();

    const other = appliedToggle(ACTIVE_OTHER);
    expect(other).toHaveTextContent(/^Mark applied$/);
    expect(other).toHaveAttribute("aria-pressed", "false");
    expect(within(card(ACTIVE_OTHER)).queryByText(/^Applied /)).toBeNull();
  });

  it("marks applied only after success, keeping Saved and Dismissed unchanged", async () => {
    const { held } = await openView("Active");
    const toggle = appliedToggle(ACTIVE_OTHER);
    press(toggle);

    expect(held).toHaveLength(1);
    expect(held[0].changes).toEqual({ applied: true });
    expect(toggle).toHaveAttribute("aria-pressed", "false");
    expect(toggle).toBeDisabled();

    await settle(held[0]);
    expect(toggle).toHaveAttribute("aria-pressed", "true");
    expect(toggle).toHaveTextContent(/^Applied$/);
    expect(within(card(ACTIVE_OTHER)).getByText(/^Applied \w{3} \d{1,2}, \d{4}$/))
      .toBeInTheDocument();
    expect(titles()).toContain(ACTIVE_OTHER);
    expect(
      within(card(ACTIVE_OTHER)).getByRole("button", { name: "☆ Save" }),
    ).toBeInTheDocument();
    expect(screen.queryByRole("status")).not.toBeInTheDocument();
    expect(toggle).toHaveFocus();
  });

  it("unmarks in Active without removing the card or offering Undo", async () => {
    const { held } = await openView("Active");
    const toggle = appliedToggle(ACTIVE_APPLIED);
    press(toggle);
    expect(held[0].changes).toEqual({ applied: false });
    await settle(held[0]);

    expect(titles()).toContain(ACTIVE_APPLIED);
    expect(toggle).toHaveAttribute("aria-pressed", "false");
    expect(within(card(ACTIVE_APPLIED)).queryByText(APPLIED_LABEL)).toBeNull();
    expect(screen.queryByRole("status")).not.toBeInTheDocument();
    expect(toggle).toHaveFocus();
  });

  it("changes nothing and shows an error when marking fails", async () => {
    const { held } = await openView("Active");
    const toggle = appliedToggle(ACTIVE_OTHER);
    press(toggle);
    await settle(held[0], "fail");

    expect(toggle).toHaveAttribute("aria-pressed", "false");
    expect(toggle).toHaveTextContent(/^Mark applied$/);
    expect(within(card(ACTIVE_OTHER)).queryByText(/^Applied /)).toBeNull();
    expect(screen.getByRole("alert")).toHaveTextContent(
      "The change could not be saved.",
    );
    expect(toggle).toHaveFocus();
  });

  it("ignores repeat clicks while a change is pending", async () => {
    const { held } = await openView("Active");
    const toggle = appliedToggle(ACTIVE_OTHER);
    fireEvent.click(toggle);
    fireEvent.click(toggle);
    fireEvent.click(toggle);
    expect(held).toHaveLength(1);
    await settle(held[0]);
    expect(toggle).toHaveAttribute("aria-pressed", "true");
  });

  it("shows Applied status and date in the drawer", async () => {
    const { held } = await openView("Active");
    fireEvent.click(
      within(card(ACTIVE_APPLIED)).getByRole("button", { name: "View details" }),
    );
    const dialog = await screen.findByRole("dialog");
    expect(within(dialog).getByText(APPLIED_LABEL)).toBeInTheDocument();
    fireEvent.keyDown(window, { key: "Escape" });

    fireEvent.click(
      within(card(ACTIVE_OTHER)).getByRole("button", { name: "View details" }),
    );
    const other = await screen.findByRole("dialog");
    expect(within(other).getByText("Not marked applied")).toBeInTheDocument();
    expect(held).toHaveLength(0);
  });
});

describe("Applied view", () => {
  it("lists dismissed, closed and no-longer-matching applied matches", async () => {
    await openView("Applied");
    expect(viewButton("Applied")).toHaveAttribute("aria-pressed", "true");
    expect([...titles()].sort()).toEqual(
      [ACTIVE_APPLIED, CLOSED, DISMISSED].sort(),
    );
    const closed = card(CLOSED);
    expect(within(closed).getByText("Closed")).toBeInTheDocument();
    expect(within(closed).getByText("No longer matches")).toBeInTheDocument();
    expect(within(card(DISMISSED)).getByText("Dismissed")).toBeInTheDocument();
    for (const title of titles()) {
      expect(within(card(title)).getByText(APPLIED_LABEL)).toBeInTheDocument();
      expect(appliedToggle(title)).toHaveAttribute("aria-pressed", "true");
    }
  });

  it("removes an unmarked card only on success; Undo restores the exact date", async () => {
    const { held, client } = await openView("Applied");
    press(appliedToggle(CLOSED));
    expect(held[0].changes).toEqual({ applied: false });
    expect(titles()).toContain(CLOSED);
    expect(screen.queryByRole("status")).not.toBeInTheDocument();

    await settle(held[0]);
    expect(titles()).not.toContain(CLOSED);
    const notice = screen.getByRole("status");
    expect(notice).toHaveTextContent(`Removed “${CLOSED}” from Applied.`);
    const undo = within(notice).getByRole("button", { name: "Undo" });
    expect(undo).toHaveFocus();

    press(undo);
    expect(held[1].id).toBe(held[0].id);
    expect(held[1].changes).toEqual({ applied: true, applied_at: APPLIED_AT });
    await settle(held[1]);
    expect(titles()).toContain(CLOSED);
    expect(within(card(CLOSED)).getByText(APPLIED_LABEL)).toBeInTheDocument();
    expect(screen.queryByRole("status")).not.toBeInTheDocument();
    expect(titleButton(CLOSED)).toHaveFocus();
    // Saved and Dismissed are untouched by unmark + Undo.
    const stored = await client.getMatch("match-figma-007");
    expect(stored).toMatchObject({
      applied_at: APPLIED_AT,
      saved: true,
      dismissed: false,
    });
  });

  it("keeps the card when unmarking fails", async () => {
    const { held } = await openView("Applied");
    const toggle = appliedToggle(DISMISSED);
    press(toggle);
    await settle(held[0], "fail");

    expect(titles()).toContain(DISMISSED);
    expect(toggle).toHaveAttribute("aria-pressed", "true");
    expect(screen.queryByRole("status")).not.toBeInTheDocument();
    expect(screen.getByRole("alert")).toHaveTextContent(
      "The change could not be saved.",
    );
    expect(toggle).toHaveFocus();
  });

  it("keeps the notice when Undo fails, so it can be retried", async () => {
    const { held } = await openView("Applied");
    press(appliedToggle(CLOSED));
    await settle(held[0]);
    const undo = within(screen.getByRole("status")).getByRole("button", {
      name: "Undo",
    });
    press(undo);
    await settle(held[1], "fail");

    expect(titles()).not.toContain(CLOSED);
    expect(screen.getByRole("status")).toHaveTextContent("from Applied");
    expect(undo).toHaveFocus();
  });

  it("keeps focus on Undo and shows the empty state after the last removal", async () => {
    const { held } = await openView("Applied");
    for (const [index, title] of [ACTIVE_APPLIED, CLOSED, DISMISSED].entries()) {
      press(appliedToggle(title));
      await settle(held[index]);
      expect(titles()).not.toContain(title);
    }
    expect(titles()).toEqual([]);
    // Undo exists for the last removal, so focus goes there first.
    expect(
      within(screen.getByRole("status")).getByRole("button", { name: "Undo" }),
    ).toHaveFocus();
    expect(screen.getByText("No applied matches yet")).toBeInTheDocument();
  });
});

describe("Applied view (HTTP client)", () => {
  afterEach(() => {
    vi.unstubAllGlobals();
  });

  it("loads every page of view=applied", async () => {
    const base = (id, overrides) => ({
      id: `11111111-1111-4111-8111-00000000000${id}`,
      job_id: `22222222-2222-4222-8222-00000000000${id}`,
      company_id: "stripe",
      company: "Stripe",
      title: `Applied Intern ${id}`,
      location: "New York, NY",
      remote: false,
      remote_status: "",
      role_id: "software_engineering",
      application_url: null,
      posting_date: null,
      deadline: null,
      is_open: true,
      match_reasons: [],
      matched_at: `2026-03-0${id}T10:00:00Z`,
      last_matched_at: `2026-03-0${id}T10:00:00Z`,
      no_longer_matches_at: null,
      saved_at: null,
      dismissed_at: null,
      applied_at: APPLIED_AT,
      ...overrides,
    });
    const routes = {
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
      "/api/matches": {
        items: [base(1, { applied_at: null, title: "Plain Intern" })],
        limit: 50,
        offset: 0,
        total: 1,
        has_more: false,
      },
      "/api/matches?view=applied": {
        items: [base(3, { dismissed_at: APPLIED_AT })],
        limit: 1,
        offset: 0,
        total: 2,
        has_more: true,
      },
      "/api/matches?view=applied&limit=1&offset=1": {
        items: [base(2, { is_open: false, no_longer_matches_at: APPLIED_AT })],
        limit: 1,
        offset: 1,
        total: 2,
        has_more: false,
      },
    };
    const fetchMock = vi.fn(async (url) => {
      if (!(url in routes)) throw new Error(`Unexpected ${url}`);
      return { ok: true, status: 200, json: async () => routes[url] };
    });
    vi.stubGlobal("fetch", fetchMock);
    render(<App initialPath="/app/matches" client={createHttpHostedApi()} />);
    await screen.findByText("Plain Intern");

    fireEvent.click(viewButton("Applied"));
    expect(await screen.findByText("Applied Intern 2")).toBeInTheDocument();
    expect(screen.getByText("Applied Intern 3")).toBeInTheDocument();
    expect(screen.queryByText("Plain Intern")).not.toBeInTheDocument();
    expect(
      fetchMock.mock.calls
        .map(([url]) => url)
        .filter((url) => url.includes("view=applied")),
    ).toEqual([
      "/api/matches?view=applied",
      "/api/matches?view=applied&limit=1&offset=1",
    ]);
    expect(within(card("Applied Intern 3")).getByText("Dismissed"))
      .toBeInTheDocument();
    expect(within(card("Applied Intern 2")).getByText("Closed"))
      .toBeInTheDocument();
  });
});
