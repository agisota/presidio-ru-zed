from __future__ import annotations

import io
import json
import os
import random
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


def _date_from_job_id(job_id: str) -> str | None:
    match = re.match(r"^(\d{4})(\d{2})(\d{2})-", job_id)
    if not match:
        return None
    return f"{match.group(1)}-{match.group(2)}-{match.group(3)}"


def build_storage_keys(job_id: str, filename: str, date: str | None = None) -> dict[str, str]:
    day = date or _date_from_job_id(job_id) or datetime.now(UTC).strftime("%Y-%m-%d")
    base = f"projects/presidio-ru-zed/work/{day}/jobs/{job_id}"
    safe_name = _safe_filename(filename)
    return {
        "raw": f"{base}/raw/{safe_name}",
        "anonymized": f"{base}/derived/anonymized.txt",
        "final": f"{base}/derived/final.txt",
        "manifest": f"{base}/MANIFEST.json",
        "state": f"{base}/state.json",
    }


def create_job_id(now: datetime | None = None) -> str:
    timestamp = now or datetime.now(UTC)
    import uuid

    return f"{timestamp.strftime('%Y%m%d')}-{uuid.uuid4().hex}"


def _auto_claim_replacement(text: str, index: int) -> str:
    rng = random.Random(f"{index}:{text}")

    def replace_number(match: re.Match) -> str:
        value = match.group(0)
        suffix = "%" if value.endswith("%") else ""
        if suffix:
            return f"{rng.randint(11, 89)}%"
        if len(re.sub(r"\D", "", value)) >= 4:
            return str(rng.randint(2027, 2042))
        return str(rng.randint(10, 99))

    replaced = re.sub(r"\d+(?:[.,]\d+)?%?", replace_number, text)
    if replaced != text:
        return replaced
    return f"Обобщенное утверждение {index + 1}"


def extract_claims(text: str, limit: int = 80) -> list[dict]:
    signals = re.compile(
        r"(\d|%|revenue|grew|scored|score|confirmed|confirms|remained|"
        r"состав|рост|сниз|увелич|подтверж|показател|утверж|доля|выруч|прибыл)",
        re.IGNORECASE,
    )
    claims: list[dict] = []
    seen: set[str] = set()
    pattern = re.compile(r"[^\n.!?]+(?:[.!?]+|$)")
    for match in pattern.finditer(text):
        candidate = match.group(0).strip()
        if len(candidate) < 18:
            continue
        if not signals.search(candidate) and len(candidate.split()) < 4:
            continue
        if not signals.search(candidate) and not re.search(r"\b(is|are|was|were|будет|является|имеет)\b", candidate, re.I):
            continue
        normalized = re.sub(r"\s+", " ", candidate)
        if normalized in seen:
            continue
        seen.add(normalized)
        start = match.start() + len(match.group(0)) - len(match.group(0).lstrip())
        end = start + len(candidate)
        claim_index = len(claims)
        claims.append(
            {
                "id": f"fact-{claim_index + 1}",
                "start": start,
                "end": end,
                "text": candidate,
                "suggestion": _auto_claim_replacement(candidate, claim_index),
                "replacement": "",
                "applied": False,
            }
        )
        if len(claims) >= limit:
            break
    return claims


def _overlaps(left: tuple[int, int], right: tuple[int, int]) -> bool:
    return left[0] < right[1] and right[0] < left[1]


def apply_fact_replacements(raw_text: str, pii_replacements: list[dict], claims: list[dict]) -> str:
    fact_ops: list[tuple[int, int, str, str]] = []
    for claim in claims:
        replacement = str(claim.get("replacement") or "").strip()
        if not replacement:
            continue
        start = int(claim["start"])
        end = int(claim["end"])
        if 0 <= start < end <= len(raw_text):
            fact_ops.append((start, end, replacement, "fact"))

    fact_ranges = [(start, end) for start, end, _, _ in fact_ops]
    pii_ops: list[tuple[int, int, str, str]] = []
    for item in pii_replacements:
        start = int(item["start"])
        end = int(item["end"])
        if any(_overlaps((start, end), fact_range) for fact_range in fact_ranges):
            continue
        replacement = str(item.get("replacement") or f"<{item['entity_type']}>")
        pii_ops.append((start, end, replacement, "pii"))

    operations = sorted(fact_ops + pii_ops, key=lambda op: (op[0], 0 if op[3] == "fact" else 1, -(op[1] - op[0])))
    accepted: list[tuple[int, int, str, str]] = []
    for operation in operations:
        if accepted and operation[0] < accepted[-1][1]:
            continue
        accepted.append(operation)

    cursor = 0
    parts: list[str] = []
    for start, end, replacement, _ in accepted:
        parts.append(raw_text[cursor:start])
        parts.append(replacement)
        cursor = end
    parts.append(raw_text[cursor:])
    return "".join(parts)


class S3Storage:
    def __init__(
        self,
        bucket: str | None = None,
        endpoint_url: str | None = None,
        region_name: str | None = None,
    ):
        import boto3
        from botocore.config import Config

        self.bucket = bucket or os.environ.get("BIT_S3_BUCKET", "agent-artifacts")
        config = Config(
            connect_timeout=float(os.environ.get("S3_CONNECT_TIMEOUT", "3")),
            read_timeout=float(os.environ.get("S3_READ_TIMEOUT", "30")),
            retries={"max_attempts": int(os.environ.get("S3_MAX_ATTEMPTS", "2")), "mode": "standard"},
            s3={"addressing_style": "path"},
        )
        self.client = boto3.client(
            "s3",
            endpoint_url=endpoint_url or os.environ.get("BIT_S3_ENDPOINT") or os.environ.get("AWS_ENDPOINT_URL_S3"),
            region_name=region_name or os.environ.get("BIT_S3_REGION") or os.environ.get("AWS_REGION", "us-east-1"),
            aws_access_key_id=os.environ.get("AWS_ACCESS_KEY_ID", "data-zed-local"),
            aws_secret_access_key=os.environ.get("AWS_SECRET_ACCESS_KEY", "data-zed-local-password"),
            config=config,
        )

    def put_text(self, key: str, value: str, content_type: str = "text/plain; charset=utf-8") -> dict:
        self.client.put_object(
            Bucket=self.bucket,
            Key=key,
            Body=value.encode("utf-8"),
            ContentType=content_type,
        )
        return {"bucket": self.bucket, "key": key}

    def get_text(self, key: str) -> str:
        response = self.client.get_object(Bucket=self.bucket, Key=key)
        return response["Body"].read().decode("utf-8")


class LocalStorage:
    def __init__(self):
        self.objects: dict[str, dict] = {}

    def put_text(self, key: str, value: str, content_type: str = "text/plain; charset=utf-8") -> dict:
        self.objects[key] = {"value": value, "content_type": content_type}
        return {"bucket": "local-dev", "key": key}

    def get_text(self, key: str) -> str:
        return self.objects[key]["value"]


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
        claims = extract_claims(text)

        manifest = {
            "job_id": job_id,
            "filename": filename,
            "language": language,
            "created_at": datetime.now(UTC).isoformat(),
            "stats": {
                "characters": len(text),
                "entities": len(analyzer_results),
                "replacements": len(replacements),
                "claims": len(claims),
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
            "claims": claims,
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
        ordered_items = sorted(
            anonymizer_items,
            key=lambda item: (
                item.get("start") is None,
                int(item.get("start") or 0),
                int(item.get("end") or 0),
            ),
        )
        replacements = []
        for index, result in enumerate(analyzer_results):
            anonymizer_item = ordered_items[index] if index < len(ordered_items) else {}
            replacement = anonymizer_item.get("text")
            replacements.append(
                {
                    "entity_type": result.entity_type,
                    "score": result.score,
                    "start": result.start,
                    "end": result.end,
                    "original": text[result.start : result.end],
                    "replacement": replacement or f"<{result.entity_type}>",
                    "replacement_start": anonymizer_item.get("start"),
                    "replacement_end": anonymizer_item.get("end"),
                }
            )
        return replacements
