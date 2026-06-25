import { useEffect } from "react";
import { fmtDeadline, fmtPay } from "../utils/format.js";
import CategoryBars from "./CategoryBars.jsx";
import ConfidenceDots from "./ConfidenceDots.jsx";
import ScoreBadge from "./ScoreBadge.jsx";

// Full transparency view for one posting: score breakdown, reasons and
// concerns, signal evidence, profile match, parse details, raw text.
export default function JobDrawer({ job, onClose, shortlist }) {
  useEffect(() => {
    const onKey = (e) => e.key === "Escape" && onClose();
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [onClose]);

  if (!job) return null;
  const s = job.score;

  return (
    <>
      <div className="drawer-scrim" onClick={onClose} />
      <aside className="drawer" role="dialog" aria-label={`${job.title} details`}>
        <div className="drawer-head">
          <button className="close" onClick={onClose} aria-label="Close">✕</button>
          <h2>{job.title || "Untitled posting"}</h2>
          <div className="meta">
            {job.company || "Unknown company"}
            {job.location ? ` · ${job.location}` : ""}
            {job.remote_status ? ` · ${job.remote_status}` : ""}
            {job.internship_type ? ` · ${job.internship_type}` : ""}
          </div>
          <div style={{ display: "flex", alignItems: "center", gap: 12, marginTop: 10, flexWrap: "wrap" }}>
            <ScoreBadge score={s.total} bucket={s.bucket} />
            <span className={`badge action-${s.action}`}>{s.action_label}</span>
            <button
              className={`star ${shortlist.has(job.id) ? "on" : ""}`}
              onClick={() => shortlist.toggle(job.id)}
              style={{ fontSize: 18 }}
            >
              {shortlist.has(job.id) ? "★ shortlisted" : "☆ shortlist"}
            </button>
            {job.source_url && (
              <a href={job.source_url} target="_blank" rel="noreferrer" style={{ fontSize: 13.5 }}>
                open posting ↗
              </a>
            )}
          </div>
          <p className="footnote" style={{ marginBottom: 0 }}>{s.explanation}</p>
        </div>

        <section>
          <h3>Reasons to apply</h3>
          <ul className="note-list">{s.reasons.map((r, i) => <li key={i}>{r}</li>)}</ul>
          <h3 style={{ marginTop: 14 }}>Concerns</h3>
          <ul className="note-list">{s.concerns.map((c, i) => <li key={i}>{c}</li>)}</ul>
        </section>

        <section>
          <h3>Score breakdown</h3>
          <CategoryBars categories={s.categories} />
        </section>

        <section>
          <h3>Why this matched your profile</h3>
          {job.profile_match.matched_skills.length === 0 &&
           job.profile_match.matched_interests.length === 0 ? (
            <p className="footnote">No direct overlap with your skills or interests.</p>
          ) : (
            <>
              {job.profile_match.matched_skills.map((sk) => <span className="chip" key={sk}>{sk}</span>)}
              {job.profile_match.matched_interests.map((it) => (
                <span className="chip dim" key={it}>{it}</span>
              ))}
              <p className="footnote">{job.profile_match.summary}</p>
            </>
          )}
        </section>

        <section>
          <h3>Signals & flags</h3>
          {job.red_flags.length === 0 && <p className="footnote">No red flags detected.</p>}
          {job.red_flags.map((f) => (
            <div key={f.id} style={{ marginBottom: 8 }}>
              <span className={`badge flag-${f.severity}`}>⚠ {f.label}</span>
              <div className="footnote" style={{ marginTop: 2 }}>evidence: “{f.evidence}”</div>
            </div>
          ))}
          {job.positive_signals.map((p) => (
            <div key={p.id} style={{ marginBottom: 8 }}>
              <span className="badge signal">✓ {p.label}</span>
              <div className="footnote" style={{ marginTop: 2 }}>evidence: “{p.evidence}”</div>
            </div>
          ))}
        </section>

        <section>
          <h3>How the fields were read</h3>
          <dl className="kv">
            <dt>Pay</dt>
            <dd>
              {fmtPay(job.compensation)}
              <ConfidenceDots value={job.compensation.confidence} label="parse confidence" />
              {job.compensation.raw && <span className="footnote"> raw: “{job.compensation.raw}”</span>}
            </dd>
            <dt>Role</dt>
            <dd>
              {job.role_classification.label}
              <ConfidenceDots value={job.role_classification.confidence} />
              <div className="footnote">{job.role_classification.evidence.join(" · ")}</div>
            </dd>
            <dt>Company</dt>
            <dd>
              {job.company_classification.category}
              {job.company_classification.is_startup ? " (startup)" : ""}
              <ConfidenceDots value={job.company_classification.confidence} />
              <ul className="note-list">
                {job.company_classification.evidence.map((e, i) => <li key={i}>{e}</li>)}
              </ul>
            </dd>
            <dt>Deadline</dt>
            <dd>{fmtDeadline(job)}</dd>
            {job.inferred_fields.length > 0 && (
              <>
                <dt>Inferred</dt>
                <dd className="footnote">{job.inferred_fields.join(", ")} (filled from context)</dd>
              </>
            )}
          </dl>
          {job.compensation.notes?.length > 0 && (
            <ul className="note-list">{job.compensation.notes.map((n, i) => <li key={i}>{n}</li>)}</ul>
          )}
        </section>

        <section>
          <h3>Raw text</h3>
          <details className="raw">
            <summary>Description & requirements as ingested</summary>
            <pre>
{`DESCRIPTION
${job.description || "(blank)"}

REQUIREMENTS
${job.requirements || "(blank)"}`}
            </pre>
          </details>
        </section>
      </aside>
    </>
  );
}
