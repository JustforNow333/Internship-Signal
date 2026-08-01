import { useEffect, useState } from "react";
import {
  ALERT_FREQUENCIES,
  LOCATION_OPTIONS,
  ROLE_OPTIONS,
  SEASON_OPTIONS,
} from "./constants.js";
import { ConfirmationDialog, SuccessNotice, Toggle } from "./ui.jsx";
import { toggleSelection } from "./utils.js";

export default function SettingsPage({
  me,
  preferences,
  savePreferences,
  navigate,
}) {
  const [form, setForm] = useState(preferences);
  const [saving, setSaving] = useState(false);
  const [notice, setNotice] = useState("");
  const [error, setError] = useState("");
  const [dialog, setDialog] = useState(null);
  useEffect(() => setForm(preferences), [preferences]);

  const toggleRole = (id) =>
    setForm({ ...form, role_ids: toggleSelection(form.role_ids, id) });
  const toggleLocation = (location) =>
    setForm({ ...form, locations: toggleSelection(form.locations, location) });
  const save = async (event) => {
    event.preventDefault();
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
  const confirmAction = async () => {
    if (dialog === "unsubscribe") {
      const next = {
        ...form,
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
        setDialog(null);
      } catch (saveError) {
        setError(
          saveError.message || "Email alerts could not be unsubscribed.",
        );
      } finally {
        setSaving(false);
      }
    } else if (dialog === "delete") {
      setDialog(null);
      navigate("/");
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
                    form.locations.includes(location) ? "selected" : ""
                  }
                >
                  <input
                    type="checkbox"
                    checked={form.locations.includes(location)}
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
                value={form.season}
                onChange={(event) =>
                  setForm({ ...form, season: event.target.value })
                }
              >
                {SEASON_OPTIONS.map((season) => (
                  <option key={season}>{season}</option>
                ))}
              </select>
            </label>
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
              disabled={saving || !form.role_ids.length}
            >
              {saving ? "Saving…" : "Save changes"}
            </button>
          </div>
        </div>
        <aside className="settings-sidebar">
          <section className="settings-card danger-zone">
            <h2>Account controls</h2>
            <button type="button" onClick={() => setDialog("unsubscribe")}>
              <span>
                <strong>Unsubscribe from alerts</strong>
                <small>Stop email delivery and keep the account.</small>
              </span>
              <span aria-hidden="true">→</span>
            </button>
            <button type="button" onClick={() => setDialog("delete")}>
              <span>
                <strong>Delete account</strong>
                <small>Permanently remove the account and watchlist.</small>
              </span>
              <span aria-hidden="true">→</span>
            </button>
          </section>
        </aside>
      </form>
      <ConfirmationDialog
        open={dialog === "unsubscribe"}
        title="Unsubscribe from all alerts?"
        confirmLabel="Unsubscribe"
        tone="danger"
        onClose={() => setDialog(null)}
        onConfirm={confirmAction}
      >
        <p>
          You will stop receiving match emails. Your account and watchlist will
          remain available if you return.
        </p>
      </ConfirmationDialog>
      <ConfirmationDialog
        open={dialog === "delete"}
        title="Delete your account permanently?"
        confirmLabel="Delete account"
        tone="danger"
        onClose={() => setDialog(null)}
        onConfirm={confirmAction}
      >
        <p>
          This permanently deletes your preferences and watchlist. The hosted
          backend must enforce this action when connected; it cannot be undone.
        </p>
      </ConfirmationDialog>
    </>
  );
}
