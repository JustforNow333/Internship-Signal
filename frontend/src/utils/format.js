// Pure formatting helpers (unit-tested in __tests__/format.test.js).

export function fmtPay(comp) {
  if (!comp) return "—";
  const k = comp.kind;
  if (k === "unpaid") return "Unpaid";
  if (k === "equity_only") return "Equity only";
  if (k === "commission_only") return "Commission only";
  if (k === "stipend_unspecified") return "Stipend (unspecified)";
  if (k === "unknown_vague") return comp.raw || "Vague";
  if (k === "unknown") return "—";
  const lo = comp.usd_hourly_min, hi = comp.usd_hourly_max;
  if (lo == null) return comp.raw || "—";
  const f = (n) => (Math.round(n * 100) / 100).toFixed(n < 10 ? 2 : 0);
  const range = lo === hi ? `$${f(lo)}` : `$${f(lo)}–$${f(hi)}`;
  return `${range}/hr${comp.period_assumed ? " (est.)" : ""}`;
}

export function hourlyMid(comp) {
  if (!comp || comp.usd_hourly_min == null) return null;
  return (comp.usd_hourly_min + comp.usd_hourly_max) / 2;
}

export function fmtDeadline(job) {
  const d = job.deadline_days_left;
  if (d == null) return job.deadline ? job.deadline : "rolling";
  if (d < 0) return `expired ${-d}d ago`;
  if (d === 0) return "due today";
  if (d === 1) return "1 day left";
  return `${d} days left`;
}

export function deadlineTone(job) {
  const d = job.deadline_days_left;
  if (d == null) return "neutral";
  if (d < 0) return "low";
  if (d <= 3) return "mid";
  return "neutral";
}

export function pct(part, whole) {
  if (!whole) return 0;
  return Math.round((100 * part) / whole);
}

export function confDots(confidence) {
  // 0..1 -> 0..4 dots
  return Math.max(0, Math.min(4, Math.round((confidence || 0) * 4)));
}
