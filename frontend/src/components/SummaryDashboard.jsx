import { pct } from "../utils/format.js";
import ScoreBadge from "./ScoreBadge.jsx";

// Top-line read of the dataset: bucket counts, pay coverage, role mix,
// and the five strongest postings.
export default function SummaryDashboard({ summary, jobs, onOpenJob }) {
  if (!summary) return null;
  const { buckets, total } = summary;
  const roleMax = Math.max(1, ...Object.values(summary.role_distribution || {}));

  return (
    <div className="grid" style={{ gap: 16 }}>
      <div className="grid cols-4">
        <div className="card stat">
          <div className="big">{total}</div>
          <div className="lbl">postings analyzed</div>
        </div>
        <div className="card stat">
          <div className="big high">{buckets.high}</div>
          <div className="lbl">high signal (≥70)</div>
        </div>
        <div className="card stat">
          <div className="big maybe">{buckets.maybe}</div>
          <div className="lbl">maybe (45–69)</div>
        </div>
        <div className="card stat">
          <div className="big low">{buckets.low}</div>
          <div className="lbl">low signal (&lt;45)</div>
        </div>
      </div>

      <div className="grid cols-2">
        <div className="card">
          <div className="card-head"><h3>Role mix</h3>
            <span className="sub">{summary.paid_pct}% paid · avg score {summary.average_score}</span>
          </div>
          <div className="card-body">
            {Object.entries(summary.role_distribution || {}).map(([role, n]) => (
              <div className="dist-row" key={role}>
                <span className="name">{role}</span>
                <span className="bar"><i style={{ width: `${pct(n, roleMax)}%` }} /></span>
                <span className="n">{n}</span>
              </div>
            ))}
          </div>
        </div>

        <div className="card">
          <div className="card-head"><h3>Strongest signals</h3><span className="sub">top 5 by score</span></div>
          <div className="card-body" style={{ paddingTop: 6 }}>
            {(summary.top_jobs || []).map((t) => {
              const job = jobs.find((j) => j.id === t.id);
              return (
                <div key={t.id} className="result-line" onClick={() => job && onOpenJob(job)}>
                  <div className="who">
                    <div className="t">{t.title}</div>
                    <div className="why">{t.company}</div>
                  </div>
                  {job && <ScoreBadge score={t.score} bucket={job.score.bucket} compact />}
                </div>
              );
            })}
          </div>
        </div>
      </div>
    </div>
  );
}
