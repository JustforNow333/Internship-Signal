const minutesAgo = (minutes) =>
  new Date(Date.now() - minutes * 60_000).toISOString();

export function makeHostedFixtures() {
  const companies = [
    {
      id: "stripe",
      name: "Stripe",
      initials: "ST",
      coverage: "direct",
      domain: "stripe.com",
    },
    {
      id: "figma",
      name: "Figma",
      initials: "FI",
      coverage: "direct",
      domain: "figma.com",
    },
    {
      id: "nvidia",
      name: "NVIDIA",
      initials: "NV",
      coverage: "direct",
      domain: "nvidia.com",
    },
    {
      id: "datadog",
      name: "Datadog",
      initials: "DD",
      coverage: "direct",
      domain: "datadoghq.com",
    },
    {
      id: "capital-one",
      name: "Capital One",
      initials: "C1",
      coverage: "direct",
      domain: "capitalone.com",
    },
    {
      id: "notion",
      name: "Notion",
      initials: "NO",
      coverage: "backstop",
      domain: "notion.so",
    },
    {
      id: "cloudflare",
      name: "Cloudflare",
      initials: "CF",
      coverage: "backstop",
      domain: "cloudflare.com",
    },
    {
      id: "spacex",
      name: "SpaceX",
      initials: "SX",
      coverage: "backstop",
      domain: "spacex.com",
    },
    {
      id: "duolingo",
      name: "Duolingo",
      initials: "DU",
      coverage: "direct",
      domain: "duolingo.com",
    },
    {
      id: "disney",
      name: "Disney",
      initials: "DI",
      coverage: "delayed",
      domain: "disney.com",
    },
    {
      id: "roblox",
      name: "Roblox",
      initials: "RX",
      coverage: "direct",
      domain: "roblox.com",
    },
    {
      id: "snowflake",
      name: "Snowflake",
      initials: "SF",
      coverage: "backstop",
      domain: "snowflake.com",
    },
  ];

  const matches = [
    {
      id: "match-stripe-001",
      company_id: "stripe",
      company: "Stripe",
      title: "Software Engineering Intern, Payments",
      role_id: "software-engineering",
      role: "Software Engineering",
      location: "San Francisco, CA",
      remote: false,
      detected_at: minutesAgo(18),
      why: [
        "Software Engineering is in your role preferences",
        "Stripe is on your watchlist",
      ],
      summary:
        "Build reliable payment infrastructure with product and platform engineering teams.",
      responsibilities: [
        "Ship production features with an engineering mentor",
        "Improve the reliability of payment services",
      ],
      qualifications: [
        "Currently pursuing a technical degree",
        "Experience programming in Java, Go, Ruby, or a similar language",
      ],
      source_url: "https://stripe.com/jobs/search",
    },
    {
      id: "match-nvidia-002",
      company_id: "nvidia",
      company: "NVIDIA",
      title: "Machine Learning Systems Intern",
      role_id: "machine-learning-ai",
      role: "Machine Learning / AI",
      location: "Santa Clara, CA",
      remote: false,
      detected_at: minutesAgo(52),
      why: [
        "Machine Learning / AI is in your role preferences",
        "NVIDIA is on your watchlist",
      ],
      summary:
        "Prototype and evaluate systems that make large-scale ML training more efficient.",
      responsibilities: [
        "Build performance experiments",
        "Collaborate with ML systems engineers",
      ],
      qualifications: [
        "Python and C++ experience",
        "Coursework in machine learning or distributed systems",
      ],
      source_url: "https://www.nvidia.com/en-us/about-nvidia/careers/",
    },
    {
      id: "match-figma-003",
      company_id: "figma",
      company: "Figma",
      title: "Data Science Intern, Product Analytics",
      role_id: "data-science",
      role: "Data Science",
      location: "New York, NY",
      remote: true,
      detected_at: minutesAgo(210),
      why: [
        "Data Science is in your role preferences",
        "New York matches a preferred location",
      ],
      summary:
        "Use product data and experimentation to help teams understand collaborative design workflows.",
      responsibilities: [
        "Design product analyses",
        "Present findings to cross-functional partners",
      ],
      qualifications: [
        "SQL proficiency",
        "Statistics or experimentation experience",
      ],
      source_url: "https://www.figma.com/careers/",
    },
    {
      id: "match-datadog-004",
      company_id: "datadog",
      company: "Datadog",
      title: "Product Management Intern, Observability",
      role_id: "product-management",
      role: "Product Management",
      location: "Boston, MA",
      remote: false,
      detected_at: minutesAgo(470),
      why: [
        "Product Management is in your role preferences",
        "Datadog is on your watchlist",
      ],
      summary:
        "Partner with design and engineering to shape observability workflows for developers.",
      responsibilities: [
        "Research developer needs",
        "Define and evaluate a product improvement",
      ],
      qualifications: [
        "Strong written communication",
        "Interest in technical products",
      ],
      source_url: "https://careers.datadoghq.com/",
    },
    {
      id: "match-capital-one-005",
      company_id: "capital-one",
      company: "Capital One",
      title: "Data Engineering Intern",
      role_id: "data-engineering",
      role: "Data Engineering",
      location: "McLean, VA",
      remote: false,
      detected_at: minutesAgo(1_560),
      why: [
        "Data Engineering is in your role preferences",
        "Capital One is on your watchlist",
      ],
      summary:
        "Develop data pipelines that support analytics and customer-facing financial products.",
      responsibilities: [
        "Build tested data workflows",
        "Contribute to cloud data services",
      ],
      qualifications: [
        "Programming fundamentals",
        "Interest in distributed data systems",
      ],
      source_url: "https://www.capitalonecareers.com/",
    },
    {
      id: "match-notion-006",
      company_id: "notion",
      company: "Notion",
      title: "Software Engineer Intern, Core Product",
      role_id: "software-engineering",
      role: "Software Engineering",
      location: "New York, NY",
      remote: false,
      detected_at: minutesAgo(3_200),
      why: [
        "Software Engineering is in your role preferences",
        "Notion is on your watchlist",
      ],
      summary:
        "Work on collaborative product experiences used by teams around the world.",
      responsibilities: [
        "Implement full-stack product features",
        "Participate in design and code review",
      ],
      qualifications: [
        "Experience building software projects",
        "Product-minded problem solving",
      ],
      source_url: "https://www.notion.so/careers",
    },
  ];

  return {
    companies,
    me: { id: "user-demo", email: "alex@example.com", email_verified: true },
    preferences: {
      role_ids: [
        "software-engineering",
        "machine-learning-ai",
        "data-science",
        "data-engineering",
        "product-management",
      ],
      locations: ["United States", "New York, NY"],
      include_remote: true,
      season: "Summer 2027",
      alert_frequency: "asap",
      globally_paused: false,
    },
    watchlist: [
      { company_id: "stripe", paused: false },
      { company_id: "figma", paused: false },
      { company_id: "nvidia", paused: false },
      { company_id: "datadog", paused: false },
      { company_id: "capital-one", paused: false },
    ],
    matches,
    last_successful_scan_at: minutesAgo(12),
  };
}
