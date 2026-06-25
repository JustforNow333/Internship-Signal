import { useState } from "react";

// Honest accounting of what ingestion did to the file: column mapping,
// duplicates merged, fields inferred, salary parses and assumptions.
export default function CleaningReport({ report }) {
  const [open, setOpen] = useState(false);
  if (!report) return null;
  const cols = report.columns || {};
  const mappedCount = Object.keys(cols.mapped || {}).length;

  return (
    <div className="card">
      <div className="card-head" style={{ cursor: "pointer" }} onClick={() => setOpen(!open)}>
        <h3>Cleaning report</h3>
        <span className="sub">
          {report.rows_in} rows in · {report.rows_out} kept · {report.duplicates_removed} duplicates
          merged · {Object.values(report.inferred_fields || {}).reduce((a, b) => a + b, 0)} fields inferred
        </span>
        <span style={{ marginLeft: "auto", color: "var(--accent)" }}>{open ? "hide ▴" : "details ▾"}</span>
      </div>
      {open && (
        <div className="card-body grid cols-2">
          <div>
            <h3 style={{ fontSize: 13, margin: "0 0 6px" }}>Columns ({mappedCount} mapped)</h3>
            <dl className="kv">
              {Object.entries(cols.mapped || {}).map(([raw, canon]) => (
                <FragmentRow key={raw} k={`“${raw.trim()}”`} v={`→ ${canon}`} />
              ))}
            </dl>
            {(cols.unmapped || []).length > 0 && (
              <p className="footnote">Unmapped (kept as extra): {cols.unmapped.join(", ")}</p>
            )}
            {(cols.collisions || []).length > 0 && (
              <p className="footnote">
                Collisions: {cols.collisions.map((c) => `“${c.header}” (already ${c.already_mapped_to})`).join(", ")}
              </p>
            )}
          </div>
          <div>
            <h3 style={{ fontSize: 13, margin: "0 0 6px" }}>Duplicates merged</h3>
            {(report.duplicates || []).length === 0 && <p className="footnote">None found.</p>}
            <ul className="note-list">
              {(report.duplicates || []).map((d, i) => (
                <li key={i}>
                  Row {d.row_number} ≡ row {d.duplicate_of} ({d.company || "?"} — matched on {d.matched_on}
                  {d.merged_fields?.length ? `; filled ${d.merged_fields.join(", ")}` : ""})
                </li>
              ))}
            </ul>
            <h3 style={{ fontSize: 13, margin: "12px 0 6px" }}>Inference & parsing</h3>
            <ul className="note-list">
              {Object.entries(report.inferred_fields || {}).map(([f, n]) => (
                <li key={f}>Inferred <code>{f}</code> on {n} row{n === 1 ? "" : "s"}</li>
              ))}
              <li>
                Salary: {report.salary_parsing?.parsed} parsed, {report.salary_parsing?.unparsed} unparseable,{" "}
                {report.salary_parsing?.period_assumed} with an assumed pay period
              </li>
              {(report.warnings || []).map((w, i) => <li key={`w${i}`}>{w}</li>)}
            </ul>
          </div>
        </div>
      )}
    </div>
  );
}

function FragmentRow({ k, v }) {
  return (
    <>
      <dt>{k}</dt>
      <dd className="mono">{v}</dd>
    </>
  );
}
