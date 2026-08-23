"""The canonical job schema shared by the backend and the watcher.

`CANONICAL_COLUMNS` defines the column set and order of a canonical job row.
The backend builds it from ingested CSVs; the watcher builds it from source
adapters. It is re-exported by `backend.app.normalize` for existing callers.
"""

CANONICAL_COLUMNS = [
    "company", "title", "location", "compensation", "description",
    "requirements", "source_url", "date_posted", "deadline",
    "remote_status", "internship_type",
]
