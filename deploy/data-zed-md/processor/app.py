from __future__ import annotations

import os
import threading
import uuid
from datetime import UTC, datetime

from fastapi import BackgroundTasks, FastAPI, File, Form, HTTPException, UploadFile
from fastapi.responses import PlainTextResponse

from document_processor import DocumentProcessor, PresidioClient, S3Storage, extract_text


MAX_UPLOAD_BYTES = int(os.environ.get("MAX_UPLOAD_BYTES", str(64 * 1024 * 1024)))
CHUNK_SIZE = int(os.environ.get("PROCESSOR_CHUNK_SIZE", "12000"))
PREVIEW_CHARS = int(os.environ.get("PROCESSOR_PREVIEW_CHARS", "300000"))

app = FastAPI(title="data.zed.md document processor", version="1.0.0")
jobs: dict[str, dict] = {}
jobs_lock = threading.Lock()


def _storage():
    return S3Storage()


def _processor() -> DocumentProcessor:
    return DocumentProcessor(
        storage=_storage(),
        presidio=PresidioClient(
            analyzer_url=os.environ.get("ANALYZER_URL", "http://analyzer:3000"),
            anonymizer_url=os.environ.get("ANONYMIZER_URL", "http://anonymizer:3000"),
            timeout=float(os.environ.get("PRESIDIO_TIMEOUT", "180")),
        ),
        chunk_size=CHUNK_SIZE,
        preview_chars=PREVIEW_CHARS,
    )


def _set_job(job_id: str, patch: dict) -> None:
    with jobs_lock:
        current = jobs.get(job_id, {})
        current.update(patch)
        jobs[job_id] = current


def _run_job(job_id: str, filename: str, content: bytes, content_type: str | None, language: str) -> None:
    try:
        _set_job(job_id, {"status": "extracting", "progress": 10})
        text = extract_text(filename, content, content_type)
        if not text.strip():
            raise ValueError("Документ не содержит извлекаемого текста.")
        _set_job(job_id, {"status": "processing", "progress": 35, "characters": len(text)})
        result = _processor().process_text(job_id=job_id, filename=filename, text=text, language=language)
        result["progress"] = 100
        result["completed_at"] = datetime.now(UTC).isoformat()
        _set_job(job_id, result)
    except Exception as exc:
        _set_job(
            job_id,
            {
                "status": "failed",
                "progress": 100,
                "error": str(exc),
                "completed_at": datetime.now(UTC).isoformat(),
            },
        )


@app.get("/health")
def health() -> dict:
    return {
        "status": "ok",
        "service": "data-zed-md-processor",
        "storage": "s3",
        "max_upload_bytes": MAX_UPLOAD_BYTES,
        "chunk_size": CHUNK_SIZE,
        "preview_chars": PREVIEW_CHARS,
    }


@app.post("/jobs", status_code=202)
async def create_job(
    background_tasks: BackgroundTasks,
    file: UploadFile | None = File(default=None),
    text: str | None = Form(default=None),
    language: str = Form(default="ru"),
) -> dict:
    if file is None and not text:
        raise HTTPException(status_code=400, detail="Загрузите файл или вставьте текст.")

    if file is not None:
        content = await file.read()
        filename = file.filename or "document.txt"
        content_type = file.content_type
    else:
        content = (text or "").encode("utf-8")
        filename = "pasted-document.txt"
        content_type = "text/plain"

    if len(content) > MAX_UPLOAD_BYTES:
        raise HTTPException(status_code=413, detail=f"Документ больше лимита {MAX_UPLOAD_BYTES} bytes.")

    job_id = uuid.uuid4().hex
    _set_job(
        job_id,
        {
            "job_id": job_id,
            "status": "queued",
            "progress": 0,
            "filename": filename,
            "language": language,
            "created_at": datetime.now(UTC).isoformat(),
        },
    )
    background_tasks.add_task(_run_job, job_id, filename, content, content_type, language)
    return jobs[job_id]


@app.get("/jobs/{job_id}")
def get_job(job_id: str) -> dict:
    job = jobs.get(job_id)
    if not job:
        raise HTTPException(status_code=404, detail="Job not found")
    return {key: value for key, value in job.items() if not key.startswith("_")}


@app.get("/jobs/{job_id}/download", response_class=PlainTextResponse)
def download_job(job_id: str) -> str:
    job = jobs.get(job_id)
    if not job:
        raise HTTPException(status_code=404, detail="Job not found")
    if job.get("status") != "complete":
        raise HTTPException(status_code=409, detail="Job is not complete")
    return job.get("_full_anonymized_text") or job.get("anonymized_text", "")
