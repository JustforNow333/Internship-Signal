export function toggleSelection(values, value) {
  return values.includes(value)
    ? values.filter((current) => current !== value)
    : [...values, value];
}

export function newestFirst(items) {
  return [...items].sort(
    (left, right) => new Date(right.detected_at) - new Date(left.detected_at),
  );
}
