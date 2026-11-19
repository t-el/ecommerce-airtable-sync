"""Local state in SQLite: webhook ledger (idempotency), last-synced fingerprints, dead letters, cursors."""

from __future__ import annotations

import json
import sqlite3
import threading
from datetime import datetime, timezone
from pathlib import Path


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


class Store:
    def __init__(self, path: str | Path):
        if str(path) != ":memory:":
            Path(path).parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(str(path), check_same_thread=False)
        self.db.row_factory = sqlite3.Row
        self.lock = threading.Lock()
        self.db.executescript("""
            PRAGMA journal_mode=WAL;
            CREATE TABLE IF NOT EXISTS webhook_events (
                event_id TEXT PRIMARY KEY, source TEXT, topic TEXT, outcome TEXT, received_at TEXT);
            CREATE TABLE IF NOT EXISTS orders (
                order_key TEXT PRIMARY KEY, fingerprint TEXT, updated_at TEXT, airtable_id TEXT, synced_at TEXT, status TEXT);
            CREATE TABLE IF NOT EXISTS dead_letters (
                id INTEGER PRIMARY KEY AUTOINCREMENT, order_key TEXT, source TEXT, step TEXT, error TEXT,
                payload TEXT, attempts INTEGER DEFAULT 1, status TEXT DEFAULT 'open', created_at TEXT, updated_at TEXT);
            CREATE TABLE IF NOT EXISTS cursors (source TEXT PRIMARY KEY, value TEXT);
        """)

    # webhook idempotency ----------------------------------------------------------------------
    def claim_event(self, event_id: str, source: str, topic: str) -> bool:
        """True the first time we see this event id, False for retries/resends."""
        with self.lock, self.db:
            cur = self.db.execute("INSERT OR IGNORE INTO webhook_events VALUES (?,?,?,?,?)",
                                  (event_id, source, topic, "received", now_iso()))
            return cur.rowcount == 1

    def set_event_outcome(self, event_id: str, outcome: str) -> None:
        with self.lock, self.db:
            self.db.execute("UPDATE webhook_events SET outcome=? WHERE event_id=?", (outcome, event_id))

    # synced orders ----------------------------------------------------------------------------
    def get_order(self, order_key: str) -> sqlite3.Row | None:
        return self.db.execute("SELECT * FROM orders WHERE order_key=?", (order_key,)).fetchone()

    def save_order(self, order_key: str, fingerprint: str, updated_at: str, airtable_id: str, status: str) -> None:
        with self.lock, self.db:
            self.db.execute("INSERT INTO orders VALUES (?,?,?,?,?,?) ON CONFLICT(order_key) DO UPDATE SET "
                            "fingerprint=excluded.fingerprint, updated_at=excluded.updated_at, "
                            "airtable_id=excluded.airtable_id, synced_at=excluded.synced_at, status=excluded.status",
                            (order_key, fingerprint, updated_at, airtable_id, now_iso(), status))

    def forget_order(self, order_key: str) -> None:
        with self.lock, self.db:
            self.db.execute("DELETE FROM orders WHERE order_key=?", (order_key,))

    # dead letters -----------------------------------------------------------------------------
    def add_dead_letter(self, order_key: str, source: str, step: str, error: str, payload: dict) -> int:
        with self.lock, self.db:
            row = self.db.execute("SELECT id, attempts FROM dead_letters WHERE order_key=? AND step=? AND status='open'",
                                  (order_key, step)).fetchone()
            if row:   # same order failing again: bump attempts instead of piling up duplicates
                self.db.execute("UPDATE dead_letters SET attempts=attempts+1, error=?, payload=?, updated_at=? WHERE id=?",
                                (error, json.dumps(payload), now_iso(), row["id"]))
                return row["id"]
            cur = self.db.execute("INSERT INTO dead_letters (order_key, source, step, error, payload, created_at, updated_at)"
                                  " VALUES (?,?,?,?,?,?,?)",
                                  (order_key, source, step, error, json.dumps(payload), now_iso(), now_iso()))
            return int(cur.lastrowid)

    def dead_letters(self, status: str | None = "open") -> list[dict]:
        q, args = "SELECT * FROM dead_letters", ()
        if status:
            q, args = q + " WHERE status=?", (status,)
        return [dict(r) | {"payload": json.loads(r["payload"])} for r in self.db.execute(q + " ORDER BY id", args)]

    def get_dead_letter(self, dlq_id: int) -> dict | None:
        r = self.db.execute("SELECT * FROM dead_letters WHERE id=?", (dlq_id,)).fetchone()
        return dict(r) | {"payload": json.loads(r["payload"])} if r else None

    def resolve_dead_letter(self, dlq_id: int) -> None:
        with self.lock, self.db:
            self.db.execute("UPDATE dead_letters SET status='resolved', updated_at=? WHERE id=?", (now_iso(), dlq_id))

    # cursors ----------------------------------------------------------------------------------
    def get_cursor(self, source: str) -> str | None:
        r = self.db.execute("SELECT value FROM cursors WHERE source=?", (source,)).fetchone()
        return r["value"] if r else None

    def set_cursor(self, source: str, value: str) -> None:
        with self.lock, self.db:
            self.db.execute("INSERT INTO cursors VALUES (?,?) ON CONFLICT(source) DO UPDATE SET value=excluded.value",
                            (source, value))
