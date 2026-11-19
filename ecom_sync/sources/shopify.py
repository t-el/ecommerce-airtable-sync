"""Shopify Admin REST API: GET /admin/api/{version}/orders.json with cursor pagination (Link header)."""

from __future__ import annotations

import base64
import hashlib
import hmac
import logging
import re
from typing import Iterator

import httpx

from ..retry import raise_for_status, with_retry

log = logging.getLogger("ecom_sync.shopify")
_NEXT = re.compile(r'<([^>]+)>;\s*rel="next"')


def verify_shopify_hmac(raw_body: bytes, header_value: str | None, secret: str) -> bool:
    """X-Shopify-Hmac-Sha256 = base64(HMAC-SHA256(app secret, raw request body)). Constant-time compare."""
    if not header_value or not secret:
        return False
    digest = base64.b64encode(hmac.new(secret.encode(), raw_body, hashlib.sha256).digest()).decode()
    return hmac.compare_digest(digest, header_value)


class ShopifyClient:
    def __init__(self, http: httpx.Client, base_url: str, token: str, api_version: str = "2024-10",
                 *, attempts=4, base_delay=1.0, sleep=None):
        self.http, self.base, self.token, self.version = http, base_url.rstrip("/"), token, api_version
        self.retry_kw = {"attempts": attempts, "base_delay": base_delay}
        if sleep:
            self.retry_kw["sleep"] = sleep

    def iter_orders(self, updated_at_min: str | None = None, limit: int = 250) -> Iterator[dict]:
        url: str | None = f"{self.base}/admin/api/{self.version}/orders.json"
        params: dict | None = {"status": "any", "limit": limit}
        if updated_at_min:
            params["updated_at_min"] = updated_at_min
        page = 0
        while url:
            page += 1

            def call(u=url, p=params):
                r = self.http.get(u, params=p, headers={"X-Shopify-Access-Token": self.token})
                raise_for_status(r, "shopify GET orders.json")
                return r

            r = with_retry(call, step="shopify_list_orders", **self.retry_kw)
            orders = r.json()["orders"]
            log.info("fetched page", extra={"source": "shopify", "page": page, "count": len(orders)})
            yield from orders
            m = _NEXT.search(r.headers.get("Link", ""))
            url, params = (m.group(1), None) if m else (None, None)   # page_info URL already has the params

    def get_order(self, order_id: str | int) -> dict:
        def call():
            r = self.http.get(f"{self.base}/admin/api/{self.version}/orders/{order_id}.json",
                              headers={"X-Shopify-Access-Token": self.token})
            raise_for_status(r, "shopify GET order")
            return r.json()["order"]

        return with_retry(call, step="shopify_get_order", **self.retry_kw)
