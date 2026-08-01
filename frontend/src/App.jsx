import { useCallback, useEffect, useMemo, useState } from "react";
import { hostedApi } from "./hosted/api.js";
import AppShell from "./hosted/AppShell.jsx";
import {
  ForgotPasswordPage,
  ResetPasswordPage,
  SigninPage,
  SignupPage,
  VerificationPendingPage,
} from "./hosted/AuthPages.jsx";
import DashboardPage from "./hosted/DashboardPage.jsx";
import LandingPage from "./hosted/LandingPage.jsx";
import MatchesPage from "./hosted/MatchesPage.jsx";
import Onboarding from "./hosted/Onboarding.jsx";
import SettingsPage from "./hosted/SettingsPage.jsx";
import WatchlistPage from "./hosted/WatchlistPage.jsx";
import { AsyncPanel, freshnessFor } from "./hosted/ui.jsx";

const APP_ROUTES = new Set([
  "/app/dashboard",
  "/app/matches",
  "/app/watchlist",
  "/app/settings",
]);
const PUBLIC_ROUTES = new Set([
  "/",
  "/signup",
  "/signin",
  "/forgot-password",
  "/reset-password",
  "/verify-email",
]);
const KNOWN_ROUTES = new Set([...PUBLIC_ROUTES, ...APP_ROUTES, "/onboarding"]);

function currentPath(initialPath) {
  const path = (initialPath || window.location.pathname).split("?")[0];
  if (path === "/app" || path === "/app/") return "/app/dashboard";
  return path.length > 1 ? path.replace(/\/+$/, "") : path;
}

function locationToken(initialPath) {
  const query = initialPath?.includes("?")
    ? initialPath.slice(initialPath.indexOf("?"))
    : window.location.search;
  return new URLSearchParams(query).get("token") || "";
}

export default function App({ client = hostedApi, initialPath }) {
  const [path, setPath] = useState(() => currentPath(initialPath));
  const [signupEmail, setSignupEmail] = useState("");
  const [verificationDeliveryAccepted, setVerificationDeliveryAccepted] =
    useState(null);
  const [sessionError, setSessionError] = useState("");
  const [resource, setResource] = useState({
    status: "idle",
    data: null,
    error: "",
  });
  const [onboardingResource, setOnboardingResource] = useState({
    status: "idle",
    companies: [],
    error: "",
  });

  const navigate = useCallback(
    (nextPath) => {
      if (!initialPath && window.location.pathname !== nextPath)
        window.history.pushState({}, "", nextPath);
      setPath(nextPath);
      window.scrollTo?.(0, 0);
    },
    [initialPath],
  );

  useEffect(() => {
    if (initialPath) return undefined;
    const onPopState = () => setPath(currentPath());
    window.addEventListener("popstate", onPopState);
    return () => window.removeEventListener("popstate", onPopState);
  }, [initialPath]);

  useEffect(() => {
    const onUnauthorized = () => {
      setSessionError("");
      setResource({ status: "idle", data: null, error: "" });
      navigate("/signin");
    };
    window.addEventListener("hosted-api-unauthorized", onUnauthorized);
    return () =>
      window.removeEventListener("hosted-api-unauthorized", onUnauthorized);
  }, [navigate]);

  const loadWorkspace = useCallback(async () => {
    setResource((current) => ({ ...current, status: "loading", error: "" }));
    try {
      const [companies, me, preferences, watchlist, matches] =
        await Promise.all([
          client.listCompanies(),
          client.getMe(),
          client.getPreferences(),
          client.getWatchlist(),
          client.getMatches(),
        ]);
      setResource({
        status: "ready",
        error: "",
        data: { companies, me, preferences, watchlist, matches },
      });
    } catch (error) {
      if (error.status === 401) {
        setResource({ status: "idle", data: null, error: "" });
        navigate("/signin");
        return;
      }
      setResource({
        status: "error",
        data: null,
        error: error.message || "The account data request failed.",
      });
    }
  }, [client, navigate]);

  const loadOnboardingCompanies = useCallback(async () => {
    setOnboardingResource((current) => ({
      ...current,
      status: "loading",
      error: "",
    }));
    try {
      const [companies] = await Promise.all([
        client.listCompanies(),
        client.getMe(),
      ]);
      setOnboardingResource({ status: "ready", companies, error: "" });
    } catch (error) {
      if (error.status === 401) {
        navigate("/signin");
        return;
      }
      setOnboardingResource({
        status: "error",
        companies: [],
        error: error.message || "The company catalog request failed.",
      });
    }
  }, [client, navigate]);

  const logout = useCallback(async () => {
    setSessionError("");
    try {
      await client.logout();
    } catch (error) {
      setSessionError(error.message || "We couldn’t sign you out. Try again.");
      return;
    }
    setResource({ status: "idle", data: null, error: "" });
    navigate("/signin");
  }, [client, navigate]);

  useEffect(() => {
    if (APP_ROUTES.has(path) && resource.status === "idle") loadWorkspace();
  }, [loadWorkspace, path, resource.status]);

  useEffect(() => {
    if (
      path === "/onboarding" &&
      onboardingResource.status === "idle" &&
      !resource.data?.companies
    ) {
      loadOnboardingCompanies();
    }
  }, [
    loadOnboardingCompanies,
    onboardingResource.status,
    path,
    resource.data?.companies,
  ]);

  const savePreferences = useCallback(
    async (preferences) => {
      const saved = await client.updatePreferences(preferences);
      setResource((current) =>
        current.data
          ? { ...current, data: { ...current.data, preferences: saved } }
          : current,
      );
      return saved;
    },
    [client],
  );

  const saveWatchlist = useCallback(
    async (entries) => {
      const saved = await client.updateWatchlist({ companies: entries });
      setResource((current) =>
        current.data
          ? { ...current, data: { ...current.data, watchlist: saved } }
          : current,
      );
      return saved;
    },
    [client],
  );

  const data = resource.data;
  const newMatchCount = useMemo(
    () =>
      data?.matches.filter(
        (match) => freshnessFor(match.detected_at).id !== "older",
      ).length || 0,
    [data?.matches],
  );

  if (path === "/") return <LandingPage navigate={navigate} />;
  if (path === "/signup")
    return (
      <SignupPage
        navigate={navigate}
        client={client}
        onSignup={(email, result) => {
          setSignupEmail(email);
          setVerificationDeliveryAccepted(
            result.verification_email_sent ?? null,
          );
        }}
      />
    );
  if (path === "/signin")
    return (
      <SigninPage
        navigate={navigate}
        client={client}
        onSignedIn={() => {
          setSessionError("");
          setResource({ status: "idle", data: null, error: "" });
        }}
      />
    );
  if (path === "/forgot-password")
    return <ForgotPasswordPage navigate={navigate} client={client} />;
  if (path === "/reset-password")
    return (
      <ResetPasswordPage
        navigate={navigate}
        client={client}
        token={locationToken(initialPath)}
      />
    );
  if (path === "/verify-email")
    return (
      <VerificationPendingPage
        navigate={navigate}
        client={client}
        email={signupEmail}
        token={locationToken(initialPath)}
        deliveryAccepted={verificationDeliveryAccepted}
      />
    );
  if (!KNOWN_ROUTES.has(path)) return <LandingPage navigate={navigate} />;

  if (path === "/onboarding") {
    const companies = resource.data?.companies || onboardingResource.companies;
    const status = resource.data?.companies
      ? "ready"
      : onboardingResource.status;
    if (status !== "ready")
      return (
        <div className="standalone-state">
          <AsyncPanel
            status={status}
            error={onboardingResource.error}
            onRetry={loadOnboardingCompanies}
            loadingLabel="Preparing your watchlist setup"
          />
        </div>
      );
    return (
      <Onboarding
        navigate={navigate}
        companies={companies}
        savePreferences={savePreferences}
        saveWatchlist={saveWatchlist}
      />
    );
  }

  // Public, onboarding, and unknown routes returned above, so only app routes
  // reach the authenticated shell.
  const appPath = path;
  return (
    <AppShell
      path={appPath}
      navigate={navigate}
      email={data?.me.email}
      matchCount={newMatchCount}
      onLogout={logout}
      sessionError={sessionError}
    >
      {resource.status !== "ready" ? (
        <AsyncPanel
          status={resource.status}
          error={resource.error}
          onRetry={loadWorkspace}
        />
      ) : (
        <>
          {appPath === "/app/dashboard" && (
            <DashboardPage navigate={navigate} data={data} />
          )}
          {appPath === "/app/matches" && <MatchesPage matches={data.matches} />}
          {appPath === "/app/watchlist" && (
            <WatchlistPage
              companies={data.companies}
              watchlist={data.watchlist}
              saveWatchlist={saveWatchlist}
              requestCompany={client.requestCompany}
            />
          )}
          {appPath === "/app/settings" && (
            <SettingsPage
              me={data.me}
              preferences={data.preferences}
              savePreferences={savePreferences}
            />
          )}
        </>
      )}
    </AppShell>
  );
}
