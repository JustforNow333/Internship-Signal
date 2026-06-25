import ScoreBadge from "./ScoreBadge.jsx";

// Postings grouped by recommended action — the "what do I actually do
// tonight" view.
const COLS = [
  { action: "apply_now", title: "Apply now" },
  { action: "apply_later", title: "Apply later" },
  { action: "research_more", title: "Research more" },
  { action: "skip", title: "Skip" },
];

export default function BucketBoard({ jobs, onOpen }) {
  return (
    <div className="board">
      {COLS.map((col) => {
        const items = jobs
          .filter((j) => j.score.action === col.action)
          .sort((a, b) => b.score.total - a.score.total);
        return (
          <div key={col.action}>
            <div className="col-head">
              <span className={`badge action-${col.action}`}>{col.title}</span>
              <span className="n">{items.length}</span>
            </div>
            {items.map((j) => (
              <div key={j.id} className="mini" onClick={() => onOpen(j)}>
                <div className="t">{j.title || "—"}</div>
                <div className="c">{j.company}</div>
                <ScoreBadge score={j.score.total} bucket={j.score.bucket} compact />
                {j.score.action === "apply_now" && j.deadline_days_left != null && (
                  <div className="footnote" style={{ marginTop: 6 }}>
                    {j.deadline_days_left <= 0 ? "due today" : `${j.deadline_days_left} day${j.deadline_days_left === 1 ? "" : "s"} left`}
                  </div>
                )}
                {j.score.action !== "apply_now" && j.score.concerns?.[0] && (
                  <div className="footnote" style={{ marginTop: 6 }}>{j.score.concerns[0]}</div>
                )}
              </div>
            ))}
          </div>
        );
      })}
    </div>
  );
}
