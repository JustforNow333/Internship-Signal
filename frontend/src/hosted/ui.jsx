import { useEffect, useId, useRef, useState } from "react";
import { ALERT_FREQUENCIES, COVERAGE_LABELS } from "./constants.js";

function handleClientNavigation(event, navigate, path) {
  const modified =
    event.metaKey || event.ctrlKey || event.shiftKey || event.altKey;
  const externalTarget =
    event.currentTarget.target && event.currentTarget.target !== "_self";
  if (!navigate || event.button !== 0 || modified || externalTarget) return;
  event.preventDefault();
  navigate(path);
}

export function Brand({ navigate, compact = false }) {
  return (
    <a
      className={`brand ${compact ? "brand-compact" : ""}`}
      href="/"
      onClick={(event) => handleClientNavigation(event, navigate, "/")}
      aria-label="Internship Signal home"
    >
      Internship Signal<span aria-hidden="true">.</span>
    </a>
  );
}

export function RouteLink({ to, navigate, className, children, ...props }) {
  return (
    <a
      href={to}
      className={className}
      onClick={(event) => handleClientNavigation(event, navigate, to)}
      {...props}
    >
      {children}
    </a>
  );
}

export function CoverageBadge({ coverage }) {
  return (
    <span className={`coverage coverage-${coverage}`}>
      <span className="coverage-dot" aria-hidden="true" />
      {COVERAGE_LABELS[coverage] || coverage}
    </span>
  );
}

export function CompanyMark({ company }) {
  return (
    <span className="company-mark" aria-hidden="true">
      {company.initials || company.name.slice(0, 2)}
    </span>
  );
}

export function Toggle({
  checked,
  onChange,
  label,
  description,
  disabled = false,
}) {
  const id = useId();
  return (
    <label
      className={`toggle-row ${disabled ? "is-disabled" : ""}`}
      htmlFor={id}
    >
      <span>
        <strong>{label}</strong>
        {description && <small>{description}</small>}
      </span>
      <span className="toggle-control">
        <input
          id={id}
          type="checkbox"
          checked={checked}
          onChange={(event) => onChange(event.target.checked)}
          disabled={disabled}
        />
        <span aria-hidden="true" />
      </span>
    </label>
  );
}

export function AsyncPanel({
  status,
  error,
  onRetry,
  loadingLabel = "Loading your Internship Signal workspace",
}) {
  if (status === "loading" || status === "idle") {
    return (
      <div className="state-panel" role="status" aria-live="polite">
        <span className="spinner" aria-hidden="true" />
        <h2>{loadingLabel}</h2>
        <p>Fetching the latest saved preferences and matches.</p>
      </div>
    );
  }
  if (status === "error") {
    return (
      <div className="state-panel state-error" role="alert">
        <span className="state-glyph" aria-hidden="true">
          !
        </span>
        <h2>We couldn’t load this page</h2>
        <p>{error || "Something went wrong while loading your account."}</p>
        <button className="primary" onClick={onRetry}>
          Try again
        </button>
      </div>
    );
  }
  return null;
}

export function SuccessNotice({ children }) {
  return (
    <div className="success-notice" role="status">
      ✓ {children}
    </div>
  );
}

export function ConfirmationDialog({
  open,
  title,
  children,
  confirmLabel,
  tone = "danger",
  onConfirm,
  onClose,
}) {
  const cancelRef = useRef(null);
  useEffect(() => {
    if (!open) return undefined;
    cancelRef.current?.focus();
    const onKeyDown = (event) => event.key === "Escape" && onClose();
    window.addEventListener("keydown", onKeyDown);
    return () => window.removeEventListener("keydown", onKeyDown);
  }, [open, onClose]);

  if (!open) return null;
  return (
    <div
      className="modal-layer"
      role="presentation"
      onMouseDown={(event) => event.target === event.currentTarget && onClose()}
    >
      <section
        className="modal"
        role="alertdialog"
        aria-modal="true"
        aria-labelledby="confirmation-title"
      >
        <h2 id="confirmation-title">{title}</h2>
        <div className="modal-copy">{children}</div>
        <div className="modal-actions">
          <button ref={cancelRef} onClick={onClose}>
            Cancel
          </button>
          <button
            className={tone === "danger" ? "danger" : "primary"}
            onClick={onConfirm}
          >
            {confirmLabel}
          </button>
        </div>
      </section>
    </div>
  );
}

export function RequestCompanyDialog({ open, onClose, onSubmit }) {
  const [companyName, setCompanyName] = useState("");
  const [careerUrl, setCareerUrl] = useState("");
  const [status, setStatus] = useState("idle");
  const [error, setError] = useState("");

  useEffect(() => {
    if (!open) return undefined;
    setCompanyName("");
    setCareerUrl("");
    setStatus("idle");
    setError("");
    const onKeyDown = (event) => event.key === "Escape" && onClose();
    window.addEventListener("keydown", onKeyDown);
    return () => window.removeEventListener("keydown", onKeyDown);
  }, [open, onClose]);

  if (!open) return null;
  const submit = async (event) => {
    event.preventDefault();
    if (!companyName.trim()) {
      setError("Enter a company name.");
      return;
    }
    setStatus("saving");
    setError("");
    try {
      await onSubmit({
        company_name: companyName.trim(),
        career_url: careerUrl.trim() || undefined,
      });
      setStatus("success");
    } catch (requestError) {
      setError(requestError.message || "The request could not be sent.");
      setStatus("error");
    }
  };

  return (
    <div
      className="modal-layer"
      role="presentation"
      onMouseDown={(event) => event.target === event.currentTarget && onClose()}
    >
      <section
        className="modal"
        role="dialog"
        aria-modal="true"
        aria-labelledby="request-company-title"
      >
        {status === "success" ? (
          <>
            <span className="confirmation-mark" aria-hidden="true">
              ✓
            </span>
            <h2 id="request-company-title">Request received</h2>
            <p>
              We’ll evaluate coverage for {companyName}. A request does not mean
              the company is monitored yet.
            </p>
            <button className="primary full" onClick={onClose}>
              Done
            </button>
          </>
        ) : (
          <form onSubmit={submit} noValidate>
            <div className="modal-heading">
              <div>
                <p className="eyebrow">Coverage request</p>
                <h2 id="request-company-title">Request a company</h2>
              </div>
              <button
                type="button"
                className="icon-button"
                aria-label="Close request form"
                onClick={onClose}
              >
                ×
              </button>
            </div>
            <p>
              Tell us which employer you’d like added. We’ll review whether its
              career page can be supported.
            </p>
            {error && (
              <div className="field-error" role="alert">
                {error}
              </div>
            )}
            <label className="field">
              <span>Company name</span>
              <input
                value={companyName}
                onChange={(event) => setCompanyName(event.target.value)}
                autoFocus
              />
            </label>
            <label className="field">
              <span>
                Career page URL <small>(optional)</small>
              </span>
              <input
                type="url"
                value={careerUrl}
                onChange={(event) => setCareerUrl(event.target.value)}
                placeholder="https://company.com/careers"
              />
            </label>
            <button className="primary full" disabled={status === "saving"}>
              {status === "saving" ? "Sending request…" : "Send request"}
            </button>
          </form>
        )}
      </section>
    </div>
  );
}

export function relativeDetection(iso) {
  const minutes = Math.max(
    0,
    Math.floor((Date.now() - new Date(iso).getTime()) / 60_000),
  );
  if (minutes < 1) return "Detected just now";
  if (minutes < 60)
    return `Detected ${minutes} minute${minutes === 1 ? "" : "s"} ago`;
  const hours = Math.floor(minutes / 60);
  if (hours < 24) return `Detected ${hours} hour${hours === 1 ? "" : "s"} ago`;
  const days = Math.floor(hours / 24);
  return `Detected ${days} day${days === 1 ? "" : "s"} ago`;
}

export function freshnessFor(iso) {
  const minutes = Math.max(0, (Date.now() - new Date(iso).getTime()) / 60_000);
  if (minutes < 60) return { id: "just", label: "Just detected" };
  if (minutes < 1_440) return { id: "today", label: "New today" };
  return { id: "older", label: "Older" };
}

export function alertFrequencyLabel(id) {
  return ALERT_FREQUENCIES.find((option) => option.id === id)?.label || id;
}

export function formatScanTime(iso) {
  if (!iso) return "Not available";
  return new Intl.DateTimeFormat(undefined, {
    hour: "numeric",
    minute: "2-digit",
  }).format(new Date(iso));
}
