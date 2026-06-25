import { useCallback, useEffect, useState } from "react";

// Shortlist persists in localStorage. Job ids are stable hashes of
// company+title+location, so stars survive re-ingesting the same CSV.
const KEY = "internship-signal:shortlist";

function read() {
  try { return new Set(JSON.parse(localStorage.getItem(KEY) || "[]")); }
  catch { return new Set(); }
}

export function useShortlist() {
  const [ids, setIds] = useState(read);

  useEffect(() => {
    try { localStorage.setItem(KEY, JSON.stringify([...ids])); }
    catch { /* private mode etc. — shortlist just won't persist */ }
  }, [ids]);

  const toggle = useCallback((id) => {
    setIds((prev) => {
      const next = new Set(prev);
      next.has(id) ? next.delete(id) : next.add(id);
      return next;
    });
  }, []);

  return { shortlist: ids, toggle, has: (id) => ids.has(id) };
}
