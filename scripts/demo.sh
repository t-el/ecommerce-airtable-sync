#!/usr/bin/env bash
# End-to-end demo in mock mode (no API keys, no network): reconcile -> backfill -> webhooks -> fix -> replay -> reconcile.
set -euo pipefail
cd "$(dirname "$0")/.."
PY=${PYTHON:-python3}
MOCK_PORT=${MOCK_PORT:-8710}; API_PORT=${API_PORT:-8711}
export MOCK_MODE=true MOCK_BASE_URL="http://127.0.0.1:$MOCK_PORT" DATA_DIR=${DATA_DIR:-data/demo} LOG_JSON=false \
       RETRY_BASE_DELAY=0.2 AIRTABLE_429_COOLDOWN=1 SHOPIFY_WEBHOOK_SECRET=shpss_demo_secret GUMROAD_WEBHOOK_TOKEN=demo-token
rm -rf "$DATA_DIR" && mkdir -p "$DATA_DIR"

# Mock Airtable penalty is 1 s instead of the real 30 s so the demo stays short.
MOCK_AIRTABLE_PENALTY=1 $PY -m uvicorn --factory mock_apis.server:create_mock_app --port "$MOCK_PORT" --log-level warning &
MOCK_PID=$!
$PY -m uvicorn --factory ecom_sync.api:create_app --port "$API_PORT" --log-level warning > "$DATA_DIR/api.log" 2>&1 &
API_PID=$!
trap 'kill $MOCK_PID $API_PID 2>/dev/null' EXIT
for port in "$MOCK_PORT" "$API_PORT"; do
  for _ in $(seq 1 60); do curl -sf "localhost:$port/${port/$API_PORT/health}" >/dev/null 2>&1 && break; curl -sf "localhost:$port/admin/state" >/dev/null 2>&1 && break; sleep 0.25; done
done

echo "### 1. Reconciliation BEFORE: what the client's Airtable base looks like today"
$PY -m ecom_sync.cli reconcile 2>&1 | grep -v "fetched page" || true

echo; echo "### 2. Backfill from Gumroad + Shopify (Shopify page 2 returns 503 twice; another tool bursts the base -> one 429)"
curl -s -X POST "localhost:$MOCK_PORT/admin/faults" -H 'content-type: application/json' -d '{"shopify_503": 2, "airtable_429_at": 4}' >/dev/null
$PY -m ecom_sync.cli backfill --source all 2>&1 | grep -v "fetched page\|order synced"

echo; echo "### 3. Run it again: idempotent. Good orders cost 0 writes; only the 2 dead-lettered ones are retried"
curl -s -X POST "localhost:$MOCK_PORT/admin/faults" -H 'content-type: application/json' -d '{}' >/dev/null
$PY -m ecom_sync.cli backfill --source all --full 2>&1 | grep -E "^(gumroad|shopify):|^Airtable"

echo; echo "### 4. Live webhooks (Shopify HMAC-signed, Gumroad Ping with secret token)"
$PY - <<PY
import httpx
from mock_apis import fixtures
from mock_apis.webhooks import gumroad_ping, shopify_webhook
API, MOCK = "http://127.0.0.1:$API_PORT", "http://127.0.0.1:$MOCK_PORT"
def show(label, r): print(f"  {label:<58} HTTP {r.status_code}  {r.json().get('status', r.json().get('detail'))}")

paid = fixtures.SHOPIFY_ORDERS[5] | {"financial_status": "paid", "updated_at": "2026-11-18T08:00:00Z"}
httpx.patch(f"{MOCK}/admin/shopify/orders/5512000006", json={"financial_status": "paid", "updated_at": "2026-11-18T08:00:00Z"})
body, h = shopify_webhook(paid, "orders/paid", "shpss_demo_secret", "wh-1006-paid")
show("Shopify orders/paid #1006 (pending -> paid)", httpx.post(f"{API}/webhooks/shopify", content=body, headers=h))
show("  same webhook delivered again (Shopify retries)", httpx.post(f"{API}/webhooks/shopify", content=body, headers=h))
body, h = shopify_webhook(fixtures.SHOPIFY_ORDERS[5], "orders/updated", "shpss_demo_secret", "wh-1006-old")
show("  older 'pending' event arriving late", httpx.post(f"{API}/webhooks/shopify", content=body, headers=h))
body, h = shopify_webhook(paid, "orders/paid", "attacker-guess")
show("Forged Shopify webhook (bad HMAC)", httpx.post(f"{API}/webhooks/shopify", content=body, headers=h))

httpx.patch(f"{MOCK}/admin/shopify/orders/5512000005", json={"financial_status": "partially_refunded",
            "updated_at": "2026-11-18T09:30:00Z", "refunds": [{"transactions": [{"kind": "refund", "amount": "50.00"}]}]})
body, h = shopify_webhook({"id": 9001, "order_id": 5512000005}, "refunds/create", "shpss_demo_secret")
show("Shopify refunds/create on #1005 (refetches order)", httpx.post(f"{API}/webhooks/shopify", content=body, headers=h))

httpx.patch(f"{MOCK}/admin/gumroad/sales/gS7gX7", json={"refunded": True})   # refund issued in Gumroad
sale = fixtures.GUMROAD_SALES[6] | {"refunded": True}
body, h = gumroad_ping(sale)
show("Gumroad refund ping for Sam Patel", httpx.post(f"{API}/webhooks/gumroad?token=demo-token", content=body, headers=h))
show("Gumroad ping with wrong token", httpx.post(f"{API}/webhooks/gumroad?token=guess", content=body, headers=h))
PY

echo; echo "### 5. Dead-letter queue (things a human must decide)"
$PY -m ecom_sync.cli dlq

echo; echo "### 6. Client fixes the base: adds 'JPY' to the Currency field, deletes the duplicate #1002 row. Replay:"
curl -s -X POST "localhost:$MOCK_PORT/admin/airtable/select-option" -H 'content-type: application/json' -d '{"field":"Currency","option":"JPY"}' >/dev/null
curl -s -X DELETE "localhost:$MOCK_PORT/admin/airtable/records/recB2chris00002b" >/dev/null
$PY -m ecom_sync.cli replay all 2>&1 | grep replay
$PY -m ecom_sync.cli dlq

echo; echo "### 7. Reconciliation AFTER"
$PY -m ecom_sync.cli reconcile 2>&1 | grep -v "fetched page" || true
cp "$DATA_DIR/reports/reconciliation-report.md" docs/reconciliation-report-example.md

sleep 1.5   # let the 1-second window clear
echo; echo "### 8. Rate-limit check: 25 single-row writes through the client limiter (4.5 req/s)"
$PY - <<PY
import logging, time, httpx
logging.disable(logging.WARNING)
from ecom_sync.airtable import AirtableClient
http = httpx.Client()
at = AirtableClient(http, "http://127.0.0.1:$MOCK_PORT/airtable", "pat", "appDEMO", "Orders", max_rps=4.5, cooldown_429=1)
t = time.time()
for i in range(25):
    at.upsert([{"Order ID": f"load:{i}", "Source": "Shopify", "Status": "paid", "Currency": "USD"}])
print(f"  with limiter:    {at.requests} requests in {time.time()-t:.1f}s, 429s received: {at.rate_limited}")
at2 = AirtableClient(http, "http://127.0.0.1:$MOCK_PORT/airtable", "pat", "appDEMO", "Orders", max_rps=1000, cooldown_429=1)
t = time.time()
for i in range(25):
    at2.upsert([{"Order ID": f"load2:{i}", "Source": "Shopify", "Status": "paid", "Currency": "USD"}])
print(f"  limiter off:     {at2.requests} requests in {time.time()-t:.1f}s, 429s received: {at2.rate_limited} (all recovered, 0 lost)")
PY
echo; echo "Demo complete. Report: $DATA_DIR/reports/reconciliation-report.md"
