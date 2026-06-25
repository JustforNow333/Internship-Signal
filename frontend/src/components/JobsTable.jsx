import { useState } from "react";
import { fmtDeadline, fmtPay, deadlineTone } from "../utils/format.js";
import { sortJobs } from "../utils/filterJobs.js";
import ScoreBadge from "./ScoreBadge.jsx";
import SignalBadges from "./SignalBadges.jsx";
import ConfidenceDots from "./ConfidenceDots.jsx";
import EmptyState from "./EmptyState.jsx";

const COLS = [
  { key: "score", label: "Score" },
  { key: "company", label: "Posting" },
  { key: "role", label: "Role" },
  { key: "pay", label: "Pay (USD/hr)" },
  { key: "deadline", label: "Deadline" },
  { key: null, label: "Signals" },
  { key: null, label: "★" },
];

export default function JobsTable({ jobs, onOpen, shortlist }) {
  const [sort, setSort] = useState({ key: "score", dir: "desc" });
  const sorted = sortJobs(jobs, sort.key, sort.dir);

  const clickSort = (key) => {
    if (!key) return;
    setSort((s) =>
      s.key === key ? { key, dir: s.dir === "desc" ? "asc" : "desc" } : { key, dir: "desc" }
    );
  };

  if (!jobs.length) {
    return <EmptyState title="No postings match these filters" hint="Loosen a filter or clear the search box." />;
  }

  return (
    <div className="card table-wrap">
      <table className="jobs">
        <thead>
          <tr>
            {COLS.map((c, i) => (
              <th key={i} className={c.key ? "sortable" : ""} onClick={() => clickSort(c.key)}>
                {c.label}
                {sort.key === c.key && <span className="arrow">{sort.dir === "desc" ? "▼" : "▲"}</span>}
              </th>
            ))}
          </tr>
        </thead>
        <tbody>
          {sorted.map((j) => (
            <tr key={j.id} className="row" onClick={() => onOpen(j)}>
              <td style={{ width: 150 }}>
                <ScoreBadge score={j.score.total} bucket={j.score.bucket} compact />
              </td>
              <td>
                <div className="title">{j.title || "—"}</div>
                <div className="company">
                  {j.company || "Unknown company"}
                  {j.location ? ` · ${j.location}` : ""}
                  {(j.remote_status || "").toLowerCase() === "remote" && " · remote"}
                </div>
              </td>
              <td>
                {j.role_classification.label}
                <ConfidenceDots value={j.role_classification.confidence} label="role confidence" />
              </td>
              <td className="num">{fmtPay(j.compensation)}</td>
              <td className="num">
                <span className={`badge ${deadlineTone(j) === "neutral" ? "neutral" : deadlineTone(j) === "mid" ? "flag-minor" : "flag-major"}`}>
                  {fmtDeadline(j)}
                </span>
              </td>
              <td><SignalBadges job={j} /></td>
              <td onClick={(e) => e.stopPropagation()}>
                <button
                  className={`star ${shortlist.has(j.id) ? "on" : ""}`}
                  title={shortlist.has(j.id) ? "Remove from shortlist" : "Save to shortlist"}
                  aria-label="Toggle shortlist"
                  onClick={() => shortlist.toggle(j.id)}
                >
                  {shortlist.has(j.id) ? "★" : "☆"}
                </button>
              </td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}
