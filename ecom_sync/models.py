from __future__ import annotations

import hashlib
import json
from dataclasses import asdict, dataclass, field
from decimal import Decimal

STATUSES = ("paid", "partially_refunded", "refunded", "chargeback", "pending", "cancelled")


@dataclass
class Order:
    """One order in a single shape, whatever store it came from."""

    order_key: str            # "shopify:5512000001" / "gumroad:Xk2a..." (the Airtable merge field)
    source: str               # shopify | gumroad
    external_id: str
    order_number: str
    email: str
    customer: str
    products: str
    quantity: int
    currency: str
    gross: Decimal
    refunded: Decimal
    status: str
    ordered_at: str           # ISO 8601 UTC
    updated_at: str           # ISO 8601 UTC, used to ignore out-of-order webhooks
    extra: dict = field(default_factory=dict)

    @property
    def net(self) -> Decimal:
        return self.gross - self.refunded

    def business_fields(self) -> dict:
        return {
            "Order ID": self.order_key,
            "Source": self.source.capitalize(),
            "Order #": self.order_number,
            "Email": self.email,
            "Customer": self.customer,
            "Products": self.products,
            "Qty": self.quantity,
            "Currency": self.currency,
            "Gross": float(self.gross),
            "Refunded": float(self.refunded),
            "Net": float(self.net),
            "Status": self.status,
            "Ordered At": self.ordered_at,
        }

    def fingerprint(self) -> str:
        """Hash of everything that matters. Same fingerprint = skip the Airtable write."""
        return hashlib.sha256(json.dumps(self.business_fields(), sort_keys=True).encode()).hexdigest()[:16]

    def to_dict(self) -> dict:
        d = asdict(self)
        d["gross"], d["refunded"] = str(self.gross), str(self.refunded)
        return d

    @classmethod
    def from_dict(cls, d: dict) -> "Order":
        d = dict(d)
        d["gross"], d["refunded"] = Decimal(d["gross"]), Decimal(d["refunded"])
        return cls(**d)
