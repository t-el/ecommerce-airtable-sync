"""Offline stand-ins for the Gumroad, Shopify and Airtable APIs, with the same URLs, payloads and limits.

Airtable mock enforces the real rules: 5 requests/second per base (HTTP 429 + a cooldown during which every
request is rejected), max 10 records per write, performUpsert with fieldsToMergeOn (422 if several rows
match), and single-select fields that reject unknown options unless typecast=true.

Fault injection (POST /admin/faults): {"shopify_503": 2} fails Shopify page 2 twice;
{"airtable_429_at": 3} answers the 3rd Airtable request with 429 (another tool bursting the same base).

    uvicorn --factory mock_apis.server:create_mock_app --port 8710
"""

from __future__ import annotations

import os
import random
import string
import threading
import time
from collections import deque
from datetime import datetime, timezone

from fastapi import FastAPI, Header, HTTPException, Request
from fastapi.responses import JSONResponse

from . import fixtures

PAGE = 3  # small pages so pagination is always exercised


class MockState:
    def __init__(self, penalty_s: float | None = None, clock=time.monotonic):
        self.lock = threading.Lock()
        self.clock = clock
        self.penalty_s = penalty_s if penalty_s is not None else float(os.getenv("MOCK_AIRTABLE_PENALTY", "30"))
        self.reset()

    def reset(self) -> None:
        d = fixtures.fresh()
        self.gumroad, self.shopify, self.airtable, self.options = d["gumroad"], d["shopify"], d["airtable"], d["select_options"]
        self.faults: dict = {}
        self.hits: deque[float] = deque()
        self.blocked_until = 0.0
        self.airtable_requests = 0
        self.airtable_429s = 0
        self.page2_calls = 0


def _rid() -> str:
    return "rec" + "".join(random.choices(string.ascii_letters + string.digits, k=14))


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def create_mock_app(state: MockState | None = None) -> FastAPI:
    st = state or MockState()
    app = FastAPI(title="Mock Gumroad / Shopify / Airtable")
    app.state.mock = st

    # ----------------------------------------------------------------- Gumroad
    @app.get("/gumroad/v2/sales")
    def gumroad_sales(access_token: str = "", page_key: str = "0", after: str | None = None):
        if not access_token:
            return JSONResponse({"success": False, "message": "The access token is invalid."}, 401)
        sales = [s for s in st.gumroad if not after or s["created_at"][:10] >= after]
        start = int(page_key)
        chunk = sales[start:start + PAGE]
        nxt = start + PAGE if start + PAGE < len(sales) else None
        body = {"success": True, "sales": chunk}
        if nxt is not None:
            body |= {"next_page_key": str(nxt), "next_page_url": f"/v2/sales?page_key={nxt}"}
        return body

    # ----------------------------------------------------------------- Shopify
    @app.get("/shopify/admin/api/{version}/orders.json")
    def shopify_orders(request: Request, version: str, page_info: str | None = None,
                       x_shopify_access_token: str = Header("")):
        if not x_shopify_access_token:
            raise HTTPException(401, "[API] Invalid API key or access token")
        start = int(page_info or 0)
        if start > 0 and st.faults.get("shopify_503", 0) > st.page2_calls:
            st.page2_calls += 1
            return JSONResponse({"errors": "Service Unavailable"}, 503)
        chunk = st.shopify[start:start + PAGE]
        headers = {}
        if start + PAGE < len(st.shopify):
            nxt = f"{str(request.base_url).rstrip('/')}/shopify/admin/api/{version}/orders.json?limit=250&page_info={start + PAGE}"
            headers["Link"] = f'<{nxt}>; rel="next"'
        return JSONResponse({"orders": chunk}, headers=headers)

    @app.get("/shopify/admin/api/{version}/orders/{order_id}.json")
    def shopify_order(version: str, order_id: int, x_shopify_access_token: str = Header("")):
        if not x_shopify_access_token:
            raise HTTPException(401)
        for o in st.shopify:
            if o["id"] == order_id:
                return {"order": o}
        return JSONResponse({"errors": "Not Found"}, 404)

    # ----------------------------------------------------------------- Airtable
    def gate(authorization: str) -> JSONResponse | None:
        if not authorization.startswith("Bearer "):
            return JSONResponse({"error": {"type": "AUTHENTICATION_REQUIRED",
                                           "message": "Authentication required"}}, 401)
        with st.lock:
            now = st.clock()
            st.airtable_requests += 1
            if st.faults.get("airtable_429_at") == st.airtable_requests or now < st.blocked_until or (
                    len([t for t in st.hits if now - t < 1.0]) >= 5):
                if now >= st.blocked_until:
                    st.blocked_until = now + st.penalty_s
                st.airtable_429s += 1
                return JSONResponse({"errors": [{"error": "RATE_LIMIT_REACHED",
                                                 "message": "Rate limit exceeded. Please try again later"}]}, 429)
            st.hits.append(now)
            while st.hits and now - st.hits[0] > 1.0:
                st.hits.popleft()
        return None

    def bad(type_: str, msg: str, code: int = 422) -> JSONResponse:
        return JSONResponse({"error": {"type": type_, "message": msg}}, code)

    @app.get("/airtable/v0/{base}/{table}")
    def airtable_list(request: Request, base: str, table: str, authorization: str = Header("")):
        if (r := gate(authorization)) is not None:
            return r
        q = request.query_params
        size = min(int(q.get("pageSize", 100)), 100)
        start = int(q.get("offset", 0))
        recs = st.airtable[start:start + size]
        body = {"records": recs}
        if start + size < len(st.airtable):
            body["offset"] = str(start + size)
        return body

    @app.patch("/airtable/v0/{base}/{table}")
    async def airtable_upsert(request: Request, base: str, table: str, authorization: str = Header("")):
        if (r := gate(authorization)) is not None:
            return r
        body = await request.json()
        records = body.get("records", [])
        if len(records) > 10:
            return bad("INVALID_RECORDS", "You can only update up to 10 records at a time")
        merge = (body.get("performUpsert") or {}).get("fieldsToMergeOn")
        if not merge:
            return bad("INVALID_REQUEST_MISSING_FIELDS", "performUpsert.fieldsToMergeOn is required in this mock")
        typecast = bool(body.get("typecast"))
        # validate everything first: Airtable is all-or-nothing per request
        for rec in records:
            for f, opts in st.options.items():
                v = rec["fields"].get(f)
                if v is not None and v not in opts and not typecast:
                    return bad("INVALID_MULTIPLE_CHOICE_OPTIONS",
                               f'Insufficient permissions to create new select option ""{v}"" in field {f}')
            matches = [x for x in st.airtable if all(x["fields"].get(m) == rec["fields"].get(m) for m in merge)]
            if len(matches) > 1:
                return bad("INVALID_VALUE_FOR_COLUMN",
                           f"Cannot update more than one record with performUpsert: "
                           f"{len(matches)} records match {merge[0]}={rec['fields'].get(merge[0])!r}")
        out, created, updated = [], [], []
        with st.lock:
            for rec in records:
                match = next((x for x in st.airtable
                              if all(x["fields"].get(m) == rec["fields"].get(m) for m in merge)), None)
                if match:
                    match["fields"].update(rec["fields"])
                    updated.append(match["id"])
                    out.append(match)
                else:
                    new = {"id": _rid(), "createdTime": _now(), "fields": dict(rec["fields"])}
                    st.airtable.append(new)
                    created.append(new["id"])
                    out.append(new)
        return {"records": out, "createdRecords": created, "updatedRecords": updated}

    # ----------------------------------------------------------------- admin (demo + tests only)
    @app.get("/admin/state")
    def admin_state():
        return {"airtable_rows": len(st.airtable), "airtable_requests": st.airtable_requests,
                "airtable_429s": st.airtable_429s, "faults": st.faults}

    @app.post("/admin/reset")
    def admin_reset():
        st.reset()
        return {"ok": True}

    @app.post("/admin/faults")
    async def admin_faults(request: Request):
        st.faults = await request.json()
        st.page2_calls = 0
        return {"faults": st.faults}

    @app.post("/admin/airtable/select-option")
    async def admin_add_option(request: Request):
        b = await request.json()
        st.options[b["field"]].add(b["option"])
        return {"field": b["field"], "options": sorted(st.options[b["field"]])}

    @app.delete("/admin/airtable/records/{rid}")
    def admin_delete(rid: str):
        st.airtable[:] = [r for r in st.airtable if r["id"] != rid]
        return {"deleted": rid}

    @app.patch("/admin/shopify/orders/{order_id}")
    async def admin_edit_order(order_id: int, request: Request):
        b = await request.json()
        for o in st.shopify:
            if o["id"] == order_id:
                o.update(b)
                return {"order": o}
        raise HTTPException(404)

    @app.patch("/admin/gumroad/sales/{sale_id}")
    async def admin_edit_sale(sale_id: str, request: Request):
        b = await request.json()
        for s in st.gumroad:
            if s["id"] == sale_id:
                s.update(b)
                return {"sale": s}
        raise HTTPException(404)

    return app
