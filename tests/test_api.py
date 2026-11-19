import pytest
from fastapi.testclient import TestClient

from ecom_sync.api import create_app
from mock_apis import fixtures
from mock_apis.webhooks import gumroad_ping, shopify_webhook


@pytest.fixture
def api(settings, engine):
    return TestClient(create_app(settings, engine))


def test_health(api):
    assert api.get("/health").json()["ok"] is True


def test_shopify_webhook_valid_hmac_creates_order(api, mock):
    state, _ = mock
    body, headers = shopify_webhook(fixtures.SHOPIFY_ORDERS[2], "orders/create", "shh", "wh-1")
    r = api.post("/webhooks/shopify", content=body, headers=headers)
    assert r.status_code == 200 and r.json()["status"] == "created"


def test_shopify_webhook_bad_hmac_rejected(api):
    body, headers = shopify_webhook(fixtures.SHOPIFY_ORDERS[2], "orders/create", "WRONG")
    assert api.post("/webhooks/shopify", content=body, headers=headers).status_code == 401


def test_shopify_redelivery_is_deduplicated(api):
    body, headers = shopify_webhook(fixtures.SHOPIFY_ORDERS[2], "orders/create", "shh", "wh-same")
    api.post("/webhooks/shopify", content=body, headers=headers)
    r = api.post("/webhooks/shopify", content=body, headers=headers)
    assert r.json()["status"] == "duplicate"


def test_refunds_create_fetches_current_order(api, mock):
    state, http = mock
    http.patch("/admin/shopify/orders/5512000005", json={
        "financial_status": "partially_refunded", "updated_at": "2026-11-18T09:00:00Z",
        "refunds": [{"transactions": [{"kind": "refund", "amount": "50.00"}]}]})
    body, headers = shopify_webhook({"id": 1, "order_id": 5512000005}, "refunds/create", "shh")
    assert api.post("/webhooks/shopify", content=body, headers=headers).json()["status"] == "created"
    rec = [r for r in state.airtable if r["fields"]["Order ID"] == "shopify:5512000005"][0]
    assert rec["fields"]["Refunded"] == 50.0 and rec["fields"]["Net"] == 160.0


def test_gumroad_ping_requires_token_and_dedupes(api):
    body, headers = gumroad_ping(fixtures.GUMROAD_SALES[6])
    assert api.post("/webhooks/gumroad?token=nope", content=body, headers=headers).status_code == 401
    assert api.post("/webhooks/gumroad?token=gum-secret", content=body, headers=headers).json()["status"] == "created"
    assert api.post("/webhooks/gumroad?token=gum-secret", content=body, headers=headers).json()["status"] == "duplicate"


def test_bad_payload_returns_202_and_lands_in_dlq(api):
    body, headers = shopify_webhook(fixtures.SHOPIFY_ORDERS[0] | {"email": ""}, "orders/updated", "shh")
    r = api.post("/webhooks/shopify", content=body, headers=headers)
    assert r.status_code == 202 and r.json()["status"] == "invalid"
    assert len(api.get("/dlq").json()) == 1


def test_backfill_and_reconcile_endpoints(api):
    assert api.post("/backfill?source=all").json()["gumroad"]["created"] == 3
    rep = api.get("/reconcile").json()
    assert rep["orphaned"] == {"shopify:5512000999": "recB9test0000999"}
