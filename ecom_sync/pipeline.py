"""The sync engine: normalise -> skip stale/unchanged -> upsert to Airtable in batches -> record or dead-letter."""

from __future__ import annotations

import logging
import re
import time
from collections import Counter
from dataclasses import dataclass, field
from typing import Callable, Iterable

import httpx

from .airtable import AirtableClient
from .config import Settings
from .models import Order
from .normalize import NormalizeError, from_gumroad, from_shopify
from .retry import PermanentError, TransientError
from .sources import GumroadClient, ShopifyClient
from .store import Store

log = logging.getLogger("ecom_sync.pipeline")
NORMALIZERS = {"gumroad": from_gumroad, "shopify": from_shopify}


@dataclass
class Outcome:
    order_key: str
    outcome: str        # created | updated | unchanged | stale | dead_lettered | invalid
    detail: str = ""


@dataclass
class SyncReport:
    outcomes: list[Outcome] = field(default_factory=list)

    @property
    def counts(self) -> dict[str, int]:
        return dict(Counter(o.outcome for o in self.outcomes))

    def extend(self, other: "SyncReport") -> None:
        self.outcomes.extend(other.outcomes)


class SyncEngine:
    def __init__(self, store: Store, airtable: AirtableClient,
                 gumroad: GumroadClient | None = None, shopify: ShopifyClient | None = None):
        self.store, self.airtable, self.gumroad, self.shopify = store, airtable, gumroad, shopify

    # -- entry points --------------------------------------------------------------------------
    def handle_raw(self, source: str, payload: dict, *, force: bool = False) -> SyncReport:
        """One raw object from a webhook or the API. Bad data goes to the DLQ, never crashes the run."""
        try:
            order = NORMALIZERS[source](payload)
        except NormalizeError as exc:
            key = f"{source}:{payload.get('id') or payload.get('sale_id') or '?'}"
            dlq = self.store.add_dead_letter(key, source, "normalize", str(exc), {"source": source, "raw": payload})
            log.error("invalid order payload", extra={"order_key": key, "error": str(exc), "dlq_id": dlq})
            return SyncReport([Outcome(key, "invalid", str(exc))])
        return self.sync_orders([order], force=force)

    def sync_orders(self, orders: Iterable[Order], *, force: bool = False) -> SyncReport:
        report, todo = SyncReport(), []
        for o in orders:
            prev = self.store.get_order(o.order_key)
            if prev and not force:
                stale = _is_stale(o, prev)
                if stale:
                    log.info("ignoring out-of-order update", extra={"order_key": o.order_key, "reason": stale})
                    report.outcomes.append(Outcome(o.order_key, "stale", stale))
                    continue
                if prev["fingerprint"] == o.fingerprint():
                    report.outcomes.append(Outcome(o.order_key, "unchanged"))
                    continue
            todo.append(o)
        for i in range(0, len(todo), 10):          # 10 = Airtable's per-request maximum
            report.extend(self._write_batch(todo[i:i + 10]))
        return report

    def backfill(self, source: str, since: str | None = None) -> SyncReport:
        client = {"gumroad": self.gumroad, "shopify": self.shopify}[source]
        t0 = time.monotonic()
        report, orders, newest = SyncReport(), [], since or ""
        it = client.iter_sales(after=since[:10] if since else None) if source == "gumroad" \
            else client.iter_orders(updated_at_min=since)
        for raw in it:
            try:
                o = NORMALIZERS[source](raw)
                orders.append(o)
                newest = max(newest, o.updated_at)
            except NormalizeError:
                report.extend(self.handle_raw(source, raw))
        report.extend(self.sync_orders(orders))
        if newest:
            self.store.set_cursor(source, newest)
        log.info("backfill finished", extra={"source": source, "fetched": len(orders), "counts": report.counts,
                                             "duration_ms": int((time.monotonic() - t0) * 1000)})
        return report

    def replay(self, dlq_id: int) -> SyncReport:
        item = self.store.get_dead_letter(dlq_id)
        if not item or item["status"] != "open":
            raise ValueError(f"dead letter {dlq_id} not found or already resolved")
        p = item["payload"]
        rep = self.handle_raw(p["source"], p["raw"], force=True) if item["step"] == "normalize" \
            else self.sync_orders([Order.from_dict(p["order"])], force=True)
        if all(o.outcome in {"created", "updated", "unchanged"} for o in rep.outcomes):
            self.store.resolve_dead_letter(dlq_id)
            log.info("dead letter replayed", extra={"dlq_id": dlq_id, "counts": rep.counts})
        return rep

    # -- internals -----------------------------------------------------------------------------
    def _write_batch(self, batch: list[Order]) -> SyncReport:
        report = SyncReport()
        by_key = {o.order_key: o for o in batch}
        try:
            res = self.airtable.upsert([o.business_fields() for o in batch])
        except (TransientError, PermanentError, httpx.HTTPError) as exc:
            for o in batch:
                self._dead_letter(o, "airtable_upsert", str(exc))
                report.outcomes.append(Outcome(o.order_key, "dead_lettered", str(exc)[:120]))
            return report
        for key, err in res.failed.items():
            self._dead_letter(by_key[key], "airtable_upsert", err)
            report.outcomes.append(Outcome(key, "dead_lettered", _short(err)))
        for kind, keys in (("created", res.created), ("updated", res.updated)):
            for key in keys:
                o = by_key[key]
                self.store.save_order(key, o.fingerprint(), o.updated_at, res.ids[key], o.status)
                log.info("order synced", extra={"order_key": key, "outcome": kind, "record_id": res.ids[key],
                                                "status": o.status, "net": str(o.net)})
                report.outcomes.append(Outcome(key, kind))
        return report

    def _dead_letter(self, o: Order, step: str, error: str) -> None:
        dlq = self.store.add_dead_letter(o.order_key, o.source, step, error, {"order": o.to_dict()})
        log.error("order dead-lettered", extra={"order_key": o.order_key, "step": step, "dlq_id": dlq,
                                                "error": _short(error)})


FINAL = {"refunded", "chargeback"}


def _is_stale(o: Order, prev) -> str:
    """Webhooks arrive out of order. Never let an older event overwrite newer data."""
    if o.source == "shopify" and prev["updated_at"] and o.updated_at < prev["updated_at"]:
        return f"updated_at {o.updated_at} is older than stored {prev['updated_at']}"
    if o.source == "gumroad" and prev["status"] in FINAL and o.status not in FINAL:
        return f"would downgrade {prev['status']} to {o.status}"
    return ""


def _short(err: str) -> str:
    """Pull Airtable's error message out of a long exception string."""
    m = re.search(r"""['"]message['"]:\s*['"](.+?)['"]\s*}""", err)
    return m.group(1) if m else err[:160]


def build_engine(settings: Settings, http: httpx.Client | None = None,
                 sleep: Callable[[float], None] | None = None) -> SyncEngine:
    http = http or httpx.Client(timeout=settings.http_timeout)
    kw = {"attempts": settings.retry_attempts, "base_delay": settings.retry_base_delay}
    if sleep:
        kw["sleep"] = sleep
    return SyncEngine(
        Store(settings.db_path),
        AirtableClient(http, settings.airtable_base, settings.airtable_token, settings.airtable_base_id,
                       settings.airtable_table, max_rps=settings.airtable_max_rps,
                       cooldown_429=settings.airtable_429_cooldown, **kw),
        GumroadClient(http, settings.gumroad_base, settings.gumroad_access_token, **kw),
        ShopifyClient(http, settings.shopify_base, settings.shopify_access_token, settings.shopify_api_version, **kw),
    )
