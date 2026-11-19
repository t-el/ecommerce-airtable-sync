"""Webhook receiver + small ops API.

    POST /webhooks/shopify            orders/create, orders/updated, orders/paid, orders/cancelled, refunds/create
    POST /webhooks/gumroad?token=...  Gumroad Ping (sales and refunds), form-encoded
    POST /backfill?source=all         pull everything changed since the last cursor
    GET  /reconcile?fix=false         run the reconciliation report
    GET  /dlq, POST /dlq/{id}/replay  inspect and replay failures
"""

from __future__ import annotations

import hashlib
import hmac
import json
import logging
from urllib.parse import parse_qsl

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse

from .config import Settings, load_settings
from .logging_setup import setup_logging
from .pipeline import SyncEngine, SyncReport, build_engine
from .reconcile import reconcile
from .sources import verify_shopify_hmac

log = logging.getLogger("ecom_sync.api")
ORDER_TOPICS = {"orders/create", "orders/updated", "orders/paid", "orders/cancelled", "orders/fulfilled"}


def _respond(event_id: str, engine: SyncEngine, rep: SyncReport) -> JSONResponse:
    outcome = rep.outcomes[0].outcome if rep.outcomes else "ignored"
    engine.store.set_event_outcome(event_id, outcome)
    code = 202 if outcome in {"dead_lettered", "invalid"} else 200   # 2xx either way: we own the retry now
    return JSONResponse({"status": outcome, "order_key": rep.outcomes[0].order_key if rep.outcomes else None,
                         "detail": rep.outcomes[0].detail if rep.outcomes else ""}, code)


def create_app(settings: Settings | None = None, engine: SyncEngine | None = None) -> FastAPI:
    settings = settings or load_settings()
    if engine is None:
        setup_logging(settings.log_json)
        engine = build_engine(settings)
    app = FastAPI(title="Ecommerce -> Airtable sync", version="1.0.0")
    app.state.engine = engine

    @app.get("/health")
    def health():
        return {"ok": True, "mock_mode": settings.mock_mode, "open_dead_letters": len(engine.store.dead_letters())}

    @app.post("/webhooks/shopify")
    async def shopify_webhook(request: Request):
        raw = await request.body()
        if not verify_shopify_hmac(raw, request.headers.get("X-Shopify-Hmac-Sha256"), settings.shopify_webhook_secret):
            log.warning("rejected shopify webhook: bad HMAC")
            raise HTTPException(401, "invalid HMAC")
        topic = request.headers.get("X-Shopify-Topic", "")
        event_id = request.headers.get("X-Shopify-Webhook-Id") or hashlib.sha256(raw).hexdigest()
        if not engine.store.claim_event(event_id, "shopify", topic):
            log.info("duplicate webhook ignored", extra={"event_id": event_id, "topic": topic})
            return {"status": "duplicate", "event_id": event_id}
        payload = json.loads(raw)
        if topic == "refunds/create":      # refund payload has order_id only: fetch the full, current order
            payload = engine.shopify.get_order(payload["order_id"])
        elif topic not in ORDER_TOPICS:
            engine.store.set_event_outcome(event_id, "ignored")
            return {"status": "ignored", "topic": topic}
        log.info("webhook received", extra={"source": "shopify", "topic": topic, "event_id": event_id})
        return _respond(event_id, engine, engine.handle_raw("shopify", payload))

    @app.post("/webhooks/gumroad")
    async def gumroad_webhook(request: Request, token: str = ""):
        # Gumroad pings are not signed, so the Ping URL carries a long secret token.
        if not hmac.compare_digest(token.encode(), settings.gumroad_webhook_token.encode()):
            raise HTTPException(401, "invalid token")
        raw = await request.body()
        form = dict(parse_qsl(raw.decode()))
        if form.get("test") == "true":
            return {"status": "ignored", "reason": "test ping"}
        event_id = "gumroad:" + hashlib.sha256(raw).hexdigest()[:32]   # identical resend = same id
        if not engine.store.claim_event(event_id, "gumroad", "ping"):
            return {"status": "duplicate", "event_id": event_id}
        log.info("webhook received", extra={"source": "gumroad", "sale_id": form.get("sale_id"),
                                            "refunded": form.get("refunded")})
        return _respond(event_id, engine, engine.handle_raw("gumroad", form))

    @app.post("/backfill")
    def backfill(source: str = "all", full: bool = False):
        out = {}
        for s in (["gumroad", "shopify"] if source == "all" else [source]):
            rep = engine.backfill(s, since=None if full else engine.store.get_cursor(s))
            out[s] = rep.counts
        return out

    @app.get("/reconcile")
    def run_reconcile(fix: bool = False):
        return reconcile(engine, fix=fix, out_dir=settings.data_dir / "reports").to_dict()

    @app.get("/dlq")
    def dlq():
        return engine.store.dead_letters()

    @app.post("/dlq/{dlq_id}/replay")
    def replay(dlq_id: int):
        try:
            rep = engine.replay(dlq_id)
        except ValueError as exc:
            raise HTTPException(404, str(exc))
        return {"counts": rep.counts, "resolved": engine.store.get_dead_letter(dlq_id)["status"] == "resolved"}

    return app
