import { useMemo, useState } from "react";
import EmptyState from "../components/EmptyState.jsx";
import {
  CompanyMark,
  CoverageBadge,
  RequestCompanyDialog,
  SuccessNotice,
} from "./ui.jsx";

export default function WatchlistPage({
  companies,
  watchlist,
  saveWatchlist,
  requestCompany,
}) {
  const [query, setQuery] = useState("");
  const [requestOpen, setRequestOpen] = useState(false);
  const [savingId, setSavingId] = useState("");
  const [notice, setNotice] = useState("");
  const [error, setError] = useState("");
  const visible = useMemo(() => {
    const needle = query.trim().toLowerCase();
    return needle
      ? companies.filter((company) =>
          company.name.toLowerCase().includes(needle),
        )
      : companies;
  }, [companies, query]);
  const entryFor = (id) => watchlist.find((entry) => entry.company_id === id);

  const persist = async (company, action) => {
    setSavingId(company.id);
    setNotice("");
    setError("");
    const entry = entryFor(company.id);
    let next;
    if (action === "add")
      next = [...watchlist, { company_id: company.id, paused: false }];
    else if (action === "remove")
      next = watchlist.filter((item) => item.company_id !== company.id);
    else
      next = watchlist.map((item) =>
        item.company_id === company.id
          ? { ...item, paused: !item.paused }
          : item,
      );
    try {
      await saveWatchlist(next);
      setNotice(
        action === "add"
          ? `${company.name} added to your watchlist.`
          : action === "remove"
            ? `${company.name} removed from your watchlist.`
            : `${company.name} monitoring ${entry.paused ? "resumed" : "paused"}.`,
      );
    } catch (saveError) {
      setError(saveError.message || "Your watchlist could not be updated.");
    } finally {
      setSavingId("");
    }
  };

  return (
    <>
      <div className="page-heading">
        <div>
          <p className="eyebrow">Personal company coverage</p>
          <h1>Watchlist</h1>
          <p>
            Choose exactly which supported companies Internship Signal should
            monitor for your account.
          </p>
        </div>
        <button className="secondary" onClick={() => setRequestOpen(true)}>
          Request a company
        </button>
      </div>
      <section className="watchlist-summary">
        <div>
          <strong>{watchlist.length}</strong>
          <span>companies on your watchlist</span>
        </div>
        <p>
          <span className="live-dot" />{" "}
          {watchlist.filter((entry) => !entry.paused).length} actively monitored
        </p>
      </section>
      {notice && <SuccessNotice>{notice}</SuccessNotice>}
      {error && (
        <div className="error-banner" role="alert">
          {error}
        </div>
      )}
      <section
        className="watchlist-catalog"
        aria-labelledby="watchlist-catalog-title"
      >
        <div className="catalog-toolbar">
          <div>
            <p className="eyebrow">Supported catalog</p>
            <h2 id="watchlist-catalog-title">Companies</h2>
          </div>
          <label className="search-field">
            <span className="sr-only">Search supported companies</span>
            <span aria-hidden="true">⌕</span>
            <input
              type="search"
              placeholder="Search supported companies"
              value={query}
              onChange={(event) => setQuery(event.target.value)}
            />
          </label>
        </div>
        {visible.length ? (
          <div className="watchlist-grid">
            {visible.map((company) => {
              const entry = entryFor(company.id);
              const saving = savingId === company.id;
              return (
                <article
                  className={`watch-company-card ${entry ? "watched" : ""}`}
                  key={company.id}
                >
                  <div className="watch-company-main">
                    <CompanyMark company={company} />
                    <div>
                      <h3>{company.name}</h3>
                      <CoverageBadge coverage={company.coverage} />
                    </div>
                    {entry && (
                      <span
                        className={`monitor-state ${entry.paused ? "paused" : "active"}`}
                      >
                        {entry.paused ? "Paused" : "Active"}
                      </span>
                    )}
                  </div>
                  <div className="watch-company-actions">
                    {entry ? (
                      <>
                        <button
                          disabled={saving}
                          onClick={() => persist(company, "pause")}
                        >
                          {entry.paused
                            ? "Resume monitoring"
                            : "Pause monitoring"}
                        </button>
                        <button
                          className="danger-ghost"
                          disabled={saving}
                          onClick={() => persist(company, "remove")}
                        >
                          Remove
                        </button>
                      </>
                    ) : (
                      <button
                        className="add-button"
                        disabled={saving}
                        onClick={() => persist(company, "add")}
                      >
                        + Add to watchlist
                      </button>
                    )}
                  </div>
                </article>
              );
            })}
          </div>
        ) : (
          <div className="empty-card">
            <EmptyState
              glyph="⌕"
              title="No supported company found"
              hint="Try another spelling, or request a company for future coverage review."
            />
            <button onClick={() => setRequestOpen(true)}>
              Request this company
            </button>
          </div>
        )}
      </section>
      <div className="coverage-note">
        <span aria-hidden="true">i</span>
        <p>
          <strong>About coverage</strong> Direct monitoring checks a supported
          employer source. Backstop coverage uses a supported aggregate listing
          when direct coverage is unavailable. Temporarily delayed sources
          remain on your watchlist, but new alerts may take longer.
        </p>
      </div>
      <RequestCompanyDialog
        open={requestOpen}
        onClose={() => setRequestOpen(false)}
        onSubmit={requestCompany}
      />
    </>
  );
}
