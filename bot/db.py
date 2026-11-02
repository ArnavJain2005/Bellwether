"""bot/state/bellwether.db: the tables of bot/CONTRACT.md, exactly as named there.

Prices and money are decimal strings (TEXT); timestamps are UTC ISO-8601 ending in Z.
Every helper commits before it returns, so a row is durable once the call ends.
"""

from __future__ import annotations

import sqlite3
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

STATE_DIR = Path(__file__).resolve().parent / "state"
DB_PATH = STATE_DIR / "bellwether.db"

ORDER_STATUSES = (
    "journaled",
    "sent",
    "resting",
    "filled",
    "partial",
    "cancelled",
    "expired",
    "rejected",
    "unknown",
)

_SCHEMA = """
CREATE TABLE IF NOT EXISTS heartbeat (
  id INTEGER PRIMARY KEY CHECK (id = 1),
  ts_utc TEXT, mode TEXT CHECK (mode IN ('dry_run','live')), version TEXT,
  loop_ms INTEGER, reads_last_min INTEGER, writes_last_min INTEGER,
  paused INTEGER CHECK (paused IN (0,1)), pause_reason TEXT,
  stop_seen INTEGER CHECK (stop_seen IN (0,1)), balance TEXT, start_balance TEXT,
  committed TEXT, scale TEXT, cap_total TEXT, cap_per_race TEXT, min_cash_reserve TEXT,
  edge_now TEXT, election_night INTEGER CHECK (election_night IN (0,1))
);
CREATE TABLE IF NOT EXISTS orders (
  key TEXT PRIMARY KEY, created_utc TEXT NOT NULL, updated_utc TEXT NOT NULL,
  race_key TEXT, sig_market_id TEXT, sig_exchange_id TEXT NOT NULL,
  side TEXT NOT NULL CHECK (side IN ('yes','no')),
  action TEXT NOT NULL CHECK (action = 'buy'),
  price_side TEXT NOT NULL, wire_price TEXT NOT NULL, quantity INTEGER NOT NULL,
  expires_utc TEXT NOT NULL,
  strategy TEXT NOT NULL CHECK (strategy IN ('complement','reference','probe')),
  reason TEXT,
  status TEXT NOT NULL CHECK (status IN ('journaled','sent','resting','filled','partial',
    'cancelled','expired','rejected','unknown')),
  sig_order_id TEXT, quantity_traded TEXT, total_cost TEXT, http_status INTEGER,
  error TEXT, response_json TEXT
);
CREATE TABLE IF NOT EXISTS decisions (
  id INTEGER PRIMARY KEY AUTOINCREMENT, ts_utc TEXT NOT NULL, race_key TEXT,
  sig_exchange_id TEXT, party TEXT CHECK (party IN ('D','R','I')), strategy TEXT,
  sig_bid TEXT, sig_ask TEXT, kalshi_mid TEXT, poly_mid TEXT, fair TEXT, edge TEXT,
  verdict TEXT NOT NULL CHECK (verdict IN ('skip','would_order','order','blocked')),
  side TEXT, price_side TEXT, quantity INTEGER, reason TEXT, order_key TEXT
);
CREATE TABLE IF NOT EXISTS quotes_latest (
  sig_exchange_id TEXT PRIMARY KEY, race_key TEXT, party TEXT, ts_utc TEXT,
  sig_bid TEXT, sig_ask TEXT, sig_last TEXT,
  kalshi_ticker TEXT, kalshi_bid TEXT, kalshi_ask TEXT, kalshi_ts_utc TEXT,
  poly_token TEXT, poly_bid TEXT, poly_ask TEXT, poly_ts_utc TEXT, fair TEXT, edge TEXT
);
CREATE TABLE IF NOT EXISTS exposure (
  race_key TEXT PRIMARY KEY, ts_utc TEXT, committed TEXT, cap TEXT
);
CREATE TABLE IF NOT EXISTS snapshots (
  id INTEGER PRIMARY KEY AUTOINCREMENT, ts_utc TEXT NOT NULL, balance TEXT,
  committed TEXT, open_orders INTEGER, positions_json TEXT, pnl_json TEXT
);
CREATE TABLE IF NOT EXISTS events (
  id INTEGER PRIMARY KEY AUTOINCREMENT, ts_utc TEXT NOT NULL,
  level TEXT NOT NULL CHECK (level IN ('info','warn','error')),
  kind TEXT NOT NULL, message TEXT NOT NULL
);
"""


def utc_stamp(moment: datetime | None = None) -> str:
    """UTC ISO-8601 to the second, ending in Z."""
    moment = moment or datetime.now(UTC)
    return moment.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def connect(path: Path = DB_PATH) -> sqlite3.Connection:
    """Open the database (creating its folder and tables), WAL mode, busy_timeout 5000."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=5000")
    conn.executescript(_SCHEMA)
    conn.commit()
    return conn


def _insert(conn: sqlite3.Connection, table: str, row: dict[str, Any]) -> int:
    cols = ", ".join(row)
    marks = ", ".join("?" for _ in row)
    cur = conn.execute(f"INSERT INTO {table} ({cols}) VALUES ({marks})", tuple(row.values()))
    conn.commit()
    return int(cur.lastrowid or 0)


def insert_order(conn: sqlite3.Connection, row: dict[str, Any]) -> None:
    """Journal a new order row and commit it (call BEFORE sending)."""
    _insert(conn, "orders", row)


def update_order(conn: sqlite3.Connection, key: str, **fields: Any) -> None:
    """Update an order row, stamp updated_utc and commit."""
    fields["updated_utc"] = utc_stamp()
    sets = ", ".join(f"{name} = ?" for name in fields)
    conn.execute(f"UPDATE orders SET {sets} WHERE key = ?", (*fields.values(), key))
    conn.commit()


def get_order(conn: sqlite3.Connection, key: str) -> sqlite3.Row | None:
    return conn.execute("SELECT * FROM orders WHERE key = ?", (key,)).fetchone()


def orders_with_status(conn: sqlite3.Connection, *statuses: str) -> list[sqlite3.Row]:
    marks = ", ".join("?" for _ in statuses)
    return conn.execute(f"SELECT * FROM orders WHERE status IN ({marks})", statuses).fetchall()


def any_unknown(conn: sqlite3.Connection) -> bool:
    return (
        conn.execute("SELECT 1 FROM orders WHERE status = 'unknown' LIMIT 1").fetchone() is not None
    )


def insert_decision(conn: sqlite3.Connection, **row: Any) -> int:
    row.setdefault("ts_utc", utc_stamp())
    return _insert(conn, "decisions", row)


def insert_event(conn: sqlite3.Connection, level: str, kind: str, message: str) -> int:
    return _insert(
        conn, "events", {"ts_utc": utc_stamp(), "level": level, "kind": kind, "message": message}
    )


def _upsert(conn: sqlite3.Connection, table: str, key: str, row: dict[str, Any]) -> None:
    cols = ", ".join(row)
    marks = ", ".join("?" for _ in row)
    sets = ", ".join(f"{name} = excluded.{name}" for name in row if name != key)
    conn.execute(
        f"INSERT INTO {table} ({cols}) VALUES ({marks}) ON CONFLICT({key}) DO UPDATE SET {sets}",
        tuple(row.values()),
    )
    conn.commit()


def upsert_heartbeat(conn: sqlite3.Connection, **row: Any) -> None:
    """Rewrite the single heartbeat row (id = 1)."""
    row["id"] = 1
    row.setdefault("ts_utc", utc_stamp())
    _upsert(conn, "heartbeat", "id", row)


def upsert_quote(conn: sqlite3.Connection, **row: Any) -> None:
    row.setdefault("ts_utc", utc_stamp())
    _upsert(conn, "quotes_latest", "sig_exchange_id", row)


def upsert_exposure(conn: sqlite3.Connection, **row: Any) -> None:
    row.setdefault("ts_utc", utc_stamp())
    _upsert(conn, "exposure", "race_key", row)


def insert_snapshot(conn: sqlite3.Connection, **row: Any) -> int:
    row.setdefault("ts_utc", utc_stamp())
    return _insert(conn, "snapshots", row)
