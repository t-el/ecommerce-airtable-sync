"""Reconciliation: compare what the stores say against what Airtable says, and explain every difference.

Finds:
  missing     in Gumroad/Shopify but not in Airtable (a webhook was missed)
  mismatched  in both, but amount/refund/status/email differ (a refund webhook was missed, or someone edited the row)
  duplicates  the same Order ID on 2+ Airtable rows (manual copy/paste, or an old zap without upsert)
  orphaned    in Airtable but no longer in the store (test orders deleted in Shopify, typos)

`--fix` re-upserts missing and mismatched orders. Duplicates and orphans are only reported:
deleting rows in a client's base is a human decision.
"""

from __future__ import annotations

import json
from collections import defaultdict
from dataclasses import asdict, dataclass, field
from pathlib import Path

from .airtable import MERGE_FIELD
from .models import Order
from .normalize import NormalizeError
from .pipeline import NORMALIZERS, SyncEngine, SyncReport
from .store import now_iso

COMPARED = ["Email", "Currency", "Gross", "Refunded", "Net", "Status"]


@dataclass
class Mismatch:
    order_key: str
    record_id: str
    diffs: dict  # field -> {"store": x, "airtable": y}


@dataclass
class ReconReport:
    generated_at: str
    source_orders: int
    airtable_records: int
    matched: int = 0
    missing: list[str] = field(default_factory=list)
    mismatched: list[Mismatch] = field(default_factory=list)
    duplicates: dict[str, list[str]] = field(default_factory=dict)
    orphaned: dict[str, str] = field(default_factory=dict)
    unreadable: list[str] = field(default_factory=list)
    fixed: dict = field(default_factory=dict)

    @property
    def clean(self) -> bool:
        return not (self.missing or self.mismatched or self.duplicates or self.orphaned)

    def to_markdown(self) -> str:
        lines = [f"# Reconciliation report ({self.generated_at})", "",
                 f"Store orders: **{self.source_orders}**, Airtable rows: **{self.airtable_records}**, "
                 f"matching: **{self.matched}**", "",
                 "| Check | Count |", "|---|---|",
                 f"| Missing in Airtable | {len(self.missing)} |",
                 f"| Mismatched | {len(self.mismatched)} |",
                 f"| Duplicate rows | {len(self.duplicates)} |",
                 f"| Orphaned rows (not in store) | {len(self.orphaned)} |", ""]
        if self.missing:
            lines += ["## Missing in Airtable", *[f"- `{k}`" for k in self.missing], ""]
        if self.mismatched:
            lines += ["## Mismatched", "| Order | Field | Store | Airtable |", "|---|---|---|---|"]
            for m in self.mismatched:
                for f, d in m.diffs.items():
                    lines.append(f"| `{m.order_key}` | {f} | {d['store']} | {d['airtable']} |")
            lines.append("")
        if self.duplicates:
            lines += ["## Duplicate rows (review and delete the extra row in Airtable)",
                      *[f"- `{k}`: records {', '.join(v)}" for k, v in self.duplicates.items()], ""]
        if self.orphaned:
            lines += ["## Orphaned rows (not found in the store; never deleted automatically)",
                      *[f"- `{k}` (record {v})" for k, v in self.orphaned.items()], ""]
        if self.fixed:
            lines += ["## Fix run", f"`{json.dumps(self.fixed)}`", ""]
        if self.clean:
            lines += ["All clear: every store order matches Airtable.", ""]
        return "\n".join(lines)

    def to_dict(self) -> dict:
        return asdict(self)


def _eq(a, b) -> bool:
    if isinstance(a, (int, float)) or isinstance(b, (int, float)):
        try:
            return round(float(a), 2) == round(float(b), 2)
        except (TypeError, ValueError):
            return False
    return (a or "") == (b or "")


def reconcile(engine: SyncEngine, *, fix: bool = False, out_dir: Path | None = None) -> ReconReport:
    store_orders: dict[str, Order] = {}
    unreadable = []
    for source, it in (("gumroad", engine.gumroad.iter_sales()), ("shopify", engine.shopify.iter_orders())):
        for raw in it:
            try:
                o = NORMALIZERS[source](raw)
                store_orders[o.order_key] = o
            except NormalizeError as exc:
                unreadable.append(f"{source}:{raw.get('id', '?')} ({exc})")

    rows = defaultdict(list)
    for rec in engine.airtable.iter_records():
        rows[rec["fields"].get(MERGE_FIELD, "")].append(rec)

    rep = ReconReport(now_iso(), len(store_orders), sum(len(v) for v in rows.values()), unreadable=unreadable)
    for key, o in store_orders.items():
        recs = rows.get(key)
        if not recs:
            rep.missing.append(key)
            continue
        if len(recs) > 1:
            rep.duplicates[key] = [r["id"] for r in recs]
        want = o.business_fields()
        diffs = {f: {"store": want[f], "airtable": recs[0]["fields"].get(f)}
                 for f in COMPARED if not _eq(want[f], recs[0]["fields"].get(f))}
        if diffs:
            rep.mismatched.append(Mismatch(key, recs[0]["id"], diffs))
        elif len(recs) == 1:
            rep.matched += 1
    for key, recs in rows.items():
        if key not in store_orders:
            for r in recs:
                rep.orphaned[key or "(blank Order ID)"] = r["id"]

    if fix and (rep.missing or rep.mismatched):
        todo = [store_orders[k] for k in rep.missing] + [store_orders[m.order_key] for m in rep.mismatched]
        res: SyncReport = engine.sync_orders(todo, force=True)
        rep.fixed = res.counts

    if out_dir:
        out_dir.mkdir(parents=True, exist_ok=True)
        (out_dir / "reconciliation-report.md").write_text(rep.to_markdown())
        (out_dir / "reconciliation-report.json").write_text(json.dumps(rep.to_dict(), indent=2, default=str))
    return rep
