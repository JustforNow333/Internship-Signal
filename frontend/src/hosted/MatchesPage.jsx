import { useEffect, useMemo, useRef, useState } from "react";
import EmptyState from "../components/EmptyState.jsx";
import MatchCard from "./MatchCard.jsx";
import MatchDrawer from "./MatchDrawer.jsx";
import { ROLE_OPTIONS } from "./constants.js";
import { AsyncPanel, freshnessFor } from "./ui.jsx";
import { newestFirst } from "./utils.js";

const DEFAULT_FILTERS = {
  search: "",
  company: "all",
  role: "all",
  location: "all",
  freshness: "all",
};

// Active is the workspace's own match list; Saved and Dismissed are loaded
// from their API views when selected.
const VIEWS = [
  {
    id: "active",
    label: "Active",
    emptyTitle: "No matching jobs yet",
    emptyHint: "We’ll add openings here when a scheduled scan finds a match.",
  },
  {
    id: "saved",
    label: "Saved",
    emptyTitle: "No saved matches yet",
    emptyHint:
      "Saved matches stay here, even after a posting closes or stops matching.",
  },
  {
    id: "dismissed",
    label: "Dismissed",
    emptyTitle: "No dismissed matches",
    emptyHint: "Matches you dismiss are kept here.",
  },
];

// Whether a match still belongs in a loaded view after a change.
function belongsInView(view, match) {
  if (view === "saved") return Boolean(match.saved);
  if (view === "dismissed") return Boolean(match.dismissed);
  return true;
}

// Apply successful changes to a view's items: merge each changed record, add
// one that now belongs (e.g. dismissed elsewhere), and drop any that left.
function reconcileView(view, items, records) {
  const byId = new Map(items.map((match) => [match.id, match]));
  for (const record of records) {
    byId.set(record.id, { ...byId.get(record.id), ...record });
  }
  return [...byId.values()].filter((match) => belongsInView(view, match));
}

// Conditions a saved or dismissed match may have reached since it matched.
function eligibilityLabels(match, view) {
  const labels = [];
  if (match.is_open === false) labels.push("Closed");
  if (match.no_longer_matches_at) labels.push("No longer matches");
  if (view === "saved" && match.dismissed) labels.push("Dismissed");
  return labels;
}

const IDLE_VIEW = { view: "active", status: "ready", items: [], error: "" };

export default function MatchesPage({ matches, updateMatch, loadMatchView }) {
  const [view, setView] = useState("active");
  const [viewResource, setViewResource] = useState(IDLE_VIEW);
  // Only the latest view request may write viewResource.
  const viewRequest = useRef(0);
  const [filters, setFilters] = useState(DEFAULT_FILTERS);
  // The drawer shows the current record for this id, never a stale copy.
  const [selectedId, setSelectedId] = useState(null);
  const [actionError, setActionError] = useState("");
  // The latest dismissal or unsave (with Undo) or restore, shown until the
  // view changes.
  const [notice, setNotice] = useState(null);
  const noticeRef = useRef(notice);
  noticeRef.current = notice;
  // The view on screen, for actions that finish after the user switched views.
  const viewRef = useRef(view);
  // Where keyboard focus goes once an action removed the focused card: see
  // the focus effect below.
  const focusRequest = useRef(null);
  const listRef = useRef(null);
  const undoRef = useRef(null);
  const viewButtons = useRef(new Map());
  // Matches with a change in flight; their actions are disabled until it ends.
  const pendingIds = useRef(new Set());
  const [pending, setPending] = useState(() => new Set());
  // Each successful change on this page, by match id, stamped with a counter.
  // A view load reapplies changes that completed after it started, so a GET
  // read before the change landed cannot undo it.
  const changeLog = useRef(new Map());
  const changeClock = useRef(0);
  const viewConfig = VIEWS.find((item) => item.id === view);

  useEffect(
    () => () => {
      viewRequest.current += 1;
    },
    [],
  );

  const loadView = async (nextView) => {
    const request = ++viewRequest.current;
    const since = changeClock.current;
    setViewResource({ view: nextView, status: "loading", items: [], error: "" });
    try {
      const loaded = await loadMatchView(nextView);
      if (viewRequest.current !== request) return;
      const later = [...changeLog.current.values()]
        .filter((entry) => entry.clock > since)
        .map((entry) => entry.record);
      const items = reconcileView(nextView, loaded, later);
      setViewResource({ view: nextView, status: "ready", items, error: "" });
    } catch (error) {
      if (viewRequest.current !== request) return;
      setViewResource({
        view: nextView,
        status: "error",
        items: [],
        error: error.message || "These matches could not be loaded.",
      });
    }
  };

  const selectView = (nextView) => {
    if (nextView === view) return;
    viewRef.current = nextView;
    focusRequest.current = null;
    setView(nextView);
    setSelectedId(null);
    setActionError("");
    setNotice(null);
    if (nextView === "active") {
      viewRequest.current += 1;
      setViewResource(IDLE_VIEW);
    } else {
      loadView(nextView);
    }
  };

  const viewStatus = view === "active" ? "ready" : viewResource.status;
  // Active keeps today's behaviour: the workspace list minus dismissals, so
  // the empty state can tell "nothing matched yet" apart from "filters hid
  // everything". Other views show exactly what their API view returned.
  const available = useMemo(
    () =>
      view === "active"
        ? matches.filter((match) => !match.dismissed)
        : viewResource.items,
    [matches, view, viewResource.items],
  );
  const selectedMatch =
    (selectedId && available.find((match) => match.id === selectedId)) ||
    null;
  // A match that left the view (e.g. unsaved from Saved) closes its drawer.
  useEffect(() => {
    if (selectedId && !selectedMatch) setSelectedId(null);
  }, [selectedId, selectedMatch]);
  // Options come from the selected view, keeping a current selection so the
  // select never shows a value it has no option for.
  const optionsFor = (key) =>
    [
      ...new Set([
        ...(view === "active" ? matches : available).map((match) => match[key]),
        ...(filters[key] === "all" ? [] : [filters[key]]),
      ]),
    ].sort();
  const companies = optionsFor("company");
  const locations = optionsFor("location");
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
  const setMatchPending = (id, isPending) => {
    if (isPending) pendingIds.current.add(id);
    else pendingIds.current.delete(id);
    setPending(new Set(pendingIds.current));
  };
  // Resolves the updated match, or null when the change failed or was
  // ignored because another change to the same match is still in flight.
  const applyChange = async (id, changes) => {
    if (pendingIds.current.has(id)) return null;
    // The button is disabled while the change is in flight, which drops its
    // keyboard focus; it gets focus back when the change ends. A caller whose
    // card leaves the list replaces this with a move (runCardAction).
    const source = document.activeElement;
    setMatchPending(id, true);
    setActionError("");
    try {
      // updateMatch keeps the workspace (Active, Dashboard) list in step; a
      // loaded Saved or Dismissed list is updated here, only on success.
      const updated = await updateMatch(id, changes);
      if (!updated) return null;
      changeClock.current += 1;
      changeLog.current.set(id, { clock: changeClock.current, record: updated });
      setViewResource((current) =>
        current.status === "ready" && current.view !== "active"
          ? {
              ...current,
              items: reconcileView(current.view, current.items, [updated]),
            }
          : current,
      );
      return updated;
    } catch (error) {
      setActionError(error.message || "That change could not be saved.");
      return null;
    } finally {
      focusRequest.current = { kind: "return", source };
      setMatchPending(id, false);
    }
  };
  // The cards after and before a match, for focus once it leaves the list.
  const neighbours = (id) => {
    const index = visible.findIndex((match) => match.id === id);
    return [visible[index + 1]?.id, visible[index - 1]?.id].filter(Boolean);
  };
  // Run a change from a card and, if it succeeds while the same view is
  // shown, set its notice and where focus goes when the card disappears.
  const runCardAction = async (id, changes, { notice: makeNotice, focus }) => {
    const startView = viewRef.current;
    const source = document.activeElement;
    const updated = await applyChange(id, changes);
    if (!updated || viewRef.current !== startView) return updated;
    if (makeNotice) setNotice(makeNotice(updated));
    focusRequest.current = { kind: "move", source, ...focus };
    return updated;
  };
  const toggleSaved = (id) => {
    const match = available.find((item) => item.id === id);
    const saved = Boolean(match?.saved);
    // Unsaving from Saved removes the card (once the server confirms), and a
    // closed or no-longer-matching save may be in no other view: offer Undo.
    if (view === "saved" && saved)
      return runCardAction(
        id,
        { saved: false },
        {
          notice: (updated) => ({ kind: "unsaved", id, title: updated.title }),
          focus: { undo: true, cards: neighbours(id) },
        },
      );
    return applyChange(id, { saved: !saved });
  };
  const dismissMatch = (id) => {
    setSelectedId(null);
    return runCardAction(
      id,
      { dismissed: true },
      {
        notice: (updated) => ({ kind: "dismissed", id, title: updated.title }),
        focus: { undo: true, cards: neighbours(id) },
      },
    );
  };
  const restoreMatch = (id) => {
    setSelectedId(null);
    return runCardAction(
      id,
      { dismissed: false },
      {
        notice: (updated) => ({
          kind: "restored",
          id,
          title: updated.title,
          stillMatches: !updated.no_longer_matches_at,
        }),
        focus: { cards: neighbours(id) },
      },
    );
  };
  // Undo reverses the change named in the notice; that notice closes only
  // if it is still the current one, and focus returns to the match's card.
  const undoNotice = async () => {
    const current = notice;
    const changes =
      current.kind === "unsaved" ? { saved: true } : { dismissed: false };
    const startView = viewRef.current;
    const source = document.activeElement;
    const updated = await applyChange(current.id, changes);
    if (!updated || viewRef.current !== startView) return;
    // A newer action replaced this notice meanwhile: leave it and focus.
    if (noticeRef.current !== current) return;
    setNotice(null);
    focusRequest.current = { kind: "move", source, cards: [current.id] };
  };

  // Keyboard focus after a change. "return": back to the action's button
  // once it is enabled again. "move": the button's card (or the Undo button)
  // left the page, so focus goes to the new Undo, else a neighbouring card,
  // else the view's button. Either way, nothing happens if the user has
  // already moved focus somewhere else.
  useEffect(() => {
    const request = focusRequest.current;
    if (!request) return;
    const { source } = request;
    const active = document.activeElement;
    const lost = !active || active === document.body;
    const sourceShown = source?.isConnected && source !== document.body;
    if (!lost && active !== source) {
      focusRequest.current = null;
      return;
    }
    if (request.kind === "return") {
      if (sourceShown && source.disabled) return;
      focusRequest.current = null;
      if (sourceShown && lost) source.focus();
      return;
    }
    if (sourceShown) return;
    const cardButton = (id) =>
      [...(listRef.current?.querySelectorAll("article[data-match-id]") || [])]
        .find((card) => card.dataset.matchId === id)
        ?.querySelector(".match-title-button");
    const target =
      (request.undo && undoRef.current) ||
      request.cards.map(cardButton).find(Boolean) ||
      viewButtons.current.get(viewRef.current);
    focusRequest.current = null;
    target?.focus();
  });

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
      <div className="match-views" role="group" aria-label="Match view">
        {VIEWS.map((item) => (
          <button
            key={item.id}
            ref={(element) =>
              element
                ? viewButtons.current.set(item.id, element)
                : viewButtons.current.delete(item.id)
            }
            className={item.id === view ? "active" : ""}
            aria-pressed={item.id === view}
            onClick={() => selectView(item.id)}
          >
            {item.label}
          </button>
        ))}
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
      {viewStatus === "ready" && (
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
      )}
      {actionError && (
        <div className="error-banner match-action-error" role="alert">
          {actionError}
        </div>
      )}
      {notice && (
        <div className="success-notice match-notice" role="status">
          <span>
            {notice.kind === "dismissed"
              ? `Dismissed “${notice.title}”.`
              : notice.kind === "unsaved"
                ? `Removed “${notice.title}” from Saved.`
                : notice.stillMatches
                  ? `Restored “${notice.title}” to Active.`
                  : `Restored “${notice.title}”. It no longer matches your preferences, so it isn’t shown in Active.`}
          </span>
          {notice.kind !== "restored" && (
            <button
              ref={undoRef}
              className="text-button"
              disabled={pending.has(notice.id)}
              onClick={undoNotice}
            >
              Undo
            </button>
          )}
        </div>
      )}
      <section
        className="matches-list"
        aria-label="Job matches"
        ref={listRef}
      >
        {viewStatus !== "ready" ? (
          <AsyncPanel
            status={viewStatus}
            error={viewResource.error}
            onRetry={() => loadView(view)}
            loadingLabel={`Loading ${viewConfig.label.toLowerCase()} matches`}
          />
        ) : visible.length ? (
          visible.map((match) => (
            <MatchCard
              key={match.id}
              match={match}
              onOpen={(item) => setSelectedId(item.id)}
              saved={Boolean(match.saved)}
              onToggleSaved={toggleSaved}
              onDismiss={view === "active" ? dismissMatch : undefined}
              onRestore={view === "dismissed" ? restoreMatch : undefined}
              busy={pending.has(match.id)}
              labels={view === "active" ? [] : eligibilityLabels(match, view)}
            />
          ))
        ) : (
          <div className="empty-card large-empty">
            <EmptyState
              glyph="⌕"
              title={
                available.length
                  ? "No matches fit these filters"
                  : viewConfig.emptyTitle
              }
              hint={
                available.length
                  ? "Try clearing a filter or searching for a broader term."
                  : viewConfig.emptyHint
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
        onClose={() => setSelectedId(null)}
      />
    </>
  );
}
