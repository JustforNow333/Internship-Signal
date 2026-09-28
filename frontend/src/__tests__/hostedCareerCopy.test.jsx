import { fireEvent, render, screen, within } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";

import indexHtml from "../../index.html?raw";
import App from "../App.jsx";
import { createMockHostedApi } from "../hosted/api.js";
import { ROLE_OPTIONS } from "../hosted/constants.js";
import { makeHostedFixtures } from "../hosted/fixtures.js";
import MatchCard from "../hosted/MatchCard.jsx";
import MatchDrawer from "../hosted/MatchDrawer.jsx";
import { careerLevelLabel, normalizeMatch, reasonText } from "../hosted/matchModel.js";

const STAGES = [
  ["internship", "Internship"],
  ["new_grad_junior", "New grad / junior"],
  ["senior_plus", "Senior+"],
];
const RAW_IDS = /internship\b(?!s)|new_grad_junior|senior_plus|career_level_selected/;
const STALE_COPY =
  /Internship Signal|internship alerts|internship matches|matching internships|new internships|About this internship|internship categories/i;

function fixturesWithStages() {
  const fixtures = makeHostedFixtures();
  fixtures.matches = fixtures.matches.map((match, index) => ({
    ...match,
    career_level: STAGES[index % STAGES.length][0],
  }));
  return fixtures;
}

function renderApp(path, fixtures = makeHostedFixtures()) {
  const client = createMockHostedApi({ fixtures });
  return render(<App initialPath={path} client={client} />);
}

function apiMatch(overrides = {}) {
  return {
    id: "match-1",
    job_id: "job-1",
    company_id: "stripe",
    company: "Stripe",
    title: "Software Engineer",
    location: "New York, NY",
    remote: false,
    role_id: "software_engineering",
    career_level: "senior_plus",
    application_url: "https://stripe.com/jobs/1",
    match_reasons: [
      { code: "company_watched", value: "stripe" },
      { code: "career_level_selected", value: "senior_plus" },
    ],
    matched_at: new Date().toISOString(),
    ...overrides,
  };
}

describe("career-stage labels", () => {
  it.each(STAGES)("maps %s to %s", (id, label) => {
    expect(careerLevelLabel(id)).toBe(label);
  });

  it("has no label for internal or missing stages", () => {
    for (const value of ["mid_level", "unknown", "", undefined, null]) {
      expect(careerLevelLabel(value)).toBe("");
    }
  });

  it("renders career_level_selected as readable text", () => {
    const match = normalizeMatch(apiMatch());
    expect(match.career_level).toBe("senior_plus");
    expect(match.why).toContain("Senior+ is in your career stages");
    expect(
      reasonText({ code: "career_level_selected" }, { career_level: "" }),
    ).toBe("Matches your career stages");
    expect(match.why.join(" ")).not.toMatch(RAW_IDS);
  });
});

describe("match surfaces show the career stage", () => {
  it.each(STAGES)("MatchCard shows %s as %s once", (id, label) => {
    const match = normalizeMatch(apiMatch({ career_level: id, match_reasons: [] }));
    render(<MatchCard match={match} onOpen={() => {}} />);
    const card = screen.getByRole("article");
    expect(within(card).getAllByText(label)).toHaveLength(1);
    expect(card.textContent).not.toMatch(/new_grad_junior|senior_plus/);
  });

  it("omits the tag when no stage is known", () => {
    const match = normalizeMatch(apiMatch({ career_level: undefined, match_reasons: [] }));
    const { container } = render(<MatchCard match={match} onOpen={() => {}} />);
    expect(container.querySelector(".career-stage-tag")).toBeNull();
  });

  it("keeps save, dismiss, details, and apply actions working", () => {
    const match = normalizeMatch(apiMatch());
    const onOpen = vi.fn();
    const onToggleSaved = vi.fn();
    const onDismiss = vi.fn();
    render(
      <MatchCard
        match={match}
        onOpen={onOpen}
        onToggleSaved={onToggleSaved}
        onDismiss={onDismiss}
      />,
    );
    fireEvent.click(screen.getByRole("button", { name: "☆ Save" }));
    fireEvent.click(screen.getByRole("button", { name: "Dismiss" }));
    fireEvent.click(screen.getByRole("button", { name: "View details" }));
    expect(onToggleSaved).toHaveBeenCalledWith("match-1");
    expect(onDismiss).toHaveBeenCalledWith("match-1");
    expect(onOpen).toHaveBeenCalledWith(match);
    expect(screen.getByRole("link", { name: /Apply now/ })).toHaveAttribute(
      "href",
      "https://stripe.com/jobs/1",
    );
  });

  it("MatchDrawer shows the stage and generalized wording", () => {
    const match = {
      ...normalizeMatch(apiMatch({ career_level: "new_grad_junior" })),
      summary: "Build payment systems.",
    };
    render(<MatchDrawer match={match} onClose={() => {}} />);
    const drawer = screen.getByRole("dialog");
    expect(within(drawer).getByText("New grad / junior")).toBeInTheDocument();
    expect(
      within(drawer).getByRole("heading", { name: "About this role" }),
    ).toBeInTheDocument();
    expect(drawer.textContent).not.toMatch(/About this internship|new_grad_junior/);
  });

  it("renders a mixed-stage dashboard and matches page", async () => {
    renderApp("/app/dashboard", fixturesWithStages());
    expect(
      await screen.findByRole("heading", { name: "Recent matches" }),
    ).toBeInTheDocument();
    for (const [, label] of STAGES) {
      expect(screen.getAllByText(label).length).toBeGreaterThan(0);
    }
    expect(document.body.textContent).not.toMatch(STALE_COPY);
  });

  it("labels every match on the matches page", async () => {
    renderApp("/app/matches", fixturesWithStages());
    const list = await screen.findByRole("region", { name: "Job matches" });
    for (const [, label] of STAGES) {
      expect(within(list).getAllByText(label).length).toBeGreaterThan(0);
    }
    expect(list.textContent).not.toMatch(/new_grad_junior|senior_plus/);
  });
});

describe("generalized empty states", () => {
  it("uses job wording on an empty dashboard", async () => {
    const fixtures = makeHostedFixtures();
    fixtures.matches = [];
    renderApp("/app/dashboard", fixtures);
    expect(await screen.findByText(/show new matches here/)).toBeInTheDocument();
    expect(document.body.textContent).not.toMatch(STALE_COPY);
  });

  it("uses job wording on an empty matches page", async () => {
    const fixtures = makeHostedFixtures();
    fixtures.matches = [];
    renderApp("/app/matches", fixtures);
    expect(await screen.findByText("No matching jobs yet")).toBeInTheDocument();
  });
});

describe("FindSooner branding and copy", () => {
  it("titles the document FindSooner and keeps the favicon", () => {
    const html = indexHtml;
    expect(html).toContain("<title>FindSooner — Never apply late again</title>");
    expect(html).toContain('<link rel="icon" type="image/png" href="/favicon.png" />');
    expect(html).not.toMatch(STALE_COPY);
  });

  it("brands the landing page as FindSooner", () => {
    renderApp("/");
    expect(screen.getAllByRole("link", { name: "FindSooner home" }).length).toBeGreaterThan(0);
    expect(document.body.textContent).toContain("FindSooner");
    expect(document.body.textContent).not.toMatch(STALE_COPY);
  });

  it("brands sign-in as FindSooner", () => {
    renderApp("/signin");
    expect(
      screen.getByRole("heading", { name: "Sign in to FindSooner" }),
    ).toBeInTheDocument();
    expect(document.body.textContent).not.toMatch(STALE_COPY);
  });

  it.each(["/app/watchlist", "/app/settings"])(
    "has no stale internship-only copy on %s",
    async (path) => {
      renderApp(path);
      await screen.findByRole("heading", { level: 1 });
      expect(document.body.textContent).not.toMatch(STALE_COPY);
    },
  );

  it("keeps legitimate internship-season wording in settings", async () => {
    renderApp("/app/settings");
    await screen.findByRole("heading", { name: "Settings" });
    expect(screen.getByText("Internship season")).toBeInTheDocument();
    expect(screen.getByRole("checkbox", { name: "Internships" })).toBeChecked();
  });

  it("has no stale copy during onboarding", async () => {
    renderApp("/onboarding");
    await screen.findByRole("heading", {
      name: "What kind of opportunities are you looking for?",
    });
    expect(screen.getByText("Internships")).toBeInTheDocument();
    expect(document.body.textContent).not.toMatch(STALE_COPY);
  });

  it("describes role categories without a career stage", () => {
    for (const role of ROLE_OPTIONS) {
      expect(role.description).not.toMatch(/internship/i);
    }
    expect(ROLE_OPTIONS.map((role) => role.id)).toEqual([
      "software_engineering",
      "machine_learning_ai",
      "data_science",
      "data_engineering",
      "quantitative_development",
      "product_management",
      "hardware_embedded",
      "other_engineering",
    ]);
  });
});
