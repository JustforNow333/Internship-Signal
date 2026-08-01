import { Brand, RouteLink } from "./ui.jsx";

function AlertExample() {
  return (
    <article
      className="email-example"
      aria-label="Example internship alert email"
    >
      <div className="email-toolbar">
        <span />
        <span />
        <span />
      </div>
      <div className="email-meta">
        <span className="email-logo">IS</span>
        <span>
          <strong>Internship Signal</strong>
          <small>to you</small>
        </span>
        <time>10:18 AM</time>
      </div>
      <div className="email-content">
        <p className="email-kicker">NEW WATCHLIST MATCH</p>
        <h3>Software Engineering Intern</h3>
        <p className="email-company">Stripe · San Francisco, CA</p>
        <div className="email-reason">
          ✓ Matches Software Engineering
          <br />✓ Stripe is on your watchlist
        </div>
        <a
          href="https://stripe.com/jobs/search"
          target="_blank"
          rel="noreferrer"
          className="button-link primary-link"
        >
          Apply now ↗
        </a>
        <small>Detected 18 minutes ago during a scheduled scan</small>
      </div>
    </article>
  );
}
export default function LandingPage({ navigate }) {
  return (
    <div className="public-page">
      <header className="public-header">
        <Brand navigate={navigate} />
        <nav aria-label="Account">
          <RouteLink to="/signin" navigate={navigate} className="text-link">
            Sign in
          </RouteLink>
          <RouteLink
            to="/signup"
            navigate={navigate}
            className="button-link primary-link"
          >
            Create your watchlist
          </RouteLink>
        </nav>
      </header>

      <main>
        <section className="hero">
          <div className="hero-copy">
            <p className="eyebrow">
              <span className="live-dot" />
              Built for internship recruiting
            </p>
            <h1>Never apply late again.</h1>
            <p className="hero-lede">
              Choose the companies and roles you care about. Internship Signal
              monitors supported career pages for new internships and emails you
              shortly after we detect a match.
            </p>
            <div className="hero-actions">
              <RouteLink
                to="/signup"
                navigate={navigate}
                className="button-link primary-link large"
              >
                Create your watchlist <span aria-hidden="true">→</span>
              </RouteLink>
              <RouteLink
                to="/signin"
                navigate={navigate}
                className="button-link secondary-link large"
              >
                Sign in
              </RouteLink>
            </div>
            <p className="honest-note">
              Currently monitors supported companies only. Coverage and scan
              timing vary by career site.
            </p>
          </div>
          <div className="hero-visual">
            <div className="radar-ring ring-one" />
            <div className="radar-ring ring-two" />
            <div className="signal-card signal-card-one">
              <span className="company-mark">NV</span>
              <span>
                <strong>Machine Learning Intern</strong>
                <small>NVIDIA · just detected</small>
              </span>
            </div>
            <div className="signal-card signal-card-two">
              <span className="company-mark">ST</span>
              <span>
                <strong>Software Engineering Intern</strong>
                <small>Stripe · 18 min ago</small>
              </span>
            </div>
            <div className="signal-card signal-card-three">
              <span className="company-mark">FI</span>
              <span>
                <strong>Data Science Intern</strong>
                <small>Figma · new today</small>
              </span>
            </div>
          </div>
        </section>

        <section className="steps-section" aria-labelledby="how-it-works">
          <div className="section-heading">
            <p className="eyebrow">A focused early-warning system</p>
            <h2 id="how-it-works">Three choices, then we keep watch.</h2>
          </div>
          <ol className="steps-grid">
            <li>
              <span className="step-number">01</span>
              <h3>Choose companies</h3>
              <p>
                Build your own watchlist from the employers we currently
                support.
              </p>
            </li>
            <li>
              <span className="step-number">02</span>
              <h3>Select roles</h3>
              <p>
                Tell us which internship categories and locations are relevant
                to you.
              </p>
            </li>
            <li>
              <span className="step-number">03</span>
              <h3>Receive early alerts</h3>
              <p>
                Get notified shortly after a scheduled scan detects a new match.
              </p>
            </li>
          </ol>
        </section>

        <section
          className="alert-showcase"
          aria-labelledby="alert-preview-title"
        >
          <div className="alert-showcase-copy">
            <p className="eyebrow">Fast to read. Faster to act on.</p>
            <h2 id="alert-preview-title">
              A useful signal, straight to your inbox.
            </h2>
            <p>
              Every alert leads with the employer’s application link and
              explains why the opening matched your watchlist—without dashboards
              full of vanity metrics.
            </p>
            <ul className="check-list">
              <li>Employer and role match context</li>
              <li>Detection time at a glance</li>
              <li>One-click access to the source posting</li>
            </ul>
          </div>
          <AlertExample />
        </section>

        <section className="coverage-callout">
          <span className="coverage-icon" aria-hidden="true">
            ◎
          </span>
          <div>
            <h2>Deliberate coverage, clearly labeled.</h2>
            <p>
              Internship Signal currently monitors a supported catalog of
              companies. Your company picker shows whether each source is
              directly monitored, covered by a backstop, or temporarily delayed.
            </p>
          </div>
          <RouteLink
            to="/signup"
            navigate={navigate}
            className="button-link secondary-link"
          >
            Browse supported companies
          </RouteLink>
        </section>
      </main>

      <footer className="public-footer">
        <Brand navigate={navigate} compact />
        <p>Early signals for internship recruiting.</p>
        <p>Coverage is limited to supported companies.</p>
      </footer>
    </div>
  );
}
