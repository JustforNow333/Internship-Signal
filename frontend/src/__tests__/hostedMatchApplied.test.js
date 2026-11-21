import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { MATCH_VIEWS, createMockHostedApi } from "../hosted/api.js";
import { makeHostedFixtures } from "../hosted/fixtures.js";
import { normalizeMatch } from "../hosted/matchModel.js";

// Demo-client Applied semantics mirror PATCH /api/matches/{id} and
// GET /api/matches?view=applied (backend/app/hosted/router.py).
const NOW = new Date("2026-11-19T12:00:00.000Z");
const ORIGINAL = "2026-11-10T09:30:00+00:00";

beforeEach(() => {
  vi.useFakeTimers({ toFake: ["Date"] });
  vi.setSystemTime(NOW);
});

afterEach(() => {
  vi.useRealTimers();
});

const ids = (matches) => matches.map((match) => match.id);

describe("normalizeMatch applied_at", () => {
  it("exposes applied_at and a derived applied flag", () => {
    const base = { id: "m1", matched_at: "2026-11-01T00:00:00Z" };
    expect(normalizeMatch({ ...base, applied_at: ORIGINAL })).toMatchObject({
      applied: true,
      applied_at: ORIGINAL,
    });
    expect(normalizeMatch(base)).toMatchObject({
      applied: false,
      applied_at: null,
    });
  });
});

describe("demo client Applied status", () => {
  it("returns applied_at (null by default) on every match", async () => {
    const api = createMockHostedApi();
    const all = await api.getMatchView("all");
    expect(all.length).toBeGreaterThan(0);
    for (const match of all) {
      expect(match).toMatchObject({ applied: false, applied_at: null });
    }
  });

  it("marks applied with the clock time and keeps it on repeat marking", async () => {
    const api = createMockHostedApi();

    const marked = await api.updateMatch("match-stripe-001", { applied: true });
    expect(marked).toMatchObject({
      applied: true,
      applied_at: NOW.toISOString(),
    });

    vi.setSystemTime(new Date("2026-11-19T15:00:00.000Z"));
    const again = await api.updateMatch("match-stripe-001", { applied: true });
    expect(again.applied_at).toBe(NOW.toISOString());
    expect((await api.getMatch("match-stripe-001")).applied_at).toBe(
      NOW.toISOString(),
    );
  });

  it("clears applied_at and restores the exact original timestamp", async () => {
    const api = createMockHostedApi();
    await api.updateMatch("match-stripe-001", {
      applied: true,
      applied_at: ORIGINAL,
    });

    const cleared = await api.updateMatch("match-stripe-001", {
      applied: false,
    });
    expect(cleared).toMatchObject({ applied: false, applied_at: null });
    expect(ids(await api.getMatchView("applied"))).not.toContain(
      "match-stripe-001",
    );

    const restored = await api.updateMatch("match-stripe-001", {
      applied: true,
      applied_at: ORIGINAL,
    });
    expect(restored).toMatchObject({ applied: true, applied_at: ORIGINAL });
    expect((await api.getMatch("match-stripe-001")).applied_at).toBe(ORIGINAL);
  });

  it("replaces an existing applied_at when one is supplied", async () => {
    const api = createMockHostedApi();
    await api.updateMatch("match-stripe-001", { applied: true });
    const result = await api.updateMatch("match-stripe-001", {
      applied: true,
      applied_at: ORIGINAL,
    });
    expect(result.applied_at).toBe(ORIGINAL);
  });

  it("keeps Applied independent of saved and dismissed", async () => {
    const api = createMockHostedApi();
    const before = await api.getMatch("match-figma-007");

    const applied = await api.updateMatch("match-figma-007", { applied: true });
    expect(applied).toMatchObject({
      saved: before.saved,
      saved_at: before.saved_at,
      dismissed: before.dismissed,
      dismissed_at: before.dismissed_at,
      detected_at: before.detected_at,
      no_longer_matches_at: before.no_longer_matches_at,
      why: before.why,
    });

    const unsaved = await api.updateMatch("match-figma-007", { saved: false });
    const dismissed = await api.updateMatch("match-figma-007", {
      dismissed: true,
    });
    expect(unsaved.applied_at).toBe(NOW.toISOString());
    expect(dismissed.applied_at).toBe(NOW.toISOString());

    const unapplied = await api.updateMatch("match-figma-007", {
      applied: false,
    });
    expect(unapplied).toMatchObject({
      saved: false,
      dismissed: true,
      dismissed_at: dismissed.dismissed_at,
    });
  });

  it("leaves an applied, undismissed, still-matching match in Active", async () => {
    const api = createMockHostedApi();
    await api.updateMatch("match-stripe-001", { applied: true });
    expect(ids(await api.getMatchView("active"))).toContain("match-stripe-001");
  });

  it("rejects invalid Applied payloads with 422 and changes nothing", async () => {
    const api = createMockHostedApi();
    await api.updateMatch("match-stripe-001", {
      applied: true,
      applied_at: ORIGINAL,
    });
    const invalid = [
      { applied: "true" },
      { applied: 1 },
      { applied: null },
      { applied: true, applied_at: null },
      { applied: false, applied_at: ORIGINAL },
      { applied_at: ORIGINAL },
      { applied: true, applied_at: "2026-11-10T09:30:00" },
      { applied: true, applied_at: "2026-11-10" },
      { applied: true, applied_at: "not a date" },
      { applied: true, applied_at: 1_700_000_000_000 },
      { applied: true, applied_at: "2026-11-20T00:00:00Z" },
    ];
    for (const changes of invalid) {
      await expect(
        api.updateMatch("match-stripe-001", changes),
        JSON.stringify(changes),
      ).rejects.toMatchObject({ status: 422 });
    }
    expect((await api.getMatch("match-stripe-001")).applied_at).toBe(ORIGINAL);
  });

  it("accepts a non-UTC offset timestamp exactly as supplied", async () => {
    const api = createMockHostedApi();
    const offset = "2026-11-19T06:00:00-05:00"; // 11:00Z, before NOW
    const result = await api.updateMatch("match-stripe-001", {
      applied: true,
      applied_at: offset,
    });
    expect(result.applied_at).toBe(offset);
  });

  it("applies nothing when the simulated update fails", async () => {
    const api = createMockHostedApi({ failures: { updateMatch: "Nope." } });
    await expect(
      api.updateMatch("match-stripe-001", { applied: true }),
    ).rejects.toThrow("Nope.");
    expect((await api.getMatch("match-stripe-001")).applied_at).toBeNull();
    expect(await api.getMatchView("applied")).toEqual([]);
  });
});

describe("demo client view=applied", () => {
  it("is a supported view", () => {
    expect(MATCH_VIEWS).toContain("applied");
  });

  it("lists dismissed, closed and no-longer-matching applied matches", async () => {
    const api = createMockHostedApi();
    // match-figma-007 is closed and no longer matching; match-stripe-008 is
    // dismissed. Neither appears in Active.
    await api.updateMatch("match-figma-007", { applied: true });
    await api.updateMatch("match-stripe-008", { applied: true });
    await api.updateMatch("match-stripe-001", { applied: true });

    const applied = await api.getMatchView("applied");
    expect(new Set(ids(applied))).toEqual(
      new Set(["match-figma-007", "match-stripe-008", "match-stripe-001"]),
    );
    const times = applied.map((match) => match.detected_at);
    expect([...times].sort().reverse()).toEqual(times);
    const active = ids(await api.getMatchView("active"));
    expect(active).not.toContain("match-figma-007");
    expect(active).not.toContain("match-stripe-008");
  });

  it("keeps the envelope, ordering and pagination across pages", async () => {
    const fixtures = makeHostedFixtures();
    const template = fixtures.matches[0];
    fixtures.matches = Array.from({ length: 120 }, (_, index) => ({
      ...template,
      id: `match-applied-${String(index).padStart(3, "0")}`,
      detected_at: new Date(Date.UTC(2026, 0, 1, 0, index)).toISOString(),
      applied: index % 2 === 0,
      applied_at: index % 2 === 0 ? ORIGINAL : null,
    }));
    const api = createMockHostedApi({ fixtures });

    const first = await api.getMatchPage({ view: "applied" });
    expect(first).toMatchObject({
      limit: 50,
      offset: 0,
      total: 60,
      has_more: true,
    });
    expect(first.items[0].id).toBe("match-applied-118");
    const last = await api.getMatchPage({ view: "applied", offset: 50 });
    expect(last).toMatchObject({ total: 60, has_more: false });
    expect(last.items).toHaveLength(10);

    const all = await api.getMatchView("applied");
    expect(all).toHaveLength(60);
    expect(new Set(ids(all)).size).toBe(60);
  });

  it("can fail only the applied view for error-state previews", async () => {
    const api = createMockHostedApi({
      failures: { "matches.applied": "Applied failed." },
    });
    await expect(api.getMatchView("applied")).rejects.toThrow(
      "Applied failed.",
    );
    await expect(api.getMatchView("saved")).resolves.toHaveLength(1);
  });
});
