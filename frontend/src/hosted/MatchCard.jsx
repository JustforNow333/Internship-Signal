import { careerLevelLabel } from "./matchModel.js";
import { freshnessFor, relativeDetection } from "./ui.jsx";
import { appliedDateInfo, deadlineInfo } from "./utils.js";

export default function MatchCard({
  match,
  onOpen,
  saved = false,
  onToggleSaved,
  onToggleApplied,
  onDismiss,
  onRestore,
  busy = false,
  compact = false,
  labels = [],
}) {
  const freshness = freshnessFor(match.detected_at);
  const stage = careerLevelLabel(match.career_level);
  const deadline = deadlineInfo(match.deadline);
  const applied = appliedDateInfo(match.applied_at);
  return (
    <article
      className={`match-card ${compact ? "compact" : ""}`}
      data-match-id={match.id}
    >
      <div className="match-card-main">
        <div className="match-topline">
          <span className={`freshness freshness-${freshness.id}`}>
            {freshness.label}
          </span>
          {stage && <span className="career-stage-tag">{stage}</span>}
          {labels.map((label) => (
            <span key={label} className="match-status-tag">
              {label}
            </span>
          ))}
          {applied && (
            <time className="match-applied-tag" dateTime={applied.dateTime}>
              {`Applied ${applied.date}`}
            </time>
          )}
          <time dateTime={match.detected_at}>
            {relativeDetection(match.detected_at)}
          </time>
        </div>
        <button className="match-title-button" onClick={() => onOpen(match)}>
          <h3>{match.title}</h3>
        </button>
        <p className="match-company">
          <strong>{match.company}</strong>
          <span>·</span>
          {match.location}
          {match.remote && <span className="remote-tag">Remote eligible</span>}
        </p>
        {deadline && (
          <p className={`match-deadline deadline-${deadline.id}`}>
            Deadline <time dateTime={deadline.dateTime}>{deadline.date}</time>
            <span>·</span>
            <span>{deadline.status}</span>
          </p>
        )}
        <div className="match-reason">
          <span aria-hidden="true">✓</span>
          <p>{match.why[0]}</p>
        </div>
      </div>
      <div className="match-actions">
        {onToggleSaved && (
          <button
            className={`save-button ${saved ? "saved" : ""}`}
            aria-pressed={saved}
            disabled={busy}
            onClick={() => onToggleSaved(match.id)}
          >
            {saved ? "★ Saved" : "☆ Save"}
          </button>
        )}
        {onToggleApplied && (
          <button
            className={`applied-button ${applied ? "applied" : ""}`}
            aria-pressed={Boolean(applied)}
            disabled={busy}
            onClick={() => onToggleApplied(match.id)}
          >
            {applied ? "Applied" : "Mark applied"}
          </button>
        )}
        {onDismiss && (
          <button
            className="dismiss-button"
            disabled={busy}
            onClick={() => onDismiss(match.id)}
          >
            Dismiss
          </button>
        )}
        {onRestore && (
          <button disabled={busy} onClick={() => onRestore(match.id)}>
            Restore
          </button>
        )}
        <button onClick={() => onOpen(match)}>View details</button>
        {match.source_url && (
          <a
            className="button-link primary-link"
            href={match.source_url}
            target="_blank"
            rel="noreferrer"
          >
            Apply now ↗
          </a>
        )}
      </div>
    </article>
  );
}
