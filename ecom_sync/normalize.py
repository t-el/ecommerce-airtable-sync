"""Turn Gumroad sales and Shopify orders (API objects or webhook payloads) into one Order shape."""

from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation

from .models import Order


class NormalizeError(ValueError):
    pass


def _iso(ts: str | None) -> str:
    if not ts:
        raise NormalizeError("missing timestamp")
    try:
        dt = datetime.fromisoformat(ts.replace("Z", "+00:00"))
    except ValueError as exc:
        raise NormalizeError(f"bad timestamp {ts!r}") from exc
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def _money(v, *, cents: bool = False) -> Decimal:
    try:
        d = Decimal(str(v))
    except (InvalidOperation, TypeError) as exc:
        raise NormalizeError(f"bad amount {v!r}") from exc
    if d < 0:
        raise NormalizeError(f"negative amount {v!r}")
    return (d / 100 if cents else d).quantize(Decimal("0.01"))


def _truthy(v) -> bool:
    return v is True or str(v).lower() in {"true", "1", "yes"}


def from_gumroad(sale: dict) -> Order:
    """Accepts both the /v2/sales API object and the form-encoded Ping payload (strings everywhere)."""
    sale_id = sale.get("id") or sale.get("sale_id")
    if not sale_id:
        raise NormalizeError("gumroad sale without id")
    email = (sale.get("email") or sale.get("purchase_email") or "").strip().lower()
    if "@" not in email:
        raise NormalizeError(f"gumroad sale {sale_id}: missing buyer email")
    qty = int(sale.get("quantity") or 1)
    gross = _money(sale.get("price"), cents=True)   # Gumroad sends cents for the whole line
    refunded = Decimal("0.00")
    status = "paid"
    if _truthy(sale.get("chargedback")) or _truthy(sale.get("disputed")):
        status, refunded = "chargeback", gross
    elif _truthy(sale.get("refunded")):
        status, refunded = "refunded", gross
    elif _truthy(sale.get("partially_refunded")):
        status = "partially_refunded"
        refunded = _money(sale.get("refunded_amount_cents") or 0, cents=True)
    created = _iso(sale.get("created_at") or sale.get("sale_timestamp"))
    return Order(
        order_key=f"gumroad:{sale_id}",
        source="gumroad",
        external_id=str(sale_id),
        order_number=str(sale.get("order_id") or sale.get("order_number") or sale_id),
        email=email,
        customer=(sale.get("full_name") or "").strip(),
        products=sale.get("product_name") or "",
        quantity=qty,
        currency=(sale.get("currency") or "usd").upper(),
        gross=gross,
        refunded=refunded,
        status=status,
        ordered_at=created,
        updated_at=_iso(sale.get("updated_at") or sale.get("refunded_at") or created),
    )


_SHOPIFY_STATUS = {
    "paid": "paid", "partially_refunded": "partially_refunded", "refunded": "refunded",
    "pending": "pending", "authorized": "pending", "voided": "cancelled", "partially_paid": "pending",
}


def from_shopify(o: dict) -> Order:
    if not o.get("id"):
        raise NormalizeError("shopify order without id")
    email = (o.get("email") or o.get("contact_email") or "").strip().lower()
    if "@" not in email:
        raise NormalizeError(f"shopify order {o.get('id')}: missing customer email")
    cust = o.get("customer") or {}
    items = o.get("line_items") or []
    refunded = sum((_money(t.get("amount", 0)) for r in o.get("refunds") or []
                    for t in r.get("transactions") or [] if t.get("kind", "refund") == "refund"), Decimal("0.00"))
    status = _SHOPIFY_STATUS.get(o.get("financial_status") or "", "pending")
    if o.get("cancelled_at") and status not in {"refunded", "partially_refunded"}:
        status = "cancelled"
    return Order(
        order_key=f"shopify:{o['id']}",
        source="shopify",
        external_id=str(o["id"]),
        order_number=o.get("name") or str(o.get("order_number") or o["id"]),
        email=email,
        customer=" ".join(x for x in [cust.get("first_name"), cust.get("last_name")] if x).strip(),
        products=", ".join(f"{i.get('title')} x{i.get('quantity', 1)}" for i in items),
        quantity=sum(int(i.get("quantity", 1)) for i in items) or 1,
        currency=(o.get("currency") or "USD").upper(),
        gross=_money(o.get("total_price", "0")),
        refunded=refunded.quantize(Decimal("0.01")),
        status=status,
        ordered_at=_iso(o.get("created_at")),
        updated_at=_iso(o.get("updated_at") or o.get("created_at")),
    )
