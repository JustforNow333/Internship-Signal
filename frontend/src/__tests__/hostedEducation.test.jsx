import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";

import Onboarding from "../hosted/Onboarding.jsx";
import SettingsPage from "../hosted/SettingsPage.jsx";
import { createMockHostedApi } from "../hosted/api.js";
import {
  CLASS_STANDING_OPTIONS,
  EDUCATION_LEVEL_OPTIONS,
} from "../hosted/constants.js";
import { makeHostedFixtures } from "../hosted/fixtures.js";
import { educationOrDefault } from "../hosted/utils.js";

function educationSelect() {
  return screen.getByRole("combobox", { name: /^Education level/ });
}

function standingSelect() {
  return screen.queryByRole("combobox", { name: /^Class standing/ });
}

function optionLabels(select) {
  return [...select.options].map((option) => [option.value, option.textContent]);
}

function renderSettings(preferences) {
  const fixtures = makeHostedFixtures();
  const savePreferences = vi.fn(async () => {});
  const merged = { ...fixtures.preferences, ...preferences };
  render(
    <SettingsPage me={fixtures.me} preferences={merged} savePreferences={savePreferences} />,
  );
  return { savePreferences, persisted: merged };
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

async function activate(savePreferences) {
  fireEvent.click(screen.getByRole("button", { name: /Activate my watchlist/i }));
  await screen.findByRole("heading", { name: /Your watchlist is active/i });
  expect(savePreferences).toHaveBeenCalledOnce();
  return savePreferences.mock.calls[0][0];
}

async function saveSettings(savePreferences) {
  fireEvent.click(screen.getByRole("button", { name: "Save changes" }));
  await waitFor(() => expect(savePreferences).toHaveBeenCalledOnce());
  return savePreferences.mock.calls[0][0];
}

describe("education constants and defaults", () => {
  it("exposes exactly the backend ids with user-facing labels", () => {
    expect(EDUCATION_LEVEL_OPTIONS.map((o) => [o.id, o.label])).toEqual([
      ["not_specified", "Not specified"],
      ["undergraduate", "Undergraduate"],
      ["masters", "Master's"],
      ["doctoral", "Doctoral/PhD"],
    ]);
    expect(CLASS_STANDING_OPTIONS.map((o) => [o.id, o.label])).toEqual([
      ["not_specified", "Not specified"],
      ["first_year", "First-year"],
      ["sophomore", "Sophomore"],
      ["junior", "Junior"],
      ["senior", "Senior"],
    ]);
  });

  it("defaults missing, unknown or inconsistent values to not_specified", () => {
    const none = { education_level: "not_specified", class_standing: "not_specified" };
    expect(educationOrDefault(undefined)).toEqual(none);
    expect(educationOrDefault({})).toEqual(none);
    expect(educationOrDefault({ education_level: null, class_standing: null })).toEqual(none);
    expect(educationOrDefault({ education_level: "bachelor", class_standing: "junior" })).toEqual(
      none,
    );
    expect(educationOrDefault({ education_level: "masters", class_standing: "junior" })).toEqual({
      education_level: "masters",
      class_standing: "not_specified",
    });
    expect(
      educationOrDefault({ education_level: "undergraduate", class_standing: "fifth_year" }),
    ).toEqual({ education_level: "undergraduate", class_standing: "not_specified" });
    expect(
      educationOrDefault({ education_level: "undergraduate", class_standing: "senior" }),
    ).toEqual({ education_level: "undergraduate", class_standing: "senior" });
  });
});

describe("settings education", () => {
  it("renders a separate education section with help text and all options", () => {
    renderSettings();

    expect(screen.getByRole("heading", { name: "Education" })).toBeInTheDocument();
    expect(screen.getByRole("heading", { name: "Career stages" })).toBeInTheDocument();
    const select = educationSelect();
    expect(optionLabels(select)).toEqual(
      EDUCATION_LEVEL_OPTIONS.map((o) => [o.id, o.label]),
    );
    expect(select).toHaveValue("not_specified");
    expect(select).toHaveAccessibleDescription(/Not specified keeps every posting/);
    expect(select).toHaveAccessibleDescription(/unknown .*requirements are always shown/i);
    expect(standingSelect()).not.toBeInTheDocument();
  });

  it("renders existing undergraduate values with class standing", () => {
    renderSettings({ education_level: "undergraduate", class_standing: "junior" });

    expect(educationSelect()).toHaveValue("undergraduate");
    expect(standingSelect()).toHaveValue("junior");
    expect(optionLabels(standingSelect())).toEqual(
      CLASS_STANDING_OPTIONS.map((o) => [o.id, o.label]),
    );
  });

  it("renders existing masters value without class standing", () => {
    renderSettings({ education_level: "masters", class_standing: "not_specified" });
    expect(educationSelect()).toHaveValue("masters");
    expect(standingSelect()).not.toBeInTheDocument();
  });

  it("treats legacy preferences without the fields as Not specified and sends both", async () => {
    const fixtures = makeHostedFixtures();
    const {
      education_level: _level,
      class_standing: _standing,
      ...legacy
    } = fixtures.preferences;
    const savePreferences = vi.fn(async () => {});
    render(
      <SettingsPage me={fixtures.me} preferences={legacy} savePreferences={savePreferences} />,
    );

    expect(educationSelect()).toHaveValue("not_specified");
    expect(standingSelect()).not.toBeInTheDocument();
    const sent = await saveSettings(savePreferences);
    expect(sent).toMatchObject({
      education_level: "not_specified",
      class_standing: "not_specified",
    });
  });

  it("shows class standing only for undergraduate and submits both fields", async () => {
    const { savePreferences, persisted } = renderSettings();
    fireEvent.change(educationSelect(), { target: { value: "undergraduate" } });
    expect(standingSelect()).toHaveValue("not_specified");
    fireEvent.change(standingSelect(), { target: { value: "senior" } });

    const sent = await saveSettings(savePreferences);
    expect(sent).toEqual({
      ...persisted,
      education_level: "undergraduate",
      class_standing: "senior",
    });
  });

  it("resets class standing when the level changes away from undergraduate", async () => {
    const { savePreferences } = renderSettings({
      education_level: "undergraduate",
      class_standing: "sophomore",
    });
    fireEvent.change(educationSelect(), { target: { value: "doctoral" } });
    expect(standingSelect()).not.toBeInTheDocument();

    fireEvent.change(educationSelect(), { target: { value: "undergraduate" } });
    expect(standingSelect()).toHaveValue("not_specified");
    fireEvent.change(educationSelect(), { target: { value: "masters" } });

    const sent = await saveSettings(savePreferences);
    expect(sent).toMatchObject({ education_level: "masters", class_standing: "not_specified" });
  });

  it("keeps education untouched when unsubscribing from persisted preferences", async () => {
    const { savePreferences, persisted } = renderSettings({
      education_level: "undergraduate",
      class_standing: "first_year",
    });
    fireEvent.change(educationSelect(), { target: { value: "masters" } });
    fireEvent.click(screen.getByRole("button", { name: /Unsubscribe from alerts/ }));
    fireEvent.click(screen.getByRole("button", { name: "Unsubscribe" }));
    await waitFor(() => expect(savePreferences).toHaveBeenCalledOnce());
    expect(savePreferences.mock.calls[0][0]).toEqual({
      ...persisted,
      alert_frequency: "paused",
      globally_paused: true,
    });
  });
});

describe("onboarding education", () => {
  it("offers education on the alerts step, not the career-stage step", async () => {
    renderOnboarding();
    expect(screen.queryByRole("combobox", { name: /^Education level/ })).not.toBeInTheDocument();

    await advanceToAlerts();
    expect(screen.getByRole("group", { name: "Education" })).toBeInTheDocument();
    expect(educationSelect()).toHaveValue("not_specified");
    expect(educationSelect()).toHaveAccessibleDescription(/Not specified keeps every posting/);
    expect(standingSelect()).not.toBeInTheDocument();
  });

  it("submits not_specified defaults when untouched", async () => {
    const { savePreferences } = renderOnboarding();
    await advanceToAlerts();
    expect(await activate(savePreferences)).toMatchObject({
      education_level: "not_specified",
      class_standing: "not_specified",
      role_ids: ["software_engineering"],
    });
  });

  it("submits undergraduate with the chosen class standing", async () => {
    const { savePreferences } = renderOnboarding();
    await advanceToAlerts();
    fireEvent.change(educationSelect(), { target: { value: "undergraduate" } });
    fireEvent.change(standingSelect(), { target: { value: "junior" } });
    expect(await activate(savePreferences)).toMatchObject({
      education_level: "undergraduate",
      class_standing: "junior",
    });
  });

  it("resets class standing when the level changes away from undergraduate", async () => {
    const { savePreferences } = renderOnboarding();
    await advanceToAlerts();
    fireEvent.change(educationSelect(), { target: { value: "undergraduate" } });
    fireEvent.change(standingSelect(), { target: { value: "senior" } });
    fireEvent.change(educationSelect(), { target: { value: "masters" } });
    expect(standingSelect()).not.toBeInTheDocument();
    expect(await activate(savePreferences)).toMatchObject({
      education_level: "masters",
      class_standing: "not_specified",
    });
  });
});

describe("demo client education persistence", () => {
  it("defaults the demo profile to not_specified", async () => {
    const api = createMockHostedApi();
    expect(await api.getPreferences()).toMatchObject({
      education_level: "not_specified",
      class_standing: "not_specified",
    });
  });

  it("round-trips both fields and clears standing for non-undergraduate levels", async () => {
    const api = createMockHostedApi();
    const current = await api.getPreferences();
    await api.updatePreferences({
      ...current,
      education_level: "undergraduate",
      class_standing: "senior",
    });
    expect(await api.getPreferences()).toMatchObject({
      education_level: "undergraduate",
      class_standing: "senior",
    });

    // An older client that omits class_standing never keeps a stale standing
    // for a non-undergraduate level.
    const { class_standing: _omit, ...withoutStanding } = await api.getPreferences();
    await api.updatePreferences({ ...withoutStanding, education_level: "masters" });
    expect(await api.getPreferences()).toMatchObject({
      education_level: "masters",
      class_standing: "not_specified",
    });

    // Omitting both keeps the stored values.
    const { education_level: _l, class_standing: _s, ...legacy } = await api.getPreferences();
    await api.updatePreferences(legacy);
    expect(await api.getPreferences()).toMatchObject({
      education_level: "masters",
      class_standing: "not_specified",
    });
  });
});
