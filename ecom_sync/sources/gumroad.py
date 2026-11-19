"""Gumroad API v2: GET /v2/sales (paginated with page_key). Ping webhooks are handled in api.py."""

from __future__ import annotations

import logging
from typing import Iterator

import httpx

from ..retry import raise_for_status, with_retry

log = logging.getLogger("ecom_sync.gumroad")


class GumroadClient:
    def __init__(self, http: httpx.Client, base_url: str, token: str, *, attempts=4, base_delay=1.0, sleep=None):
        self.http, self.base, self.token = http, base_url.rstrip("/"), token
        self.retry_kw = {"attempts": attempts, "base_delay": base_delay}
        if sleep:
            self.retry_kw["sleep"] = sleep

    def iter_sales(self, after: str | None = None) -> Iterator[dict]:
        """Yields every sale, following next_page_key until Gumroad stops returning one."""
        params: dict = {"access_token": self.token}
        if after:
            params["after"] = after
        page = 0
        while True:
            page += 1

            def call(p=dict(params)):
                r = self.http.get(f"{self.base}/v2/sales", params=p)
                raise_for_status(r, "gumroad GET /v2/sales")
                return r.json()

            data = with_retry(call, step="gumroad_list_sales", **self.retry_kw)
            if not data.get("success", False):
                raise RuntimeError(f"gumroad error: {data.get('message')}")
            log.info("fetched page", extra={"source": "gumroad", "page": page, "count": len(data["sales"])})
            yield from data["sales"]
            key = data.get("next_page_key")
            if not key:
                return
            params["page_key"] = key
