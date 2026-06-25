import { roleOptions } from "../utils/filterJobs.js";

// Every control narrows the table live; the export button writes exactly
// what is currently visible.
export default function FiltersBar({ jobs, filters, setFilters, hits, onExport }) {
  const set = (patch) => setFilters({ ...filters, ...patch });
  return (
    <div className="card filters">
      <input
        type="search"
        placeholder="Search company, title, location…"
        value={filters.q}
        onChange={(e) => set({ q: e.target.value })}
        style={{ minWidth: 220 }}
        aria-label="Search"
      />
      <label>
        Bucket
        <select value={filters.bucket} onChange={(e) => set({ bucket: e.target.value })}>
          <option value="all">all</option>
          <option value="high">high signal</option>
          <option value="maybe">maybe</option>
          <option value="low">low signal</option>
        </select>
      </label>
      <label>
        Role
        <select value={filters.role} onChange={(e) => set({ role: e.target.value })}>
          <option value="all">all</option>
          {roleOptions(jobs).map((o) => (
            <option key={o.value} value={o.value}>{o.label}</option>
          ))}
        </select>
      </label>
      <label>
        Min score
        <input
          type="number" min="0" max="100" step="5"
          value={filters.minScore}
          onChange={(e) => set({ minScore: Number(e.target.value) || 0 })}
          style={{ width: 64 }}
        />
      </label>
      <label>
        <input type="checkbox" checked={filters.paidOnly}
               onChange={(e) => set({ paidOnly: e.target.checked })} /> paid only
      </label>
      <label>
        <input type="checkbox" checked={filters.remoteOnly}
               onChange={(e) => set({ remoteOnly: e.target.checked })} /> remote only
      </label>
      <label>
        Flags
        <select value={filters.flagged} onChange={(e) => set({ flagged: e.target.value })}>
          <option value="all">any</option>
          <option value="clean">no major flags</option>
          <option value="flagged">flagged only</option>
        </select>
      </label>
      <label>
        <input type="checkbox" checked={filters.shortlistOnly}
               onChange={(e) => set({ shortlistOnly: e.target.checked })} /> ★ shortlist
      </label>
      <span className="spacer" />
      <span className="hits">{hits} shown</span>
      <button onClick={onExport} disabled={!hits}>Export CSV</button>
    </div>
  );
}
