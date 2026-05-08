from __future__ import annotations

import io
import json
import os
import re
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from pathlib import PurePosixPath
from typing import Iterable


@dataclass(frozen=True)
class AnalyzerResult:
    entity_type: str
    start: int
    end: int
    score: float

    @classmethod
    def from_api(cls, item: dict, offset: int = 0) -> "AnalyzerResult":
        return cls(
            entity_type=str(item["entity_type"]),
            start=int(item["start"]) + offset,
            end=int(item["end"]) + offset,
            score=float(item.get("score", 0)),
        )

    def to_api(self) -> dict:
        return asdict(self)


def chunk_text(text: str, chunk_size: int = 12000) -> Iterable[tuple[int, str]]:
    if chunk_size <= 0:
        raise ValueError("chunk_size must be positive")
    start = 0
    while start < len(text):
        end = min(start + chunk_size, len(text))
        if end < len(text) and not text[end - 1].isspace() and not text[end].isspace():
            floor = start + max(1, chunk_size // 2)
            boundary = text.rfind(" ", floor, end)
            if boundary != -1:
                end = boundary + 1
        yield start, text[start:end]
        start = end


def normalize_results(results: list[AnalyzerResult], text_length: int) -> list[AnalyzerResult]:
    valid = [
        item
        for item in results
        if 0 <= item.start < item.end <= text_length
    ]
    ordered = sorted(
        valid,
        key=lambda item: (item.start, -(item.end - item.start), -item.score, item.entity_type),
    )
    accepted: list[AnalyzerResult] = []
    for item in ordered:
        previous = accepted[-1] if accepted else None
        if previous and item.start < previous.end:
            continue
        accepted.append(item)
    return accepted


def _safe_filename(filename: str) -> str:
    name = PurePosixPath(filename or "document.txt").name
    name = re.sub(r"[^A-Za-z0-9._-]+", "-", name).strip(".-")
    return name or "document.txt"


def build_storage_keys(job_id: str, filename: str, date: str | None = None) -> dict[str, str]:
    day = date or datetime.now(UTC).strftime("%Y-%m-%d")
    base = f"projects/presidio-ru-zed/work/{day}/jobs/{job_id}"
    safe_name = _safe_filename(filename)
    return {
        "raw": f"{base}/raw/{safe_name}",
        "anonymized": f"{base}/derived/anonymized.txt",
        "manifest": f"{base}/MANIFEST.json",
    }


class S3Storage:
    def __init__(
        self,
        bucket: str | None = None,
        endpoint_url: str | None = None,
        region_name: str | None = None,
    ):
        import boto3

        self.bucket = bucket or os.environ["BIT_S3_BUCKET"]
        self.client = boto3.client(
            "s3",
            endpoint_url=endpoint_url or os.environ.get("BIT_S3_ENDPOINT") or os.environ.get("AWS_ENDPOINT_URL_S3"),
            region_name=region_name or os.environ.get("BIT_S3_REGION") or os.environ.get("AWS_REGION", "us-east-1"),
        )

    def put_text(self, key: str, value: str, content_type: str = "text/plain; charset=utf-8") -> dict:
        self.client.put_object(
            Bucket=self.bucket,
            Key=key,
            Body=value.encode("utf-8"),
            ContentType=content_type,
        )
        return {"bucket": self.bucket, "key": key}


class LocalStorage:
    def __init__(self):
        self.objects: dict[str, dict] = {}

    def put_text(self, key: str, value: str, content_type: str = "text/plain; charset=utf-8") -> dict:
        self.objects[key] = {"value": value, "content_type": content_type}
        return {"bucket": "local-dev", "key": key}


class PresidioClient:
    def __init__(
        self,
        analyzer_url: str = "http://analyzer:3000",
        anonymizer_url: str = "http://anonymizer:3000",
        timeout: float = 120.0,
    ):
        import httpx

        self.analyzer_url = analyzer_url.rstrip("/")
        self.anonymizer_url = anonymizer_url.rstrip("/")
        self.client = httpx.Client(timeout=timeout)

    def analyze(self, text: str, language: str) -> list[AnalyzerResult]:
        response = self.client.post(
            f"{self.analyzer_url}/analyze",
            json={"text": text, "language": language},
        )
        response.raise_for_status()
        return [AnalyzerResult.from_api(item) for item in response.json()]

    def anonymize(self, text: str, analyzer_results: list[AnalyzerResult]) -> tuple[str, list[dict]]:
        response = self.client.post(
            f"{self.anonymizer_url}/anonymize",
            json={"text": text, "analyzer_results": [item.to_api() for item in analyzer_results]},
        )
        response.raise_for_status()
        payload = response.json()
        return payload.get("text", ""), payload.get("items", [])


def extract_text(filename: str, content: bytes, content_type: str | None = None) -> str:
    extension = PurePosixPath(filename or "").suffix.lower()
    if extension == ".pdf" or content_type == "application/pdf":
        from pypdf import PdfReader

        reader = PdfReader(io.BytesIO(content))
        return "\n\n".join(page.extract_text() or "" for page in reader.pages).strip()
    if extension == ".docx" or content_type == "application/vnd.openxmlformats-officedocument.wordprocessingml.document":
        from docx import Document

        document = Document(io.BytesIO(content))
        return "\n".join(paragraph.text for paragraph in document.paragraphs).strip()
    return content.decode("utf-8", errors="replace")


class DocumentProcessor:
    def __init__(
        self,
        storage,
        presidio,
        chunk_size: int = 12000,
        preview_chars: int = 300000,
    ):
        self.storage = storage
        self.presidio = presidio
        self.chunk_size = chunk_size
        self.preview_chars = preview_chars

    def process_text(self, job_id: str, filename: str, text: str, language: str = "ru") -> dict:
        keys = build_storage_keys(job_id, filename)
        raw_artifact = self.storage.put_text(keys["raw"], text)

        analyzer_results: list[AnalyzerResult] = []
        for offset, chunk in chunk_text(text, self.chunk_size):
            chunk_results = self.presidio.analyze(chunk, language)
            analyzer_results.extend(
                AnalyzerResult(
                    entity_type=item.entity_type,
                    start=item.start + offset,
                    end=item.end + offset,
                    score=item.score,
                )
                for item in chunk_results
            )

        analyzer_results = normalize_results(analyzer_results, len(text))
        anonymized_text, anonymizer_items = self.presidio.anonymize(text, analyzer_results)
        anonymized_artifact = self.storage.put_text(keys["anonymized"], anonymized_text)
        replacements = self._build_replacements(text, analyzer_results, anonymizer_items)

        manifest = {
            "job_id": job_id,
            "filename": filename,
            "language": language,
            "created_at": datetime.now(UTC).isoformat(),
            "stats": {
                "characters": len(text),
                "entities": len(analyzer_results),
                "replacements": len(replacements),
                "chunks": max(1, (len(text) + self.chunk_size - 1) // self.chunk_size),
            },
            "artifacts": {
                "raw": raw_artifact,
                "anonymized": anonymized_artifact,
            },
        }
        manifest_artifact = self.storage.put_text(
            keys["manifest"],
            json.dumps(manifest, ensure_ascii=False, indent=2),
            "application/json; charset=utf-8",
        )

        truncated = len(text) > self.preview_chars or len(anonymized_text) > self.preview_chars
        return {
            "job_id": job_id,
            "status": "complete",
            "filename": filename,
            "language": language,
            "source_text": text[: self.preview_chars],
            "anonymized_text": anonymized_text[: self.preview_chars],
            "truncated": truncated,
            "preview_chars": self.preview_chars,
            "analyzer_results": [item.to_api() for item in analyzer_results],
            "replacements": replacements,
            "stats": manifest["stats"],
            "artifacts": {
                "raw": raw_artifact,
                "anonymized": anonymized_artifact,
                "manifest": manifest_artifact,
            },
            "_full_anonymized_text": anonymized_text,
        }

    @staticmethod
    def _build_replacements(text: str, analyzer_results: list[AnalyzerResult], anonymizer_items: list[dict]) -> list[dict]:
        replacements = []
        for index, result in enumerate(analyzer_results):
            replacement = anonymizer_items[index].get("text") if index < len(anonymizer_items) else None
            replacements.append(
                {
                    "entity_type": result.entity_type,
                    "score": result.score,
                    "start": result.start,
                    "end": result.end,
                    "original": text[result.start : result.end],
                    "replacement": replacement or f"<{result.entity_type}>",
                }
            )
        return replacements
