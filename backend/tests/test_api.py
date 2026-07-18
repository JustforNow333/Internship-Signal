import pytest
from fastapi.testclient import TestClient

from app.main import app
from app.ingest import process_csv
from tests.conftest import SAMPLE

client = TestClient(app)


@pytest.fixture(scope="module")
def dataset_id():
    res = client.get("/api/sample")
    assert res.status_code == 200, res.text
    return res.json()["dataset_id"]


def test_health():
    assert client.get("/api/health").json() == {"status": "ok"}


def test_sample_ingest_shape():
    res = client.get("/api/sample").json()
    assert res["summary"]["total"] >= 25
    assert res["cleaning_report"]["duplicates_removed"] == 2
    assert res["cleaning_report"]["columns"]["unmapped"] == []
    assert len(res["jobs"]) == res["summary"]["total"]
    job = res["jobs"][0]
    for key in ("id", "company", "score", "compensation", "red_flags",
                "positive_signals", "role_classification", "company_classification"):
        assert key in job


def test_missing_sample_does_not_expose_server_path(monkeypatch, tmp_path):
    missing = tmp_path / "private" / "sample.csv"
    monkeypatch.setattr("app.config.SAMPLE_CSV_PATH", missing)

    response = client.get("/api/sample")

    assert response.status_code == 404
    assert response.json() == {"detail": "Sample CSV not found."}
    assert str(missing) not in response.text


def test_jobs_filtering(dataset_id):
    high = client.get(f"/api/datasets/{dataset_id}/jobs", params={"bucket": "high"}).json()
    assert high["count"] > 0
    assert all(j["score"]["bucket"] == "high" for j in high["jobs"])

    paid_ds = client.get(
        f"/api/datasets/{dataset_id}/jobs",
        params={"role": "data_science", "paid_only": True},
    ).json()
    assert paid_ds["count"] > 0
    for j in paid_ds["jobs"]:
        assert j["role_classification"]["role"] == "data_science"
        assert j["compensation"]["kind"] in ("paid", "stipend_unspecified")

    search = client.get(f"/api/datasets/{dataset_id}/jobs", params={"q": "stripe"}).json()
    assert search["count"] == 1 and search["jobs"][0]["company"] == "Stripe"


def test_single_job_and_404(dataset_id):
    jobs = client.get(f"/api/datasets/{dataset_id}/jobs").json()["jobs"]
    jid = jobs[0]["id"]
    assert client.get(f"/api/datasets/{dataset_id}/jobs/{jid}").json()["id"] == jid
    assert client.get(f"/api/datasets/{dataset_id}/jobs/nope").status_code == 404
    assert client.get("/api/datasets/nope/jobs").status_code == 404


def test_summary_endpoint(dataset_id):
    res = client.get(f"/api/datasets/{dataset_id}/summary").json()
    assert set(res["summary"]["buckets"]) == {"high", "maybe", "low"}
    assert res["cleaning_report"]["rows_in"] > res["cleaning_report"]["rows_out"]


def test_ask_endpoint(dataset_id):
    res = client.post(f"/api/datasets/{dataset_id}/ask",
                      json={"question": "which ones look exploitative?"})
    body = res.json()
    assert res.status_code == 200
    assert body["results"] and body["interpretation"]


@pytest.mark.parametrize(
    ("content", "headers"),
    [
        ("{", {"content-type": "application/json"}),
        ('["not", "an", "object"]', {"content-type": "application/json"}),
        ('{"question": 123}', {"content-type": "application/json"}),
    ],
)
def test_ask_rejects_malformed_or_wrong_shape_json(dataset_id, content, headers):
    res = client.post(f"/api/datasets/{dataset_id}/ask", content=content, headers=headers)

    assert res.status_code == 400


def test_ingest_json_paste():
    csv_text = SAMPLE.read_text(encoding="utf-8")
    res = client.post("/api/ingest", json={"csv_text": csv_text})
    assert res.status_code == 200
    assert res.json()["summary"]["total"] >= 25


def test_ingest_multipart_upload():
    res = client.post(
        "/api/ingest",
        files={"file": ("postings.csv", SAMPLE.read_bytes(), "text/csv")},
    )
    assert res.status_code == 200
    assert res.json()["summary"]["total"] >= 25


@pytest.mark.parametrize(
    ("content", "headers"),
    [
        ("{", {"content-type": "application/json"}),
        ('["not", "an", "object"]', {"content-type": "application/json"}),
        ('{"csv_text": 123}', {"content-type": "application/json"}),
    ],
)
def test_ingest_rejects_malformed_or_wrong_shape_json(content, headers):
    res = client.post("/api/ingest", content=content, headers=headers)

    assert res.status_code == 400


def test_ingest_rejects_non_file_multipart_field():
    res = client.post("/api/ingest", files={"file": (None, "not a file")})

    assert res.status_code == 400


def test_ingest_rejects_oversized_multipart(monkeypatch):
    monkeypatch.setattr("app.main.MAX_CSV_BYTES", 64)
    res = client.post(
        "/api/ingest",
        files={"file": ("large.csv", b"Company,Title\n" + b"x" * 128, "text/csv")},
    )

    assert res.status_code == 413


def test_ingest_rejects_oversized_json_body(monkeypatch):
    monkeypatch.setattr("app.main.MAX_CSV_BYTES", 64)
    res = client.post("/api/ingest", json={"csv_text": "Company,Title\n" + "x" * 128})

    assert res.status_code == 413


def test_ingest_rejects_empty_and_junk():
    assert client.post("/api/ingest", json={"csv_text": ""}).status_code == 400
    assert client.post("/api/ingest", json={"csv_text": "no,recognizable\nheaders,here"}).status_code == 400


def test_ingest_tolerates_extra_cells_in_csv_rows():
    result = process_csv("Company,Title\nAcme,Software Engineer Intern,unexpected extra\n")

    assert result["summary"]["total"] == 1
    assert result["jobs"][0]["company"] == "Acme"


def test_profile_endpoint():
    body = client.get("/api/profile").json()
    assert "flask" in body["skills"]
    assert body["role_affinity"]["ml_ai"] == 95
