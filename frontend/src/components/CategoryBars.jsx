import { useState } from "react";

const tone = (s) => (s >= 70 ? "high" : s >= 45 ? "maybe" : "low");

// Per-category score bars with weights; click a row to see the scorer's
// one-line explanation (the "why" behind every number).
export default function CategoryBars({ categories }) {
  const [open, setOpen] = useState(null);
  const entries = Object.entries(categories);
  return (
    <div>
      {entries.map(([name, c]) => (
        <div key={name}>
          <div
            className="catbar"
            onClick={() => setOpen(open === name ? null : name)}
            style={{ cursor: "pointer" }}
            title="Click for explanation"
          >
            <span className="label">
              {name.replaceAll("_", " ")}
              <span className="w">×{c.weight.toFixed(2)}</span>
            </span>
            <span className={`sigbar ${tone(c.score)}`}>
              <span className="track"><span className="fill" style={{ width: `${c.score}%` }} /></span>
            </span>
            <span className="num">{c.score}</span>
          </div>
          {open === name && <div className="catexpl">{c.explanation}</div>}
        </div>
      ))}
      <div className="footnote">Click any category for how it was scored. Weights sum to 1.00.</div>
    </div>
  );
}
