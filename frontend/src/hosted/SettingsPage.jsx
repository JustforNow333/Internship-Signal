import { useEffect, useState } from "react";
import {
  ALERT_FREQUENCIES,
  CAREER_LEVEL_OPTIONS,
  LOCATION_OPTIONS,
  ROLE_OPTIONS,
  SEASON_OPTIONS,
} from "./constants.js";
import { ConfirmationDialog, SuccessNotice, Toggle } from "./ui.jsx";
import { careerLevelsOrDefault, toggleSelection } from "./utils.js";

function withCareerLevels(preferences) {
  return {
    ...preferences,
    career_levels: careerLevelsOrDefault(preferences.career_levels),
  };
}

export default function SettingsPage({ me, preferences, savePreferences }) {
  const [form, setForm] = useState(() => withCareerLevels(preferences));
  const [saving, setSaving] = useState(false);
  const [notice, setNotice] = useState("");
  const [error, setError] = useState("");
  const [unsubscribeOpen, setUnsubscribeOpen] = useState(false);
  useEffect(() => setForm(withCareerLevels(preferences)), [preferences]);

  // The season is kept (not cleared) while internships are deselected so it
  // returns intact if they are selected again.
  const internshipsSelected = form.career_levels.includes("internship");
  const toggleCareerLevel = (id) =>
    setForm({
      ...form,
      career_levels: toggleSelection(form.career_levels, id),
    });
  const toggleRole = (id) =>
    setForm({ ...form, role_ids: toggleSelection(form.role_ids, id) });
  const toggleLocation = (location) =>
    setForm({
      ...form,
      preferred_locations: toggleSelection(form.preferred_locations, location),
    });
  const save = async (event) => {
    event.preventDefault();
    if (!form.career_levels.length) return;
    setSaving(true);
    setNotice("");
    setError("");
    try {
      await savePreferences(form);
      setNotice("Your alert preferences have been saved.");
    } catch (saveError) {
      setError(saveError.message || "Your settings could not be saved.");
    } finally {
      setSaving(false);
    }
  };
  const confirmUnsubscribe = async () => {
    // Unsubscribing changes delivery only, so it starts from the last
    // persisted preferences rather than possibly incomplete unsaved edits.
    const next = {
      ...withCareerLevels(preferences),
      alert_frequency: "paused",
      globally_paused: true,
    };
    setForm(next);
    setSaving(true);
    setError("");
    try {
      await savePreferences(next);
      setNotice(
        "Email alerts are now unsubscribed. Your watchlist remains available.",
      );
      setUnsubscribeOpen(false);
    } catch (saveError) {
      setError(saveError.message || "Email alerts could not be unsubscribed.");
    } finally {
      setSaving(false);
    }
  };

  return (
    <>
      <div className="page-heading">
        <div>
          <p className="eyebrow">Account and delivery</p>
          <h1>Settings</h1>
          <p>Manage your match preferences and email alert delivery.</p>
        </div>
      </div>
      {notice && <SuccessNotice>{notice}</SuccessNotice>}
      {error && (
        <div className="error-banner" role="alert">
          {error}
        </div>
      )}
      <form className="settings-layout" onSubmit={save}>
        <div className="settings-main">
          <section className="settings-card">
            <div className="settings-card-heading">
              <span aria-hidden="true">@</span>
              <div>
                <h2>Account email</h2>
                <p>Used for sign-in and internship alerts.</p>
              </div>
            </div>
            <label className="field">
              <span>Email address</span>
              <input type="email" value={me.email} disabled />
              <small>Contact support to change your sign-in email.</small>
            </label>
          </section>
          <section className="settings-card">
            <div className="settings-card-heading">
              <span aria-hidden="true">◎</span>
              <div>
                <h2>Career stages</h2>
                <p>Choose every career stage that should match.</p>
              </div>
            </div>
            <div className="settings-role-grid">
              {CAREER_LEVEL_OPTIONS.map((stage) => (
                <label
                  key={stage.id}
                  className={
                    form.career_levels.includes(stage.id) ? "selected" : ""
                  }
                  title={stage.description}
                >
                  <input
                    type="checkbox"
                    checked={form.career_levels.includes(stage.id)}
                    onChange={() => toggleCareerLevel(stage.id)}
                  />
                  <span aria-hidden="true">
                    {form.career_levels.includes(stage.id) ? "✓" : ""}
                  </span>
                  {stage.name}
                </label>
              ))}
            </div>
            {!form.career_levels.length && (
              <p className="scan-note">Select at least one career stage.</p>
            )}
          </section>
          <section className="settings-card">
            <div className="settings-card-heading">
              <span aria-hidden="true">⌁</span>
              <div>
                <h2>Role preferences</h2>
                <p>Choose all internship categories that should match.</p>
              </div>
            </div>
            <div className="settings-role-grid">
              {ROLE_OPTIONS.map((role) => (
                <label
                  key={role.id}
                  className={form.role_ids.includes(role.id) ? "selected" : ""}
                >
                  <input
                    type="checkbox"
                    checked={form.role_ids.includes(role.id)}
                    onChange={() => toggleRole(role.id)}
                  />
                  <span aria-hidden="true">
                    {form.role_ids.includes(role.id) ? "✓" : ""}
                  </span>
                  {role.name}
                </label>
              ))}
            </div>
          </section>
          <section className="settings-card">
            <div className="settings-card-heading">
              <span aria-hidden="true">⌖</span>
              <div>
                <h2>Location preferences</h2>
                <p>Set the places that should appear in your alerts.</p>
              </div>
            </div>
            <div className="choice-chips">
              {LOCATION_OPTIONS.map((location) => (
                <label
                  key={location}
                  className={
                    form.preferred_locations.includes(location)
                      ? "selected"
                      : ""
                  }
                >
                  <input
                    type="checkbox"
                    checked={form.preferred_locations.includes(location)}
                    onChange={() => toggleLocation(location)}
                  />
                  {location}
                </label>
              ))}
            </div>
            <Toggle
              checked={form.include_remote}
              onChange={(checked) =>
                setForm({ ...form, include_remote: checked })
              }
              label="Include remote roles"
              description="Include postings explicitly listed as remote in the United States."
            />
            <label className="field season-field">
              <span>Internship season</span>
              <select
                disabled={!internshipsSelected}
                value={form.internship_season}
                onChange={(event) =>
                  setForm({ ...form, internship_season: event.target.value })
                }
              >
                {SEASON_OPTIONS.map((season) => (
                  <option key={season}>{season}</option>
                ))}
              </select>
              {!internshipsSelected && (
                <small>
                  Applies only to internship matches. Select Internships under
                  Career stages to change it.
                </small>
              )}
            </label>
            <Toggle
              checked={form.include_recent_openings}
              onChange={(checked) =>
                setForm({ ...form, include_recent_openings: checked })
              }
              label="Include recent openings"
              description="When you add a company, show matching openings posted within the last 90 days."
            />
          </section>
          <section className="settings-card">
            <div className="settings-card-heading">
              <span aria-hidden="true">✉</span>
              <div>
                <h2>Alert delivery</h2>
                <p>Choose how often matching openings reach your inbox.</p>
              </div>
            </div>
            <div className="radio-stack compact-radios">
              {ALERT_FREQUENCIES.map((frequency) => (
                <label
                  key={frequency.id}
                  className={
                    form.alert_frequency === frequency.id ? "selected" : ""
                  }
                >
                  <input
                    type="radio"
                    name="settings-frequency"
                    checked={form.alert_frequency === frequency.id}
                    onChange={() =>
                      setForm({ ...form, alert_frequency: frequency.id })
                    }
                  />
                  <span>
                    <strong>{frequency.label}</strong>
                    <small>{frequency.description}</small>
                  </span>
                </label>
              ))}
            </div>
            <p className="scan-note">
              Alerts are sent after scheduled scans, not through continuous
              real-time monitoring.
            </p>
            <Toggle
              checked={form.globally_paused}
              onChange={(checked) =>
                setForm({ ...form, globally_paused: checked })
              }
              label="Global pause"
              description="Pause all monitoring alerts while preserving every preference."
            />
          </section>
          <div className="settings-save">
            <button
              className="primary large"
              disabled={
                saving || !form.role_ids.length || !form.career_levels.length
              }
            >
              {saving ? "Saving…" : "Save changes"}
            </button>
          </div>
        </div>
        <aside className="settings-sidebar">
          <section className="settings-card danger-zone">
            <h2>Account controls</h2>
            <button type="button" onClick={() => setUnsubscribeOpen(true)}>
              <span>
                <strong>Unsubscribe from alerts</strong>
                <small>Stop email delivery and keep the account.</small>
              </span>
              <span aria-hidden="true">→</span>
            </button>
            <button type="button" disabled>
              <span>
                <strong>Delete account</strong>
                <small>Account deletion is not available yet.</small>
              </span>
            </button>
          </section>
        </aside>
      </form>
      <ConfirmationDialog
        open={unsubscribeOpen}
        title="Unsubscribe from all alerts?"
        confirmLabel="Unsubscribe"
        tone="danger"
        onClose={() => setUnsubscribeOpen(false)}
        onConfirm={confirmUnsubscribe}
      >
        <p>
          You will stop receiving match emails. Your account and watchlist will
          remain available if you return.
        </p>
      </ConfirmationDialog>
    </>
  );
}
