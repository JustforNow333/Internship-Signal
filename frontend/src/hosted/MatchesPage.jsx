import { useMemo, useState } from "react";
import EmptyState from "../components/EmptyState.jsx";
import MatchCard from "./MatchCard.jsx";
import MatchDrawer from "./MatchDrawer.jsx";
import { ROLE_OPTIONS } from "./constants.js";
import { freshnessFor } from "./ui.jsx";
import { newestFirst } from "./utils.js";

const DEFAULT_FILTERS = {
  search: "",
  company: "all",
  role: "all",
  location: "all",
  freshness: "all",
};

export default function MatchesPage({ matches, updateMatch }) {
  const [filters, setFilters] = useState(DEFAULT_FILTERS);
  const [selectedMatch, setSelectedMatch] = useState(null);
  const [actionError, setActionError] = useState("");
  const companies = useMemo(
    () => [...new Set(matches.map((match) => match.company))].sort(),
    [matches],
  );
  const locations = useMemo(
    () => [...new Set(matches.map((match) => match.location))].sort(),
    [matches],
  );
  // Dismissed matches stay out of every count so the empty state can tell
  // "nothing matched yet" apart from "filters hid everything".
  const available = useMemo(
    () => matches.filter((match) => !match.dismissed),
    [matches],
  );
  const visible = useMemo(
    () =>
      newestFirst(available).filter((match) => {
        const haystack =
          `${match.title} ${match.company} ${match.location}`.toLowerCase();
        return (
          (!filters.search ||
            haystack.includes(filters.search.toLowerCase())) &&
          (filters.company === "all" || match.company === filters.company) &&
          (filters.role === "all" || match.role_id === filters.role) &&
          (filters.location === "all" || match.location === filters.location) &&
          (filters.freshness === "all" ||
            freshnessFor(match.detected_at).id === filters.freshness)
        );
      }),
    [available, filters],
  );
  const filtersActive = Object.entries(filters).some(
    ([key, value]) => value !== DEFAULT_FILTERS[key],
  );
  const update = (key, value) =>
    setFilters((current) => ({ ...current, [key]: value }));
  const applyChange = async (id, changes) => {
    setActionError("");
    try {
      await updateMatch(id, changes);
    } catch (error) {
      setActionError(error.message || "That change could not be saved.");
    }
  };
  const toggleSaved = (id) => {
    const match = matches.find((item) => item.id === id);
    return applyChange(id, { saved: !match?.saved });
  };
  const dismissMatch = (id) => {
    setSelectedMatch(null);
    return applyChange(id, { dismissed: true });
  };

  return (
    <>
      <div className="page-heading">
        <div>
          <p className="eyebrow">Your detected openings</p>
          <h1>Matches</h1>
          <p>
            Freshness comes first. Results are sorted by detection time, newest
            first.
          </p>
        </div>
        <span className="sort-note">↓ Newest first</span>
      </div>
      <section className="filter-card" aria-label="Filter matches">
        <label className="search-field match-search">
          <span className="sr-only">Search matches</span>
          <span aria-hidden="true">⌕</span>
          <input
            type="search"
            placeholder="Search title, company, or location"
            value={filters.search}
            onChange={(event) => update("search", event.target.value)}
          />
        </label>
        <div className="filter-selects">
          <label>
            <span>Company</span>
            <select
              aria-label="Company filter"
              value={filters.company}
              onChange={(event) => update("company", event.target.value)}
            >
              <option value="all">All companies</option>
              {companies.map((company) => (
                <option key={company}>{company}</option>
              ))}
            </select>
          </label>
          <label>
            <span>Role</span>
            <select
              aria-label="Role filter"
              value={filters.role}
              onChange={(event) => update("role", event.target.value)}
            >
              <option value="all">All roles</option>
              {ROLE_OPTIONS.map((role) => (
                <option key={role.id} value={role.id}>
                  {role.name}
                </option>
              ))}
            </select>
          </label>
          <label>
            <span>Location</span>
            <select
              aria-label="Location filter"
              value={filters.location}
              onChange={(event) => update("location", event.target.value)}
            >
              <option value="all">All locations</option>
              {locations.map((location) => (
                <option key={location}>{location}</option>
              ))}
            </select>
          </label>
          <label>
            <span>Freshness</span>
            <select
              aria-label="Freshness filter"
              value={filters.freshness}
              onChange={(event) => update("freshness", event.target.value)}
            >
              <option value="all">Any time</option>
              <option value="just">Just detected</option>
              <option value="today">New today</option>
              <option value="older">Older</option>
            </select>
          </label>
        </div>
      </section>
      <div className="results-summary" aria-live="polite">
        <span>
          <strong>{visible.length}</strong>{" "}
          {visible.length === 1 ? "match" : "matches"}
        </span>
        {filtersActive && (
          <button
            className="text-button"
            onClick={() => setFilters(DEFAULT_FILTERS)}
          >
            Clear all filters
          </button>
        )}
      </div>
      {actionError && (
        <div className="error-banner" role="alert">
          {actionError}
        </div>
      )}
      <section className="matches-list" aria-label="Internship matches">
        {visible.length ? (
          visible.map((match) => (
            <MatchCard
              key={match.id}
              match={match}
              onOpen={setSelectedMatch}
              saved={Boolean(match.saved)}
              onToggleSaved={toggleSaved}
              onDismiss={dismissMatch}
            />
          ))
        ) : (
          <div className="empty-card large-empty">
            <EmptyState
              glyph="⌕"
              title={
                available.length
                  ? "No matches fit these filters"
                  : "No matching internships yet"
              }
              hint={
                available.length
                  ? "Try clearing a filter or searching for a broader term."
                  : "We’ll add openings here when a scheduled scan finds a match."
              }
            />
            {filtersActive && (
              <button onClick={() => setFilters(DEFAULT_FILTERS)}>
                Clear filters
              </button>
            )}
          </div>
        )}
      </section>
      <MatchDrawer
        match={selectedMatch}
        onClose={() => setSelectedMatch(null)}
      />
    </>
  );
}
