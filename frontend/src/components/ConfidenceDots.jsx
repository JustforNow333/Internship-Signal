import { confDots } from "../utils/format.js";

// Four-dot confidence indicator used wherever the app shows a verdict it
// inferred (company type, role, salary parse).
export default function ConfidenceDots({ value, label = "confidence" }) {
  const on = confDots(value);
  return (
    <span className="conf" title={`${Math.round((value || 0) * 100)}% ${label}`}
          aria-label={`${Math.round((value || 0) * 100)}% ${label}`}>
      {[0, 1, 2, 3].map((i) => <i key={i} className={i < on ? "on" : ""} />)}
    </span>
  );
}
