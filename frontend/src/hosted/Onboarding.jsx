import { useState } from "react";
import CompanyCatalog from "./CompanyCatalog.jsx";
import {
  ALERT_FREQUENCIES,
  LOCATION_OPTIONS,
  ROLE_OPTIONS,
  SEASON_OPTIONS,
} from "./constants.js";
import { alertFrequencyLabel, Brand, Toggle } from "./ui.jsx";
import { toggleSelection } from "./utils.js";

function Progress({ step }) {
  const labels = ["Roles", "Companies", "Alerts"];
  return (
    <div
      className="onboarding-progress"
      aria-label={`Onboarding step ${step} of 3`}
    >
      <div className="progress-copy">
        <span>SET UP YOUR SIGNAL</span>
        <strong>Step {step} of 3</strong>
      </div>
      <ol>
        {labels.map((label, index) => {
          const number = index + 1;
          return (
            <li
              key={label}
              className={
                number === step ? "active" : number < step ? "complete" : ""
              }
            >
              <span>{number < step ? "✓" : number}</span>
              <small>{label}</small>
            </li>
          );
        })}
      </ol>
    </div>
  );
}

function RolesStep({ roleIds, setRoleIds, onNext }) {
  const toggle = (id) => setRoleIds(toggleSelection(roleIds, id));
  return (
    <section className="onboarding-card" aria-labelledby="onboarding-title">
      <div className="onboarding-heading">
        <p className="eyebrow">Step 1 · Role preferences</p>
        <h1 id="onboarding-title">
          What kind of internships are you looking for?
        </h1>
        <p>
          Select every category you want us to match. You can change these
          later.
        </p>
      </div>
      <div className="role-grid">
        {ROLE_OPTIONS.map((role) => {
          const selected = roleIds.includes(role.id);
          return (
            <label
              className={`role-option ${selected ? "selected" : ""}`}
              key={role.id}
            >
              <input
                type="checkbox"
                checked={selected}
                onChange={() => toggle(role.id)}
              />
              <span className="role-check" aria-hidden="true">
                {selected ? "✓" : ""}
              </span>
              <span>
                <strong>{role.name}</strong>
                <small>{role.description}</small>
              </span>
            </label>
          );
        })}
      </div>
      <div className="onboarding-actions">
        <p>
          {roleIds.length
            ? `${roleIds.length} role ${roleIds.length === 1 ? "category" : "categories"} selected`
            : "Select at least one role category"}
        </p>
        <button
          className="primary large"
          disabled={!roleIds.length}
          onClick={onNext}
        >
          Continue to companies <span aria-hidden="true">→</span>
        </button>
      </div>
    </section>
  );
}

function CompaniesStep({
  companies,
  companyIds,
  setCompanyIds,
  onBack,
  onNext,
}) {
  const toggle = (id) => setCompanyIds(toggleSelection(companyIds, id));
  return (
    <section
      className="onboarding-card wide"
      aria-labelledby="onboarding-title"
    >
      <div className="onboarding-heading">
        <p className="eyebrow">Step 2 · Your company watchlist</p>
        <h1 id="onboarding-title">Which companies should we watch for you?</h1>
        <p>
          Choose from the companies currently supported. This is your personal
          watchlist—add only the employers you care about.
        </p>
      </div>
      <CompanyCatalog
        companies={companies}
        selectedIds={companyIds}
        onToggle={toggle}
      />
      <div className="onboarding-actions">
        <button className="ghost-back" onClick={onBack}>
          ← Back
        </button>
        <button
          className="primary large"
          disabled={!companyIds.length}
          onClick={onNext}
        >
          Continue to alerts <span aria-hidden="true">→</span>
        </button>
      </div>
    </section>
  );
}

function AlertsStep({
  preferences,
  setPreferences,
  onBack,
  onFinish,
  saving,
  error,
}) {
  const toggleLocation = (location) =>
    setPreferences({
      ...preferences,
      locations: toggleSelection(preferences.locations, location),
    });
  return (
    <section className="onboarding-card" aria-labelledby="onboarding-title">
      <div className="onboarding-heading">
        <p className="eyebrow">Step 3 · Alert preferences</p>
        <h1 id="onboarding-title">When and where should we look?</h1>
        <p>Fine-tune the openings that reach your inbox.</p>
      </div>
      {error && (
        <div className="error-banner" role="alert">
          {error}
        </div>
      )}
      <fieldset className="preference-section">
        <legend>Preferred locations</legend>
        <p>
          Select all that apply. City selections narrow matching without
          removing broader U.S. options.
        </p>
        <div className="choice-chips">
          {LOCATION_OPTIONS.map((location) => (
            <label
              key={location}
              className={
                preferences.locations.includes(location) ? "selected" : ""
              }
            >
              <input
                type="checkbox"
                checked={preferences.locations.includes(location)}
                onChange={() => toggleLocation(location)}
              />
              {location}
            </label>
          ))}
        </div>
      </fieldset>
      <div className="preference-section">
        <Toggle
          label="Include remote roles"
          description="Include postings explicitly listed as remote in the United States."
          checked={preferences.include_remote}
          onChange={(checked) =>
            setPreferences({ ...preferences, include_remote: checked })
          }
        />
      </div>
      <label className="field preference-section">
        <span className="field-legend">Internship season</span>
        <select
          value={preferences.season}
          onChange={(event) =>
            setPreferences({ ...preferences, season: event.target.value })
          }
        >
          {SEASON_OPTIONS.map((season) => (
            <option key={season}>{season}</option>
          ))}
        </select>
      </label>
      <fieldset className="preference-section">
        <legend>Alert frequency</legend>
        <p>
          “As soon as detected” sends after scheduled scans. Internship Signal
          does not monitor career pages continuously in real time.
        </p>
        <div className="radio-stack">
          {ALERT_FREQUENCIES.map((frequency) => (
            <label
              key={frequency.id}
              className={
                preferences.alert_frequency === frequency.id ? "selected" : ""
              }
            >
              <input
                type="radio"
                name="alert-frequency"
                value={frequency.id}
                checked={preferences.alert_frequency === frequency.id}
                onChange={() =>
                  setPreferences({
                    ...preferences,
                    alert_frequency: frequency.id,
                  })
                }
              />
              <span>
                <strong>{frequency.label}</strong>
                <small>{frequency.description}</small>
              </span>
            </label>
          ))}
        </div>
      </fieldset>
      <div className="onboarding-actions">
        <button className="ghost-back" onClick={onBack}>
          ← Back
        </button>
        <button
          className="primary large"
          disabled={
            saving ||
            (!preferences.locations.length && !preferences.include_remote)
          }
          onClick={onFinish}
        >
          {saving ? "Activating watchlist…" : "Activate my watchlist"}
        </button>
      </div>
    </section>
  );
}

function CompleteStep({ navigate, companyCount, roleCount, alertFrequency }) {
  return (
    <section
      className="onboarding-card confirmation-card"
      aria-labelledby="onboarding-title"
    >
      <span className="confirmation-mark" aria-hidden="true">
        ✓
      </span>
      <p className="eyebrow">Your signal is live</p>
      <h1 id="onboarding-title">Your watchlist is active.</h1>
      <p>
        We’ll monitor your {companyCount} selected{" "}
        {companyCount === 1 ? "company" : "companies"} for {roleCount} role{" "}
        {roleCount === 1 ? "category" : "categories"}. You’ll get notified
        shortly after a scheduled scan detects a new opening that matches.
      </p>
      <div className="confirmation-summary">
        <span>
          <strong>{companyCount}</strong>{" "}
          {companyCount === 1 ? "company" : "companies"} watched
        </span>
        <span>
          <strong>{alertFrequencyLabel(alertFrequency)}</strong> alert frequency
        </span>
      </div>
      <button
        className="primary large"
        onClick={() => navigate("/app/dashboard")}
      >
        Go to my dashboard <span aria-hidden="true">→</span>
      </button>
      <small>
        Coverage is limited to supported companies and may vary by source.
      </small>
    </section>
  );
}

export default function Onboarding({
  navigate,
  companies,
  savePreferences,
  saveWatchlist,
}) {
  const [step, setStep] = useState(1);
  const [roleIds, setRoleIds] = useState([]);
  const [companyIds, setCompanyIds] = useState([]);
  const [preferences, setPreferences] = useState({
    locations: ["United States"],
    include_remote: true,
    season: "Summer 2027",
    alert_frequency: "asap",
    globally_paused: false,
  });
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState("");

  const finish = async () => {
    setSaving(true);
    setError("");
    try {
      await Promise.all([
        savePreferences({ ...preferences, role_ids: roleIds }),
        saveWatchlist(
          companyIds.map((companyId) => ({
            company_id: companyId,
            paused: false,
          })),
        ),
      ]);
      setStep(4);
    } catch (saveError) {
      setError(saveError.message || "We couldn’t activate your watchlist.");
    } finally {
      setSaving(false);
    }
  };

  return (
    <div className="onboarding-page">
      <header className="onboarding-header">
        <Brand navigate={navigate} />
        <button className="text-button" onClick={() => navigate("/signin")}>
          Exit setup
        </button>
      </header>
      <main className="onboarding-main">
        {step <= 3 && <Progress step={step} />}
        {step === 1 && (
          <RolesStep
            roleIds={roleIds}
            setRoleIds={setRoleIds}
            onNext={() => setStep(2)}
          />
        )}
        {step === 2 && (
          <CompaniesStep
            companies={companies}
            companyIds={companyIds}
            setCompanyIds={setCompanyIds}
            onBack={() => setStep(1)}
            onNext={() => setStep(3)}
          />
        )}
        {step === 3 && (
          <AlertsStep
            preferences={preferences}
            setPreferences={setPreferences}
            onBack={() => setStep(2)}
            onFinish={finish}
            saving={saving}
            error={error}
          />
        )}
        {step === 4 && (
          <CompleteStep
            navigate={navigate}
            companyCount={companyIds.length}
            roleCount={roleIds.length}
            alertFrequency={preferences.alert_frequency}
          />
        )}
      </main>
    </div>
  );
}
