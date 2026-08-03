import { ROLE_OPTIONS } from "./constants.js";

// Presentation only. The backend decides what matched; this file just renders
// the allowlisted reason codes it returns. No matching logic lives here.
const REASON_TEXT = {
  company_watched: (match) => `${match.company} is on your watchlist`,
  role_selected: (match) => `${roleName(match.role_id)} is in your role preferences`,
  location_preferred: (match) =>
    match.location
      ? `${match.location} matches a preferred location`
      : "Matches a preferred location",
  location_united_states: () => "Located in the United States",
  location_any: () => "You have not restricted locations",
  remote_included: () => "Remote eligible, which your preferences include",
  season_match: () => "Matches your internship season",
  season_any: () => "You accept any internship season",
  season_unspecified: () => "The posting does not state a season",
};

export function roleName(roleId) {
  return ROLE_OPTIONS.find((role) => role.id === roleId)?.name || "This role";
}

export function reasonText(reason, match) {
  const render = REASON_TEXT[reason?.code];
  return render ? render(match) : "";
}

/**
 * Map one authenticated `/api/matches` record onto the shape the existing
 * match components already render.
 */
export function normalizeMatch(record) {
  if (!record) return null;
  const base = {
    id: record.id,
    job_id: record.job_id,
    company_id: record.company_id,
    company: record.company,
    title: record.title,
    role_id: record.role_id,
    role: roleName(record.role_id),
    location: record.location || "",
    remote: Boolean(record.remote),
    remote_status: record.remote_status || "",
    is_open: record.is_open !== false,
    posting_date: record.posting_date || null,
    deadline: record.deadline || null,
    detected_at: record.matched_at,
    last_matched_at: record.last_matched_at,
    no_longer_matches_at: record.no_longer_matches_at || null,
    saved_at: record.saved_at || null,
    dismissed_at: record.dismissed_at || null,
    saved: Boolean(record.saved_at),
    dismissed: Boolean(record.dismissed_at),
    source_url: record.application_url || "",
  };
  const why = (record.match_reasons || [])
    .map((reason) => reasonText(reason, base))
    .filter(Boolean);
  return {
    ...base,
    // MatchCard always renders the first reason, so never leave it empty.
    why: why.length ? why : ["Matches your saved watchlist and preferences"],
  };
}

export function normalizeMatchList(page) {
  const items = Array.isArray(page) ? page : page?.items || [];
  return items.map(normalizeMatch).filter(Boolean);
}
