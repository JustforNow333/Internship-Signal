import { useRef, useState } from "react";

// Upload a CSV, paste raw CSV text, or load the bundled sample dataset.
export default function UploadPanel({ onIngestFile, onIngestText, onLoadSample, busy }) {
  const [pasted, setPasted] = useState("");
  const [over, setOver] = useState(false);
  const fileRef = useRef(null);

  const pick = (files) => {
    if (files && files[0]) onIngestFile(files[0]);
  };

  return (
    <div className="card">
      <div className="card-head">
        <h2>Load postings</h2>
        <span className="sub">CSV in, signal out — nothing leaves your machine.</span>
      </div>
      <div className="card-body">
        <div
          className={`dropzone ${over ? "over" : ""}`}
          onDragOver={(e) => { e.preventDefault(); setOver(true); }}
          onDragLeave={() => setOver(false)}
          onDrop={(e) => { e.preventDefault(); setOver(false); pick(e.dataTransfer.files); }}
        >
          <div>
            Drop a CSV here, or{" "}
            <button className="primary" onClick={() => fileRef.current?.click()} disabled={busy}>
              choose a file
            </button>{" "}
            or{" "}
            <button onClick={onLoadSample} disabled={busy}>
              load the sample dataset
            </button>
          </div>
          <div className="hint">
            Messy headers are fine — “Pay”, “Apply By”, “Remote?” all get mapped, and the
            cleaning report shows exactly what happened.
          </div>
          <input
            ref={fileRef}
            type="file"
            accept=".csv,text/csv"
            hidden
            onChange={(e) => { pick(e.target.files); e.target.value = ""; }}
          />
        </div>

        <div style={{ marginTop: 14 }}>
          <textarea
            rows={5}
            placeholder="…or paste raw CSV text here (header row first)"
            value={pasted}
            onChange={(e) => setPasted(e.target.value)}
          />
          <div style={{ marginTop: 8, display: "flex", gap: 8 }}>
            <button
              className="primary"
              disabled={busy || !pasted.trim()}
              onClick={() => onIngestText(pasted)}
            >
              Analyze pasted CSV
            </button>
            {busy && <span className="loading">Scoring postings</span>}
          </div>
        </div>
      </div>
    </div>
  );
}
