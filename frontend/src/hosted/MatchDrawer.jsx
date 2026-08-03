import { useEffect, useRef } from "react";
import { freshnessFor, relativeDetection } from "./ui.jsx";

export default function MatchDrawer({ match, onClose }) {
  const closeRef = useRef(null);
  useEffect(() => {
    if (!match) return undefined;
    closeRef.current?.focus();
    const onKeyDown = (event) => event.key === "Escape" && onClose();
    window.addEventListener("keydown", onKeyDown);
    return () => window.removeEventListener("keydown", onKeyDown);
  }, [match, onClose]);

  if (!match) return null;
  const freshness = freshnessFor(match.detected_at);
  return (
    <>
      <div className="drawer-scrim" onClick={onClose} />
      <aside
        className="drawer match-drawer"
        role="dialog"
        aria-modal="true"
        aria-labelledby="match-detail-title"
      >
        <div className="drawer-head">
          <button
            ref={closeRef}
            className="close"
            onClick={onClose}
            aria-label="Close match details"
          >
            ×
          </button>
          <span className={`freshness freshness-${freshness.id}`}>
            {freshness.label}
          </span>
          <h2 id="match-detail-title">{match.title}</h2>
          <p className="drawer-company">
            <strong>{match.company}</strong> · {match.location}
            {match.remote ? " · Remote eligible" : ""}
          </p>
          <time dateTime={match.detected_at}>
            {relativeDetection(match.detected_at)}
          </time>
          {match.source_url && (
            <a
              className="button-link primary-link drawer-apply"
              href={match.source_url}
              target="_blank"
              rel="noreferrer"
            >
              Apply on {match.company}’s site ↗
            </a>
          )}
        </div>
        <section>
          <h3>Why this matched</h3>
          <ul className="reason-list">
            {match.why.map((reason) => (
              <li key={reason}>
                <span aria-hidden="true">✓</span>
                {reason}
              </li>
            ))}
          </ul>
        </section>
        {match.summary && (
          <section>
            <h3>About this internship</h3>
            <p>{match.summary}</p>
          </section>
        )}
        {match.responsibilities?.length > 0 && (
          <section>
            <h3>What you’ll do</h3>
            <ul className="detail-list">
              {match.responsibilities.map((item) => (
                <li key={item}>{item}</li>
              ))}
            </ul>
          </section>
        )}
        {match.qualifications?.length > 0 && (
          <section>
            <h3>Available qualifications</h3>
            <ul className="detail-list">
              {match.qualifications.map((item) => (
                <li key={item}>{item}</li>
              ))}
            </ul>
          </section>
        )}
        {match.source_url && (
          <section className="source-section">
            <h3>Source</h3>
            <p>
              Review the employer’s source posting for the complete, current
              requirements and deadline.
            </p>
            <a href={match.source_url} target="_blank" rel="noreferrer">
              Open source posting ↗
            </a>
          </section>
        )}
        {match.source_url && (
          <div className="drawer-sticky-action">
            <a
              className="button-link primary-link large"
              href={match.source_url}
              target="_blank"
              rel="noreferrer"
            >
              Apply now ↗
            </a>
          </div>
        )}
      </aside>
    </>
  );
}
