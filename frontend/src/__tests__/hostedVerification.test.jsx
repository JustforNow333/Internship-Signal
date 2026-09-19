import { StrictMode } from "react";
import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";

import App from "../App.jsx";
import { HostedApiError, createMockHostedApi } from "../hosted/api.js";
import { Brand } from "../hosted/ui.jsx";

function renderApp(path, client) {
  return render(<App initialPath={path} client={client} />);
}

function unverifiedClient(overrides = {}) {
  const client = createMockHostedApi();
  return { ...client, ...overrides };
}

describe("brand", () => {
  it("renders the product name without a trailing period", () => {
    render(<Brand navigate={() => {}} />);
    const brand = screen.getByRole("link", { name: "Internship Signal home" });

    expect(brand.textContent).toBe("Internship Signal");
    expect(brand.textContent).not.toContain(".");
  });
});

describe("/verify-email with a token", () => {
  it("verifies automatically and continues to onboarding", async () => {
    // Held open so the in-flight state is observable rather than a flash.
    let release;
    const verifyEmail = vi
      .fn()
      .mockReturnValue(new Promise((resolve) => (release = resolve)));
    const client = unverifiedClient({ verifyEmail });
    renderApp("/verify-email?token=one-time-token", client);

    expect(await screen.findByText(/Verifying your email/i)).toBeInTheDocument();
    expect(verifyEmail).toHaveBeenCalledWith({ token: "one-time-token" });
    expect(verifyEmail).toHaveBeenCalledTimes(1);

    release({ accepted: true });
    await screen.findByText(/Your email is verified/i);

    fireEvent.click(screen.getByRole("button", { name: /Continue to setup/i }));
    await screen.findByRole("heading", {
      name: /What kind of internships are you looking for/i,
    });
  });

  it("submits a single-use token only once under StrictMode double effects", async () => {
    const verifyEmail = vi.fn().mockResolvedValue({ accepted: true });
    const client = unverifiedClient({ verifyEmail });
    render(
      <StrictMode>
        <App initialPath="/verify-email?token=strict-token" client={client} />
      </StrictMode>,
    );

    await screen.findByText(/Your email is verified/i);
    expect(verifyEmail).toHaveBeenCalledTimes(1);
  });

  it("shows the backend error and offers another email after a bad link", async () => {
    const verifyEmail = vi
      .fn()
      .mockRejectedValue(
        new HostedApiError("This verification link is invalid or expired.", 400),
      );
    const resendVerification = vi.fn().mockResolvedValue({ accepted: true });
    const client = unverifiedClient({
      verifyEmail,
      resendVerification,
      login: async () => ({
        user: { id: "u", email: "alex@example.com", email_verified: false },
      }),
    });
    renderApp("/signin", client);

    fireEvent.change(screen.getByLabelText(/Email/i), {
      target: { value: "alex@example.com" },
    });
    fireEvent.change(screen.getByLabelText(/Password/i), {
      target: { value: "secure password" },
    });
    fireEvent.click(screen.getByRole("button", { name: /^Sign in$/i }));

    await screen.findByRole("heading", { name: /Verify your email/i });
    expect(verifyEmail).not.toHaveBeenCalled();

    fireEvent.click(
      screen.getByRole("button", { name: /Resend verification email/i }),
    );
    await waitFor(() =>
      expect(resendVerification).toHaveBeenCalledWith({
        email: "alex@example.com",
      }),
    );
  });

  it("surfaces the backend message when the token is rejected", async () => {
    const verifyEmail = vi
      .fn()
      .mockRejectedValue(
        new HostedApiError("This verification link is invalid or expired.", 400),
      );
    const client = unverifiedClient({ verifyEmail });
    renderApp("/verify-email?token=expired-token", client);

    expect(await screen.findByRole("alert")).toHaveTextContent(
      "This verification link is invalid or expired.",
    );
  });
});

describe("/verify-email without a token", () => {
  it("keeps the check-your-email state and states failed delivery honestly", async () => {
    const client = unverifiedClient({
      signup: async () => ({
        user: { id: "u", email: "new@example.com", email_verified: false },
        verification_email_sent: false,
      }),
    });
    renderApp("/signup", client);

    fireEvent.change(screen.getByLabelText(/Email/i), {
      target: { value: "new@example.com" },
    });
    fireEvent.change(screen.getByLabelText(/^Password/), {
      target: { value: "secure password" },
    });
    fireEvent.change(screen.getByLabelText(/Confirm password/i), {
      target: { value: "secure password" },
    });
    fireEvent.click(screen.getByRole("button", { name: /Create account/i }));

    await screen.findByRole("heading", { name: /Verify your email/i });
    expect(screen.getByText("new@example.com")).toBeInTheDocument();
    expect(
      screen.getByText(/couldn’t send the verification email/i),
    ).toBeInTheDocument();
    expect(
      screen.getByRole("button", { name: /Resend verification email/i }),
    ).toBeInTheDocument();
  });
});

describe("login verification gate", () => {
  it("sends an unverified user to /verify-email", async () => {
    const client = unverifiedClient({
      login: async () => ({
        user: { id: "u", email: "unverified@example.com", email_verified: false },
      }),
    });
    renderApp("/signin", client);

    fireEvent.change(screen.getByLabelText(/Email/i), {
      target: { value: "unverified@example.com" },
    });
    fireEvent.change(screen.getByLabelText(/Password/i), {
      target: { value: "secure password" },
    });
    fireEvent.click(screen.getByRole("button", { name: /^Sign in$/i }));

    await screen.findByRole("heading", { name: /Verify your email/i });
    expect(screen.getByText("unverified@example.com")).toBeInTheDocument();
  });

  it("keeps the dashboard flow for a verified user", async () => {
    const client = unverifiedClient({
      login: async () => ({
        user: { id: "u", email: "verified@example.com", email_verified: true },
      }),
    });
    renderApp("/signin", client);

    fireEvent.change(screen.getByLabelText(/Email/i), {
      target: { value: "verified@example.com" },
    });
    fireEvent.change(screen.getByLabelText(/Password/i), {
      target: { value: "secure password" },
    });
    fireEvent.click(screen.getByRole("button", { name: /^Sign in$/i }));

    await screen.findByRole("heading", { name: /Good morning/i });
  });
});
