import { readFileSync } from "node:fs";
import { resolve } from "node:path";

import { describe, expect, it } from "vitest";

// Regression: with the "Applied <date>" tag, a card's top line (freshness,
// career stage, status tags, detection time) no longer fits a 320 px screen
// on one row; unwrapped, it widened the card and the page scrolled sideways.

// Vitest stubs CSS imports, so the stylesheet is read from disk (tests run
// from frontend/).
const styles = readFileSync(resolve("src/styles.css"), "utf8").replace(
  /\/\*[\s\S]*?\*\//g,
  "",
);

function ruleBody(selector) {
  const rule = [...styles.matchAll(/([^{}]+)\{([^{}]*)\}/g)].find(
    ([, selectors]) =>
      selectors.split(",").some((item) => item.trim() === selector),
  );
  return rule ? rule[2] : "";
}

describe("match card layout", () => {
  it("lets the card's top line wrap on narrow screens", () => {
    expect(ruleBody(".match-topline")).toMatch(/flex-wrap:\s*wrap/);
  });
});
