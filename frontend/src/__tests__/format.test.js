import { describe, expect, it } from "vitest";
import { confDots, fmtDeadline, fmtPay, hourlyMid } from "../utils/format.js";

const paid = (lo, hi, extra = {}) => ({
  kind: "paid", usd_hourly_min: lo, usd_hourly_max: hi, period_assumed: false, ...extra,
});

describe("fmtPay", () => {
  it("formats a single hourly rate", () => {
    expect(fmtPay(paid(45, 45))).toBe("$45/hr");
  });
  it("formats a range", () => {
    expect(fmtPay(paid(25, 30))).toBe("$25–$30/hr");
  });
  it("keeps cents for small rates", () => {
    expect(fmtPay(paid(6.25, 6.25))).toBe("$6.25/hr");
  });
  it("marks assumed periods as estimates", () => {
    expect(fmtPay(paid(38.46, 38.46, { period_assumed: true }))).toBe("$38/hr (est.)");
  });
  it("labels non-cash kinds", () => {
    expect(fmtPay({ kind: "unpaid" })).toBe("Unpaid");
    expect(fmtPay({ kind: "equity_only" })).toBe("Equity only");
    expect(fmtPay({ kind: "commission_only" })).toBe("Commission only");
    expect(fmtPay({ kind: "unknown" })).toBe("—");
    expect(fmtPay({ kind: "unknown_vague", raw: "Competitive" })).toBe("Competitive");
  });
});

describe("hourlyMid", () => {
  it("averages a range", () => {
    expect(hourlyMid(paid(25, 30))).toBe(27.5);
  });
  it("is null when unparsed", () => {
    expect(hourlyMid({ kind: "unknown", usd_hourly_min: null })).toBeNull();
  });
});

describe("fmtDeadline", () => {
  it("handles expired / today / singular / plural / rolling", () => {
    expect(fmtDeadline({ deadline_days_left: -8 })).toBe("expired 8d ago");
    expect(fmtDeadline({ deadline_days_left: 0 })).toBe("due today");
    expect(fmtDeadline({ deadline_days_left: 1 })).toBe("1 day left");
    expect(fmtDeadline({ deadline_days_left: 5 })).toBe("5 days left");
    expect(fmtDeadline({ deadline_days_left: null, deadline: "" })).toBe("rolling");
  });
});

describe("confDots", () => {
  it("maps confidence to 0-4 dots", () => {
    expect(confDots(0)).toBe(0);
    expect(confDots(0.5)).toBe(2);
    expect(confDots(0.95)).toBe(4);
    expect(confDots(1.4)).toBe(4); // clamped
  });
});
