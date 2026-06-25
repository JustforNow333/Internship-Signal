export default function EmptyState({ glyph = "◌", title, hint }) {
  return (
    <div className="empty">
      <div className="glyph">{glyph}</div>
      <div style={{ fontWeight: 600, marginTop: 6 }}>{title}</div>
      {hint && <div style={{ fontSize: 13, marginTop: 4 }}>{hint}</div>}
    </div>
  );
}
