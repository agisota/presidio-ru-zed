from __future__ import annotations

import os
import threading
from datetime import UTC, datetime

from fastapi import BackgroundTasks, FastAPI, File, Form, HTTPException, UploadFile
from fastapi.responses import JSONResponse, PlainTextResponse
from pydantic import BaseModel

from document_processor import (
    DocumentProcessor,
    PresidioClient,
    S3Storage,
    apply_fact_replacements,
    build_storage_keys,
    create_job_id,
    extract_text,
)


MAX_UPLOAD_BYTES = int(os.environ.get("MAX_UPLOAD_BYTES", str(64 * 1024 * 1024)))
CHUNK_SIZE = int(os.environ.get("PROCESSOR_CHUNK_SIZE", "12000"))
PREVIEW_CHARS = int(os.environ.get("PROCESSOR_PREVIEW_CHARS", "300000"))

app = FastAPI(title="data.zed.md document processor", version="1.0.0")
jobs: dict[str, dict] = {}
jobs_lock = threading.Lock()


class FactReplacement(BaseModel):
    id: str
    replacement: str


class EntityReplacement(BaseModel):
    entity_type: str
    start: int
    end: int
    replacement: str


class FactReplacementRequest(BaseModel):
    replacements: list[FactReplacement] = []
    entity_replacements: list[EntityReplacement] = []


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


def _public_job(job: dict) -> dict:
    return {key: value for key, value in job.items() if not key.startswith("_")}


def _probe_http_service(name: str, base_url: str) -> dict:
    import httpx

    try:
        response = httpx.get(f"{base_url.rstrip('/')}/health", timeout=3.0)
        response.raise_for_status()
        return {"status": "ok", "service": name}
    except Exception as exc:
        return {"status": "error", "service": name, "error": str(exc)[:200]}


def _probe_storage() -> dict:
    try:
        storage = _storage()
        details = storage.check() if hasattr(storage, "check") else {"bucket": getattr(storage, "bucket", "unknown")}
        return {"status": "ok", "service": "storage", **details}
    except Exception as exc:
        return {"status": "error", "service": "storage", "error": str(exc)[:200]}


def _state_key(job_id: str, filename: str = "state.json") -> str:
    return build_storage_keys(job_id, filename)["state"]


def _persist_job_state(job_id: str, job: dict) -> dict | None:
    try:
        storage = _storage()
        public = _public_job(job)
        key = _state_key(job_id, public.get("filename", "state.json"))
        public.setdefault("artifacts", {})
        state_artifact = {"bucket": getattr(storage, "bucket", "agent-artifacts"), "key": key}
        public["artifacts"]["state"] = state_artifact
        storage.put_text(key, public_json(public), "application/json; charset=utf-8")
        return state_artifact
    except Exception:
        # A transient state write must not break the running anonymization job.
        return None


def public_json(payload: dict) -> str:
    import json

    return json.dumps(payload, ensure_ascii=False, indent=2)


def _load_job_state(job_id: str) -> dict | None:
    try:
        import json

        storage = _storage()
        payload = storage.get_text(_state_key(job_id))
        return json.loads(payload)
    except Exception:
        return None


def _set_job(job_id: str, patch: dict, persist: bool = True) -> None:
    with jobs_lock:
        current = jobs.get(job_id, {})
        current.update(patch)
        current["updated_at"] = datetime.now(UTC).isoformat()
        jobs[job_id] = current
        public = _public_job(current)
    if not persist:
        return
    state_artifact = _persist_job_state(job_id, public)
    if state_artifact:
        with jobs_lock:
            jobs[job_id].setdefault("artifacts", {})["state"] = state_artifact


def _run_job(job_id: str, filename: str, content: bytes, content_type: str | None, language: str) -> None:
    try:
        _set_job(job_id, {"status": "extracting", "progress": 10}, persist=False)
        text = extract_text(filename, content, content_type)
        if not text.strip():
            raise ValueError("Документ не содержит извлекаемого текста.")
        _set_job(job_id, {"status": "processing", "progress": 35, "characters": len(text)}, persist=False)
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


@app.get("/ready")
def ready() -> JSONResponse:
    analyzer_url = os.environ.get("ANALYZER_URL", "http://analyzer:3000")
    anonymizer_url = os.environ.get("ANONYMIZER_URL", "http://anonymizer:3000")
    checks = {
        "analyzer": _probe_http_service("analyzer", analyzer_url),
        "anonymizer": _probe_http_service("anonymizer", anonymizer_url),
        "storage": _probe_storage(),
    }
    is_ready = all(check["status"] == "ok" for check in checks.values())
    payload = {
        "status": "ok" if is_ready else "degraded",
        "service": "data-zed-md-processor",
        "checks": checks,
    }
    return JSONResponse(status_code=200 if is_ready else 503, content=payload)


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

    job_id = create_job_id()
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
        persist=False,
    )
    background_tasks.add_task(_run_job, job_id, filename, content, content_type, language)
    return jobs[job_id]


@app.get("/jobs/{job_id}")
def get_job(job_id: str) -> dict:
    job = jobs.get(job_id)
    if not job:
        job = _load_job_state(job_id)
    if not job:
        raise HTTPException(status_code=404, detail="Job not found")
    return _public_job(job)


@app.post("/jobs/{job_id}/facts")
def replace_facts(job_id: str, request: FactReplacementRequest) -> dict:
    job = jobs.get(job_id) or _load_job_state(job_id)
    if not job:
        raise HTTPException(status_code=404, detail="Job not found")
    if job.get("status") != "complete":
        raise HTTPException(status_code=409, detail="Job is not complete")
    raw_key = job.get("artifacts", {}).get("raw", {}).get("key")
    if not raw_key:
        raise HTTPException(status_code=409, detail="Raw artifact is missing")

    storage = _storage()
    raw_text = storage.get_text(raw_key)
    replacement_map = {item.id: item.replacement for item in request.replacements}
    entity_replacement_map = {
        (item.entity_type, item.start, item.end): item.replacement
        for item in request.entity_replacements
        if item.replacement.strip()
    }
    pii_replacements = []
    for item in job.get("replacements", []):
        updated = dict(item)
        override = entity_replacement_map.get((updated.get("entity_type"), updated.get("start"), updated.get("end")))
        if override:
            updated["replacement"] = override
            updated["manual_replacement"] = True
        pii_replacements.append(updated)

    claims = []
    for claim in job.get("claims", []):
        updated = dict(claim)
        if updated["id"] in replacement_map:
            updated["replacement"] = replacement_map[updated["id"]]
            updated["applied"] = bool(replacement_map[updated["id"]].strip())
        claims.append(updated)

    final_text = apply_fact_replacements(raw_text, pii_replacements, claims)
    keys = build_storage_keys(job_id, job.get("filename", "document.txt"))
    final_artifact = storage.put_text(keys["final"], final_text)
    job.update(
        {
            "claims": claims,
            "replacements": pii_replacements,
            "final_text": final_text[:PREVIEW_CHARS],
            "truncated": len(final_text) > PREVIEW_CHARS,
            "_full_final_text": final_text,
        }
    )
    job.setdefault("artifacts", {})["final"] = final_artifact
    _set_job(job_id, job)
    return _public_job(jobs.get(job_id, job))


@app.get("/jobs/{job_id}/download", response_class=PlainTextResponse)
def download_job(job_id: str) -> str:
    job = jobs.get(job_id) or _load_job_state(job_id)
    if not job:
        raise HTTPException(status_code=404, detail="Job not found")
    if job.get("status") != "complete":
        raise HTTPException(status_code=409, detail="Job is not complete")
    if job.get("_full_final_text"):
        return job["_full_final_text"]
    if job.get("_full_anonymized_text"):
        return job["_full_anonymized_text"]
    storage = _storage()
    final_key = job.get("artifacts", {}).get("final", {}).get("key")
    if final_key:
        return storage.get_text(final_key)
    anonymized_key = job.get("artifacts", {}).get("anonymized", {}).get("key")
    if anonymized_key:
        return storage.get_text(anonymized_key)
    return job.get("final_text") or job.get("anonymized_text", "")
