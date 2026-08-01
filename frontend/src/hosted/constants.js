export const ROLE_OPTIONS = [
  {
    id: "software-engineering",
    name: "Software Engineering",
    description:
      "Backend, frontend, mobile, platform, and full-stack internships.",
  },
  {
    id: "machine-learning-ai",
    name: "Machine Learning / AI",
    description:
      "Applied AI, ML engineering, research engineering, and model infrastructure.",
  },
  {
    id: "data-science",
    name: "Data Science",
    description:
      "Analytics, experimentation, modeling, and decision science roles.",
  },
  {
    id: "data-engineering",
    name: "Data Engineering",
    description:
      "Data platforms, pipelines, infrastructure, and analytics engineering.",
  },
  {
    id: "quantitative-development",
    name: "Quantitative Development",
    description:
      "Quant engineering, trading systems, and financial modeling roles.",
  },
  {
    id: "product-management",
    name: "Product Management",
    description:
      "Technical product, product strategy, and program internships.",
  },
  {
    id: "hardware-embedded",
    name: "Hardware / Embedded",
    description:
      "Firmware, embedded systems, silicon, robotics, and computer hardware.",
  },
  {
    id: "other-engineering",
    name: "Other Engineering",
    description:
      "Security, cloud, developer tools, and other technical engineering roles.",
  },
];

export const ALERT_FREQUENCIES = [
  {
    id: "asap",
    label: "As soon as detected",
    description: "Send an alert after the scheduled scan that finds a match.",
  },
  {
    id: "three_hours",
    label: "Every 3 hours",
    description: "Bundle recent matches into a short update every three hours.",
  },
  {
    id: "daily",
    label: "Daily summary",
    description: "Receive one daily roundup of new matches.",
  },
  {
    id: "paused",
    label: "Paused",
    description: "Keep your watchlist but stop alert emails.",
  },
];

export const COVERAGE_LABELS = {
  direct: "Directly monitored",
  backstop: "Backstop coverage",
  delayed: "Temporarily delayed",
};

export const LOCATION_OPTIONS = [
  "United States",
  "New York, NY",
  "San Francisco Bay Area",
  "Seattle, WA",
  "Boston, MA",
];

export const SEASON_OPTIONS = [
  "Summer 2027",
  "Fall 2026",
  "Spring 2027",
  "Any season",
];
