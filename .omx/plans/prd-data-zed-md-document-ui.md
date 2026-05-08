# data.zed.md Document UI Plan

## Requirements Summary

Replace the current API-oriented landing page with an operational Russian-first UI for document anonymization:

- Users can upload or paste a text document.
- The UI calls the existing Presidio Analyzer API and shows detected PII visually in the original text.
- The UI calls the existing Anonymizer API and shows the anonymized result.
- Users can inspect each replacement: entity type, score, source text, replacement text, and character range.
- Users can copy or download the anonymized text.

## Assumptions And Boundaries

- This pass is browser-only and does not store uploaded content server-side.
- Supported upload formats in this pass: `.txt`, `.md`, `.csv`, `.json`, `.log`, and other `text/*` files.
- PDF/DOCX/OCR extraction is out of scope for this immediate repair because it requires a backend extractor, dependency/security review, and larger runtime changes.
- Existing endpoints remain authoritative:
  - `/analyzer/analyze`
  - `/anonymizer/anonymize`
  - `/health`

## Component / Screen Map

| Surface | Hierarchy | Ownership |
| --- | --- | --- |
| Upload/Input | file dropzone, paste textarea, language selector, analyze button | `deploy/data-zed-md/site/index.html` |
| Document Preview | original text, inline detected-span highlights, legend | `deploy/data-zed-md/site/index.html` |
| Replacement Review | table of detected entities and replacements | `deploy/data-zed-md/site/index.html` |
| Anonymized Output | anonymized text preview, copy/download actions | `deploy/data-zed-md/site/index.html` |
| Status/Errors | health/API status, unsupported-file warnings | `deploy/data-zed-md/site/index.html` |

## Options And Tradeoffs

### Option A: Static Browser UI

Pros:
- No new backend service.
- No persistent document storage.
- Fast deploy on the existing nginx gateway.
- Uses already verified Analyzer/Anonymizer APIs.

Cons:
- No PDF/DOCX parsing in this pass.
- Large files run in the browser and may be slow.

### Option B: Document Extraction Backend

Pros:
- Can support PDF/DOCX/OCR and richer document formats.
- Better place for size limits, queues, and conversion logging.

Cons:
- New service, new dependencies, new attack surface.
- Needs auth, rate limiting, file-size limits, deletion policy, and storage policy before public exposure.

## Decision

Implement Option A now. Add explicit unsupported-file messaging for PDF/DOCX and keep the next backend extractor as follow-up work.

## Acceptance Criteria

- Opening `https://data.zed.md` shows a usable document anonymization app, not a curl/API landing page.
- Pasting Russian text with passport, INN, SNILS, phone, and email produces highlighted detected spans in the original preview.
- The replacement table lists entity type, score, original text, replacement text, and range.
- The anonymized output is visible and copy/download controls work.
- Unsupported binary formats show a clear error without sending content.
- Existing public API health checks still pass.

## Implementation Steps

1. Replace `deploy/data-zed-md/site/index.html` with a single-page document UI.
2. Keep API calls same-origin through nginx.
3. Add client-side text-file loading and size guard.
4. Implement safe HTML escaping and span rendering from Analyzer offsets.
5. Implement anonymizer call and replacement-diff table.
6. Verify locally with static parsing and against deployed domain after redeploy.

## Risks And Mitigations

- Risk: entity offsets corrupt HTML rendering. Mitigation: escape all non-highlighted and highlighted text segments.
- Risk: overlapping Analyzer results create broken highlights. Mitigation: sort by start/end and ignore overlaps after the first accepted span.
- Risk: user expects PDF/DOCX. Mitigation: explicit UI warning and follow-up backend extractor plan.
- Risk: public service accepts sensitive data. Mitigation: no browser persistence and no new server-side storage in this pass.

## Verification Steps

- Parse HTML with a basic syntax/smoke check.
- Run `curl` public health checks.
- Run Analyzer/Anonymizer API smoke with Russian PII.
- Capture deployed page HTML/title and API proof after rollout.

## Follow-ups

- Add backend extractor for PDF/DOCX with file-size limits and no retention.
- Add auth/rate limiting before heavy document processing.
- Add visual PDF/DOCX redaction export if document fidelity becomes required.
