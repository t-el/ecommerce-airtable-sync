"""Build webhook requests exactly as Shopify and Gumroad send them (used by the demo and tests)."""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import uuid
from urllib.parse import urlencode


def shopify_webhook(payload: dict, topic: str, secret: str, webhook_id: str | None = None) -> tuple[bytes, dict]:
    body = json.dumps(payload).encode()
    sig = base64.b64encode(hmac.new(secret.encode(), body, hashlib.sha256).digest()).decode()
    return body, {"Content-Type": "application/json", "X-Shopify-Topic": topic, "X-Shopify-Hmac-Sha256": sig,
                  "X-Shopify-Shop-Domain": "demo-store.myshopify.com",
                  "X-Shopify-Webhook-Id": webhook_id or str(uuid.uuid4())}


def gumroad_ping(sale: dict) -> tuple[bytes, dict]:
    """Gumroad Ping: application/x-www-form-urlencoded, everything a string."""
    form = {"sale_id": sale["id"], "sale_timestamp": sale["created_at"], "order_number": sale["order_id"],
            "seller_id": sale.get("seller_id", ""), "product_id": sale.get("product_id", ""),
            "product_name": sale["product_name"], "email": sale["email"], "full_name": sale.get("full_name", ""),
            "price": sale["price"], "currency": sale["currency"], "quantity": sale.get("quantity", 1),
            "refunded": str(sale.get("refunded", False)).lower(), "test": "false"}
    if sale.get("updated_at"):
        form["updated_at"] = sale["updated_at"]
    return urlencode(form).encode(), {"Content-Type": "application/x-www-form-urlencoded"}
