"""Internship Signal API.

Run from the repository root with `PYTHONPATH=.:backend`:
uvicorn app.main:app --reload --port 8000
"""

import json
from json import JSONDecodeError

from fastapi import FastAPI, HTTPException, Request
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from sqlalchemy.exc import SQLAlchemyError

from . import config, store
from .ask import ask
from .hosted.router import router as hosted_router
from .hosted.services import HostedServices
from .ingest import process_csv
from .profile import load_profile

app = FastAPI(title="Internship Signal API", version="1.0.0")
MAX_CSV_BYTES = 10 * 1024 * 1024
MAX_HOSTED_JSON_BYTES = 64 * 1024
HOSTED_MUTATION_PREFIXES = (
    "/api/auth/",
    "/api/preferences",
    "/api/watchlist",
    "/api/company-requests",
)

app.state.hosted_services = HostedServices.build()

app.add_middleware(
    CORSMiddleware,
    allow_origins=list(app.state.hosted_services.settings.allowed_frontend_origins),
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.middleware("http")
async def limit_hosted_request_size(request: Request, call_next):
    if request.method in {"POST", "PUT", "PATCH"} and request.url.path.startswith(
        HOSTED_MUTATION_PREFIXES
    ):
        raw_length = request.headers.get("content-length")
        if raw_length:
            try:
                content_length = int(raw_length)
            except ValueError:
                return JSONResponse(
                    status_code=400,
                    content={"detail": "Content-Length must be an integer."},
                )
            if content_length > MAX_HOSTED_JSON_BYTES:
                return JSONResponse(
                    status_code=413,
                    content={"detail": "Request body is too large."},
                )
        body = await request.body()
        if len(body) > MAX_HOSTED_JSON_BYTES:
            return JSONResponse(
                status_code=413,
                content={"detail": "Request body is too large."},
            )
    return await call_next(request)


@app.exception_handler(RequestValidationError)
async def safe_validation_error(_request: Request, exc: RequestValidationError):
    errors = []
    for error in exc.errors():
        location = [str(part) for part in error.get("loc", ()) if part != "body"]
        errors.append(
            {
                "field": ".".join(location) or "request",
                "message": error.get("msg", "Invalid value."),
                "type": error.get("type", "value_error"),
            }
        )
    return JSONResponse(status_code=422, content={"detail": errors})


@app.exception_handler(SQLAlchemyError)
async def safe_database_error(_request: Request, _exc: SQLAlchemyError):
    return JSONResponse(
        status_code=503,
        content={"detail": "Hosted account storage is temporarily unavailable."},
    )


app.include_router(hosted_router)


def _ingest_text(csv_text: str) -> dict:
    try:
        result = process_csv(csv_text)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    dataset_id = store.save_dataset(result)
    return {
        "dataset_id": dataset_id,
        "cleaning_report": result["cleaning_report"],
        "summary": result["summary"],
        "jobs": result["jobs"],
    }


@app.post("/api/ingest")
async def ingest(request: Request):
    """Accepts either multipart form-data with a `file` field or a JSON body
    {"csv_text": "..."} so the UI can support upload *and* paste."""
    ctype = request.headers.get("content-type", "")
    if ctype.startswith("multipart/form-data"):
        form = await request.form()
        upload = form.get("file")
        if upload is None:
            raise HTTPException(status_code=400, detail="No file field in the upload.")
        read_upload = getattr(upload, "read", None)
        if not callable(read_upload):
            raise HTTPException(status_code=400, detail="The file field must be a file upload.")
        raw = await read_upload(MAX_CSV_BYTES + 1)
        if len(raw) > MAX_CSV_BYTES:
            raise HTTPException(status_code=413, detail="CSV content exceeds the 10 MiB limit.")
        try:
            csv_text = raw.decode("utf-8-sig")
        except UnicodeDecodeError:
            csv_text = raw.decode("latin-1")
    else:
        body = await _json_object(request, max_bytes=MAX_CSV_BYTES + 1024)
        csv_text = body.get("csv_text", "")
        if not isinstance(csv_text, str):
            raise HTTPException(status_code=400, detail="csv_text must be a string.")
        if len(csv_text.encode("utf-8")) > MAX_CSV_BYTES:
            raise HTTPException(status_code=413, detail="CSV content exceeds the 10 MiB limit.")
    if not csv_text.strip():
        raise HTTPException(status_code=400, detail="No CSV content provided.")
    return _ingest_text(csv_text)


@app.get("/api/sample")
def ingest_sample():
    """Loads the bundled demo CSV so the app is useful with zero setup."""
    path = config.SAMPLE_CSV_PATH
    if not path.exists():
        raise HTTPException(status_code=404, detail="Sample CSV not found.")
    return _ingest_text(path.read_text(encoding="utf-8"))


def _dataset_or_404(dataset_id: str) -> dict:
    ds = store.get_dataset(dataset_id)
    if ds is None:
        raise HTTPException(status_code=404, detail="Dataset not found (the store is in-memory; re-ingest after a restart).")
    return ds


@app.get("/api/datasets/{dataset_id}/jobs")
def list_jobs(
    dataset_id: str,
    bucket: str | None = None,
    role: str | None = None,
    action: str | None = None,
    paid_only: bool = False,
    remote_only: bool = False,
    min_score: int = 0,
    q: str | None = None,
):
    jobs = _dataset_or_404(dataset_id)["jobs"]
    if bucket:
        jobs = [j for j in jobs if j["score"]["bucket"] == bucket]
    if role:
        jobs = [j for j in jobs if j["role_classification"]["role"] == role]
    if action:
        jobs = [j for j in jobs if j["score"]["action"] == action]
    if paid_only:
        jobs = [j for j in jobs if j["compensation"]["kind"] in ("paid", "stipend_unspecified")]
    if remote_only:
        jobs = [j for j in jobs if (j.get("remote_status") or "").lower() == "remote"]
    if min_score:
        jobs = [j for j in jobs if j["score"]["total"] >= min_score]
    if q:
        needle = q.lower()
        jobs = [
            j for j in jobs
            if needle in (j["company"] or "").lower()
            or needle in (j["title"] or "").lower()
            or needle in (j["location"] or "").lower()
        ]
    return {"count": len(jobs), "jobs": jobs}


@app.get("/api/datasets/{dataset_id}/jobs/{job_id}")
def get_job(dataset_id: str, job_id: str):
    _dataset_or_404(dataset_id)
    job = store.get_job(dataset_id, job_id)
    if job is None:
        raise HTTPException(status_code=404, detail="Job not found in this dataset.")
    return job


@app.get("/api/datasets/{dataset_id}/summary")
def get_summary(dataset_id: str):
    ds = _dataset_or_404(dataset_id)
    return {"summary": ds["summary"], "cleaning_report": ds["cleaning_report"]}


@app.post("/api/datasets/{dataset_id}/ask")
async def ask_dataset(dataset_id: str, request: Request):
    ds = _dataset_or_404(dataset_id)
    body = await _json_object(request)
    question = body.get("question", "")
    if not isinstance(question, str):
        raise HTTPException(status_code=400, detail="question must be a string.")
    return ask(question, ds["jobs"])


async def _json_object(request: Request, *, max_bytes: int | None = None) -> dict:
    """Read a JSON request body and turn client-shape errors into HTTP 400s."""

    try:
        if max_bytes is None:
            body = await request.json()
        else:
            raw = bytearray()
            async for chunk in request.stream():
                raw.extend(chunk)
                if len(raw) > max_bytes:
                    raise HTTPException(status_code=413, detail="Request body is too large.")
            body = json.loads(raw.decode("utf-8"))
    except (JSONDecodeError, UnicodeDecodeError, RecursionError) as exc:
        raise HTTPException(status_code=400, detail="Request body must contain valid JSON.") from exc
    if not isinstance(body, dict):
        raise HTTPException(status_code=400, detail="Request body must be a JSON object.")
    return body


@app.get("/api/profile")
def get_profile():
    return load_profile()


@app.get("/api/health")
def health():
    return {"status": "ok"}
