"""Airtable Web API client: list records (offset pagination) and batch upsert (performUpsert, 10 per call).

Airtable rules this client respects:
  * 5 requests/second per base -> client-side token bucket (AIRTABLE_MAX_RPS)
  * HTTP 429 -> stop sending for AIRTABLE_429_COOLDOWN seconds (Airtable asks for 30 s), then retry
  * max 10 records per create/update/upsert request
  * one invalid record (422) fails the whole batch -> retry that batch one record at a time
    so only the bad record goes to the dead-letter queue
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Callable, Iterator
from urllib.parse import quote

import httpx

from .ratelimit import RateLimiter
from .retry import PermanentError, TransientError, raise_for_status, with_retry

log = logging.getLogger("ecom_sync.airtable")
MERGE_FIELD = "Order ID"


@dataclass
class UpsertResult:
    created: list[str] = field(default_factory=list)
    updated: list[str] = field(default_factory=list)
    ids: dict[str, str] = field(default_factory=dict)          # order_key -> record id
    failed: dict[str, str] = field(default_factory=dict)       # order_key -> error


class AirtableClient:
    def __init__(self, http: httpx.Client, base_url: str, token: str, base_id: str, table: str, *,
                 max_rps: float = 4.5, cooldown_429: float = 30.0, attempts: int = 4, base_delay: float = 1.0,
                 sleep: Callable[[float], None] | None = None, limiter: RateLimiter | None = None):
        self.http = http
        self.url = f"{base_url.rstrip('/')}/v0/{base_id}/{quote(table)}"
        self.headers = {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}
        kw = {"sleep": sleep} if sleep else {}
        self.limiter = limiter or RateLimiter(max_rps, **kw)
        self.cooldown = cooldown_429
        self.retry_kw = {"attempts": attempts, "base_delay": base_delay, **kw}
        self.requests = 0
        self.rate_limited = 0

    # -- low level -------------------------------------------------------------------------------
    def _request(self, method: str, step: str, **kw) -> dict:
        def call():
            self.limiter.acquire()
            self.requests += 1
            r = self.http.request(method, self.url, headers=self.headers, **kw)
            if r.status_code == 429:
                self.rate_limited += 1
                wait = float(r.headers.get("Retry-After") or self.cooldown)
                self.limiter.pause(wait)
                log.warning("airtable rate limit hit, pausing all requests", extra={"step": step, "pause_s": wait})
                raise TransientError("airtable HTTP 429", retry_after=0)   # limiter already holds the pause
            raise_for_status(r, f"airtable {method}")
            return r.json()

        return with_retry(call, step=step, **self.retry_kw)

    # -- reads -----------------------------------------------------------------------------------
    def iter_records(self, fields: list[str] | None = None, formula: str | None = None) -> Iterator[dict]:
        params: list[tuple[str, str]] = [("pageSize", "100")]
        for f in fields or []:
            params.append(("fields[]", f))
        if formula:
            params.append(("filterByFormula", formula))
        offset = None
        while True:
            p = params + ([("offset", offset)] if offset else [])
            data = self._request("GET", "airtable_list", params=p)
            yield from data["records"]
            offset = data.get("offset")
            if not offset:
                return

    # -- writes ----------------------------------------------------------------------------------
    def _upsert_batch(self, rows: list[dict], typecast: bool = False) -> dict:
        body = {"performUpsert": {"fieldsToMergeOn": [MERGE_FIELD]}, "records": [{"fields": f} for f in rows],
                "typecast": typecast}
        return self._request("PATCH", "airtable_upsert", json=body)

    def upsert(self, rows: list[dict]) -> UpsertResult:
        """Upsert rows (dicts of Airtable fields) merging on 'Order ID'. Safe to repeat."""
        res = UpsertResult()
        for i in range(0, len(rows), 10):
            batch = rows[i:i + 10]
            try:
                self._absorb(res, self._upsert_batch(batch))
            except PermanentError as exc:
                if len(batch) == 1:
                    res.failed[batch[0][MERGE_FIELD]] = str(exc)
                    continue
                log.warning("batch rejected, isolating the bad record(s)",
                            extra={"batch_size": len(batch), "error": str(exc)[:200]})
                for row in batch:
                    try:
                        self._absorb(res, self._upsert_batch([row]))
                    except PermanentError as one:
                        res.failed[row[MERGE_FIELD]] = str(one)
        return res

    @staticmethod
    def _absorb(res: UpsertResult, data: dict) -> None:
        created = set(data.get("createdRecords", []))
        for rec in data["records"]:
            key = rec["fields"].get(MERGE_FIELD)
            res.ids[key] = rec["id"]
            (res.created if rec["id"] in created else res.updated).append(key)
