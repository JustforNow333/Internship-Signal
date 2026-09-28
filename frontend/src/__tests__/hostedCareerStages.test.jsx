import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";

import Onboarding from "../hosted/Onboarding.jsx";
import SettingsPage from "../hosted/SettingsPage.jsx";
import { CAREER_LEVEL_OPTIONS, ROLE_OPTIONS } from "../hosted/constants.js";
import { makeHostedFixtures } from "../hosted/fixtures.js";
import { careerLevelsOrDefault } from "../hosted/utils.js";

const STAGE_LABELS = {
  internship: "Internships",
  new_grad_junior: "New grad / junior",
  senior_plus: "Senior+",
};

function stageCheckbox(id) {
  return screen.getByRole("checkbox", { name: new RegExp(`^${escape(STAGE_LABELS[id])}`) });
}

function escape(text) {
  return text.replace(/[.*+?^${}()|[\]\\/]/g, "\\$&");
}

function seasonSelect() {
  return screen.getByRole("combobox", { name: /Internship season/ });
}

function renderOnboarding() {
  const fixtures = makeHostedFixtures();
  const savePreferences = vi.fn(async () => {});
  const saveWatchlist = vi.fn(async () => {});
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

function setStages(ids) {
  for (const id of Object.keys(STAGE_LABELS)) {
    if (stageCheckbox(id).checked !== ids.includes(id)) {
      fireEvent.click(stageCheckbox(id));
    }
  }
}

async function advanceToAlerts() {
  fireEvent.click(screen.getByRole("button", { name: /Continue to roles/i }));
  await screen.findByRole("heading", { name: /Which role categories/i });
  fireEvent.click(screen.getByText("Software Engineering").closest("label"));
  fireEvent.click(screen.getByRole("button", { name: /Continue to companies/i }));
  await screen.findByRole("heading", { name: /Which companies should we watch/i });
  fireEvent.click(screen.getByRole("button", { name: "Add Stripe" }));
  fireEvent.click(screen.getByRole("button", { name: /Continue to alerts/i }));
  await screen.findByRole("heading", { name: /When and where should we look/i });
}

describe("career-stage constants", () => {
  it("exposes exactly the backend's selectable stages", () => {
    expect(CAREER_LEVEL_OPTIONS.map((option) => option.id)).toEqual([
      "internship",
      "new_grad_junior",
      "senior_plus",
    ]);
  });

  it("falls back to internships for missing or unusable values", () => {
    expect(careerLevelsOrDefault(undefined)).toEqual(["internship"]);
    expect(careerLevelsOrDefault(null)).toEqual(["internship"]);
    expect(careerLevelsOrDefault([])).toEqual(["internship"]);
    expect(careerLevelsOrDefault(["mid_level", "unknown"])).toEqual(["internship"]);
    expect(careerLevelsOrDefault(["senior_plus", "internship"])).toEqual([
      "senior_plus",
      "internship",
    ]);
  });
});

describe("onboarding career stages", () => {
  it("starts on career stages with internships selected", () => {
    renderOnboarding();

    expect(
      screen.getByRole("heading", {
        name: "What kind of opportunities are you looking for?",
      }),
    ).toBeInTheDocument();
    expect(screen.getByLabelText("Onboarding step 1 of 4")).toBeInTheDocument();
    expect(screen.getByText("Step 1 of 4")).toBeInTheDocument();
    expect(stageCheckbox("internship")).toBeChecked();
    expect(stageCheckbox("new_grad_junior")).not.toBeChecked();
    expect(stageCheckbox("senior_plus")).not.toBeChecked();
    for (const option of CAREER_LEVEL_OPTIONS) {
      expect(screen.getByText(option.description)).toBeInTheDocument();
    }
    expect(screen.queryByText(/mid[_ -]?level/i)).not.toBeInTheDocument();
    expect(screen.queryByText(/unknown/i)).not.toBeInTheDocument();
  });

  it("supports multiple stages and blocks continuing with none", () => {
    renderOnboarding();
    const next = screen.getByRole("button", { name: /Continue to roles/i });

    fireEvent.click(stageCheckbox("new_grad_junior"));
    expect(screen.getByText("2 career stages selected")).toBeInTheDocument();
    expect(next).toBeEnabled();

    fireEvent.click(stageCheckbox("internship"));
    fireEvent.click(stageCheckbox("new_grad_junior"));
    expect(screen.getByText("Select at least one career stage")).toBeInTheDocument();
    expect(next).toBeDisabled();
  });

  it("walks all four steps and keeps selections when going back", async () => {
    renderOnboarding();
    fireEvent.click(stageCheckbox("senior_plus"));
    fireEvent.click(screen.getByRole("button", { name: /Continue to roles/i }));

    expect(await screen.findByText("Step 2 of 4")).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: /Back/ }));
    expect(screen.getByText("Step 1 of 4")).toBeInTheDocument();
    expect(stageCheckbox("internship")).toBeChecked();
    expect(stageCheckbox("senior_plus")).toBeChecked();

    fireEvent.click(screen.getByRole("button", { name: /Continue to roles/i }));
    fireEvent.click(screen.getByText("Software Engineering").closest("label"));
    fireEvent.click(screen.getByRole("button", { name: /Continue to companies/i }));
    expect(await screen.findByText("Step 3 of 4")).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "Add Stripe" }));
    fireEvent.click(screen.getByRole("button", { name: /Continue to alerts/i }));
    expect(await screen.findByText("Step 4 of 4")).toBeInTheDocument();
  });

  const combinations = [
    ["internship"],
    ["new_grad_junior"],
    ["senior_plus"],
    ["internship", "new_grad_junior"],
    ["internship", "senior_plus"],
    ["new_grad_junior", "senior_plus"],
    ["internship", "new_grad_junior", "senior_plus"],
  ];

  it.each(combinations.map((ids) => [ids.join(" + "), ids]))(
    "saves career_levels %s with the preferences before the watchlist",
    async (_, ids) => {
    const { savePreferences, saveWatchlist } = renderOnboarding();
    setStages(ids);
    await advanceToAlerts();
    fireEvent.click(screen.getByRole("button", { name: /Activate my watchlist/i }));

    await screen.findByRole("heading", { name: /Your watchlist is active/i });
    expect(savePreferences).toHaveBeenCalledOnce();
    const saved = savePreferences.mock.calls[0][0];
    expect([...saved.career_levels].sort()).toEqual([...ids].sort());
    expect(saved).toMatchObject({
      role_ids: ["software_engineering"],
      internship_season: "Summer 2027",
    });
    expect(saveWatchlist).toHaveBeenCalledWith([
      { company_id: "stripe", paused: false },
    ]);
    expect(savePreferences.mock.invocationCallOrder[0]).toBeLessThan(
      saveWatchlist.mock.invocationCallOrder[0],
    );
    },
  );

  it("disables internship season without internships and keeps its value", async () => {
    renderOnboarding();
    await advanceToAlerts();
    expect(seasonSelect()).toBeEnabled();
    fireEvent.change(seasonSelect(), { target: { value: "Fall 2026" } });

    // Return to step 1, drop internships, and come back to alerts.
    for (const step of [3, 2, 1]) {
      fireEvent.click(screen.getByRole("button", { name: /Back/ }));
      await screen.findByText(`Step ${step} of 4`);
    }
    setStages(["new_grad_junior"]);
    fireEvent.click(screen.getByRole("button", { name: /Continue to roles/i }));
    fireEvent.click(screen.getByRole("button", { name: /Continue to companies/i }));
    fireEvent.click(screen.getByRole("button", { name: /Continue to alerts/i }));
    await screen.findByText("Step 4 of 4");
    expect(seasonSelect()).toBeDisabled();
    expect(seasonSelect()).toHaveValue("Fall 2026");
    expect(screen.getByText(/applies only to internship/i)).toBeInTheDocument();

    for (const step of [3, 2, 1]) {
      fireEvent.click(screen.getByRole("button", { name: /Back/ }));
      await screen.findByText(`Step ${step} of 4`);
    }
    fireEvent.click(stageCheckbox("internship"));
    fireEvent.click(screen.getByRole("button", { name: /Continue to roles/i }));
    fireEvent.click(screen.getByRole("button", { name: /Continue to companies/i }));
    fireEvent.click(screen.getByRole("button", { name: /Continue to alerts/i }));
    await screen.findByText("Step 4 of 4");
    expect(seasonSelect()).toBeEnabled();
    expect(seasonSelect()).toHaveValue("Fall 2026");
  });
});

describe("settings career stages", () => {
  function renderSettings(preferences) {
    const fixtures = makeHostedFixtures();
    const savePreferences = vi.fn(async () => {});
    render(
      <SettingsPage
        me={fixtures.me}
        preferences={{ ...fixtures.preferences, ...preferences }}
        savePreferences={savePreferences}
      />,
    );
    return { savePreferences, fixtures };
  }

  it("renders the saved career stages", () => {
    renderSettings({ career_levels: ["new_grad_junior", "senior_plus"] });

    expect(screen.getByRole("heading", { name: "Career stages" })).toBeInTheDocument();
    expect(stageCheckbox("internship")).not.toBeChecked();
    expect(stageCheckbox("new_grad_junior")).toBeChecked();
    expect(stageCheckbox("senior_plus")).toBeChecked();
    expect(screen.queryByText(/mid[_ -]?level/i)).not.toBeInTheDocument();
  });

  it("falls back to internships when career_levels is missing", async () => {
    const fixtures = makeHostedFixtures();
    const { career_levels: _omit, ...legacy } = fixtures.preferences;
    const savePreferences = vi.fn(async () => {});
    render(
      <SettingsPage me={fixtures.me} preferences={legacy} savePreferences={savePreferences} />,
    );

    expect(stageCheckbox("internship")).toBeChecked();
    expect(stageCheckbox("new_grad_junior")).not.toBeChecked();
    expect(seasonSelect()).toBeEnabled();
    fireEvent.click(screen.getByRole("button", { name: "Save changes" }));
    await waitFor(() => expect(savePreferences).toHaveBeenCalledOnce());
    expect(savePreferences.mock.calls[0][0].career_levels).toEqual(["internship"]);
  });

  it("saves multi-select changes with every other preference intact", async () => {
    const { savePreferences, fixtures } = renderSettings();
    fireEvent.click(stageCheckbox("senior_plus"));
    fireEvent.click(stageCheckbox("new_grad_junior"));
    fireEvent.click(screen.getByRole("button", { name: "Save changes" }));

    await waitFor(() => expect(savePreferences).toHaveBeenCalledOnce());
    expect(savePreferences).toHaveBeenCalledWith({
      ...fixtures.preferences,
      career_levels: ["internship", "senior_plus", "new_grad_junior"],
    });
  });

  it("prevents saving with no career stage selected", () => {
    const { savePreferences } = renderSettings();
    fireEvent.click(stageCheckbox("internship"));

    expect(screen.getByText("Select at least one career stage.")).toBeInTheDocument();
    const save = screen.getByRole("button", { name: "Save changes" });
    expect(save).toBeDisabled();
    fireEvent.submit(save.closest("form"));
    expect(savePreferences).not.toHaveBeenCalled();
  });

  it("disables internship season without internships and restores it on reselect", async () => {
    const { savePreferences } = renderSettings({ internship_season: "Spring 2027" });
    expect(seasonSelect()).toBeEnabled();

    fireEvent.click(stageCheckbox("senior_plus"));
    fireEvent.click(stageCheckbox("internship"));
    expect(seasonSelect()).toBeDisabled();
    expect(seasonSelect()).toHaveValue("Spring 2027");
    expect(screen.getByText(/applies only to internship/i)).toBeInTheDocument();

    fireEvent.click(screen.getByRole("button", { name: "Save changes" }));
    await waitFor(() => expect(savePreferences).toHaveBeenCalledOnce());
    expect(savePreferences.mock.calls[0][0]).toMatchObject({
      career_levels: ["senior_plus"],
      internship_season: "Spring 2027",
    });

    fireEvent.click(stageCheckbox("internship"));
    expect(seasonSelect()).toBeEnabled();
    expect(seasonSelect()).toHaveValue("Spring 2027");
  });
});

describe("settings unsubscribe", () => {
  function renderSettings() {
    const fixtures = makeHostedFixtures();
    const savePreferences = vi.fn(async () => {});
    render(
      <SettingsPage
        me={fixtures.me}
        preferences={fixtures.preferences}
        savePreferences={savePreferences}
      />,
    );
    return { savePreferences, persisted: fixtures.preferences };
  }

  async function unsubscribe(savePreferences) {
    fireEvent.click(screen.getByRole("button", { name: /Unsubscribe from alerts/ }));
    fireEvent.click(screen.getByRole("button", { name: "Unsubscribe" }));
    await waitFor(() => expect(savePreferences).toHaveBeenCalledOnce());
    expect(await screen.findByRole("status")).toHaveTextContent(
      "Email alerts are now unsubscribed.",
    );
    return savePreferences.mock.calls[0][0];
  }

  it("pauses delivery from persisted preferences when no career stage is selected", async () => {
    const { savePreferences, persisted } = renderSettings();
    fireEvent.click(stageCheckbox("internship"));
    expect(screen.getByRole("button", { name: "Save changes" })).toBeDisabled();

    expect(await unsubscribe(savePreferences)).toEqual({
      ...persisted,
      alert_frequency: "paused",
      globally_paused: true,
    });
  });

  it("pauses delivery from persisted preferences when no role is selected", async () => {
    const { savePreferences, persisted } = renderSettings();
    for (const roleId of persisted.role_ids) {
      const role = ROLE_OPTIONS.find((option) => option.id === roleId);
      fireEvent.click(screen.getByRole("checkbox", { name: role.name }));
    }
    expect(screen.getByRole("button", { name: "Save changes" })).toBeDisabled();

    const sent = await unsubscribe(savePreferences);
    expect(sent.role_ids).toEqual(persisted.role_ids);
    expect(sent.career_levels).toEqual(persisted.career_levels);
    expect(sent).toMatchObject({ alert_frequency: "paused", globally_paused: true });
  });
});
