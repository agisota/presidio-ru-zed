#!/usr/bin/env bash
set -euo pipefail

base_url="${1:-https://data.zed.md}"
sample_text="Иван Петров, паспорт 4510 123456, телефон +7 916 123-45-67, email ivan@example.ru"

echo "smoke: ${base_url}/health"
curl -fsS "${base_url}/health" >/dev/null

echo "smoke: ${base_url}/processor/health"
curl -fsS "${base_url}/processor/health" >/dev/null

echo "smoke: ${base_url}/processor/ready"
curl -fsS "${base_url}/processor/ready" >/dev/null

echo "smoke: ${base_url}/analyzer/health"
curl -fsS "${base_url}/analyzer/health" >/dev/null

echo "smoke: ${base_url}/anonymizer/health"
curl -fsS "${base_url}/anonymizer/health" >/dev/null

job_json="$(
  curl -fsS -X POST "${base_url}/processor/jobs" \
    -F "language=ru" \
    -F "text=${sample_text}"
)"

job_id="$(
  JOB_JSON="${job_json}" python3 - <<'PY'
import json
import os

print(json.loads(os.environ["JOB_JSON"])["job_id"])
PY
)"

echo "smoke: processor job ${job_id}"

last_json=""
for _ in $(seq 1 60); do
  last_json="$(curl -fsS "${base_url}/processor/jobs/${job_id}")"
  status="$(
    JOB_JSON="${last_json}" python3 - <<'PY'
import json
import os

print(json.loads(os.environ["JOB_JSON"]).get("status", "unknown"))
PY
  )"
  case "${status}" in
    complete) break ;;
    failed)
      echo "${last_json}" >&2
      exit 1
      ;;
  esac
  sleep 1
done

JOB_JSON="${last_json}" python3 - <<'PY'
import json
import os
import sys

payload = json.loads(os.environ["JOB_JSON"])
expected = [
    ("RU_PASSPORT", "<RU_PASSPORT>"),
    ("RU_PHONE_NUMBER", "<RU_PHONE_NUMBER>"),
    ("EMAIL_ADDRESS", "<EMAIL_ADDRESS>"),
]
actual = [(item.get("entity_type"), item.get("replacement")) for item in payload.get("replacements", [])]
if payload.get("status") != "complete":
    print(json.dumps(payload, ensure_ascii=False, indent=2), file=sys.stderr)
    raise SystemExit("job did not complete")
if actual != expected:
    print(json.dumps(payload.get("replacements", []), ensure_ascii=False, indent=2), file=sys.stderr)
    raise SystemExit(f"unexpected replacement mapping: {actual!r}")
print(f"smoke: job complete, replacements={actual}")
PY
