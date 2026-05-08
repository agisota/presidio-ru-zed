import json

from document_processor import (
    AnalyzerResult,
    DocumentProcessor,
    S3Storage,
    apply_fact_replacements,
    build_storage_keys,
    chunk_text,
    extract_claims,
    normalize_results,
)


class FakeStorage:
    def __init__(self):
        self.objects = {}

    def put_text(self, key, value, content_type="text/plain; charset=utf-8"):
        self.objects[key] = {
            "value": value,
            "content_type": content_type,
        }
        return {"bucket": "agent-artifacts", "key": key}

    def get_text(self, key):
        return self.objects[key]["value"]

    def check(self):
        return {"bucket": "agent-artifacts"}


class FakePresidio:
    def analyze(self, text, language):
        results = []
        if "4510 123456" in text:
            start = text.index("4510 123456")
            results.append(
                AnalyzerResult(
                    entity_type="RU_PASSPORT",
                    start=start,
                    end=start + len("4510 123456"),
                    score=1.0,
                )
            )
        if "test@example.com" in text:
            start = text.index("test@example.com")
            results.append(
                AnalyzerResult(
                    entity_type="EMAIL_ADDRESS",
                    start=start,
                    end=start + len("test@example.com"),
                    score=1.0,
                )
            )
        return results

    def anonymize(self, text, analyzer_results):
        anonymized = text
        items = []
        for result in sorted(analyzer_results, key=lambda item: item.start, reverse=True):
            replacement = f"<{result.entity_type}>"
            original = anonymized[result.start : result.end]
            anonymized = anonymized[: result.start] + replacement + anonymized[result.end :]
            items.append(
                {
                    "start": result.start,
                    "end": result.start + len(replacement),
                    "entity_type": result.entity_type,
                    "text": replacement,
                    "operator": "replace",
                    "original": original,
                }
            )
        return anonymized, list(reversed(items))


def test_chunk_text_preserves_global_offsets():
    chunks = list(chunk_text("abcdefghi", chunk_size=4))

    assert chunks == [
        (0, "abcd"),
        (4, "efgh"),
        (8, "i"),
    ]


def test_normalize_results_drops_overlaps_and_prefers_longer_first():
    normalized = normalize_results(
        [
            AnalyzerResult("PHONE_NUMBER", 10, 20, 0.4),
            AnalyzerResult("RU_INN", 10, 20, 0.9),
            AnalyzerResult("URL", 15, 25, 0.5),
            AnalyzerResult("EMAIL_ADDRESS", 30, 45, 1.0),
        ],
        text_length=50,
    )

    assert [item.entity_type for item in normalized] == ["RU_INN", "EMAIL_ADDRESS"]


def test_build_storage_keys_uses_private_s3_taxonomy():
    keys = build_storage_keys("job-123", "passport.txt", date="2026-05-08")

    assert keys["raw"].startswith("projects/presidio-ru-zed/work/2026-05-08/jobs/job-123/")
    assert keys["raw"].endswith("/raw/passport.txt")
    assert keys["anonymized"].endswith("/derived/anonymized.txt")
    assert keys["final"].endswith("/derived/final.txt")
    assert keys["manifest"].endswith("/MANIFEST.json")
    assert keys["state"].endswith("/state.json")


def test_s3_storage_uses_path_style_and_short_timeouts(monkeypatch):
    captured = {}

    def fake_client(service, **kwargs):
        captured["service"] = service
        captured.update(kwargs)
        return object()

    import boto3

    monkeypatch.setattr(boto3, "client", fake_client)
    monkeypatch.delenv("BIT_S3_BUCKET", raising=False)
    monkeypatch.delenv("AWS_ACCESS_KEY_ID", raising=False)
    monkeypatch.delenv("AWS_SECRET_ACCESS_KEY", raising=False)
    monkeypatch.setenv("BIT_S3_ENDPOINT", "https://bit.blenny-gar.ts.net")

    storage = S3Storage()

    assert captured["service"] == "s3"
    assert storage.bucket == "agent-artifacts"
    assert captured["endpoint_url"] == "https://bit.blenny-gar.ts.net"
    assert captured["aws_access_key_id"] == "data-zed-local"
    assert captured["aws_secret_access_key"] == "data-zed-local-password"
    assert captured["config"].s3["addressing_style"] == "path"
    assert captured["config"].connect_timeout == 3
    assert captured["config"].read_timeout == 30


def test_s3_storage_check_uses_head_bucket(monkeypatch):
    captured = {}

    class FakeClient:
        def head_bucket(self, **kwargs):
            captured.update(kwargs)

    def fake_client(service, **kwargs):
        captured["service"] = service
        return FakeClient()

    import boto3

    monkeypatch.setattr(boto3, "client", fake_client)
    monkeypatch.setenv("BIT_S3_BUCKET", "agent-artifacts")

    storage = S3Storage()

    assert storage.check() == {"bucket": "agent-artifacts"}
    assert captured["Bucket"] == "agent-artifacts"


def test_process_text_document_stores_artifacts_and_returns_visual_replacements():
    storage = FakeStorage()
    processor = DocumentProcessor(storage=storage, presidio=FakePresidio(), chunk_size=32)

    result = processor.process_text(
        job_id="job-123",
        filename="passport.txt",
        text="Паспорт 4510 123456 и email test@example.com",
        language="ru",
    )

    assert result["status"] == "complete"
    assert result["stats"]["entities"] == 2
    assert result["anonymized_text"] == "Паспорт <RU_PASSPORT> и email <EMAIL_ADDRESS>"
    assert [item["entity_type"] for item in result["replacements"]] == [
        "RU_PASSPORT",
        "EMAIL_ADDRESS",
    ]
    assert result["artifacts"]["raw"]["key"] in storage.objects
    assert result["artifacts"]["anonymized"]["key"] in storage.objects
    assert result["artifacts"]["manifest"]["key"] in storage.objects

    manifest = json.loads(storage.objects[result["artifacts"]["manifest"]["key"]]["value"])
    assert manifest["job_id"] == "job-123"
    assert manifest["filename"] == "passport.txt"


def test_build_replacements_aligns_reversed_anonymizer_items_by_output_position():
    text = "Иван Петров, паспорт 4510 123456, телефон +7 916 123-45-67, email ivan@example.ru"
    analyzer_results = [
        AnalyzerResult("RU_PASSPORT", 21, 32, 1.0),
        AnalyzerResult("RU_PHONE_NUMBER", 42, 58, 1.0),
        AnalyzerResult("EMAIL_ADDRESS", 66, 81, 1.0),
    ]
    anonymizer_items = [
        {"entity_type": "EMAIL_ADDRESS", "text": "<EMAIL_ADDRESS>", "start": 69, "end": 84},
        {"entity_type": "RU_PHONE_NUMBER", "text": "<RU_PHONE_NUMBER>", "start": 44, "end": 61},
        {"entity_type": "RU_PASSPORT", "text": "<RU_PASSPORT>", "start": 21, "end": 34},
    ]

    replacements = DocumentProcessor._build_replacements(text, analyzer_results, anonymizer_items)

    assert [(item["entity_type"], item["replacement"]) for item in replacements] == [
        ("RU_PASSPORT", "<RU_PASSPORT>"),
        ("RU_PHONE_NUMBER", "<RU_PHONE_NUMBER>"),
        ("EMAIL_ADDRESS", "<EMAIL_ADDRESS>"),
    ]
    assert [item["replacement_start"] for item in replacements] == [21, 44, 69]


def test_extract_claims_returns_replaceable_fact_spans_with_suggestions():
    claims = extract_claims(
        "Revenue grew 47% in 2026.\n"
        "Coordination & control scored 53%.\n"
        "Short label\n"
        "Leadership remained distinctive."
    )

    assert [claim["text"] for claim in claims] == [
        "Revenue grew 47% in 2026.",
        "Coordination & control scored 53%.",
        "Leadership remained distinctive.",
    ]
    assert claims[0]["id"] == "fact-1"
    assert claims[0]["suggestion"] != claims[0]["text"]
    assert claims[0]["start"] == 0


def test_apply_fact_replacements_overrides_overlapping_pii_and_persists_final_text():
    storage = FakeStorage()
    processor = DocumentProcessor(storage=storage, presidio=FakePresidio(), chunk_size=128)
    result = processor.process_text(
        job_id="job-123",
        filename="passport.txt",
        text="Passport 4510 123456 confirms revenue grew 47%.",
        language="ru",
    )
    claim = {
        "id": "fact-1",
        "start": 0,
        "end": len("Passport 4510 123456 confirms revenue grew 47%."),
        "text": "Passport 4510 123456 confirms revenue grew 47%.",
        "replacement": "A sanitized business statement replaces this claim.",
    }

    final = apply_fact_replacements(
        raw_text="Passport 4510 123456 confirms revenue grew 47%.",
        pii_replacements=result["replacements"],
        claims=[claim],
    )

    assert final == "A sanitized business statement replaces this claim."
