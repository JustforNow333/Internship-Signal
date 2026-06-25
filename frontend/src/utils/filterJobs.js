// Pure client-side filtering + sorting over the jobs array.
// Kept free of React so it can be unit-tested directly.

import { hourlyMid } from "./format.js";

export const DEFAULT_FILTERS = {
  q: "",
  bucket: "all",        // all | high | maybe | low
  role: "all",
  paidOnly: false,
  remoteOnly: false,
  minScore: 0,
  flagged: "all",       // all | clean | flagged
  shortlistOnly: false,
};

export function filterJobs(jobs, f, shortlist = new Set()) {
  const q = (f.q || "").trim().toLowerCase();
  return jobs.filter((j) => {
    if (f.bucket !== "all" && j.score.bucket !== f.bucket) return false;
    if (f.role !== "all" && j.role_classification.role !== f.role) return false;
    if (f.paidOnly && !["paid", "stipend_unspecified"].includes(j.compensation.kind)) return false;
    if (f.remoteOnly && (j.remote_status || "").toLowerCase() !== "remote") return false;
    if (f.minScore && j.score.total < f.minScore) return false;
    const hasMajor = j.red_flags.some((x) => x.severity !== "minor");
    if (f.flagged === "flagged" && j.red_flags.length === 0) return false;
    if (f.flagged === "clean" && hasMajor) return false;
    if (f.shortlistOnly && !shortlist.has(j.id)) return false;
    if (q) {
      const blob = `${j.company} ${j.title} ${j.location}`.toLowerCase();
      if (!blob.includes(q)) return false;
    }
    return true;
  });
}

const SORTERS = {
  score: (j) => j.score.total,
  company: (j) => (j.company || "").toLowerCase(),
  pay: (j) => hourlyMid(j.compensation) ?? -1,
  deadline: (j) => (j.deadline_days_left == null ? 9999 : j.deadline_days_left),
  role: (j) => j.role_classification.label,
};

export function sortJobs(jobs, key = "score", dir = "desc") {
  const get = SORTERS[key] || SORTERS.score;
  const mul = dir === "asc" ? 1 : -1;
  return [...jobs].sort((a, b) => {
    const va = get(a), vb = get(b);
    if (va < vb) return -1 * mul;
    if (va > vb) return 1 * mul;
    return 0;
  });
}

export function roleOptions(jobs) {
  const seen = new Map();
  jobs.forEach((j) => seen.set(j.role_classification.role, j.role_classification.label));
  return [...seen.entries()].map(([value, label]) => ({ value, label }));
}
