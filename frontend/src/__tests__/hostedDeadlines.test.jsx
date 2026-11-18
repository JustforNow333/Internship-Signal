import { render, screen, within } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import MatchCard from "../hosted/MatchCard.jsx";
import MatchDrawer from "../hosted/MatchDrawer.jsx";
import { normalizeMatch } from "../hosted/matchModel.js";
import {
  calendarDateIn,
  deadlineInfo,
  parseCalendarDate,
} from "../hosted/utils.js";

function match(overrides = {}) {
  return normalizeMatch({
    id: "11111111-1111-4111-8111-111111111111",
    job_id: "22222222-2222-4222-8222-222222222222",
    company_id: "stripe",
    company: "Stripe",
    title: "Software Engineering Intern, Payments",
    location: "New York, NY",
    remote: false,
    role_id: "software_engineering",
    application_url: "https://stripe.com/jobs/1",
    deadline: null,
    is_open: true,
    match_reasons: [{ code: "company_watched", value: "stripe" }],
    matched_at: "2026-11-18T09:00:00Z",
    last_matched_at: "2026-11-18T09:00:00Z",
    ...overrides,
  });
}

afterEach(() => {
  vi.useRealTimers();
});

describe("deadline calendar dates", () => {
  beforeEach(() => {
    vi.useFakeTimers();
    vi.setSystemTime(new Date(2026, 10, 18, 12, 0, 0));
  });

  it("returns nothing for a missing or invalid deadline", () => {
    for (const value of [null, undefined, "", "not a date", "2026-02-30", "2026-13-01"]) {
      expect(parseCalendarDate(value)).toBeNull();
      expect(deadlineInfo(value)).toBeNull();
    }
  });

  it("labels past, today, tomorrow and future deadlines", () => {
    expect(deadlineInfo("2026-11-17")).toEqual({
      id: "passed",
      date: "Nov 17, 2026",
      dateTime: "2026-11-17",
      status: "Deadline passed",
    });
    expect(deadlineInfo("2026-11-18")).toMatchObject({
      id: "today",
      date: "Nov 18, 2026",
      status: "Closes today",
    });
    expect(deadlineInfo("2026-11-19").status).toBe("Closes in 1 day");
    expect(deadlineInfo("2026-12-02")).toMatchObject({
      id: "upcoming",
      date: "Dec 2, 2026",
      status: "Closes in 14 days",
    });
  });

  it("keeps a date-only value as its calendar parts", () => {
    expect(parseCalendarDate("2026-12-01")).toEqual({ year: 2026, month: 12, day: 1 });
    expect(parseCalendarDate("2028-02-29")).toEqual({ year: 2028, month: 2, day: 29 });
  });

  it("uses the viewer's calendar day west of UTC, late in the evening", () => {
    const LA = "America/Los_Angeles";
    // 23:30 Pacific on Nov 18 is already Nov 19 in UTC.
    const now = new Date("2026-11-19T07:30:00Z");
    expect(calendarDateIn(now, LA)).toEqual({ year: 2026, month: 11, day: 18 });

    // A UTC parse of "2026-12-01" shown in Pacific time would read Nov 30.
    expect(deadlineInfo("2026-12-01", { now, timeZone: LA }).date).toBe("Dec 1, 2026");
    expect(deadlineInfo("2026-11-18", { now, timeZone: LA }).status).toBe("Closes today");
    expect(deadlineInfo("2026-11-19", { now, timeZone: LA }).status).toBe(
      "Closes in 1 day",
    );
    // The same instant is already Nov 19 for a viewer in UTC.
    expect(deadlineInfo("2026-11-18", { now, timeZone: "UTC" }).status).toBe(
      "Deadline passed",
    );
  });

  it("uses the viewer's calendar day east of UTC", () => {
    const AUCKLAND = "Pacific/Auckland";
    // 00:30 on Nov 19 in Auckland is still Nov 18 in UTC.
    const now = new Date("2026-11-18T11:30:00Z");
    expect(deadlineInfo("2026-11-19", { now, timeZone: AUCKLAND }).status).toBe(
      "Closes today",
    );
    expect(deadlineInfo("2026-11-18", { now, timeZone: AUCKLAND })).toMatchObject({
      date: "Nov 18, 2026",
      status: "Deadline passed",
    });
  });

  it("counts calendar days across daylight-saving changes", () => {
    const LA = "America/Los_Angeles";
    // US DST ends Nov 1, 2026 (a 25-hour day) and starts Mar 8, 2026 (23 hours).
    const fallNow = new Date("2026-10-31T19:00:00-07:00");
    expect(deadlineInfo("2026-11-07", { now: fallNow, timeZone: LA }).status).toBe(
      "Closes in 7 days",
    );
    const springNow = new Date("2026-03-07T23:30:00-08:00");
    expect(deadlineInfo("2026-03-09", { now: springNow, timeZone: LA }).status).toBe(
      "Closes in 2 days",
    );
  });
});

describe("deadline rendering", () => {
  beforeEach(() => {
    vi.useFakeTimers();
    vi.setSystemTime(new Date(2026, 10, 18, 12, 0, 0));
  });

  it("shows the date and status on a match card", () => {
    render(<MatchCard match={match({ deadline: "2026-11-21" })} onOpen={() => {}} />);
    const deadline = screen.getByText("Closes in 3 days").closest(".match-deadline");
    expect(within(deadline).getByText("Nov 21, 2026")).toHaveAttribute(
      "dateTime",
      "2026-11-21",
    );
  });

  it("shows the deadline status on a compact dashboard card", () => {
    render(
      <MatchCard compact match={match({ deadline: "2026-11-10" })} onOpen={() => {}} />,
    );
    expect(screen.getByText("Deadline passed")).toBeInTheDocument();
    expect(screen.getByText("Nov 10, 2026")).toBeInTheDocument();
  });

  it("renders no deadline on a card when none is listed or it is invalid", () => {
    const { container, rerender } = render(
      <MatchCard match={match()} onOpen={() => {}} />,
    );
    expect(container.querySelector(".match-deadline")).toBeNull();
    rerender(<MatchCard match={match({ deadline: "garbage" })} onOpen={() => {}} />);
    expect(container.querySelector(".match-deadline")).toBeNull();
    expect(container.textContent).not.toContain("Invalid Date");
  });

  it("shows the deadline in the detail drawer", () => {
    render(<MatchDrawer match={match({ deadline: "2026-11-18" })} onClose={() => {}} />);
    const dialog = screen.getByRole("dialog");
    expect(within(dialog).getByText("Closes today")).toBeInTheDocument();
    expect(within(dialog).getByText("Nov 18, 2026")).toHaveAttribute(
      "dateTime",
      "2026-11-18",
    );
  });

  it("says no deadline is listed in the drawer for a null or invalid deadline", () => {
    const { rerender } = render(<MatchDrawer match={match()} onClose={() => {}} />);
    expect(screen.getByText("No deadline listed")).toBeInTheDocument();
    rerender(<MatchDrawer match={match({ deadline: "2026-02-30" })} onClose={() => {}} />);
    expect(screen.getByText("No deadline listed")).toBeInTheDocument();
    expect(screen.getByRole("dialog").textContent).not.toContain("Invalid Date");
  });
});
