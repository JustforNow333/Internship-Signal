import {
  act,
  fireEvent,
  render,
  screen,
  waitFor,
  within,
} from "@testing-library/react";
import { describe, expect, it } from "vitest";

import App from "../App.jsx";
import { createMockHostedApi } from "../hosted/api.js";
import { makeHostedFixtures } from "../hosted/fixtures.js";

// Undo after unsaving from Saved, and keyboard focus after dismiss, restore,
// unsave, Undo and closing the detail drawer.

// The demo client, with updateMatch calls held until the test settles them.
function heldClient() {
  const fixtures = makeHostedFixtures();
  // A second save, still active, besides the demo's closed one.
  fixtures.matches.find((match) => match.id === "match-nvidia-002").saved =
    true;
  // A second dismissal besides the demo's one.
  Object.assign(
    fixtures.matches.find((match) => match.id === "match-capital-one-005"),
    { dismissed: true },
  );
  const client = createMockHostedApi({ fixtures });
  const held = [];
  const updateMatch = client.updateMatch;
  client.updateMatch = (id, changes) =>
    new Promise((resolve, reject) => {
      held.push({
        id,
        changes,
        succeed: () => updateMatch(id, changes).then(resolve, reject),
        fail: () => reject(new Error("The change could not be saved.")),
      });
    });
  return { client, held };
}

const list = () => screen.getByRole("region", { name: "Job matches" });
const viewButton = (name) =>
  within(screen.getByRole("group", { name: "Match view" })).getByRole(
    "button",
    { name },
  );
const card = (title) =>
  within(list())
    .getAllByRole("article")
    .find((item) => within(item).queryByRole("heading", { name: title }));
const titles = () =>
  within(list())
    .queryAllByRole("article")
    .map((item) => within(item).getByRole("heading").textContent);
const titleButton = (title) =>
  within(card(title)).getByRole("button", { name: title });
// A keyboard press: the button has focus, then activates.
const press = (button) => {
  button.focus();
  fireEvent.click(button);
};
const settle = async (call, how = "succeed") => {
  await act(async () => {
    await call[how]();
  });
};

async function openView(name) {
  const { client, held } = heldClient();
  render(<App initialPath="/app/matches" client={client} />);
  await screen.findAllByRole("article");
  if (name !== "Active") {
    fireEvent.click(viewButton(name));
    await waitFor(() => expect(titles().length).toBeGreaterThan(0));
  }
  return held;
}

const CLOSED = "Software Engineering Intern, Editor"; // closed, no longer matches
const ACTIVE_SAVE = "Machine Learning Systems Intern";

describe("Undo after unsaving from Saved", () => {
  it("removes the match only on success, then Undo saves it again", async () => {
    const held = await openView("Saved");
    press(within(card(CLOSED)).getByRole("button", { name: "★ Saved" }));

    expect(held).toHaveLength(1);
    expect(held[0].changes).toEqual({ saved: false });
    expect(titles()).toContain(CLOSED);
    expect(screen.queryByRole("status")).not.toBeInTheDocument();

    await settle(held[0]);
    expect(titles()).not.toContain(CLOSED);
    const notice = screen.getByRole("status");
    expect(notice).toHaveTextContent(`Removed “${CLOSED}” from Saved.`);
    const undo = within(notice).getByRole("button", { name: "Undo" });
    expect(undo).toHaveFocus();

    press(undo);
    expect(held[1].id).toBe(held[0].id);
    expect(held[1].changes).toEqual({ saved: true });
    await settle(held[1]);
    expect(titles()).toContain(CLOSED);
    expect(screen.queryByRole("status")).not.toBeInTheDocument();
    expect(titleButton(CLOSED)).toHaveFocus();
  });

  it("keeps the match and shows no notice when unsaving fails", async () => {
    const held = await openView("Saved");
    const saved = within(card(CLOSED)).getByRole("button", { name: "★ Saved" });
    press(saved);
    await settle(held[0], "fail");

    expect(titles()).toContain(CLOSED);
    expect(screen.queryByRole("status")).not.toBeInTheDocument();
    expect(screen.getByRole("alert")).toHaveTextContent(
      "The change could not be saved.",
    );
    expect(saved).toHaveFocus();
  });

  it("keeps the notice when Undo fails, so it can be retried", async () => {
    const held = await openView("Saved");
    press(within(card(CLOSED)).getByRole("button", { name: "★ Saved" }));
    await settle(held[0]);
    const undo = within(screen.getByRole("status")).getByRole("button", {
      name: "Undo",
    });
    press(undo);
    await settle(held[1], "fail");

    expect(titles()).not.toContain(CLOSED);
    expect(screen.getByRole("alert")).toHaveTextContent(
      "The change could not be saved.",
    );
    expect(screen.getByRole("status")).toHaveTextContent("Removed");
    expect(undo).toHaveFocus();
  });

  it("ignores repeat clicks while pending and offers Undo for the latest", async () => {
    const held = await openView("Saved");
    const first = within(card(CLOSED)).getByRole("button", { name: "★ Saved" });
    fireEvent.click(first);
    fireEvent.click(first);
    fireEvent.click(
      within(card(ACTIVE_SAVE)).getByRole("button", { name: "★ Saved" }),
    );
    expect(held.map((call) => call.changes)).toEqual([
      { saved: false },
      { saved: false },
    ]);

    await settle(held[1]);
    await settle(held[0]);
    expect(titles()).toEqual([]);
    // The notice is for the change that finished last.
    expect(screen.getByRole("status")).toHaveTextContent(CLOSED);
  });

  it("shows no notice when the view changed before the response", async () => {
    const held = await openView("Saved");
    fireEvent.click(
      within(card(ACTIVE_SAVE)).getByRole("button", { name: "★ Saved" }),
    );
    fireEvent.click(viewButton("Active"));
    await settle(held[0]);

    expect(screen.queryByRole("status")).not.toBeInTheDocument();
    expect(
      within(card(ACTIVE_SAVE)).getByRole("button", { name: "☆ Save" }),
    ).toBeInTheDocument();
  });

  it("offers no Undo when unsaving in Active, where the card stays", async () => {
    const held = await openView("Active");
    const saved = within(card(ACTIVE_SAVE)).getByRole("button", {
      name: "★ Saved",
    });
    press(saved);
    await settle(held[0]);

    expect(screen.queryByRole("status")).not.toBeInTheDocument();
    expect(saved).toHaveTextContent("☆ Save");
    expect(saved).toHaveFocus();
  });
});

describe("Keyboard focus after match actions", () => {
  it("moves to Undo after dismissing, then back to the card", async () => {
    const held = await openView("Active");
    const [, title] = titles();
    press(within(card(title)).getByRole("button", { name: "Dismiss" }));
    await settle(held[0]);

    const undo = within(screen.getByRole("status")).getByRole("button", {
      name: "Undo",
    });
    expect(undo).toHaveFocus();
    press(undo);
    await settle(held[1]);
    expect(titleButton(title)).toHaveFocus();
  });

  it("moves to the next card after a restore, then to the view", async () => {
    const held = await openView("Dismissed");
    const [first, second] = titles();
    press(within(card(first)).getByRole("button", { name: "Restore" }));
    await settle(held[0]);
    expect(titles()).toEqual([second]);
    expect(titleButton(second)).toHaveFocus();

    press(within(card(second)).getByRole("button", { name: "Restore" }));
    await settle(held[1]);
    // No card is left to focus.
    expect(titles()).toEqual([]);
    expect(viewButton("Dismissed")).toHaveFocus();
  });

  it("returns focus to the opener when the detail drawer closes", async () => {
    await openView("Active");
    const [title] = titles();
    const details = within(card(title)).getByRole("button", {
      name: "View details",
    });
    press(details);
    const close = await screen.findByRole("button", {
      name: "Close match details",
    });
    expect(close).toHaveFocus();

    fireEvent.keyDown(window, { key: "Escape" });
    await waitFor(() =>
      expect(screen.queryByRole("dialog")).not.toBeInTheDocument(),
    );
    expect(details).toHaveFocus();
  });
});
