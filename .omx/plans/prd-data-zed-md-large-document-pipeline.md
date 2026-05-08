# data.zed.md Large Document Pipeline Plan

## Reformulated Task

Upgrade `data.zed.md` from a browser-only text anonymizer into a document workflow for larger uploads:

- upload text-like, PDF, or DOCX documents through the UI;
- process large content through a backend job endpoint instead of a 2 MB browser limit;
- store raw input, anonymized output, and manifest artifacts in private `bit` S3;
- show progress, visual source highlights, replacement table, and downloadable anonymized output;
- improve throughput with chunked Analyzer calls and avoid keeping secrets in the browser.

## Assumptions And Boundaries

- `bit` S3 is private Tailnet infrastructure and must not be exposed to the public browser.
- Browser should send documents only to `data.zed.md`; S3 credentials live only on the server.
- First implementation uses an in-process job runner, not Redis/Celery. This is enough for the current VM and keeps deployment small.
- Raw/anonymized artifacts are stored for operational recovery. Deletion/retention policy remains a follow-up unless explicitly requested.
- Visual PDF/DOCX layout-preserving redaction is not included. PDF/DOCX are extracted to text, then anonymized.

## Data Flow

```text
Sources
  Browser upload/paste
    |
    v
Processor /processor/jobs
  validate size/type
  extract text
  store raw -> bit S3
    |
    v
Chunker
  split text into bounded ranges
  preserve global offsets
    |
    v
Analyzer
  POST chunks to analyzer:3000/analyze
  normalize/dedupe overlaps
    |
    v
Anonymizer
  POST full text + normalized results to anonymizer:3000/anonymize
    |
    v
Storage
  store anonymized text -> bit S3
  store manifest/result JSON -> bit S3
    |
    v
Consumers
  UI polls /processor/jobs/{id}
  UI renders highlights, replacements, output, download links

Checkpoints
  /processor/health
  job status/progress
  S3 object keys in manifest
  public Analyzer/Anonymizer smoke
```

## Component / Screen Map

| Surface | Hierarchy | Ownership |
| --- | --- | --- |
| Upload | file dropzone, text paste, type/size hints, submit | `deploy/data-zed-md/site/index.html` |
| Job Status | queued/running/complete/failed, progress bar, artifact info | `deploy/data-zed-md/site/index.html` |
| Preview | original highlights with global offsets | `deploy/data-zed-md/site/index.html` |
| Replacements | type, score, source, replacement, range | `deploy/data-zed-md/site/index.html` |
| Output | anonymized preview, copy, download | `deploy/data-zed-md/site/index.html` |
| Processor API | health, create job, read job, download result | `deploy/data-zed-md/processor/*` |
| Gateway | static UI, Analyzer/Anonymizer proxy, Processor proxy | `deploy/data-zed-md/nginx.conf` |
| Compose | processor service + S3/env wiring | `deploy/data-zed-md/docker-compose.yml` |

## Options And Tradeoffs

### Option A: In-process FastAPI job runner

Pros:
- Fastest production repair.
- One small container.
- No public S3 exposure.
- Easy to test with fake storage and fake Presidio clients.

Cons:
- Jobs are memory-indexed while running.
- VM restart loses in-memory status, though S3 artifacts remain.

### Option B: Queue worker with Redis/Celery

Pros:
- Better durability and retry semantics.
- Cleaner horizontal scaling.

Cons:
- More services and ops surface.
- More secrets/config and failure modes on a small 4 GiB VM.

### Option C: Direct browser multipart to S3

Pros:
- Offloads upload bandwidth.

Cons:
- Requires presigned URL surface and browser-facing object policy.
- Does not solve Analyzer/Anonymizer orchestration.

## Decision

Implement Option A now. Keep the API shape compatible with a future queue backend by using explicit job IDs and polling.

## TDD Acceptance Criteria

- Chunking preserves global offsets.
- Overlapping Analyzer results are normalized deterministically.
- Storage keys follow the private S3 taxonomy.
- A fake end-to-end job stores raw/anonymized artifacts and returns replacements.
- Gateway exposes `/processor/`.
- UI can submit file/text jobs, poll progress, render results, and download output.
- Public deploy passes `/health`, `/processor/health`, document job smoke, and browser screenshot.
