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
    assert payload["artifacts"]["manifest"]["key"] in storage.objects

    downloaded = client.get(f"/jobs/{job_id}/download")
    assert downloaded.status_code == 200
    assert "<RU_PASSPORT>" in downloaded.text
