import { Brand, RouteLink } from "./ui.jsx";

const NAV_ITEMS = [
  { path: "/app/dashboard", label: "Dashboard", icon: "⌂" },
  { path: "/app/matches", label: "Matches", icon: "✦" },
  { path: "/app/watchlist", label: "Watchlist", icon: "◎" },
  { path: "/app/settings", label: "Settings", icon: "⚙" },
];

export default function AppShell({
  path,
  navigate,
  email,
  matchCount,
  children,
}) {
  return (
    <div className="app-page">
      <header className="app-header">
        <div className="app-header-inner">
          <Brand navigate={() => navigate("/app/dashboard")} />
          <nav className="app-nav" aria-label="Application">
            {NAV_ITEMS.map((item) => (
              <RouteLink
                key={item.path}
                to={item.path}
                navigate={navigate}
                className={path === item.path ? "active" : ""}
                aria-current={path === item.path ? "page" : undefined}
              >
                <span aria-hidden="true">{item.icon}</span>
                {item.label}
                {item.label === "Matches" && matchCount > 0 && (
                  <b>{matchCount}</b>
                )}
              </RouteLink>
            ))}
          </nav>
          <button
            className="account-button"
            aria-label={
              email ? `Account settings for ${email}` : "Account settings"
            }
            onClick={() => navigate("/app/settings")}
          >
            <span>{email?.slice(0, 1).toUpperCase() || "A"}</span>
            <span className="account-copy">
              <strong>{email}</strong>
              <small>My account</small>
            </span>
          </button>
        </div>
      </header>
      <main className="app-main">{children}</main>
      <nav className="mobile-nav" aria-label="Mobile application navigation">
        {NAV_ITEMS.map((item) => (
          <RouteLink
            key={item.path}
            to={item.path}
            navigate={navigate}
            className={path === item.path ? "active" : ""}
          >
            <span aria-hidden="true">{item.icon}</span>
            <small>{item.label}</small>
          </RouteLink>
        ))}
      </nav>
    </div>
  );
}
