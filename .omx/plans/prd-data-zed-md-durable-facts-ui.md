# data.zed.md Durable Jobs And Fact Replacement Plan

## Reformulated Task

Finish the production workflow for `data.zed.md`:

- make processor job status durable across restarts by persisting job metadata in private `bit` S3;
- show original and anonymized documents side-by-side with synchronized scrolling;
- add arrow navigation across detected replacements/facts;
- extract factual claims/statements from the document and let the user manually replace them;
- add an automatic "replace facts and statements" path which proposes randomized/sanitized values;
- let the user save and download a final text with both PII and chosen fact replacements applied.

## Assumptions And Boundaries

- Job metadata and final text are stored server-side in `bit` S3. Browser never receives S3 credentials.
- Claim extraction in this pass is deterministic heuristic extraction, not LLM semantic verification.
- Facts are extracted as text spans from the raw extracted text. User-entered fact replacements override overlapping PII replacements.
- This is still text-first for visual review. Layout-preserving PDF/DOCX redaction remains out of scope.

## Schema View

```text
JobState
  job_id PK
  status indexed in memory while running
  progress
  filename
  language
  created_at
  completed_at
  source_text preview
  anonymized_text preview
  final_text preview nullable
  stats JSON
  artifacts JSON
    raw.key
    anonymized.key
    final.key nullable
    manifest.key
    state.key

Replacement
  job_id FK -> JobState.job_id
  entity_type
  score
  start
  end
  original
  replacement
  replacement_start nullable
  replacement_end nullable

Claim
  job_id FK -> JobState.job_id
  id local key fact-N
  start
  end
  text
  suggestion
  replacement nullable
  applied bool
```

Indexing assumptions:

- `job_id` includes UTC date prefix: `YYYYMMDD-<uuid>`.
- S3 job state key is reconstructable from `job_id`.
- Completed jobs are read from S3 if not present in process memory.

## Data Flow

```text
Upload/Paste
  -> Processor create job
  -> persist queued state.json
  -> extract text
  -> persist extracting/processing state.json
  -> Analyzer chunks
  -> Anonymizer
  -> extract Claims
  -> persist raw/anonymized/manifest/state
  -> UI polls state
  -> user edits Claim replacements
  -> POST /jobs/{id}/facts
  -> processor loads raw + state
  -> apply fact ops + non-overlapping PII ops
  -> persist final.txt + updated state.json
  -> UI downloads final
```

## Component / Screen Map

| Surface | Hierarchy | Ownership |
| --- | --- | --- |
| Side-by-side review | original panel, anonymized/final panel, synchronized scroll | `site/index.html` |
| Finding navigator | previous/next buttons, active counter, row click jump | `site/index.html` |
| PII replacements | replacement table, clickable rows | `site/index.html` |
| Fact replacements | claims table, manual input, auto replace, save final | `site/index.html` |
| Durable job state | S3 state load/save, fallback from memory | `processor/app.py`, `processor/document_processor.py` |
| Final text | apply user facts + non-overlapping PII, store `final.txt` | `processor/document_processor.py` |

## Options And Tradeoffs

### Option A: S3-backed metadata JSON

Pros:
- Uses existing private storage.
- No new service.
- Survives processor restarts for completed jobs.
- Easy rollback.

Cons:
- Running jobs are not resumed after restart.
- S3 is not a queue.

### Option B: Redis queue plus S3 artifacts

Pros:
- Better for running job recovery and high throughput.

Cons:
- More infra and ops surface.
- Not necessary for this repair pass.

### Option C: Browser-only claim replacement

Pros:
- Fast UI-only implementation.

Cons:
- Final text is not durable.
- Cannot safely combine full raw text, PII replacements, and large documents.

## Decision

Implement Option A now. Keep endpoint shapes compatible with a future Redis queue.

## Acceptance Criteria

- Existing completed job state can be retrieved after clearing process memory.
- Final text can be generated and persisted from user fact replacements.
- UI shows synchronized original/result panes.
- Arrow navigation jumps through PII replacements.
- Claim table supports manual and automatic replacement suggestions.
- Public deploy verifies health, job, persisted manifest/state, final download, and browser screenshot.
