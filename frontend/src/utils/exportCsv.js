// Export the currently filtered jobs back out as a clean CSV.

import { fmtPay } from "./format.js";

const esc = (v) => {
  const s = String(v ?? "");
  return /[",\n]/.test(s) ? `"${s.replace(/"/g, '""')}"` : s;
};

function jobsToCsv(jobs) {
  const head = [
    "company", "title", "location", "remote_status", "score", "bucket",
    "action", "pay", "usd_hourly_mid", "deadline", "days_left", "role",
    "company_type", "red_flags", "positive_signals", "source_url",
  ];
  const rows = jobs.map((j) => [
    j.company, j.title, j.location, j.remote_status,
    j.score.total, j.score.bucket, j.score.action_label,
    fmtPay(j.compensation),
    j.compensation.usd_hourly_min == null
      ? ""
      : ((j.compensation.usd_hourly_min + j.compensation.usd_hourly_max) / 2).toFixed(2),
    j.deadline, j.deadline_days_left ?? "",
    j.role_classification.label, j.company_classification.category,
    j.red_flags.map((f) => f.label).join("; "),
    j.positive_signals.map((s) => s.label).join("; "),
    j.source_url,
  ]);
  return [head, ...rows].map((r) => r.map(esc).join(",")).join("\n");
}

export function downloadCsv(jobs, filename = "internship-signal-export.csv") {
  const blob = new Blob([jobsToCsv(jobs)], { type: "text/csv;charset=utf-8" });
  const url = URL.createObjectURL(blob);
  const a = document.createElement("a");
  a.href = url;
  a.download = filename;
  a.click();
  URL.revokeObjectURL(url);
}
