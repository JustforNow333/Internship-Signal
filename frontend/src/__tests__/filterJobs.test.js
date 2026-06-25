import { describe, expect, it } from "vitest";
import { DEFAULT_FILTERS, filterJobs, sortJobs } from "../utils/filterJobs.js";

let n = 0;
const job = (over = {}) => ({
  id: `j${++n}`,
  company: "Acme",
  title: "Backend Intern",
  location: "NYC",
  remote_status: "",
  deadline_days_left: 10,
  score: { total: 80, bucket: "high", action: "apply_now", action_label: "Apply now" },
  role_classification: { role: "swe", label: "Software engineering", confidence: 0.8 },
  compensation: { kind: "paid", usd_hourly_min: 30, usd_hourly_max: 30 },
  red_flags: [],
  positive_signals: [],
  ...over,
});

const f = (over = {}) => ({ ...DEFAULT_FILTERS, ...over });

describe("filterJobs", () => {
  it("filters by bucket, role and min score", () => {
    const jobs = [
      job({ score: { ...job().score, bucket: "high", total: 85 } }),
      job({ score: { ...job().score, bucket: "low", total: 30 } }),
      job({ role_classification: { role: "ml_ai", label: "ML / AI", confidence: 0.9 } }),
    ];
    expect(filterJobs(jobs, f({ bucket: "high" }))).toHaveLength(2);
    expect(filterJobs(jobs, f({ role: "ml_ai" }))).toHaveLength(1);
    expect(filterJobs(jobs, f({ minScore: 60 }))).toHaveLength(2);
  });

  it("paid-only excludes unpaid and equity-only", () => {
    const jobs = [
      job(),
      job({ compensation: { kind: "unpaid", usd_hourly_min: 0, usd_hourly_max: 0 } }),
      job({ compensation: { kind: "equity_only", usd_hourly_min: null, usd_hourly_max: null } }),
    ];
    expect(filterJobs(jobs, f({ paidOnly: true }))).toHaveLength(1);
  });

  it("remote-only matches the remote_status field", () => {
    const jobs = [job({ remote_status: "Remote" }), job({ remote_status: "On-site" })];
    expect(filterJobs(jobs, f({ remoteOnly: true }))).toHaveLength(1);
  });

  it("flag filter separates clean from flagged", () => {
    const clean = job();
    const flagged = job({ red_flags: [{ id: "unpaid", severity: "major", label: "Unpaid" }] });
    const minor = job({ red_flags: [{ id: "vague_comp", severity: "minor", label: "Vague" }] });
    expect(filterJobs([clean, flagged, minor], f({ flagged: "flagged" }))).toHaveLength(2);
    // "clean" means no critical/major flags; minor flags are tolerated.
    expect(filterJobs([clean, flagged, minor], f({ flagged: "clean" }))).toHaveLength(2);
  });

  it("shortlist-only respects the saved set", () => {
    const a = job(), b = job();
    expect(filterJobs([a, b], f({ shortlistOnly: true }), new Set([b.id]))).toEqual([b]);
  });

  it("search matches company, title and location", () => {
    const jobs = [job({ company: "Stripe" }), job({ title: "Quant Intern" }), job({ location: "Ithaca, NY" })];
    expect(filterJobs(jobs, f({ q: "stripe" }))).toHaveLength(1);
    expect(filterJobs(jobs, f({ q: "quant" }))).toHaveLength(1);
    expect(filterJobs(jobs, f({ q: "ithaca" }))).toHaveLength(1);
    expect(filterJobs(jobs, f({ q: "zzz" }))).toHaveLength(0);
  });
});

describe("sortJobs", () => {
  it("sorts by score desc by default and does not mutate", () => {
    const jobs = [job({ score: { ...job().score, total: 50 } }), job({ score: { ...job().score, total: 90 } })];
    const sorted = sortJobs(jobs);
    expect(sorted.map((j) => j.score.total)).toEqual([90, 50]);
    expect(jobs.map((j) => j.score.total)).toEqual([50, 90]);
  });

  it("puts rolling deadlines last when sorting by urgency", () => {
    const soon = job({ deadline_days_left: 2 });
    const rolling = job({ deadline_days_left: null });
    const later = job({ deadline_days_left: 12 });
    expect(sortJobs([rolling, later, soon], "deadline", "asc")[0]).toBe(soon);
    expect(sortJobs([rolling, later, soon], "deadline", "asc")[2]).toBe(rolling);
  });

  it("treats unparseable pay as lowest", () => {
    const known = job();
    const unknown = job({ compensation: { kind: "unknown", usd_hourly_min: null, usd_hourly_max: null } });
    expect(sortJobs([unknown, known], "pay", "desc")[0]).toBe(known);
  });
});
