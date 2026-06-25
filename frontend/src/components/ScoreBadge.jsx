// The signature "signal bar": a horizontal meter with the numeric score.
// Used in the table, the drawer, and the bucket board so scores read the
// same way everywhere.
export default function ScoreBadge({ score, bucket, compact = false }) {
  return (
    <span
      className={`sigbar ${bucket}`}
      role="meter"
      aria-valuenow={score}
      aria-valuemin={0}
      aria-valuemax={100}
      aria-label={`Score ${score} of 100 (${bucket})`}
      style={compact ? { minWidth: 92 } : undefined}
    >
      <span className="track">
        <span className="fill" style={{ width: `${score}%` }} />
      </span>
      <span className="num">{score}</span>
    </span>
  );
}
