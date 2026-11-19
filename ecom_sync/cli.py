"""Command line: backfill, reconcile, dlq, replay.

    python -m ecom_sync.cli backfill [--source gumroad|shopify|all] [--full]
    python -m ecom_sync.cli reconcile [--fix]
    python -m ecom_sync.cli dlq
    python -m ecom_sync.cli replay <id|all>
"""

from __future__ import annotations

import argparse
import sys

from .config import load_settings
from .logging_setup import setup_logging
from .pipeline import _short, build_engine
from .reconcile import reconcile


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="ecom_sync")
    sub = ap.add_subparsers(dest="cmd", required=True)
    b = sub.add_parser("backfill")
    b.add_argument("--source", default="all", choices=["all", "gumroad", "shopify"])
    b.add_argument("--full", action="store_true", help="ignore the saved cursor and re-read everything")
    r = sub.add_parser("reconcile")
    r.add_argument("--fix", action="store_true")
    sub.add_parser("dlq")
    rp = sub.add_parser("replay")
    rp.add_argument("id")
    args = ap.parse_args(argv)

    s = load_settings()
    setup_logging(s.log_json)
    eng = build_engine(s)

    if args.cmd == "backfill":
        for src in (["gumroad", "shopify"] if args.source == "all" else [args.source]):
            rep = eng.backfill(src, since=None if args.full else eng.store.get_cursor(src))
            print(f"\n{src}: {rep.counts}")
            for o in rep.outcomes:
                print(f"  {o.outcome:<14} {o.order_key:<22} {o.detail[:90]}")
        print(f"\nAirtable requests: {eng.airtable.requests}, 429s received: {eng.airtable.rate_limited}, "
              f"time spent throttling: {eng.airtable.limiter.waited:.1f}s")
    elif args.cmd == "reconcile":
        rep = reconcile(eng, fix=args.fix, out_dir=s.data_dir / "reports")
        print(rep.to_markdown())
        return 0 if rep.clean or args.fix else 2
    elif args.cmd == "dlq":
        items = eng.store.dead_letters()
        if not items:
            print("Dead-letter queue is empty.")
        for d in items:
            print(f"  #{d['id']:<3} {d['order_key']:<22} step={d['step']:<16} attempts={d['attempts']}  {_short(d['error'])[:110]}")
    elif args.cmd == "replay":
        ids = [d["id"] for d in eng.store.dead_letters()] if args.id == "all" else [int(args.id)]
        for i in ids:
            rep = eng.replay(i)
            print(f"  replay #{i}: {rep.counts}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
