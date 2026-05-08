from fastapi.testclient import TestClient

import app as processor_app
from document_processor import DocumentProcessor
from test_document_processor import FakePresidio, FakeStorage


def test_job_api_accepts_text_and_returns_completed_result(monkeypatch):
    storage = FakeStorage()

    def fake_processor():
        return DocumentProcessor(storage=storage, presidio=FakePresidio(), chunk_size=128)

    monkeypatch.setattr(processor_app, "_processor", fake_processor)
    processor_app.jobs.clear()
    client = TestClient(processor_app.app)

    created = client.post(
        "/jobs",
        data={
            "language": "ru",
            "text": "Паспорт 4510 123456 и email test@example.com",
        },
    )

    assert created.status_code == 202
    job_id = created.json()["job_id"]
    completed = client.get(f"/jobs/{job_id}")

    assert completed.status_code == 200
    payload = completed.json()
    assert payload["status"] == "complete"
    assert payload["stats"]["entities"] == 2
    assert isinstance(payload["duration_ms"], int)
    assert payload["duration_ms"] >= 0
    assert payload["artifacts"]["manifest"]["key"] in storage.objects

    downloaded = client.get(f"/jobs/{job_id}/download")
    assert downloaded.status_code == 200
    assert "<RU_PASSPORT>" in downloaded.text


def test_ready_endpoint_reports_dependency_health(monkeypatch):
    storage = FakeStorage()

    def fake_probe(name, base_url):
        return {"status": "ok", "service": name}

    monkeypatch.setattr(processor_app, "_storage", lambda: storage)
    monkeypatch.setattr(processor_app, "_probe_http_service", fake_probe)
    client = TestClient(processor_app.app)

    ready = client.get("/ready")

    assert ready.status_code == 200
    payload = ready.json()
    assert payload["status"] == "ok"
    assert payload["checks"]["analyzer"]["status"] == "ok"
    assert payload["checks"]["anonymizer"]["status"] == "ok"
    assert payload["checks"]["storage"] == {"status": "ok", "service": "storage", "bucket": "agent-artifacts"}


def test_ready_endpoint_returns_503_when_dependency_is_down(monkeypatch):
    storage = FakeStorage()

    def fake_probe(name, base_url):
        if name == "anonymizer":
            return {"status": "error", "service": name, "error": "connection refused"}
        return {"status": "ok", "service": name}

    monkeypatch.setattr(processor_app, "_storage", lambda: storage)
    monkeypatch.setattr(processor_app, "_probe_http_service", fake_probe)
    client = TestClient(processor_app.app)

    ready = client.get("/ready")

    assert ready.status_code == 503
    payload = ready.json()
    assert payload["status"] == "degraded"
    assert payload["checks"]["anonymizer"]["error"] == "connection refused"


def test_job_api_recovers_completed_state_from_storage_after_memory_clear(monkeypatch):
    storage = FakeStorage()

    def fake_processor():
        return DocumentProcessor(storage=storage, presidio=FakePresidio(), chunk_size=128)

    monkeypatch.setattr(processor_app, "_processor", fake_processor)
    monkeypatch.setattr(processor_app, "_storage", lambda: storage)
    processor_app.jobs.clear()
    client = TestClient(processor_app.app)

    created = client.post(
        "/jobs",
        data={"language": "ru", "text": "Паспорт 4510 123456 и email test@example.com"},
    )
    job_id = created.json()["job_id"]
    processor_app.jobs.clear()

    restored = client.get(f"/jobs/{job_id}")

    assert restored.status_code == 200
    assert restored.json()["status"] == "complete"
    assert restored.json()["stats"]["entities"] == 2


def test_fact_replacement_endpoint_persists_final_text(monkeypatch):
    storage = FakeStorage()

    def fake_processor():
        return DocumentProcessor(storage=storage, presidio=FakePresidio(), chunk_size=128)

    monkeypatch.setattr(processor_app, "_processor", fake_processor)
    monkeypatch.setattr(processor_app, "_storage", lambda: storage)
    processor_app.jobs.clear()
    client = TestClient(processor_app.app)

    created = client.post(
        "/jobs",
        data={"language": "ru", "text": "Паспорт 4510 123456. Revenue grew 47%."},
    )
    job_id = created.json()["job_id"]
    job = client.get(f"/jobs/{job_id}").json()
    claim_id = job["claims"][0]["id"]

    updated = client.post(
        f"/jobs/{job_id}/facts",
        json={"replacements": [{"id": claim_id, "replacement": "Sanitized claim."}]},
    )

    assert updated.status_code == 200
    assert updated.json()["final_text"].startswith("Sanitized claim.")
    assert updated.json()["artifacts"]["final"]["key"] in storage.objects
    assert client.get(f"/jobs/{job_id}/download").text.startswith("Sanitized claim.")


def test_entity_replacement_endpoint_persists_manual_pii_text(monkeypatch):
    storage = FakeStorage()

    def fake_processor():
        return DocumentProcessor(storage=storage, presidio=FakePresidio(), chunk_size=128)

    monkeypatch.setattr(processor_app, "_processor", fake_processor)
    monkeypatch.setattr(processor_app, "_storage", lambda: storage)
    processor_app.jobs.clear()
    client = TestClient(processor_app.app)

    created = client.post(
        "/jobs",
        data={"language": "ru", "text": "Паспорт 4510 123456. Revenue grew 47%."},
    )
    job_id = created.json()["job_id"]
    job = client.get(f"/jobs/{job_id}").json()
    passport = job["replacements"][0]

    updated = client.post(
        f"/jobs/{job_id}/facts",
        json={
            "entity_replacements": [
                {
                    "entity_type": passport["entity_type"],
                    "start": passport["start"],
                    "end": passport["end"],
                    "replacement": "<DOCUMENT_ID>",
                }
            ]
        },
    )

    assert updated.status_code == 200
    assert updated.json()["replacements"][0]["replacement"] == "<DOCUMENT_ID>"
    assert "<DOCUMENT_ID>" in updated.json()["final_text"]
    assert client.get(f"/jobs/{job_id}/download").text.startswith("Паспорт <DOCUMENT_ID>")
