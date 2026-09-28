import { CAREER_LEVEL_OPTIONS, DEFAULT_CAREER_LEVELS } from "./constants.js";

const CAREER_LEVEL_IDS = new Set(CAREER_LEVEL_OPTIONS.map((option) => option.id));

// Preferences saved before career stages existed have no career_levels; they
// keep the backend's internship-only default.
export function careerLevelsOrDefault(values) {
  const valid = Array.isArray(values)
    ? [...new Set(values)].filter((value) => CAREER_LEVEL_IDS.has(value))
    : [];
  return valid.length ? valid : [...DEFAULT_CAREER_LEVELS];
}

export function toggleSelection(values, value) {
  return values.includes(value)
    ? values.filter((current) => current !== value)
    : [...values, value];
}

export function newestFirst(items) {
  return [...items].sort(
    (left, right) => new Date(right.detected_at) - new Date(left.detected_at),
  );
}
