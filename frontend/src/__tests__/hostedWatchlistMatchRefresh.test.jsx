import { fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";

import App from "../App.jsx";
import { HostedApiError, createMockHostedApi } from "../hosted/api.js";
import { makeHostedFixtures } from "../hosted/fixtures.js";

const STRIPE_TITLE = "Software Engineering Intern, Payments";
const NVIDIA_TITLE = "Machine Learning Systems Intern";
const CLOUDFLARE_TITLE = "Platform Engineering Intern, Edge Network";

// A Cloudflare posting the backend only matches once Cloudflare is watched.
function cloudflareMatch(base) {
  return {
    ...base,
    id: "match-cloudflare-101",
    company_id: "cloudflare",
    company: "Cloudflare",
    title: CLOUDFLARE_TITLE,
    location: "Austin, TX",
    detected_at: new Date(Date.now() - 5 * 60_000).toISOString(),
    why: [
      "Software Engineering is in your role preferences",
      "Cloudflare is on your watchlist",
    ],
    source_url: "https://www.cloudflare.com/careers/",
  };
}

// The backend reconciles a user's matches when the watchlist changes: the
// Active list it serves holds only watched, unpaused companies. The mock
// models that server-side effect, so every watchlist mutation changes what a
// fresh GET /api/matches returns.
function reconcilingClient(adjust = (fixtures) => fixtures) {
  const fixtures = adjust(makeHostedFixtures());
  const pool = [cloudflareMatch(fixtures.matches[0])];
  const client = createMockHostedApi({ fixtures });
  const getMatches = client.getMatches;
  const original = client.updateWatchlist;
  let serverWatchlist = fixtures.watchlist;
  const updateWatchlist = vi.spyOn(client, "updateWatchlist").mockImplementation(async (input) => {
    const saved = await original(input);
    serverWatchlist = saved;
    return saved;
  });
  const matchesSpy = vi.spyOn(client, "getMatches").mockImplementation(async (params) => {
    const matches = await getMatches(params);
    const monitored = new Set(
      serverWatchlist.filter((entry) => !entry.paused).map((entry) => entry.company_id),
    );
    return [...pool, ...matches].filter((match) => monitored.has(match.company_id));
  });
  return { client, matchesSpy, updateWatchlist };
}

function renderApp(path, client) {
  return render(<App initialPath={path} client={client} />);
}

function nav(label) {
  const menu = screen.getByRole("navigation", { name: "Application" });
  fireEvent.click(within(menu).getByRole("link", { name: new RegExp(label) }));
}

function companyCard(name) {
  return screen.getByRole("heading", { level: 3, name }).closest("article");
}

async function openWatchlist(client) {
  renderApp("/app/watchlist", client);
  await screen.findByRole("heading", { level: 1, name: "Watchlist" });
}

async function showDashboard() {
  nav("Dashboard");
  await screen.findByRole("heading", { name: "Recent matches" });
}

async function showMatches() {
  nav("Matches");
  await screen.findByRole("heading", { level: 1, name: "Matches" });
}

// The reconciled list must reach both cached consumers without a reload.
async function expectEverywhere({ present = [], absent = [] }) {
  for (const show of [showDashboard, showMatches]) {
    await show();
    for (const title of present)
      expect(await screen.findAllByText(title)).not.toHaveLength(0);
    for (const title of absent)
      await waitFor(() => expect(screen.queryAllByText(title)).toHaveLength(0));
  }
}

// The refresh must request the same default Active view as the workspace load.
function expectActiveRefresh(matchesSpy) {
  expect(matchesSpy).toHaveBeenCalledTimes(2);
  expect(matchesSpy.mock.calls[1]).toEqual(matchesSpy.mock.calls[0]);
}

describe("Active matches after a watchlist save", () => {
  it("shows the backend's new match after adding a company", async () => {
    const { client, matchesSpy, updateWatchlist } = reconcilingClient();
    await openWatchlist(client);
    expect(matchesSpy).toHaveBeenCalledTimes(1);
    expect(screen.queryAllByText(CLOUDFLARE_TITLE)).toHaveLength(0);

    fireEvent.click(
      within(companyCard("Cloudflare")).getByRole("button", { name: "+ Add to watchlist" }),
    );
    expect(await screen.findByRole("status")).toHaveTextContent(
      "Cloudflare added to your watchlist.",
    );
    expect(updateWatchlist).toHaveBeenCalledOnce();

    await expectEverywhere({ present: [CLOUDFLARE_TITLE] });
    expectActiveRefresh(matchesSpy);
  });

  it("drops the removed company's matches", async () => {
    const { client, matchesSpy } = reconcilingClient();
    await openWatchlist(client);

    fireEvent.click(within(companyCard("Stripe")).getByRole("button", { name: "Remove" }));
    expect(await screen.findByRole("status")).toHaveTextContent(
      "Stripe removed from your watchlist.",
    );

    await expectEverywhere({ present: [NVIDIA_TITLE], absent: [STRIPE_TITLE] });
    expectActiveRefresh(matchesSpy);
  });

  it("drops a paused company's matches", async () => {
    const { client, matchesSpy } = reconcilingClient();
    await openWatchlist(client);

    fireEvent.click(
      within(companyCard("NVIDIA")).getByRole("button", { name: "Pause monitoring" }),
    );
    expect(await screen.findByRole("status")).toHaveTextContent("NVIDIA monitoring paused.");

    await expectEverywhere({ present: [STRIPE_TITLE], absent: [NVIDIA_TITLE] });
    expectActiveRefresh(matchesSpy);
  });

  it("restores a resumed company's matches", async () => {
    const { client, matchesSpy } = reconcilingClient((fixtures) => ({
      ...fixtures,
      watchlist: fixtures.watchlist.map((entry) =>
        entry.company_id === "nvidia" ? { ...entry, paused: true } : entry,
      ),
    }));
    await openWatchlist(client);
    expect(screen.queryAllByText(NVIDIA_TITLE)).toHaveLength(0);

    fireEvent.click(
      within(companyCard("NVIDIA")).getByRole("button", { name: "Resume monitoring" }),
    );
    expect(await screen.findByRole("status")).toHaveTextContent("NVIDIA monitoring resumed.");

    await expectEverywhere({ present: [NVIDIA_TITLE, STRIPE_TITLE] });
    expectActiveRefresh(matchesSpy);
  });

  it("changes nothing and reloads nothing when the watchlist update fails", async () => {
    const { client, matchesSpy, updateWatchlist } = reconcilingClient();
    await openWatchlist(client);
    updateWatchlist.mockRejectedValueOnce(new HostedApiError("Network down", 503));

    fireEvent.click(within(companyCard("Stripe")).getByRole("button", { name: "Remove" }));
    expect(await screen.findByRole("alert")).toHaveTextContent("Network down");
    expect(updateWatchlist).toHaveBeenCalledOnce();
    expect(matchesSpy).toHaveBeenCalledTimes(1);
    expect(within(companyCard("Stripe")).getByRole("button", { name: "Remove" })).toBeEnabled();
    expect(screen.getByText("companies on your watchlist").previousSibling).toHaveTextContent(
      "5",
    );

    await expectEverywhere({ present: [STRIPE_TITLE, NVIDIA_TITLE] });
    expect(matchesSpy).toHaveBeenCalledTimes(1);
  });

  it("keeps the saved watchlist and says so when the match reload fails", async () => {
    const { client, matchesSpy, updateWatchlist } = reconcilingClient();
    await openWatchlist(client);
    matchesSpy.mockRejectedValueOnce(new HostedApiError("Network down", 503));

    fireEvent.click(within(companyCard("Stripe")).getByRole("button", { name: "Remove" }));
    const alert = await screen.findByRole("alert");
    expect(alert).toHaveTextContent(
      /watchlist was saved, but your matches could not be refreshed/i,
    );
    expect(alert).not.toHaveTextContent(/could not be updated/i);
    expect(alert).not.toHaveTextContent("Network down");
    expect(updateWatchlist).toHaveBeenCalledOnce();
    expect(matchesSpy).toHaveBeenCalledTimes(2);
    expect(
      within(companyCard("Stripe")).getByRole("button", { name: "+ Add to watchlist" }),
    ).toBeInTheDocument();
    expect(screen.getByText("companies on your watchlist").previousSibling).toHaveTextContent(
      "4",
    );

    // The previously loaded matches stay until a refresh succeeds.
    await expectEverywhere({ present: [STRIPE_TITLE, NVIDIA_TITLE] });
  });
});

describe("onboarding watchlist save", () => {
  it("does not request matches before a workspace is loaded", async () => {
    const { client, matchesSpy, updateWatchlist } = reconcilingClient();
    renderApp("/onboarding", client);
    fireEvent.click(await screen.findByRole("button", { name: /Continue to roles/i }));
    fireEvent.click((await screen.findByText("Software Engineering")).closest("label"));
    fireEvent.click(screen.getByRole("button", { name: /Continue to companies/i }));
    await screen.findByRole("heading", { name: /Which companies should we watch/i });
    fireEvent.click(screen.getByRole("button", { name: "Add Stripe" }));
    fireEvent.click(screen.getByRole("button", { name: /Continue to alerts/i }));
    await screen.findByRole("heading", { name: /When and where should we look/i });
    fireEvent.click(screen.getByRole("button", { name: /Activate my watchlist/i }));
    await screen.findByRole("heading", { name: /Your watchlist is active/i });
    expect(updateWatchlist).toHaveBeenCalledOnce();
    expect(matchesSpy).not.toHaveBeenCalled();
  });
});
