import { freshnessFor, relativeDetection } from "./ui.jsx";

export default function MatchCard({ match, onOpen, saved = false, onToggleSaved, compact = false }) {
  const freshness = freshnessFor(match.detected_at);
  return (
    <article className={`match-card ${compact ? "compact" : ""}`}>
      <div className="match-card-main">
        <div className="match-topline"><span className={`freshness freshness-${freshness.id}`}>{freshness.label}</span><time dateTime={match.detected_at}>{relativeDetection(match.detected_at)}</time></div>
        <button className="match-title-button" onClick={() => onOpen(match)}><h3>{match.title}</h3></button>
        <p className="match-company"><strong>{match.company}</strong><span>·</span>{match.location}{match.remote && <span className="remote-tag">Remote eligible</span>}</p>
        <div className="match-reason"><span aria-hidden="true">✓</span><p>{match.why[0]}</p></div>
      </div>
      <div className="match-actions">
        {onToggleSaved && <button className={`save-button ${saved ? "saved" : ""}`} aria-pressed={saved} onClick={() => onToggleSaved(match.id)}>{saved ? "★ Saved" : "☆ Save"}</button>}
        <button onClick={() => onOpen(match)}>View details</button>
        <a className="button-link primary-link" href={match.source_url} target="_blank" rel="noreferrer">Apply now ↗</a>
      </div>
    </article>
  );
}
