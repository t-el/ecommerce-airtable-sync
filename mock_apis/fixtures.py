"""Realistic sample data in the shape the real APIs return. Prices: Gumroad = cents, Shopify = strings."""

from __future__ import annotations

import copy


def _g(id_, order_id, email, name, product, cents, created, currency="usd", **kw):
    return {"id": id_, "order_id": order_id, "email": email, "full_name": name, "product_name": product,
            "product_id": "prd_" + product[:4].lower(), "price": cents, "gumroad_fee": int(cents * 0.1),
            "currency": currency, "quantity": 1, "created_at": created, "refunded": False,
            "partially_refunded": False, "chargedback": False, "seller_id": "sel_demo", **kw}


GUMROAD_SALES = [
    _g("gS1aQ1", 524001, "lina@studio-nord.se", "Lina Berg", "Notion Ops Template Pack", 2900, "2026-11-10T09:14:00Z"),
    _g("gS2bR2", 524002, "omar.k@gmail.com", "Omar Kadiri", "n8n Automation Course", 4900, "2026-11-11T13:02:00Z"),
    _g("gS3cT3", 524003, "j.alvarez@brightmail.com", "Julia Alvarez", "Notion Ops Template Pack", 2900,
       "2026-11-12T18:40:00Z", refunded=True, updated_at="2026-11-14T08:00:00Z"),
    _g("gS4dU4", 524004, "mark@ledgerly.io", "Mark Owens", "Airtable CRM Starter Kit", 9900, "2026-11-13T07:55:00Z"),
    _g("gS5eV5", 524005, "haruto.s@example.jp", "Haruto Sato", "n8n Automation Course", 250000,
       "2026-11-14T02:10:00Z", currency="jpy"),
    _g("gS6fW6", 524006, "elena.r@posteo.de", "Elena Rossi", "Prompt Library for Support Teams", 1900,
       "2026-11-15T16:21:00Z", currency="eur"),
    _g("gS7gX7", 524007, "sam@tinyhq.co", "Sam Patel", "Zapier to n8n Migration Guide", 1500, "2026-11-16T11:30:00Z"),
]


def _s(id_, number, email, first, last, items, total, created, status="paid", refunds=None, currency="USD",
       updated=None):
    return {"id": id_, "name": f"#{number}", "order_number": number, "email": email, "currency": currency,
            "created_at": created, "updated_at": updated or created, "financial_status": status,
            "total_price": total, "subtotal_price": total, "total_tax": "0.00", "cancelled_at": None,
            "customer": {"id": id_ + 900, "first_name": first, "last_name": last},
            "line_items": [{"title": t, "quantity": q, "price": p} for t, q, p in items],
            "refunds": [{"id": id_ + 50, "transactions": [{"kind": "refund", "amount": a, "status": "success"}]}
                        for a in (refunds or [])]}


SHOPIFY_ORDERS = [
    _s(5512000001, 1001, "nadia@marrakech-living.ma", "Nadia", "Bennani", [("Handwoven Berber Rug", 1, "120.00")],
       "120.00", "2026-11-09T10:00:00Z"),
    _s(5512000002, 1002, "chris@coastalcafe.com", "Chris", "Moore", [("Ceramic Tagine", 2, "40.00")], "80.00",
       "2026-11-10T15:30:00Z", status="partially_refunded", refunds=["20.00"], updated="2026-11-12T09:00:00Z"),
    _s(5512000003, 1003, "amelie.d@orange.fr", "Amelie", "Dupont", [("Argan Oil Set", 1, "45.50")], "45.50",
       "2026-11-12T08:45:00Z", currency="EUR"),
    _s(5512000004, 1004, "tyler@northpeak.co", "Tyler", "Grant", [("Leather Pouf", 1, "60.00")], "60.00",
       "2026-11-13T19:05:00Z", status="refunded", refunds=["60.00"], updated="2026-11-15T10:00:00Z"),
    _s(5512000005, 1005, "olivia@hartwell.co.uk", "Olivia", "Hart",
       [("Brass Lantern", 3, "50.00"), ("Mint Tea Glasses", 2, "30.00")], "210.00", "2026-11-15T12:12:00Z",
       currency="GBP"),
    _s(5512000006, 1006, "dev.rao@gmail.com", "Dev", "Rao", [("Ceramic Tagine", 1, "40.00")], "40.00",
       "2026-11-17T21:40:00Z", status="pending"),
]


def _rec(rid, created, **fields):
    return {"id": rid, "createdTime": created, "fields": fields}


def airtable_seed() -> list[dict]:
    """The client's base before we arrive: partly synced by an old zap, with typical drift."""
    base = {"Ordered At": "2026-11-10T09:14:00Z"}
    return [
        _rec("recA1lina0000001", "2026-11-10T09:15:00.000Z", **{
            "Order ID": "gumroad:gS1aQ1", "Source": "Gumroad", "Order #": "524001", "Email": "lina@studio-nord.se",
            "Customer": "Lina Berg", "Products": "Notion Ops Template Pack", "Qty": 1, "Currency": "USD",
            "Gross": 29.0, "Refunded": 0.0, "Net": 29.0, "Status": "paid", **base}),
        _rec("recA2omar0000002", "2026-11-11T13:03:00.000Z", **{   # someone typed 39 instead of 49
            "Order ID": "gumroad:gS2bR2", "Source": "Gumroad", "Order #": "524002", "Email": "omar.k@gmail.com",
            "Customer": "Omar Kadiri", "Products": "n8n Automation Course", "Qty": 1, "Currency": "USD",
            "Gross": 39.0, "Refunded": 0.0, "Net": 39.0, "Status": "paid", "Ordered At": "2026-11-11T13:02:00Z"}),
        _rec("recA3julia000003", "2026-11-12T18:41:00.000Z", **{   # refund webhook was missed
            "Order ID": "gumroad:gS3cT3", "Source": "Gumroad", "Order #": "524003",
            "Email": "j.alvarez@brightmail.com", "Customer": "Julia Alvarez", "Products": "Notion Ops Template Pack",
            "Qty": 1, "Currency": "USD", "Gross": 29.0, "Refunded": 0.0, "Net": 29.0, "Status": "paid",
            "Ordered At": "2026-11-12T18:40:00Z"}),
        _rec("recB1nadia000001", "2026-11-09T10:01:00.000Z", **{
            "Order ID": "shopify:5512000001", "Source": "Shopify", "Order #": "#1001",
            "Email": "nadia@marrakech-living.ma", "Customer": "Nadia Bennani", "Products": "Handwoven Berber Rug x1",
            "Qty": 1, "Currency": "USD", "Gross": 120.0, "Refunded": 0.0, "Net": 120.0, "Status": "paid",
            "Ordered At": "2026-11-09T10:00:00Z"}),
        *[_rec(rid, "2026-11-10T15:31:00.000Z", **{     # old zap created the same order twice
            "Order ID": "shopify:5512000002", "Source": "Shopify", "Order #": "#1002", "Email": "chris@coastalcafe.com",
            "Customer": "Chris Moore", "Products": "Ceramic Tagine x2", "Qty": 2, "Currency": "USD", "Gross": 80.0,
            "Refunded": 0.0, "Net": 80.0, "Status": "paid", "Ordered At": "2026-11-10T15:30:00Z"})
          for rid in ("recB2chris00002a", "recB2chris00002b")],
        _rec("recB9test0000999", "2026-11-08T12:00:00.000Z", **{   # test order deleted in Shopify
            "Order ID": "shopify:5512000999", "Source": "Shopify", "Order #": "#0999", "Email": "test@example.com",
            "Customer": "Test Order", "Products": "Test product x1", "Qty": 1, "Currency": "USD", "Gross": 1.0,
            "Refunded": 0.0, "Net": 1.0, "Status": "paid", "Ordered At": "2026-11-08T12:00:00Z"}),
    ]


SELECT_OPTIONS = {
    "Source": {"Gumroad", "Shopify"},
    "Currency": {"USD", "EUR", "GBP", "MAD", "CAD"},
    "Status": {"paid", "partially_refunded", "refunded", "chargeback", "pending", "cancelled"},
}


def fresh() -> dict:
    return {"gumroad": copy.deepcopy(GUMROAD_SALES), "shopify": copy.deepcopy(SHOPIFY_ORDERS),
            "airtable": airtable_seed(), "select_options": copy.deepcopy(SELECT_OPTIONS)}
