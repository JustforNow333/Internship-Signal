import {
  fireEvent,
  render,
  screen,
  waitFor,
  within,
} from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";

import App from "../App.jsx";
import { HostedApiError, createMockHostedApi } from "../hosted/api.js";
import { makeHostedFixtures } from "../hosted/fixtures.js";
import WatchlistPage from "../hosted/WatchlistPage.jsx";

function renderApp(path, options = {}) {
  const client = options.client || createMockHostedApi(options);
  return { client, ...render(<App initialPath={path} client={client} />) };
}

async function reachCompanyStep() {
  renderApp("/onboarding");
  const role = await screen.findByText("Software Engineering");
  fireEvent.click(role.closest("label"));
  fireEvent.click(
    screen.getByRole("button", { name: /Continue to companies/i }),
  );
  await screen.findByRole("heading", {
    name: /Which companies should we watch/i,
  });
}

async function reachAlertStep() {
  await reachCompanyStep();
  fireEvent.click(screen.getByRole("button", { name: "Add Stripe" }));
  fireEvent.click(screen.getByRole("button", { name: /Continue to alerts/i }));
  await screen.findByRole("heading", {
    name: /When and where should we look/i,
  });
}

describe("hosted Internship Signal MVP", () => {
  it("navigates from the landing page to account creation and sign in", () => {
    renderApp("/");
    expect(
      screen.getByRole("heading", { name: "Never apply late again." }),
    ).toBeInTheDocument();

    fireEvent.click(
      screen.getAllByRole("link", { name: "Create your watchlist" })[0],
    );
    expect(
      screen.getByRole("heading", { name: "Create your account" }),
    ).toBeInTheDocument();

    fireEvent.click(screen.getByRole("link", { name: "Sign in" }));
    expect(
      screen.getByRole("heading", { name: "Sign in to Internship Signal" }),
    ).toBeInTheDocument();
  });

  it("preserves native modified-click behavior on internal links", () => {
    renderApp("/");
    const signIn = screen.getAllByRole("link", { name: "Sign in" })[0];
    signIn.addEventListener("click", (event) => event.preventDefault(), {
      once: true,
    });
    fireEvent.click(signIn, { ctrlKey: true });
    expect(
      screen.getByRole("heading", { name: "Never apply late again." }),
    ).toBeInTheDocument();
  });

  it("validates the minimal signup form", () => {
    renderApp("/signup");
    fireEvent.change(screen.getByLabelText("Email"), {
      target: { value: "not-an-email" },
    });
    fireEvent.change(screen.getByLabelText(/^Password/), {
      target: { value: "short" },
    });
    fireEvent.change(screen.getByLabelText("Confirm password"), {
      target: { value: "different" },
    });
    fireEvent.click(screen.getByRole("button", { name: "Create account" }));
    expect(
      screen.getByText("Enter a valid email address."),
    ).toBeInTheDocument();
    expect(screen.getByText("Use at least 8 characters.")).toBeInTheDocument();
    expect(screen.getByText("Passwords do not match.")).toBeInTheDocument();
  });

  it("validates sign-in email and password", () => {
    renderApp("/signin");
    fireEvent.change(screen.getByLabelText("Email"), {
      target: { value: "alex" },
    });
    fireEvent.click(screen.getByRole("button", { name: "Sign in" }));
    expect(
      screen.getByText("Enter a valid email address."),
    ).toBeInTheDocument();
    expect(screen.getByText("Enter your password.")).toBeInTheDocument();
  });

  it("submits signup and supports verification resend", async () => {
    const client = createMockHostedApi();
    const resend = vi.spyOn(client, "resendVerification");
    renderApp("/signup", { client });
    fireEvent.change(screen.getByLabelText("Email"), {
      target: { value: "student@example.com" },
    });
    fireEvent.change(screen.getByLabelText(/^Password/), {
      target: { value: "password123" },
    });
    fireEvent.change(screen.getByLabelText("Confirm password"), {
      target: { value: "password123" },
    });
    fireEvent.click(screen.getByRole("button", { name: "Create account" }));

    expect(
      await screen.findByRole("heading", { name: "Verify your email" }),
    ).toBeInTheDocument();
    fireEvent.click(
      screen.getByRole("button", { name: "Resend verification email" }),
    );
    await waitFor(() =>
      expect(resend).toHaveBeenCalledWith({
        email: "student@example.com",
      }),
    );
  });

  it("submits verification and password-reset tokens", async () => {
    const client = createMockHostedApi();
    const verify = vi.spyOn(client, "verifyEmail");
    const reset = vi.spyOn(client, "resetPassword");
    const { unmount } = renderApp("/verify-email?token=verification-token", {
      client,
    });
    fireEvent.click(screen.getByRole("button", { name: "Verify email" }));
    await waitFor(() =>
      expect(verify).toHaveBeenCalledWith({ token: "verification-token" }),
    );
    unmount();

    renderApp("/reset-password?token=reset-token", { client });
    fireEvent.change(screen.getByLabelText(/^New password/), {
      target: { value: "new-password" },
    });
    fireEvent.change(screen.getByLabelText("Confirm new password"), {
      target: { value: "new-password" },
    });
    fireEvent.click(screen.getByRole("button", { name: "Update password" }));
    await waitFor(() =>
      expect(reset).toHaveBeenCalledWith({
        token: "reset-token",
        password: "new-password",
      }),
    );
    expect(await screen.findByText("Password updated")).toBeInTheDocument();
  });

  it("requires and preserves multi-select role choices", async () => {
    renderApp("/onboarding");
    const continueButton = await screen.findByRole("button", {
      name: /Continue to companies/i,
    });
    expect(continueButton).toBeDisabled();
    fireEvent.click(screen.getByText("Software Engineering").closest("label"));
    fireEvent.click(screen.getByText("Machine Learning / AI").closest("label"));
    expect(screen.getByText("2 role categories selected")).toBeInTheDocument();
    expect(continueButton).toBeEnabled();
  });

  it("searches supported companies and makes add/remove state explicit", async () => {
    await reachCompanyStep();
    const search = screen.getByPlaceholderText("Search supported companies");
    fireEvent.change(search, { target: { value: "stripe" } });
    expect(screen.getByText("Stripe")).toBeInTheDocument();
    expect(screen.queryByText("NVIDIA")).not.toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "Add Stripe" }));
    expect(
      screen.getByText(
        (_, element) =>
          element.tagName === "P" &&
          element.textContent === "1 company selected",
      ),
    ).toBeInTheDocument();
    expect(
      screen.getByRole("button", { name: "Remove Stripe" }),
    ).toHaveAttribute("aria-pressed", "true");
  });

  it("does not allow an unavailable catalog company to be selected", async () => {
    const fixtures = makeHostedFixtures();
    fixtures.companies = [{ ...fixtures.companies[0], selectable: false }];
    renderApp("/onboarding", { fixtures });
    fireEvent.click(
      (await screen.findByText("Software Engineering")).closest("label"),
    );
    fireEvent.click(
      screen.getByRole("button", { name: /Continue to companies/i }),
    );

    expect(
      await screen.findByRole("button", { name: "Stripe unavailable" }),
    ).toBeDisabled();
  });

  it("shows the company empty-search state", async () => {
    await reachCompanyStep();
    fireEvent.change(
      screen.getByPlaceholderText("Search supported companies"),
      { target: { value: "company that is not supported" } },
    );
    expect(
      screen.getByText("No supported companies match your search"),
    ).toBeInTheDocument();
  });

  it("defaults to ASAP alerts and allows another alert preference", async () => {
    await reachAlertStep();
    expect(screen.getByLabelText(/As soon as detected/i)).toBeChecked();
    expect(screen.getByLabelText("United States")).toBeChecked();
    fireEvent.click(screen.getByLabelText("United States"));
    expect(screen.getByLabelText("United States")).not.toBeChecked();
    fireEvent.click(screen.getByLabelText(/Daily summary/i));
    expect(screen.getByLabelText(/Daily summary/i)).toBeChecked();
    expect(
      screen.getByText(/does not monitor career pages continuously/i),
    ).toBeInTheDocument();

    fireEvent.click(
      screen.getByRole("button", { name: "Activate my watchlist" }),
    );

    expect(
      await screen.findByRole("heading", { name: "Your watchlist is active." }),
    ).toBeInTheDocument();
    expect(screen.getByText("Daily summary")).toBeInTheDocument();
  });

  it("submits onboarding and confirms the personal watchlist is active", async () => {
    await reachAlertStep();
    fireEvent.click(
      screen.getByRole("button", { name: "Activate my watchlist" }),
    );
    expect(
      await screen.findByRole("heading", { name: "Your watchlist is active." }),
    ).toBeInTheDocument();
    expect(
      screen.getByText(
        (_, element) =>
          element.tagName === "SPAN" &&
          element.textContent === "1 company watched",
      ),
    ).toBeInTheDocument();
  });

  it("renders the dashboard with watchlist status and newest matches", async () => {
    renderApp("/app/dashboard");
    expect(
      await screen.findByRole("heading", { name: "Good morning." }),
    ).toBeInTheDocument();
    expect(screen.getByText("Companies watched")).toBeInTheDocument();
    expect(screen.getByText("Last successful scan")).toBeInTheDocument();
    expect(
      screen.getAllByText("Software Engineering Intern, Payments").length,
    ).toBeGreaterThan(0);
    expect(
      screen.getByRole("button", { name: "Edit watchlist" }),
    ).toBeInTheDocument();
  });

  it("toggles saved state from the dashboard", async () => {
    renderApp("/app/dashboard");
    await screen.findByRole("heading", { name: "Good morning." });
    const save = screen.getAllByRole("button", { name: "☆ Save" })[0];

    // Saving now persists through the hosted API, so the label updates after
    // the request resolves rather than from local-only component state.
    fireEvent.click(save);
    expect(
      await screen.findByRole("button", { name: "★ Saved" }),
    ).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "★ Saved" }));
    await waitFor(() =>
      expect(
        screen.queryByRole("button", { name: "★ Saved" }),
      ).not.toBeInTheDocument(),
    );
    expect(
      screen.getAllByRole("button", { name: "☆ Save" }).length,
    ).toBeGreaterThan(0);
  });

  it("filters matches by company while keeping newest-first context", async () => {
    renderApp("/app/matches");
    await screen.findByRole("heading", { name: "Matches" });
    fireEvent.change(screen.getByLabelText("Company filter"), {
      target: { value: "Stripe" },
    });
    expect(
      screen.getByText("Software Engineering Intern, Payments"),
    ).toBeInTheDocument();
    expect(
      screen.queryByText("Machine Learning Systems Intern"),
    ).not.toBeInTheDocument();
    expect(screen.getByText("↓ Newest first")).toBeInTheDocument();
  });

  it("toggles saved state from the matches page", async () => {
    renderApp("/app/matches");
    await screen.findByRole("heading", { name: "Matches" });
    fireEvent.click(screen.getAllByRole("button", { name: "☆ Save" })[0]);
    expect(
      await screen.findByRole("button", { name: "★ Saved" }),
    ).toBeInTheDocument();
  });

  it("opens a match with reasons, source, and a prominent application action", async () => {
    renderApp("/app/matches");
    const title = await screen.findByText(
      "Software Engineering Intern, Payments",
    );
    fireEvent.click(title.closest("button"));
    const drawer = screen.getByRole("dialog", {
      name: "Software Engineering Intern, Payments",
    });
    expect(within(drawer).getByText("Why this matched")).toBeInTheDocument();
    expect(
      within(drawer).getByRole("link", { name: /Apply on Stripe/i }),
    ).toHaveAttribute("href", "https://stripe.com/jobs/search");
  });

  it("adds and removes a company from the authenticated watchlist", async () => {
    renderApp("/app/watchlist");
    await screen.findByRole("heading", { name: "Watchlist" });
    fireEvent.change(
      screen.getByPlaceholderText("Search supported companies"),
      { target: { value: "Duolingo" } },
    );
    fireEvent.click(screen.getByRole("button", { name: "+ Add to watchlist" }));
    expect(
      await screen.findByText(/Duolingo added to your watchlist/),
    ).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "Remove" }));
    expect(
      await screen.findByText(/Duolingo removed from your watchlist/),
    ).toBeInTheDocument();
  });

  it("blocks every watchlist mutation while a replacement save is pending", () => {
    const saveWatchlist = vi.fn(() => new Promise(() => {}));
    render(
      <WatchlistPage
        companies={[
          {
            id: "alpha",
            name: "Alpha",
            aliases: [],
            coverage: "direct",
            selectable: true,
          },
          {
            id: "beta",
            name: "Beta",
            aliases: [],
            coverage: "direct",
            selectable: true,
          },
        ]}
        watchlist={[]}
        saveWatchlist={saveWatchlist}
        requestCompany={vi.fn()}
      />,
    );

    const addButtons = screen.getAllByRole("button", {
      name: "+ Add to watchlist",
    });
    fireEvent.click(addButtons[0]);

    expect(saveWatchlist).toHaveBeenCalledTimes(1);
    expect(addButtons[0]).toBeDisabled();
    expect(addButtons[1]).toBeDisabled();
    fireEvent.click(addButtons[1]);
    expect(saveWatchlist).toHaveBeenCalledTimes(1);
  });

  it("updates role and location selections in settings", async () => {
    const client = createMockHostedApi();
    const updatePreferences = vi.spyOn(client, "updatePreferences");
    renderApp("/app/settings", { client });
    await screen.findByRole("heading", { name: "Settings" });
    const software = screen.getByRole("checkbox", {
      name: "Software Engineering",
    });
    const boston = screen.getByRole("checkbox", { name: "Boston, MA" });
    expect(software).toBeChecked();
    expect(boston).not.toBeChecked();

    fireEvent.click(software);
    fireEvent.click(boston);
    fireEvent.click(screen.getByRole("button", { name: "Save changes" }));

    expect(await screen.findByRole("status")).toHaveTextContent(
      "Your alert preferences have been saved.",
    );
    expect(updatePreferences).toHaveBeenCalledWith(
      expect.objectContaining({
        preferred_locations: ["United States", "New York, NY", "Boston, MA"],
        internship_season: "Summer 2027",
      }),
    );
    expect(software).not.toBeChecked();
    expect(boston).toBeChecked();
  });

  it("does not claim account deletion without a backend endpoint", async () => {
    renderApp("/app/settings");
    await screen.findByRole("heading", { name: "Settings" });

    expect(
      screen.getByRole("button", { name: /Delete account/ }),
    ).toBeDisabled();
    expect(
      screen.getByText("Account deletion is not available yet."),
    ).toBeInTheDocument();
  });

  it("renders a loading state while hosted data is pending", () => {
    renderApp("/app/dashboard", { latency: 200 });
    expect(screen.getByRole("status")).toHaveTextContent(
      "Loading your Internship Signal workspace",
    );
    expect(
      screen.getByRole("button", { name: "Account settings" }),
    ).toBeInTheDocument();
  });

  it("signs out through the hosted adapter", async () => {
    const client = createMockHostedApi();
    const logout = vi.spyOn(client, "logout");
    renderApp("/app/dashboard", { client });
    await screen.findByRole("heading", { name: "Good morning." });

    fireEvent.click(screen.getByRole("button", { name: "Sign out" }));

    await waitFor(() => expect(logout).toHaveBeenCalledOnce());
    expect(
      screen.getByRole("heading", { name: "Sign in to Internship Signal" }),
    ).toBeInTheDocument();
  });

  it("does not claim sign-out when session revocation fails", async () => {
    const client = createMockHostedApi();
    client.logout = vi
      .fn()
      .mockRejectedValue(new Error("Sign-out unavailable."));
    renderApp("/app/dashboard", { client });
    await screen.findByRole("heading", { name: "Good morning." });

    fireEvent.click(screen.getByRole("button", { name: "Sign out" }));

    expect(await screen.findByRole("alert")).toHaveTextContent(
      "Sign-out unavailable.",
    );
    expect(
      screen.getByRole("heading", { name: "Good morning." }),
    ).toBeInTheDocument();
  });

  it("redirects a protected route to sign in after HTTP 401", async () => {
    const client = createMockHostedApi();
    client.getMe = vi
      .fn()
      .mockRejectedValue(new HostedApiError("Authentication required.", 401));
    renderApp("/app/dashboard", { client });

    expect(
      await screen.findByRole("heading", {
        name: "Sign in to Internship Signal",
      }),
    ).toBeInTheDocument();
  });

  it("renders the matches empty state", async () => {
    const fixtures = makeHostedFixtures();
    fixtures.matches = [];
    renderApp("/app/matches", { fixtures });
    expect(
      await screen.findByText("No matching internships yet"),
    ).toBeInTheDocument();
  });

  it("renders an API error state and offers retry", async () => {
    const client = createMockHostedApi({
      failures: { companies: "Company catalog is unavailable." },
    });
    renderApp("/app/dashboard", { client });
    expect(await screen.findByRole("alert")).toHaveTextContent(
      "Company catalog is unavailable.",
    );
    expect(
      screen.getByRole("button", { name: "Try again" }),
    ).toBeInTheDocument();
  });

  it("does not leave an unknown route stuck in an idle loading state", () => {
    renderApp("/missing-page");
    expect(
      screen.getByRole("heading", { name: "Never apply late again." }),
    ).toBeInTheDocument();
  });

  it("loads onboarding when an unrelated matches request is unavailable", async () => {
    const client = createMockHostedApi({
      failures: { matches: "Matches are temporarily unavailable." },
    });
    renderApp("/onboarding", { client });
    expect(
      await screen.findByRole("heading", { name: /What kind of internships/i }),
    ).toBeInTheDocument();
  });

  it("dismisses the company-request dialog with Escape", async () => {
    renderApp("/app/watchlist");
    await screen.findByRole("heading", { name: "Watchlist" });
    fireEvent.click(screen.getByRole("button", { name: "Request a company" }));
    expect(
      screen.getByRole("dialog", { name: "Request a company" }),
    ).toBeInTheDocument();

    fireEvent.keyDown(window, { key: "Escape" });

    expect(
      screen.queryByRole("dialog", { name: "Request a company" }),
    ).not.toBeInTheDocument();
  });
});
