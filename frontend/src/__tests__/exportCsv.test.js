import { describe, expect, it } from "vitest";
import { jobsToCsv } from "../utils/exportCsv.js";

const job = (over = {}) => ({
  company: "Acme",
  title: "Software Engineer Intern",
  location: "New York, NY",
  remote_status: "",
  deadline: "",
  deadline_days_left: -2,
  source_url: "https://example.test/job",
  score: { total: 80, bucket: "high", action_label: "Apply now" },
  compensation: { kind: "paid", usd_hourly_min: 30, usd_hourly_max: 30 },
  role_classification: { label: "Software engineering" },
  company_classification: { category: "tech" },
  red_flags: [],
  positive_signals: [],
  ...over,
});

describe("jobsToCsv", () => {
  it("neutralizes spreadsheet formulas in untrusted text fields", () => {
    const csv = jobsToCsv([
      job({
        company: "=HYPERLINK(\"https://evil.test\",\"click\")",
        title: "+cmd|' /C calc'!A0",
        location: "\n@SUM(1,1)",
      }),
    ]);

    expect(csv).toContain(`"'=HYPERLINK(""https://evil.test"",""click"")"`);
    expect(csv).toContain(`'+cmd|' /C calc'!A0`);
    expect(csv).toContain(`"'\n@SUM(1,1)"`);
    expect(csv).toContain(",-2,");
  });
});
