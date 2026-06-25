import { useState } from "react";
import { api } from "../api.js";
import ScoreBadge from "./ScoreBadge.jsx";

const SUGGESTIONS = [
  "Which postings are best for backend experience?",
  "Show paid data science internships only",
  "Which ones look exploitative?",
  "Which companies seem like actual startups?",
  "Which ones should I apply to tonight?",
];

// Natural-language questions answered by the backend's deterministic
// query engine. The interpretation line shows exactly how the question
// was understood — no black box.
export default function AskPanel({ datasetId, jobs, onOpenJob }) {
  const [q, setQ] = useState("");
  const [busy, setBusy] = useState(false);
  const [answer, setAnswer] = useState(null);
  const [error, setError] = useState("");

  const run = async (question) => {
    if (!question.trim() || busy) return;
    setBusy(true);
    setError("");
    setQ(question);
    try {
      setAnswer(await api.ask(datasetId, question));
    } catch (e) {
      setError(e.message);
    } finally {
      setBusy(false);
    }
  };

  return (
    <div className="card">
      <div className="card-head">
        <h2>Ask the dataset</h2>
        <span className="sub">deterministic — keyword rules, not an LLM</span>
      </div>
      <div className="card-body">
        <div className="ask-row">
          <input
            type="text"
            value={q}
            placeholder="e.g. which ones should I apply to tonight?"
            onChange={(e) => setQ(e.target.value)}
            onKeyDown={(e) => e.key === "Enter" && run(q)}
            aria-label="Question"
          />
          <button className="primary" onClick={() => run(q)} disabled={busy || !q.trim()}>
            Ask
          </button>
        </div>
        <div className="suggestions">
          {SUGGESTIONS.map((s) => (
            <button key={s} onClick={() => run(s)} disabled={busy}>{s}</button>
          ))}
        </div>

        {busy && <div className="loading">Reading the dataset</div>}
        {error && <div className="error-banner">{error}</div>}

        {answer && !busy && (
          <div className="ask-answer">
            <div className="interp">
              <strong>Interpreted as:</strong> {answer.interpretation}
              {answer.filters_applied?.length > 0 && <> · filters: {answer.filters_applied.join("; ")}</>}
            </div>
            <p style={{ margin: "12px 0 6px" }}>{answer.summary_text}</p>

            {answer.results?.map((r) => {
              const job = jobs.find((j) => j.id === r.id);
              return (
                <div key={r.id} className="result-line" onClick={() => job && onOpenJob(job)}>
                  <div className="who">
                    <div className="t">{r.title} <span style={{ color: "var(--ink-soft)", fontWeight: 400 }}>· {r.company}</span></div>
                    <div className="why">{r.headline_reason}</div>
                  </div>
                  <span className={`badge action-${job?.score.action || "skip"}`}>{r.action_label}</span>
                  {job && <ScoreBadge score={r.score} bucket={job.score.bucket} compact />}
                </div>
              );
            })}

            {answer.examples && (
              <ul className="note-list">{answer.examples.map((e) => <li key={e}>{e}</li>)}</ul>
            )}
            <div className="llm-note">{answer.llm_note}</div>
          </div>
        )}
      </div>
    </div>
  );
}
