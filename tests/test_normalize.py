from decimal import Decimal

import pytest

from ecom_sync.normalize import NormalizeError, from_gumroad, from_shopify
from mock_apis import fixtures


def test_gumroad_cents_and_currency():
    o = from_gumroad(fixtures.GUMROAD_SALES[5])
    assert o.order_key == "gumroad:gS6fW6"
    assert o.gross == Decimal("19.00") and o.currency == "EUR" and o.status == "paid"


def test_gumroad_refund_sets_net_to_zero():
    o = from_gumroad(fixtures.GUMROAD_SALES[2])
    assert o.status == "refunded" and o.refunded == o.gross and o.net == 0


def test_gumroad_ping_form_payload_strings():
    ping = {"sale_id": "abc", "sale_timestamp": "2026-11-16T11:30:00Z", "email": "A@B.CO", "price": "1500",
            "currency": "usd", "quantity": "2", "refunded": "true", "product_name": "Guide"}
    o = from_gumroad(ping)
    assert o.email == "a@b.co" and o.quantity == 2 and o.status == "refunded" and o.gross == Decimal("15.00")


def test_shopify_partial_refund_sums_transactions():
    o = from_shopify(fixtures.SHOPIFY_ORDERS[1])
    assert o.status == "partially_refunded" and o.refunded == Decimal("20.00") and o.net == Decimal("60.00")
    assert o.order_number == "#1002" and o.products == "Ceramic Tagine x2" and o.quantity == 2


def test_shopify_multi_line_items():
    o = from_shopify(fixtures.SHOPIFY_ORDERS[4])
    assert o.quantity == 5 and "Brass Lantern x3" in o.products and o.currency == "GBP"


@pytest.mark.parametrize("bad", [{"id": 1, "email": "", "created_at": "2026-01-01T00:00:00Z"},
                                 {"id": 1, "email": "x@y.z", "created_at": "nope", "total_price": "1"},
                                 {"id": 1, "email": "x@y.z", "created_at": "2026-01-01T00:00:00Z", "total_price": "-5"}])
def test_shopify_rejects_bad_payloads(bad):
    with pytest.raises(NormalizeError):
        from_shopify(bad)


def test_fingerprint_changes_only_when_business_data_changes():
    a = from_shopify(fixtures.SHOPIFY_ORDERS[0])
    b = from_shopify(fixtures.SHOPIFY_ORDERS[0] | {"updated_at": "2026-11-20T00:00:00Z"})
    c = from_shopify(fixtures.SHOPIFY_ORDERS[0] | {"total_price": "99.00"})
    assert a.fingerprint() == b.fingerprint() != c.fingerprint()
