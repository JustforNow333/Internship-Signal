import { fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";

import App from "../App.jsx";
import { HostedApiError, createMockHostedApi } from "../hosted/api.js";

const EXCLUDED_TITLE = "Software Engineering Intern, Payments";

// The backend reconciles a user's matches when a matching preference changes,
// so after the save the Active list it serves no longer contains the
// now-ineligible posting. The mock models that server-side effect here.
function reconcilingClient() {
  const client = createMockHostedApi();
  const getMatches = client.getMatches;
  const original = client.updatePreferences;
  let reconciled = false;
  const updatePreferences = vi.spyOn(client, "updatePreferences").mockImplementation(async (input) => {
    const saved = await original(input);
    if (input.education_level === "undergraduate") reconciled = true;
    return saved;
  });
  const matchesSpy = vi.spyOn(client, "getMatches").mockImplementation(async (params) => {
    const matches = await getMatches(params);
    return reconciled ? matches.filter((match) => match.title !== EXCLUDED_TITLE) : matches;
  });
  return { client, matchesSpy, updatePreferences };
}

function renderApp(path, client) {
  return render(<App initialPath={path} client={client} />);
}

function nav(label) {
  const menu = screen.getByRole("navigation", { name: "Application" });
  fireEvent.click(within(menu).getByRole("link", { name: new RegExp(label) }));
}

async function saveSettings() {
  fireEvent.click(screen.getByRole("button", { name: "Save changes" }));
}

describe("Active matches after a preference save", () => {
  it("drops matches the backend deactivated when education changes", async () => {
    const { client, matchesSpy } = reconcilingClient();
    renderApp("/app/matches", client);
    expect(await screen.findAllByText(EXCLUDED_TITLE)).not.toHaveLength(0);
    expect(matchesSpy).toHaveBeenCalledTimes(1);

    nav("Settings");
    await screen.findByRole("heading", { name: "Settings" });
    fireEvent.change(screen.getByRole("combobox", { name: /^Education level/ }), {
      target: { value: "undergraduate" },
    });
    await saveSettings();
    expect(await screen.findByRole("status")).toHaveTextContent(
      "Your alert preferences have been saved.",
    );
    expect(matchesSpy).toHaveBeenCalledTimes(2);

    nav("Matches");
    await screen.findByRole("heading", { name: /Matches/ });
    await waitFor(() => expect(screen.queryByText(EXCLUDED_TITLE)).not.toBeInTheDocument());
  });

  it("does not reload matches for a delivery-only change", async () => {
    const { client, matchesSpy, updatePreferences } = reconcilingClient();
    renderApp("/app/settings", client);
    await screen.findByRole("heading", { name: "Settings" });
    fireEvent.click(screen.getByRole("radio", { name: /Every 3 hours/ }));
    await saveSettings();
    expect(await screen.findByRole("status")).toHaveTextContent(
      "Your alert preferences have been saved.",
    );
    expect(updatePreferences).toHaveBeenCalledOnce();
    expect(matchesSpy).toHaveBeenCalledTimes(1);
  });

  it("keeps the saved preferences and says so when the match reload fails", async () => {
    const { client, matchesSpy, updatePreferences } = reconcilingClient();
    renderApp("/app/settings", client);
    await screen.findByRole("heading", { name: "Settings" });
    matchesSpy.mockRejectedValueOnce(new HostedApiError("Network down", 503));
    fireEvent.change(screen.getByRole("combobox", { name: /^Education level/ }), {
      target: { value: "undergraduate" },
    });
    await saveSettings();
    expect(await screen.findByRole("alert")).toHaveTextContent(
      /preferences were saved, but your matches could not be refreshed/i,
    );
    expect(updatePreferences).toHaveBeenCalledOnce();
    expect(screen.getByRole("combobox", { name: /^Education level/ })).toHaveValue(
      "undergraduate",
    );
  });
});

describe("onboarding preference save", () => {
  it("does not request matches before a workspace is loaded", async () => {
    const { client, matchesSpy, updatePreferences } = reconcilingClient();
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
    expect(updatePreferences).toHaveBeenCalledOnce();
    expect(matchesSpy).not.toHaveBeenCalled();
  });
});
