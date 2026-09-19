import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";

import Onboarding from "../hosted/Onboarding.jsx";
import SettingsPage from "../hosted/SettingsPage.jsx";
import { makeHostedFixtures } from "../hosted/fixtures.js";

const TOGGLE = "Include recent openings";
const DESCRIPTION =
  "When you add a company, show matching openings posted within the last 90 days.";

function renderOnboarding(overrides = {}) {
  const fixtures = makeHostedFixtures();
  const savePreferences = overrides.savePreferences || vi.fn(async () => {});
  const saveWatchlist = overrides.saveWatchlist || vi.fn(async () => {});
  render(
    <Onboarding
      navigate={() => {}}
      companies={fixtures.companies}
      savePreferences={savePreferences}
      saveWatchlist={saveWatchlist}
    />,
  );
  return { savePreferences, saveWatchlist };
}

async function reachAlertsStep() {
  fireEvent.click(screen.getByText("Software Engineering").closest("label"));
  fireEvent.click(
    screen.getByRole("button", { name: /Continue to companies/i }),
  );
  await screen.findByRole("heading", {
    name: /Which companies should we watch/i,
  });
  fireEvent.click(screen.getByRole("button", { name: "Add Stripe" }));
  fireEvent.click(screen.getByRole("button", { name: /Continue to alerts/i }));
  await screen.findByRole("heading", { name: /When and where should we look/i });
}

function recentOpeningsCheckbox() {
  return screen.getByRole("checkbox", { name: new RegExp(TOGGLE, "i") });
}

describe("onboarding recent openings", () => {
  it("renders the toggle with its description and defaults it on", async () => {
    renderOnboarding();
    await reachAlertsStep();

    expect(screen.getByText(TOGGLE)).toBeInTheDocument();
    expect(screen.getByText(DESCRIPTION)).toBeInTheDocument();
    expect(recentOpeningsCheckbox()).toBeChecked();
  });

  it("sends include_recent_openings=true by default", async () => {
    const { savePreferences } = renderOnboarding();
    await reachAlertsStep();

    fireEvent.click(
      screen.getByRole("button", { name: /Activate my watchlist/i }),
    );

    await waitFor(() => expect(savePreferences).toHaveBeenCalled());
    expect(savePreferences.mock.calls[0][0]).toMatchObject({
      include_recent_openings: true,
    });
  });

  it("sends false once the toggle is turned off", async () => {
    const { savePreferences } = renderOnboarding();
    await reachAlertsStep();

    fireEvent.click(recentOpeningsCheckbox());
    expect(recentOpeningsCheckbox()).not.toBeChecked();
    fireEvent.click(
      screen.getByRole("button", { name: /Activate my watchlist/i }),
    );

    await waitFor(() => expect(savePreferences).toHaveBeenCalled());
    expect(savePreferences.mock.calls[0][0]).toMatchObject({
      include_recent_openings: false,
    });
  });

  it("commits preferences before sending the watchlist", async () => {
    const order = [];
    let releasePreferences;
    const savePreferences = vi.fn(() => {
      order.push("preferences:start");
      return new Promise((resolve) => {
        releasePreferences = () => {
          order.push("preferences:done");
          resolve();
        };
      });
    });
    const saveWatchlist = vi.fn(async () => {
      order.push("watchlist:start");
    });
    renderOnboarding({ savePreferences, saveWatchlist });
    await reachAlertsStep();

    fireEvent.click(
      screen.getByRole("button", { name: /Activate my watchlist/i }),
    );
    await waitFor(() => expect(savePreferences).toHaveBeenCalled());

    // The watchlist must not be in flight while preferences are unresolved.
    expect(saveWatchlist).not.toHaveBeenCalled();
    releasePreferences();
    await waitFor(() => expect(saveWatchlist).toHaveBeenCalled());
    expect(order).toEqual([
      "preferences:start",
      "preferences:done",
      "watchlist:start",
    ]);
  });

  it("mentions recent openings on completion only when enabled", async () => {
    renderOnboarding();
    await reachAlertsStep();
    fireEvent.click(
      screen.getByRole("button", { name: /Activate my watchlist/i }),
    );

    await screen.findByRole("heading", { name: /Your watchlist is active/i });
    expect(
      screen.getByText(/may also include matching openings from the last 90 days/i),
    ).toBeInTheDocument();
  });

  it("omits the completion sentence when the toggle is off", async () => {
    renderOnboarding();
    await reachAlertsStep();
    fireEvent.click(recentOpeningsCheckbox());
    fireEvent.click(
      screen.getByRole("button", { name: /Activate my watchlist/i }),
    );

    await screen.findByRole("heading", { name: /Your watchlist is active/i });
    expect(
      screen.queryByText(/may also include matching openings/i),
    ).not.toBeInTheDocument();
  });
});

describe("settings recent openings", () => {
  const renderSettings = (preferences, savePreferences = vi.fn(async () => {})) => {
    const fixtures = makeHostedFixtures();
    const { rerender } = render(
      <SettingsPage
        me={fixtures.me}
        preferences={preferences || fixtures.preferences}
        savePreferences={savePreferences}
      />,
    );
    return { savePreferences, fixtures, rerender };
  };

  it("renders the persisted value", () => {
    const fixtures = makeHostedFixtures();
    renderSettings({ ...fixtures.preferences, include_recent_openings: false });

    expect(recentOpeningsCheckbox()).not.toBeChecked();
    expect(screen.getByText(DESCRIPTION)).toBeInTheDocument();
  });

  it("toggles and saves the setting", async () => {
    const { savePreferences } = renderSettings();
    expect(recentOpeningsCheckbox()).toBeChecked();

    fireEvent.click(recentOpeningsCheckbox());
    fireEvent.click(screen.getByRole("button", { name: /Save changes/i }));

    await waitFor(() => expect(savePreferences).toHaveBeenCalled());
    expect(savePreferences.mock.calls[0][0]).toMatchObject({
      include_recent_openings: false,
    });
  });

  it("re-renders from the value the backend returned", async () => {
    const fixtures = makeHostedFixtures();
    const { rerender } = renderSettings();
    expect(recentOpeningsCheckbox()).toBeChecked();

    rerender(
      <SettingsPage
        me={fixtures.me}
        preferences={{ ...fixtures.preferences, include_recent_openings: false }}
        savePreferences={vi.fn(async () => {})}
      />,
    );

    await waitFor(() => expect(recentOpeningsCheckbox()).not.toBeChecked());
  });

  it("keeps the setting with matching preferences, not alert delivery", () => {
    renderSettings();
    const toggle = screen.getByText(TOGGLE).closest("section");

    expect(toggle).toBe(screen.getByText("Internship season").closest("section"));
    expect(toggle).not.toBe(
      screen.getByText("Alert delivery").closest("section"),
    );
  });
});
