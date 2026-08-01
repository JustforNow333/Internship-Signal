import { afterEach, describe, expect, it, vi } from "vitest";

import { freshnessFor, relativeDetection } from "../hosted/ui.jsx";
import { newestFirst, toggleSelection } from "../hosted/utils.js";

describe("hosted relative-time labels", () => {
  afterEach(() => vi.useRealTimers());

  it("keeps a sub-hour detection consistent with the Just detected bucket", () => {
    vi.useFakeTimers();
    vi.setSystemTime(new Date("2026-08-01T12:00:00Z"));
    const detectedAt = "2026-08-01T11:00:20Z";

    expect(freshnessFor(detectedAt)).toEqual({
      id: "just",
      label: "Just detected",
    });
    expect(relativeDetection(detectedAt)).toBe("Detected 59 minutes ago");
  });

  it("keeps a sub-day detection consistent with the New today bucket", () => {
    vi.useFakeTimers();
    vi.setSystemTime(new Date("2026-08-02T12:00:00Z"));
    const detectedAt = "2026-08-01T12:00:20Z";

    expect(freshnessFor(detectedAt)).toEqual({
      id: "today",
      label: "New today",
    });
    expect(relativeDetection(detectedAt)).toBe("Detected 23 hours ago");
  });
});

describe("hosted collection utilities", () => {
  it("adds and removes a selection without mutating the input", () => {
    const original = ["software-engineering"];

    expect(toggleSelection(original, "data-science")).toEqual([
      "software-engineering",
      "data-science",
    ]);
    expect(toggleSelection(original, "software-engineering")).toEqual([]);
    expect(original).toEqual(["software-engineering"]);
  });

  it("sorts matches newest first without mutating the API response", () => {
    const original = [
      { id: "older", detected_at: "2026-08-01T10:00:00Z" },
      { id: "newer", detected_at: "2026-08-01T11:00:00Z" },
    ];

    expect(newestFirst(original).map((match) => match.id)).toEqual([
      "newer",
      "older",
    ]);
    expect(original.map((match) => match.id)).toEqual(["older", "newer"]);
  });
});
