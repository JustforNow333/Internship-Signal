import { fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";

import MatchesPage from "../hosted/MatchesPage.jsx";
import Onboarding from "../hosted/Onboarding.jsx";
import SettingsPage from "../hosted/SettingsPage.jsx";
import { ROLE_OPTIONS } from "../hosted/constants.js";
import { makeHostedFixtures } from "../hosted/fixtures.js";
import { normalizeMatch, roleName } from "../hosted/matchModel.js";

const QUANT_TRADING = "Quantitative Trading & Research";

function roleCheckbox(name) {
  return within(screen.getByText(name).closest("label")).getByRole("checkbox");
}

async function onboardWithRoles(names) {
  const fixtures = makeHostedFixtures();
  const savePreferences = vi.fn(async () => {});
  render(
    <Onboarding
      navigate={() => {}}
      companies={fixtures.companies}
      savePreferences={savePreferences}
      saveWatchlist={vi.fn(async () => {})}
    />,
  );
  fireEvent.click(screen.getByRole("button", { name: /Continue to roles/i }));
  await screen.findByRole("heading", { name: /Which role categories/i });
  for (const name of names) {
    fireEvent.click(screen.getByText(name).closest("label"));
  }
  fireEvent.click(screen.getByRole("button", { name: /Continue to companies/i }));
  await screen.findByRole("heading", { name: /Which companies should we watch/i });
  fireEvent.click(screen.getByRole("button", { name: "Add Stripe" }));
  fireEvent.click(screen.getByRole("button", { name: /Continue to alerts/i }));
  await screen.findByRole("heading", { name: /When and where should we look/i });
  fireEvent.click(screen.getByRole("button", { name: /Activate my watchlist/i }));
  await screen.findByRole("heading", { name: /Your watchlist is active/i });
  return savePreferences.mock.calls[0][0].role_ids;
}

describe("data science and quantitative trading & research categories", () => {
  it("offers each category exactly once with distinct ids", () => {
    const ids = ROLE_OPTIONS.map((role) => role.id);
    const names = ROLE_OPTIONS.map((role) => role.name);
    expect(new Set(ids).size).toBe(ids.length);
    expect(names.filter((name) => name === "Data Science")).toHaveLength(1);
    expect(ids).toContain("data_science");
    expect(ids).toContain("quantitative_development");
    expect(ids).toContain("quantitative_trading_research");
    expect(roleName("quantitative_trading_research")).toBe(QUANT_TRADING);
  });

  it.each([
    [["Data Science"], ["data_science"]],
    [[QUANT_TRADING], ["quantitative_trading_research"]],
    [["Data Science", QUANT_TRADING], ["data_science", "quantitative_trading_research"]],
    [["Software Engineering"], ["software_engineering"]],
  ])("onboarding saves %j as %j", async (names, expected) => {
    const roleIds = await onboardWithRoles(names);
    expect([...roleIds].sort()).toEqual([...expected].sort());
  });

  it("settings keeps existing roles and adds the new category only when chosen", async () => {
    const fixtures = makeHostedFixtures();
    const savePreferences = vi.fn(async () => {});
    const preferences = { ...fixtures.preferences, role_ids: ["quantitative_development"] };
    render(
      <SettingsPage me={fixtures.me} preferences={preferences} savePreferences={savePreferences} />,
    );

    expect(roleCheckbox("Quantitative Development")).toBeChecked();
    expect(roleCheckbox(QUANT_TRADING)).not.toBeChecked();
    expect(roleCheckbox("Data Science")).not.toBeChecked();
    fireEvent.click(roleCheckbox(QUANT_TRADING));
    fireEvent.click(screen.getByRole("button", { name: "Save changes" }));

    await waitFor(() => expect(savePreferences).toHaveBeenCalledOnce());
    expect(savePreferences.mock.calls[0][0].role_ids).toEqual([
      "quantitative_development",
      "quantitative_trading_research",
    ]);
  });

  it("filters and labels matches by each category", () => {
    const matches = [
      ["quantitative_trading_research", "Quantitative Trader Intern"],
      ["quantitative_development", "Quant Developer Intern"],
      ["data_science", "Data Scientist Intern"],
    ].map(([roleId, title], index) =>
      normalizeMatch({
        id: `00000000-0000-4000-8000-00000000000${index}`,
        job_id: `00000000-0000-4000-9000-00000000000${index}`,
        company_id: "stripe",
        company: "Stripe",
        title,
        location: "New York, NY",
        role_id: roleId,
        career_level: "internship",
        application_url: `https://example.com/jobs/${index}`,
        is_open: true,
        match_reasons: [{ code: "role_selected", value: roleId }],
        matched_at: "2026-10-24T12:00:00Z",
        last_matched_at: "2026-10-24T12:00:00Z",
      }),
    );
    expect(matches[0].why[0]).toBe(`${QUANT_TRADING} is in your role preferences`);
    render(<MatchesPage matches={matches} updateMatch={vi.fn()} />);

    fireEvent.change(screen.getByLabelText("Role filter"), {
      target: { value: "quantitative_trading_research" },
    });
    expect(screen.getByText("Quantitative Trader Intern")).toBeInTheDocument();
    expect(screen.queryByText("Quant Developer Intern")).not.toBeInTheDocument();
    expect(screen.queryByText("Data Scientist Intern")).not.toBeInTheDocument();

    fireEvent.change(screen.getByLabelText("Role filter"), {
      target: { value: "data_science" },
    });
    expect(screen.getByText("Data Scientist Intern")).toBeInTheDocument();
    expect(screen.queryByText("Quantitative Trader Intern")).not.toBeInTheDocument();
  });
});
