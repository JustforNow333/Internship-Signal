// Thin client over the FastAPI backend. The Vite dev server proxies /api.

async function handle(res) {
  if (!res.ok) {
    let detail = `Request failed (${res.status})`;
    try { detail = (await res.json()).detail || detail; } catch { /* keep default */ }
    throw new Error(detail);
  }
  return res.json();
}

export const api = {
  ingestText: (csv_text) =>
    fetch("/api/ingest", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ csv_text }),
    }).then(handle),

  ingestFile: (file) => {
    const fd = new FormData();
    fd.append("file", file);
    return fetch("/api/ingest", { method: "POST", body: fd }).then(handle);
  },

  loadSample: () => fetch("/api/sample").then(handle),

  ask: (datasetId, question) =>
    fetch(`/api/datasets/${datasetId}/ask`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ question }),
    }).then(handle),

  profile: () => fetch("/api/profile").then(handle),
};
