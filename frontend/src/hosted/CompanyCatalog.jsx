import { useMemo, useState } from "react";
import EmptyState from "../components/EmptyState.jsx";
import { CompanyMark, CoverageBadge } from "./ui.jsx";

export default function CompanyCatalog({
  companies,
  selectedIds,
  onToggle,
  heading = "Supported companies",
}) {
  const [query, setQuery] = useState("");
  const visible = useMemo(() => {
    const needle = query.trim().toLowerCase();
    return needle
      ? companies.filter((company) =>
          company.name.toLowerCase().includes(needle),
        )
      : companies;
  }, [companies, query]);

  return (
    <section
      className="company-catalog"
      aria-labelledby="company-catalog-title"
    >
      <div className="catalog-toolbar">
        <div>
          <h2 id="company-catalog-title">{heading}</h2>
          <p>
            <strong>{selectedIds.length}</strong>{" "}
            {selectedIds.length === 1 ? "company" : "companies"} selected
          </p>
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
          {query && (
            <button
              type="button"
              className="clear-search"
              aria-label="Clear company search"
              onClick={() => setQuery("")}
            >
              ×
            </button>
          )}
        </label>
      </div>
      {visible.length ? (
        <div className="company-list">
          {visible.map((company) => {
            const selected = selectedIds.includes(company.id);
            return (
              <article
                className={`company-row ${selected ? "selected" : ""}`}
                key={company.id}
              >
                <CompanyMark company={company} />
                <div className="company-identity">
                  <h3>{company.name}</h3>
                  <CoverageBadge coverage={company.coverage} />
                </div>
                <button
                  type="button"
                  className={selected ? "selected-button" : "add-button"}
                  aria-pressed={selected}
                  aria-label={`${selected ? "Remove" : "Add"} ${company.name}`}
                  onClick={() => onToggle(company.id)}
                >
                  {selected ? (
                    <>
                      <span aria-hidden="true">✓</span> Selected
                    </>
                  ) : (
                    <>
                      <span aria-hidden="true">+</span> Add
                    </>
                  )}
                </button>
              </article>
            );
          })}
        </div>
      ) : (
        <EmptyState
          glyph="⌕"
          title="No supported companies match your search"
          hint="Try a shorter name, or clear the search to browse the full catalog."
        />
      )}
    </section>
  );
}
