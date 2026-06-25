"""In-memory dataset store.

Local-first by design: datasets live in process memory and vanish on restart.
Swapping this for SQLite/Postgres only requires reimplementing these four
functions — nothing else in the app touches storage directly.
"""

import uuid

_DATASETS: dict = {}


def save_dataset(payload: dict) -> str:
    dataset_id = uuid.uuid4().hex[:12]
    _DATASETS[dataset_id] = payload
    return dataset_id


def get_dataset(dataset_id: str):
    return _DATASETS.get(dataset_id)


def get_job(dataset_id: str, job_id: str):
    ds = _DATASETS.get(dataset_id)
    if not ds:
        return None
    return next((j for j in ds["jobs"] if j["id"] == job_id), None)


def clear():
    """Used by tests."""
    _DATASETS.clear()
