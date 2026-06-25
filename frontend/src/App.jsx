import { useMemo, useState } from "react";
import { api } from "./api.js";
import { useShortlist } from "./hooks/useShortlist.js";
import { DEFAULT_FILTERS, filterJobs } from "./utils/filterJobs.js";
import { downloadCsv } from "./utils/exportCsv.js";

import AskPanel from "./components/AskPanel.jsx";
import BucketBoard from "./components/BucketBoard.jsx";
import CleaningReport from "./components/CleaningReport.jsx";
import EmptyState from "./components/EmptyState.jsx";
import FiltersBar from "./components/FiltersBar.jsx";
import JobDrawer from "./components/JobDrawer.jsx";
import JobsTable from "./components/JobsTable.jsx";
import SummaryDashboard from "./components/SummaryDashboard.jsx";
import UploadPanel from "./components/UploadPanel.jsx";

const TABS = ["Overview", "Postings", "Buckets", "Ask"];

export default function App() {
  const [tab, setTab] = useState("Overview");
  const [dataset, setDataset] = useState(null); // {dataset_id, jobs, cleaning_report, summary}
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const [selected, setSelected] = useState(null);
  const [filters, setFilters] = useState(DEFAULT_FILTERS);
  const shortlist = useShortlist();

  const ingest = async (promise) => {
    setBusy(true);
    setError("");
    try {
      const data = await promise;
      setDataset(data);
      setFilters(DEFAULT_FILTERS);
      setSelected(null);
      setTab("Overview");
    } catch (e) {
      setError(e.message || "Something went wrong while analyzing the CSV.");
    } finally {
      setBusy(false);
    }
  };

  const jobs = dataset?.jobs || [];
  const visible = useMemo(
    () => filterJobs(jobs, filters, shortlist.shortlist),
    [jobs, filters, shortlist.shortlist]
  );

  const counts = {
    Overview: null,
    Postings: jobs.length || null,
    Buckets: dataset ? dataset.summary.buckets.high : null,
    Ask: null,
  };

  return (
    <>
      <header className="masthead">
        <div className="masthead-inner">
          <div className="brand-row">
            <h1>
              Internship Signal<span className="tick">.</span>
            </h1>
            <span className="tagline">
              Separate real engineering internships from busywork — scored, flagged, explained.
            </span>
            {dataset && <span className="session">dataset {dataset.dataset_id}</span>}
          </div>
          <nav className="tabs" aria-label="Views">
            {TABS.map((t) => (
              <button
                key={t}
                className={tab === t ? "active" : ""}
                onClick={() => setTab(t)}
                disabled={!dataset && t !== "Overview"}
              >
                {t}
                {counts[t] != null && <span className="count">{counts[t]}</span>}
              </button>
            ))}
          </nav>
        </div>
      </header>

      <main className="shell">
        {error && <div className="error-banner" role="alert">{error}</div>}

        {!dataset && (
          <UploadPanel
            onIngestFile={(f) => ingest(api.ingestFile(f))}
            onIngestText={(t) => ingest(api.ingestText(t))}
            onLoadSample={() => ingest(api.loadSample())}
            busy={busy}
          />
        )}

        {dataset && tab === "Overview" && (
          <div className="grid" style={{ gap: 16 }}>
            <SummaryDashboard
              summary={dataset.summary}
              jobs={jobs}
              onOpenJob={setSelected}
            />
            <CleaningReport report={dataset.cleaning_report} />
            <details>
              <summary style={{ cursor: "pointer", color: "var(--accent-ink)", fontSize: 13.5 }}>
                Load a different CSV
              </summary>
              <div style={{ marginTop: 12 }}>
                <UploadPanel
                  onIngestFile={(f) => ingest(api.ingestFile(f))}
                  onIngestText={(t) => ingest(api.ingestText(t))}
                  onLoadSample={() => ingest(api.loadSample())}
                  busy={busy}
                />
              </div>
            </details>
          </div>
        )}

        {dataset && tab === "Postings" && (
          <div className="grid" style={{ gap: 16 }}>
            <FiltersBar
              jobs={jobs}
              filters={filters}
              setFilters={setFilters}
              hits={visible.length}
              onExport={() => downloadCsv(visible)}
            />
            <JobsTable jobs={visible} onOpen={setSelected} shortlist={shortlist} />
          </div>
        )}

        {dataset && tab === "Buckets" && (
          jobs.length
            ? <BucketBoard jobs={jobs} onOpen={setSelected} />
            : <EmptyState title="Nothing to bucket yet" />
        )}

        {dataset && tab === "Ask" && (
          <AskPanel datasetId={dataset.dataset_id} jobs={jobs} onOpenJob={setSelected} />
        )}
      </main>

      {selected && (
        <JobDrawer job={selected} onClose={() => setSelected(null)} shortlist={shortlist} />
      )}
    </>
  );
}
