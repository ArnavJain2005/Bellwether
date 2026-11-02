"""Sample SQLite database generator for testing the Bellwether Streamlit monitor.

Creates isolated sample databases matching ``bot/CONTRACT.md`` in a temporary
directory for the four test states:
- ``normal``: populated tables with an alive heartbeat (< 30s)
- ``empty``: schema created, zero rows in all tables
- ``missing``: directory without a database file
- ``stale``: populated tables with a stale heartbeat (>= 30s), stale quotes (> 60s),
  and ``STOP`` / ``PAUSED`` control files in the temporary state directory

Safety rule: refuses to write to the repository's ``bot/state/`` directory.
"""

from __future__ import annotations

import argparse
import json
import sqlite3
import tempfile
from datetime import UTC, datetime, timedelta
from pathlib import Path

from dashboard.db import default_repo_root

CONTRACT_DDL = """
PRAGMA journal_mode = WAL;
PRAGMA synchronous = NORMAL;

CREATE TABLE IF NOT EXISTS heartbeat (
    id              INTEGER PRIMARY KEY CHECK (id = 1),
    ts_utc          TEXT NOT NULL,
    mode            TEXT NOT NULL,
    version         TEXT NOT NULL,
    loop_ms         INTEGER,
    reads_last_min  INTEGER,
    writes_last_min INTEGER,
    paused          INTEGER NOT NULL,
    pause_reason    TEXT,
    stop_seen       INTEGER NOT NULL,
    balance         TEXT,
    start_balance   TEXT,
    committed       TEXT NOT NULL,
    scale           TEXT NOT NULL,
    cap_total       TEXT NOT NULL,
    cap_per_race    TEXT NOT NULL,
    min_cash_reserve TEXT NOT NULL,
    edge_now        TEXT NOT NULL,
    election_night  INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS orders (
    key             TEXT PRIMARY KEY,
    created_utc     TEXT NOT NULL,
    updated_utc     TEXT NOT NULL,
    race_key        TEXT NOT NULL,
    sig_market_id   TEXT NOT NULL,
    sig_exchange_id TEXT NOT NULL,
    side            TEXT NOT NULL,
    action          TEXT NOT NULL,
    price_side      TEXT NOT NULL,
    wire_price      TEXT NOT NULL,
    quantity        INTEGER NOT NULL,
    expires_utc     TEXT,
    strategy        TEXT NOT NULL,
    reason          TEXT NOT NULL,
    status          TEXT NOT NULL,
    sig_order_id    TEXT,
    quantity_traded INTEGER NOT NULL DEFAULT 0,
    total_cost      TEXT NOT NULL DEFAULT '0',
    http_status     INTEGER,
    error           TEXT,
    response_json   TEXT
);

CREATE INDEX IF NOT EXISTS idx_orders_status ON orders(status);
CREATE INDEX IF NOT EXISTS idx_orders_race   ON orders(race_key);

CREATE TABLE IF NOT EXISTS decisions (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    ts_utc          TEXT NOT NULL,
    race_key        TEXT NOT NULL,
    sig_exchange_id TEXT,
    party           TEXT,
    strategy        TEXT NOT NULL,
    sig_bid         TEXT,
    sig_ask         TEXT,
    kalshi_mid      TEXT,
    poly_mid        TEXT,
    fair            TEXT,
    edge            TEXT,
    verdict         TEXT NOT NULL,
    side            TEXT,
    price_side      TEXT,
    quantity        INTEGER,
    reason          TEXT NOT NULL,
    order_key       TEXT
);

CREATE INDEX IF NOT EXISTS idx_decisions_ts ON decisions(ts_utc);

CREATE TABLE IF NOT EXISTS quotes_latest (
    sig_exchange_id TEXT PRIMARY KEY,
    race_key        TEXT NOT NULL,
    party           TEXT NOT NULL,
    ts_utc          TEXT NOT NULL,
    sig_bid         TEXT,
    sig_ask         TEXT,
    sig_last        TEXT,
    kalshi_ticker   TEXT,
    kalshi_bid      TEXT,
    kalshi_ask      TEXT,
    kalshi_ts_utc   TEXT,
    poly_token      TEXT,
    poly_bid        TEXT,
    poly_ask        TEXT,
    poly_ts_utc     TEXT,
    fair            TEXT,
    edge            TEXT
);

CREATE TABLE IF NOT EXISTS exposure (
    race_key        TEXT PRIMARY KEY,
    ts_utc          TEXT NOT NULL,
    committed       TEXT NOT NULL,
    cap             TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS snapshots (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    ts_utc          TEXT NOT NULL,
    balance         TEXT,
    committed       TEXT NOT NULL,
    open_orders     INTEGER NOT NULL,
    positions_json  TEXT NOT NULL,
    pnl_json        TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS events (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    ts_utc          TEXT NOT NULL,
    level           TEXT NOT NULL,
    kind            TEXT NOT NULL,
    message         TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_events_ts ON events(ts_utc);
"""


def _iso_utc(dt: datetime) -> str:
    return dt.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def _assert_not_real_state_dir(target_dir: Path) -> None:
    """Prevent any sample generator from touching ``<repo>/bot/state``."""
    real_state = (default_repo_root() / "bot" / "state").resolve()
    resolved_target = target_dir.resolve()
    if resolved_target == real_state or real_state in resolved_target.parents:
        raise ValueError(
            f"Refusing to write sample database inside real bot/state directory: {resolved_target}"
        )


def create_sample_state(
    mode: str = "normal",
    target_dir: str | Path | None = None,
    now_utc: datetime | None = None,
) -> Path:
    """Create a sample state directory for ``mode`` and return ``db_path``.

    Modes:
    - ``normal``: alive heartbeat (5s old), populated tables, mixed fresh/stale quotes
    - ``empty``: all contract tables exist with 0 rows
    - ``missing``: directory exists but ``bellwether.db`` does not exist
    - ``stale``: heartbeat 95s old (`STALE`), `PAUSED` and `STOP` files present
    """
    valid_modes = {"normal", "empty", "missing", "stale"}
    if mode not in valid_modes:
        raise ValueError(f"Unsupported sample mode '{mode}'. Choose from {sorted(valid_modes)}")

    if target_dir is None:
        base_dir = Path(tempfile.mkdtemp(prefix=f"bellwether_sample_{mode}_"))
    else:
        base_dir = Path(target_dir).expanduser().resolve()
        _assert_not_real_state_dir(base_dir)
        base_dir.mkdir(parents=True, exist_ok=True)

    _assert_not_real_state_dir(base_dir)
    db_path = base_dir / "bellwether.db"
    stop_file = base_dir / "STOP"
    paused_file = base_dir / "PAUSED"

    for existing in (
        db_path,
        Path(f"{db_path}-wal"),
        Path(f"{db_path}-shm"),
        stop_file,
        paused_file,
    ):
        if existing.exists():
            existing.unlink()

    if mode == "missing":
        return db_path

    conn = sqlite3.connect(str(db_path))
    try:
        conn.executescript(CONTRACT_DDL)
        if mode == "empty":
            conn.commit()
            return db_path

        current = now_utc.astimezone(UTC) if now_utc else datetime.now(UTC)
        hb_offset = timedelta(seconds=5) if mode == "normal" else timedelta(seconds=95)
        hb_ts = _iso_utc(current - hb_offset)

        if mode == "stale":
            paused_file.write_text("429 cooldown from SIG gateway\n", encoding="utf-8")
            stop_file.write_text("Operator emergency halt\n", encoding="utf-8")

        conn.execute(
            """
            INSERT INTO heartbeat (
                id, ts_utc, mode, version, loop_ms, reads_last_min, writes_last_min,
                paused, pause_reason, stop_seen, balance, start_balance, committed,
                scale, cap_total, cap_per_race, min_cash_reserve, edge_now, election_night
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                1,
                hb_ts,
                "dry_run" if mode == "normal" else "live",
                "ew-0.4.2",
                142 if mode == "normal" else 890,
                28,
                4,
                0 if mode == "normal" else 1,
                None if mode == "normal" else "429 rate-limit backoff",
                0 if mode == "normal" else 1,
                "10482.50",
                "10000.00",
                "1845.00",
                "1.00",
                "3000.00",
                "600.00",
                "2500.00",
                "0.030",
                1,
            ),
        )

        # Snapshots (2 rows; latest has id=2)
        conn.execute(
            """
            INSERT INTO snapshots (
                ts_utc, balance, committed, open_orders, positions_json, pnl_json
            ) VALUES (?, ?, ?, ?, ?, ?)
            """,
            (
                _iso_utc(current - timedelta(minutes=5)),
                "10310.00",
                "1620.00",
                2,
                json.dumps({"SEN-PA-2026:D": 400, "SEN-MI-2026:D": 300}),
                json.dumps({"total": "310.00", "realized": "120.00", "unrealized": "190.00"}),
            ),
        )
        conn.execute(
            """
            INSERT INTO snapshots (
                ts_utc, balance, committed, open_orders, positions_json, pnl_json
            ) VALUES (?, ?, ?, ?, ?, ?)
            """,
            (
                _iso_utc(current - timedelta(seconds=8 if mode == "normal" else 100)),
                "10482.50" if mode == "normal" else "9815.25",
                "1845.00",
                3,
                json.dumps(
                    {
                        "SEN-PA-2026:D": 550,
                        "SEN-MI-2026:D": 420,
                        "GOV-PA-2026:D": 310,
                        "HOUSE-NY-17:R": 250,
                    }
                ),
                json.dumps(
                    {
                        "total": "482.50" if mode == "normal" else "-184.75",
                        "realized": "215.00" if mode == "normal" else "-90.00",
                        "unrealized": "267.50" if mode == "normal" else "-94.75",
                    }
                ),
            ),
        )

        # Exposure rows
        exposure_rows = [
            ("SEN-PA-2026", _iso_utc(current - timedelta(seconds=10)), "540.00", "600.00"),
            ("SEN-MI-2026", _iso_utc(current - timedelta(seconds=12)), "450.00", "600.00"),
            ("SEN-WI-2026", _iso_utc(current - timedelta(seconds=15)), "360.00", "600.00"),
            ("GOV-PA-2026", _iso_utc(current - timedelta(seconds=18)), "285.00", "600.00"),
            ("HOUSE-NY-17", _iso_utc(current - timedelta(seconds=22)), "210.00", "600.00"),
        ]
        conn.executemany(
            "INSERT INTO exposure (race_key, ts_utc, committed, cap) VALUES (?, ?, ?, ?)",
            exposure_rows,
        )

        # Orders rows (mix of open and terminal statuses)
        orders_rows = [
            (
                "ord-20261103-006",
                _iso_utc(current - timedelta(seconds=14)),
                _iso_utc(current - timedelta(seconds=6)),
                "SEN-PA-2026",
                "mkt-pa-sen",
                "SIG-PA-SEN-D",
                "yes",
                "buy",
                "yes",
                "0.535",
                120,
                _iso_utc(current + timedelta(minutes=10)),
                "reference",
                "fair=0.572 vs sig_ask=0.535 (+3.7c edge)",
                "resting",
                "sig-992014",
                0,
                "0.00",
                200,
                None,
                "{}",
            ),
            (
                "ord-20261103-005",
                _iso_utc(current - timedelta(seconds=28)),
                _iso_utc(current - timedelta(seconds=9)),
                "SEN-MI-2026",
                "mkt-mi-sen",
                "SIG-MI-SEN-D",
                "yes",
                "buy",
                "yes",
                "0.560",
                150,
                _iso_utc(current + timedelta(minutes=8)),
                "complement",
                "D_ask(0.560)+R_ask(0.405)=0.965 < 0.970",
                "partial",
                "sig-992011",
                90,
                "50.40",
                200,
                None,
                "{}",
            ),
            (
                "ord-20261103-004",
                _iso_utc(current - timedelta(seconds=35)),
                _iso_utc(current - timedelta(seconds=11)),
                "SEN-WI-2026",
                "mkt-wi-sen",
                "SIG-WI-SEN-R",
                "no",
                "buy",
                "no",
                "0.480",
                80,
                _iso_utc(current + timedelta(minutes=12)),
                "reference",
                "poly/kalshi consensus 0.518 vs ask 0.480",
                "sent",
                "sig-992009",
                0,
                "0.00",
                202,
                None,
                "{}",
            ),
            (
                "ord-20261103-003",
                _iso_utc(current - timedelta(minutes=2)),
                _iso_utc(current - timedelta(seconds=45)),
                "GOV-PA-2026",
                "mkt-pa-gov",
                "SIG-PA-GOV-D",
                "yes",
                "buy",
                "yes",
                "0.615",
                200,
                _iso_utc(current + timedelta(minutes=5)),
                "reference",
                "kalshi=0.650 poly=0.648 -> +3.4c edge",
                "filled",
                "sig-991998",
                200,
                "123.00",
                200,
                None,
                "{}",
            ),
            (
                "ord-20261103-002",
                _iso_utc(current - timedelta(minutes=4)),
                _iso_utc(current - timedelta(minutes=3)),
                "HOUSE-NY-17",
                "mkt-ny17",
                "SIG-NY17-R",
                "yes",
                "buy",
                "yes",
                "0.520",
                100,
                _iso_utc(current - timedelta(minutes=1)),
                "probe",
                "initial live probe order",
                "cancelled",
                "sig-991950",
                0,
                "0.00",
                200,
                None,
                "{}",
            ),
            (
                "ord-20261103-001",
                _iso_utc(current - timedelta(minutes=6)),
                _iso_utc(current - timedelta(minutes=5)),
                "SEN-AZ-2026",
                "mkt-az-sen",
                "SIG-AZ-SEN-D",
                "yes",
                "buy",
                "yes",
                "0.545",
                100,
                _iso_utc(current - timedelta(minutes=2)),
                "reference",
                "fair=0.580 vs ask=0.545",
                "rejected",
                None,
                0,
                "0.00",
                429,
                "HTTP 429 rate limit exceeded",
                "{}",
            ),
        ]
        conn.executemany(
            """
            INSERT INTO orders (
                key, created_utc, updated_utc, race_key, sig_market_id, sig_exchange_id,
                side, action, price_side, wire_price, quantity, expires_utc, strategy,
                reason, status, sig_order_id, quantity_traded, total_cost, http_status,
                error, response_json
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            orders_rows,
        )

        # Decisions rows
        decisions_rows = [
            (
                _iso_utc(current - timedelta(seconds=6)),
                "SEN-PA-2026",
                "SIG-PA-SEN-D",
                "D",
                "reference",
                "0.525",
                "0.535",
                "0.574",
                "0.570",
                "0.572",
                "0.037",
                "order",
                "yes",
                "yes",
                120,
                "edge +3.7c >= 3.0c floor; kalshi/poly diverge 0.4c <= 6c",
                "ord-20261103-006",
            ),
            (
                _iso_utc(current - timedelta(seconds=9)),
                "SEN-MI-2026",
                "SIG-MI-SEN-D",
                "D",
                "complement",
                "0.550",
                "0.560",
                "0.585",
                "0.582",
                "0.595",
                "0.035",
                "order",
                "yes",
                "yes",
                150,
                "sum of asks D(0.560)+R(0.405)=0.965 <= 0.970 arb bound",
                "ord-20261103-005",
            ),
            (
                _iso_utc(current - timedelta(seconds=12)),
                "SEN-OH-2026",
                "SIG-OH-SEN-R",
                "R",
                "reference",
                "0.575",
                "0.590",
                "0.630",
                "0.545",
                "0.588",
                "-0.002",
                "blocked",
                "yes",
                "yes",
                0,
                "venue disagreement |kalshi(0.630)-poly(0.545)|=8.5c > 6.0c limit",
                None,
            ),
            (
                _iso_utc(current - timedelta(seconds=16)),
                "GOV-PA-2026",
                "SIG-PA-GOV-D",
                "D",
                "reference",
                "0.605",
                "0.615",
                "0.632",
                "0.628",
                "0.630",
                "0.015",
                "skip",
                "yes",
                "yes",
                0,
                "edge +1.5c below election-night threshold 3.0c",
                None,
            ),
            (
                _iso_utc(current - timedelta(seconds=21)),
                "SEN-WI-2026",
                "SIG-WI-SEN-D",
                "D",
                "reference",
                "0.505",
                "0.515",
                "0.552",
                "0.548",
                "0.550",
                "0.035",
                "would_order",
                "yes",
                "yes",
                100,
                "dry_run: fair 0.550 vs ask 0.515 (+3.5c edge, cap ok)",
                None,
            ),
        ]
        conn.executemany(
            """
            INSERT INTO decisions (
                ts_utc, race_key, sig_exchange_id, party, strategy, sig_bid, sig_ask,
                kalshi_mid, poly_mid, fair, edge, verdict, side, price_side, quantity,
                reason, order_key
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            list(reversed(decisions_rows)),
        )

        # Quotes latest (includes fresh <60s and stale >60s quotes)
        quotes_rows = [
            (
                "SIG-PA-SEN-D",
                "SEN-PA-2026",
                "D",
                _iso_utc(current - timedelta(seconds=4 if mode == "normal" else 82)),
                "0.525",
                "0.535",
                "0.530",
                "KXSENPA-26-D",
                "0.570",
                "0.578",
                _iso_utc(current - timedelta(seconds=5)),
                "poly-pa-sen-d",
                "0.566",
                "0.574",
                _iso_utc(current - timedelta(seconds=6)),
                "0.572",
                "0.037",
            ),
            (
                "SIG-MI-SEN-D",
                "SEN-MI-2026",
                "D",
                _iso_utc(current - timedelta(seconds=7 if mode == "normal" else 90)),
                "0.550",
                "0.560",
                "0.555",
                "KXSENMI-26-D",
                "0.580",
                "0.590",
                _iso_utc(current - timedelta(seconds=8)),
                "poly-mi-sen-d",
                "0.578",
                "0.586",
                _iso_utc(current - timedelta(seconds=7)),
                "0.584",
                "0.024",
            ),
            (
                "SIG-WI-SEN-D",
                "SEN-WI-2026",
                "D",
                _iso_utc(current - timedelta(seconds=11 if mode == "normal" else 115)),
                "0.505",
                "0.515",
                "0.510",
                "KXSENWI-26-D",
                "0.548",
                "0.556",
                _iso_utc(current - timedelta(seconds=10)),
                "poly-wi-sen-d",
                "0.544",
                "0.552",
                _iso_utc(current - timedelta(seconds=11)),
                "0.550",
                "0.035",
            ),
            (
                "SIG-PA-GOV-D",
                "GOV-PA-2026",
                "D",
                _iso_utc(current - timedelta(seconds=15 if mode == "normal" else 130)),
                "0.605",
                "0.615",
                "0.610",
                "KXGOVPA-26-D",
                "0.628",
                "0.636",
                _iso_utc(current - timedelta(seconds=14)),
                "poly-pa-gov-d",
                "0.624",
                "0.632",
                _iso_utc(current - timedelta(seconds=15)),
                "0.630",
                "0.015",
            ),
            (
                "SIG-AZ-SEN-D",
                "SEN-AZ-2026",
                "D",
                _iso_utc(current - timedelta(seconds=78)),  # > 60s -> flagged STALE
                "0.530",
                "0.545",
                "0.538",
                "KXSENAZ-26-D",
                "0.555",
                "0.565",
                _iso_utc(current - timedelta(seconds=79)),
                "poly-az-sen-d",
                "0.550",
                "0.560",
                _iso_utc(current - timedelta(seconds=80)),
                "0.558",
                "0.013",
            ),
        ]
        conn.executemany(
            """
            INSERT INTO quotes_latest (
                sig_exchange_id, race_key, party, ts_utc, sig_bid, sig_ask, sig_last,
                kalshi_ticker, kalshi_bid, kalshi_ask, kalshi_ts_utc,
                poly_token, poly_bid, poly_ask, poly_ts_utc, fair, edge
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            quotes_rows,
        )

        # Events rows (info, warn, error)
        events_rows = [
            (
                _iso_utc(current - timedelta(minutes=6)),
                "info",
                "startup",
                "Bellwether election-week bot initialized (WAL mode, contract v1)",
            ),
            (
                _iso_utc(current - timedelta(minutes=5)),
                "error",
                "rate_limit",
                "SIG HTTP 429 on order ord-20261103-001 (SEN-AZ-2026); backing off 60s",
            ),
            (
                _iso_utc(current - timedelta(minutes=2)),
                "info",
                "fill",
                "Order ord-20261103-003 filled 200 @ 0.615 SUSQ on SIG-PA-GOV-D",
            ),
            (
                _iso_utc(current - timedelta(seconds=78)),
                "warn",
                "stale_quote",
                "Quote SIG-AZ-SEN-D older than 60s (78s); skipping SEN-AZ-2026 decisions",
            ),
            (
                _iso_utc(current - timedelta(seconds=12)),
                "warn",
                "venue_divergence",
                "SEN-OH-2026 blocked: Kalshi (0.630) vs Polymarket (0.545) spread 8.5c > 6.0c",
            ),
            (
                _iso_utc(current - timedelta(seconds=6)),
                "info" if mode == "normal" else "error",
                "order_resting" if mode == "normal" else "halt_detected",
                (
                    "Resting BUY YES 120 @ 0.535 on SIG-PA-SEN-D (edge +3.7c)"
                    if mode == "normal"
                    else "STOP and PAUSED flags active; loop halted pending operator review"
                ),
            ),
        ]
        conn.executemany(
            "INSERT INTO events (ts_utc, level, kind, message) VALUES (?, ?, ?, ?)",
            events_rows,
        )

        conn.commit()
    finally:
        conn.close()

    return db_path


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Generate a sample Bellwether SQLite database in a temporary directory."
    )
    parser.add_argument(
        "--mode",
        choices=("normal", "empty", "missing", "stale"),
        default="normal",
        help="Sample database scenario to generate.",
    )
    parser.add_argument(
        "--dir",
        type=str,
        default=None,
        help="Target temporary directory (must not be inside bot/state).",
    )
    args = parser.parse_args()
    db_path = create_sample_state(mode=args.mode, target_dir=args.dir)
    print(f"Created sample state (mode={args.mode}):")
    print(f"  BELLWETHER_STATE_DIR={db_path.parent}")
    print(f"  BELLWETHER_DB_PATH={db_path}")


if __name__ == "__main__":
    main()
