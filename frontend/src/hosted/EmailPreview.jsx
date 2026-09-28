import { careerLevelLabel } from "./matchModel.js";
import { relativeDetection } from "./ui.jsx";

export default function EmailPreview({ match }) {
  if (!match) return null;
  const stage = careerLevelLabel(match.career_level);
  return (
    <article
      className="inbox-preview"
      aria-label="Preview of your match alert email"
    >
      <div className="inbox-header">
        <span className="email-logo">FS</span>
        <span>
          <strong>FindSooner</strong>
          <small>New watchlist match</small>
        </span>
        <time>now</time>
      </div>
      <div className="inbox-subject">
        <span className="live-dot" />
        New match at {match.company}
      </div>
      <div className="inbox-body">
        <p className="email-kicker">NEW WATCHLIST MATCH</p>
        <h3>{match.title}</h3>
        <p>
          {match.company} · {match.location}
          {match.remote ? " · Remote eligible" : ""}
          {stage ? ` · ${stage}` : ""}
        </p>
        <div className="email-reason">
          {match.why.slice(0, 2).map((reason) => (
            <span key={reason}>✓ {reason}</span>
          ))}
        </div>
        <a
          className="button-link primary-link"
          href={match.source_url}
          target="_blank"
          rel="noreferrer"
        >
          Apply now ↗
        </a>
        <small>
          {relativeDetection(match.detected_at)} during a scheduled scan
        </small>
      </div>
    </article>
  );
}
