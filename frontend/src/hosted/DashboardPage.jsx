import { useMemo, useState } from "react";
import { ROLE_OPTIONS } from "./constants.js";
import EmailPreview from "./EmailPreview.jsx";
import MatchCard from "./MatchCard.jsx";
import MatchDrawer from "./MatchDrawer.jsx";
import { alertFrequencyLabel, formatScanTime } from "./ui.jsx";

export default function DashboardPage({ navigate, data }) {
  const [selectedMatch, setSelectedMatch] = useState(null);
  const [savedIds, setSavedIds] = useState([]);
  const matches = useMemo(() => [...data.matches].sort((a, b) => new Date(b.detected_at) - new Date(a.detected_at)), [data.matches]);
  const selectedRoles = ROLE_OPTIONS.filter((role) => data.preferences.role_ids.includes(role.id));
  const alertsPaused = data.preferences.globally_paused || data.preferences.alert_frequency === "paused";
  const toggleSaved = (id) => setSavedIds((current) => current.includes(id) ? current.filter((savedId) => savedId !== id) : [...current, id]);

  return (
    <>
      <div className="page-heading dashboard-heading"><div><p className="eyebrow">Your internship signal</p><h1>Good morning.</h1><p>Your watchlist is active. New matches are ordered by detection time so the freshest openings stay first.</p></div><button className="primary large" onClick={() => navigate("/app/watchlist")}>Edit watchlist</button></div>
      <section className="dashboard-stats" aria-label="Watchlist summary">
        <article><span className="stat-icon" aria-hidden="true">◎</span><div><strong>{data.watchlist.length}</strong><span>Companies watched</span></div><button onClick={() => navigate("/app/watchlist")}>Manage</button></article>
        <article><span className="stat-icon" aria-hidden="true">⌁</span><div><strong>{selectedRoles.length}</strong><span>Role categories</span></div><button onClick={() => navigate("/app/settings")}>Edit</button></article>
        <article><span className={`stat-icon ${alertsPaused ? "paused" : "active"}`} aria-hidden="true">◉</span><div><strong>{alertsPaused ? "Paused" : "Active"}</strong><span>{alertFrequencyLabel(data.preferences.alert_frequency)}</span></div><button onClick={() => navigate("/app/settings")}>Settings</button></article>
        <article><span className="stat-icon" aria-hidden="true">↻</span><div><strong>{formatScanTime(data.me.last_successful_scan_at)}</strong><span>Last successful scan</span></div><small>Scheduled scans</small></article>
      </section>

      <section className="dashboard-grid">
        <div className="dashboard-primary">
          <div className="section-title-row"><div><p className="eyebrow">Newest first</p><h2>Recent matching internships</h2></div><button className="text-button" onClick={() => navigate("/app/matches")}>View all matches →</button></div>
          {matches.length ? matches.slice(0, 3).map((match) => <MatchCard key={match.id} match={match} onOpen={setSelectedMatch} saved={savedIds.includes(match.id)} onToggleSaved={toggleSaved} compact />) : <div className="empty-card"><span aria-hidden="true">◎</span><h3>No matches yet</h3><p>We’ll show new internships here after a scheduled scan finds one that matches your watchlist.</p></div>}
        </div>
        <aside className="dashboard-sidebar">
          <section className="side-card"><div className="side-card-head"><p className="eyebrow">Your role signal</p><button onClick={() => navigate("/app/settings")}>Edit</button></div><h2>Selected roles</h2><div className="role-chip-list">{selectedRoles.map((role) => <span key={role.id}>{role.name}</span>)}</div></section>
          <section className="side-card"><p className="eyebrow">Alert preview</p><h2>What lands in your inbox</h2><p className="side-description">Application links stay prominent so you can move quickly.</p><EmailPreview match={matches[0]} /></section>
        </aside>
      </section>
      <MatchDrawer match={selectedMatch} onClose={() => setSelectedMatch(null)} />
    </>
  );
}
