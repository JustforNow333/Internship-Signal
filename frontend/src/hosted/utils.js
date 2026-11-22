import {
  CAREER_LEVEL_OPTIONS,
  CLASS_STANDING_OPTIONS,
  DEFAULT_CAREER_LEVELS,
  EDUCATION_LEVEL_OPTIONS,
  EDUCATION_NOT_SPECIFIED,
} from "./constants.js";

const CAREER_LEVEL_IDS = new Set(CAREER_LEVEL_OPTIONS.map((option) => option.id));
const EDUCATION_LEVEL_IDS = new Set(EDUCATION_LEVEL_OPTIONS.map((option) => option.id));
const CLASS_STANDING_IDS = new Set(CLASS_STANDING_OPTIONS.map((option) => option.id));

// Preferences saved before education existed (or carrying unknown ids) render
// as Not specified, which applies no education exclusions. Class standing is
// meaningful only for undergraduates, matching the backend's validation.
export function educationOrDefault(preferences) {
  const level = EDUCATION_LEVEL_IDS.has(preferences?.education_level)
    ? preferences.education_level
    : EDUCATION_NOT_SPECIFIED;
  const standing =
    level === "undergraduate" && CLASS_STANDING_IDS.has(preferences?.class_standing)
      ? preferences.class_standing
      : EDUCATION_NOT_SPECIFIED;
  return { education_level: level, class_standing: standing };
}

// Changing the level away from undergraduate always clears class standing.
export function withEducationLevel(preferences, level) {
  return {
    ...preferences,
    education_level: level,
    class_standing:
      level === "undergraduate"
        ? preferences.class_standing || EDUCATION_NOT_SPECIFIED
        : EDUCATION_NOT_SPECIFIED,
  };
}

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

const CALENDAR_DATE = /^(\d{4})-(\d{2})-(\d{2})$/;
const DAY_MS = 86_400_000;
// The calendar date is pinned to UTC midnight and formatted in UTC, so the
// displayed day never depends on the viewer's time zone.
const DEADLINE_FORMAT = new Intl.DateTimeFormat("en-US", {
  month: "short",
  day: "numeric",
  year: "numeric",
  timeZone: "UTC",
});

function utcDay({ year, month, day }) {
  return Date.UTC(year, month - 1, day);
}

// Deadlines are date-only values ("YYYY-MM-DD"), so they are kept as plain
// calendar parts. `new Date("YYYY-MM-DD")` is UTC midnight, which is the
// previous day west of UTC. Rejects rollovers such as Feb 30.
export function parseCalendarDate(value) {
  const parts = typeof value === "string" ? CALENDAR_DATE.exec(value) : null;
  if (!parts) return null;
  const [year, month, day] = parts.slice(1).map(Number);
  const check = new Date(utcDay({ year, month, day }));
  return check.getUTCFullYear() === year &&
    check.getUTCMonth() === month - 1 &&
    check.getUTCDate() === day
    ? { year, month, day }
    : null;
}

// Today's calendar date for an instant in a time zone (default: the
// viewer's own zone).
export function calendarDateIn(now, timeZone) {
  const parts = new Intl.DateTimeFormat("en-US", {
    timeZone,
    year: "numeric",
    month: "numeric",
    day: "numeric",
  }).formatToParts(now);
  const part = (type) => Number(parts.find((item) => item.type === type).value);
  return { year: part("year"), month: part("month"), day: part("day") };
}

/**
 * Display data for a match deadline, or null when none is usable. Days are
 * counted between calendar dates, so DST's 23- and 25-hour days don't matter.
 */
export function deadlineInfo(value, { now = new Date(), timeZone } = {}) {
  const date = parseCalendarDate(value);
  if (!date) return null;
  const days = Math.round(
    (utcDay(date) - utcDay(calendarDateIn(now, timeZone))) / DAY_MS,
  );
  const base = {
    date: DEADLINE_FORMAT.format(new Date(utcDay(date))),
    dateTime: value,
  };
  if (days < 0) return { ...base, id: "passed", status: "Deadline passed" };
  if (days === 0) return { ...base, id: "today", status: "Closes today" };
  return {
    ...base,
    id: "upcoming",
    status: `Closes in ${days} day${days === 1 ? "" : "s"}`,
  };
}

/**
 * The calendar date (in the viewer's time zone, or `timeZone`) of an
 * applied_at timestamp, or null when it is missing or unparseable.
 */
export function appliedDateInfo(value, { timeZone } = {}) {
  if (typeof value !== "string" || !value) return null;
  const instant = new Date(value);
  if (!Number.isFinite(instant.getTime())) return null;
  const date = new Intl.DateTimeFormat("en-US", {
    month: "short",
    day: "numeric",
    year: "numeric",
    timeZone,
  }).format(instant);
  return { date, dateTime: value };
}

export function newestFirst(items) {
  return [...items].sort(
    (left, right) => new Date(right.detected_at) - new Date(left.detected_at),
  );
}
