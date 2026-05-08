# Dataclenear Improvement Plan

Date: 2026-05-08
Mode: EXECUTION + RECOMMENDATION
Skills initialized: analyze, plan, web-design-guidelines

## Current System Facts

- Product surface: `deploy/data-zed-md/site/index.html` is a single-file Russian UI for document upload, side-by-side review, manual PII replacement, fact replacement, copy, and download.
- Runtime shape: `deploy/data-zed-md/docker-compose.yml` runs `gateway`, `processor`, `minio`, `minio-init`, `analyzer`, and `anonymizer`.
- Public target: README documents `data.zed.md`; `deploy/data-zed-md/traefik-data-zed-md.yml` routes `Host(data.zed.md)` to `presidio-ru-gateway:8080`.
- Local gateway bind: compose maps `127.0.0.1:18088:8080`; public ingress is expected to proxy into that gateway.
- Live smoke on 2026-05-08:
  - `https://data.zed.md/health` returned OK.
  - `https://data.zed.md/processor/health` returned OK.
  - `https://data.zed.md/analyzer/health` returned OK.
  - `https://data.zed.md/anonymizer/health` returned 502.
  - A synthetic `/processor/jobs` run completed and detected `RU_PASSPORT`, `RU_PHONE_NUMBER`, and `EMAIL_ADDRESS`.
- Live functional bug found: `anonymized_text` was correct, but UI `replacements` mapped `anonymizer_items` by array index and swapped replacement labels for passport/email when the anonymizer returned items in reverse output order.

## Principles

1. Fix runtime correctness before cosmetic redesign.
2. Keep the first pass source-compatible: no frontend build chain, no new runtime dependency.
3. Surface health truth in the UI instead of hiding degraded public routes.
4. Preserve private S3 policy and never print or commit real credentials.
5. Keep changes small enough to verify with processor tests and public endpoint smoke.

## Work Plan

### Phase 1: Runtime Stability

1. Update `deploy/data-zed-md/nginx.conf` so analyzer and anonymizer use Docker DNS resolver-backed variable upstreams, matching the existing processor pattern.
2. Preserve prefix stripping for `/analyzer/*` and `/anonymizer/*` via explicit rewrites.
3. Verify config shape statically and, after deployment, re-smoke `https://data.zed.md/anonymizer/health`.

Acceptance:
- `/analyzer/health` and `/anonymizer/health` keep the public route contract.
- A restarted analyzer/anonymizer container does not leave Nginx pinned to a stale Docker IP.

### Phase 2: Processor/UI Replacement Correctness

1. Add regression coverage for reversed `anonymizer_items`.
2. Sort anonymizer items by output `start/end` before mapping them to analyzer results.
3. Keep fallback behavior for missing items as `<ENTITY_TYPE>`.

Acceptance:
- Regression test proves `RU_PASSPORT -> <RU_PASSPORT>`, `RU_PHONE_NUMBER -> <RU_PHONE_NUMBER>`, `EMAIL_ADDRESS -> <EMAIL_ADDRESS>` even when anonymizer items arrive in reverse.
- Existing processor tests still pass.

### Phase 3: Visual & Operational UX

1. Add a compact service status strip for gateway, processor, analyzer, anonymizer.
2. Make health status partial-aware: show all OK, degraded `n/4`, or critical API failure.
3. Improve accessibility and interaction states: visible focus, hover states, icon button `aria-label`, labels/names/autocomplete for form controls, reduced-motion handling, touch manipulation.
4. Keep the operational dashboard-first layout; do not turn the app into a marketing landing page.

Acceptance:
- First viewport still starts with the usable document workspace.
- Public route degradation is visible without opening DevTools.
- Keyboard focus is visible on controls.

### Phase 4: Next Technical Hardening

1. Add a processor `/ready` endpoint that checks analyzer/anonymizer/internal storage readiness without writing data.
2. Add a gateway readiness probe or deployment smoke script for all public routes.
3. Replace in-memory `jobs` as primary state with S3-backed lookup from creation time or a small persistent index.
4. Add upload/job rate limits and optional auth before sharing outside trusted users.
5. Add an integration test that exercises text upload through the processor API with fake storage and real replacement ordering.

### Phase 5: Larger Visual Product Pass

1. Add entity filters and confidence threshold controls.
2. Add per-entity color legend and table filtering.
3. Add review queue/history using S3 manifests.
4. Add export bundle: final text, JSON manifest, CSV replacement log.
5. Add PDF/DOCX preview/redaction roadmap only after the text workflow is stable.

## Rollback

- Runtime rollback: revert `deploy/data-zed-md/nginx.conf`.
- Processor rollback: revert `_build_replacements` plus the regression test.
- UI rollback: revert `deploy/data-zed-md/site/index.html`.
- Production rollback after deployment: redeploy previous git commit with the same compose files.

## Verification Commands

```bash
/private/tmp/presidio-processor-venv/bin/python -m pytest tests -q
curl -fsS https://data.zed.md/health
curl -fsS https://data.zed.md/processor/health
curl -fsS https://data.zed.md/analyzer/health
curl -fsS https://data.zed.md/anonymizer/health
```

