import { render, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";
import ScoreBadge from "../components/ScoreBadge.jsx";

describe("ScoreBadge", () => {
  it("renders the score as an accessible meter", () => {
    render(<ScoreBadge score={87} bucket="high" />);
    const meter = screen.getByRole("meter");
    expect(meter).toHaveAttribute("aria-valuenow", "87");
    expect(meter.className).toContain("high");
    expect(screen.getByText("87")).toBeInTheDocument();
  });

  it("applies the bucket tone class", () => {
    render(<ScoreBadge score={32} bucket="low" />);
    expect(screen.getByRole("meter").className).toContain("low");
  });
});
