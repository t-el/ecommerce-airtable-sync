#!/usr/bin/env bash
# Run the webhook receiver (and, in mock mode, the mock Gumroad/Shopify/Airtable APIs).
set -euo pipefail
cd "$(dirname "$0")/.."
PY=${PYTHON:-python3}
set -a; [ -f .env ] && source .env; set +a
if [ "${MOCK_MODE:-true}" = "true" ]; then
  MOCK_AIRTABLE_PENALTY=${MOCK_AIRTABLE_PENALTY:-30} $PY -m uvicorn --factory mock_apis.server:create_mock_app --port 8710 --log-level warning &
  trap 'kill $! 2>/dev/null' EXIT
  echo "Mock APIs on http://127.0.0.1:8710"
fi
exec $PY -m uvicorn --factory ecom_sync.api:create_app --host 0.0.0.0 --port "${PORT:-8711}"
