import { readFileSync } from "node:fs";
import { resolve } from "node:path";

import { fireEvent, render, screen, within } from "@testing-library/react";
import { describe, expect, it } from "vitest";

import App from "../App.jsx";
import { createMockHostedApi } from "../hosted/api.js";

// Regression: the Undo notice and action errors render above a long results
// list. Unpinned, dismissing a match far down the page put them off-screen,
// so the Undo was never seen. They must stay pinned in view (sticky).

// Vitest stubs CSS imports, so the stylesheet is read from disk (tests run
// from frontend/).
const styles = readFileSync(resolve("src/styles.css"), "utf8");

function stickyRule(selector) {
  const css = styles.replace(/\/\*[\s\S]*?\*\//g, "");
  return [...css.matchAll(/([^{}]+)\{([^{}]*)\}/g)].some(
    ([, selectors, body]) =>
      selectors.split(",").some((item) => item.trim() === selector) &&
      /position:\s*sticky/.test(body),
  );
}

const firstCard = async () =>
  (await screen.findAllByRole("article"))[0];

describe("Matches page notices stay in view", () => {
  it("pins the Undo notice after a dismissal", async () => {
    render(<App initialPath="/app/matches" client={createMockHostedApi()} />);
    const card = await firstCard();
    fireEvent.click(within(card).getByRole("button", { name: "Dismiss" }));
    const notice = await screen.findByRole("status");
    expect(notice).toHaveTextContent(/Dismissed/);
    expect(notice).toHaveClass("match-notice");
    expect(stickyRule(".match-notice")).toBe(true);
  });

  it("pins the error shown when a change fails", async () => {
    const client = createMockHostedApi({
      failures: { updateMatch: "That change could not be saved." },
    });
    render(<App initialPath="/app/matches" client={client} />);
    const card = await firstCard();
    fireEvent.click(within(card).getByRole("button", { name: "Dismiss" }));
    const alert = await screen.findByRole("alert");
    expect(alert).toHaveTextContent("That change could not be saved.");
    expect(alert).toHaveClass("match-action-error");
    expect(stickyRule(".match-action-error")).toBe(true);
  });
});
