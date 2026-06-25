// Compact warning/positive badges for table rows. Shows the worst flags
// first, then the strongest positives, capped so rows stay scannable.
export default function SignalBadges({ job, max = 3 }) {
  const order = { critical: 0, major: 1, minor: 2 };
  const flags = [...job.red_flags].sort((a, b) => order[a.severity] - order[b.severity]);
  const pos = [...job.positive_signals].sort((a, b) => b.strength - a.strength);

  const shown = [];
  for (const f of flags) {
    if (shown.length >= max) break;
    shown.push(
      <span key={`f-${f.id}`} className={`badge flag-${f.severity}`} title={f.evidence}>
        ⚠ {f.label}
      </span>
    );
  }
  for (const s of pos) {
    if (shown.length >= max) break;
    shown.push(
      <span key={`s-${s.id}`} className="badge signal" title={s.evidence}>
        ✓ {s.label}
      </span>
    );
  }
  const hidden = flags.length + pos.length - shown.length;
  return (
    <span style={{ display: "inline-flex", gap: 5, flexWrap: "wrap" }}>
      {shown}
      {hidden > 0 && <span className="badge neutral">+{hidden}</span>}
    </span>
  );
}
