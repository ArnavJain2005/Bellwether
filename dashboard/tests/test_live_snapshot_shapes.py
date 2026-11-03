"""Live-data shapes the bot writes today (bot/main.py), seen on the first live run.

- ``snapshots.positions_json`` holds SIG ``TournamentPosition`` rows (camelCase keys).
- ``snapshots.pnl_json`` is NULL.
- ``quotes_latest`` keeps rows for contracts the bot stopped refreshing.
"""

from __future__ import annotations

import json
import sqlite3
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path

from dashboard.app import (
    build_needs_attention_items,
    render_positions_panel_html,
    render_race_pnl_panel_html,
)
from dashboard.db import (
    derive_pnl_from_positions,
    load_monitor_snapshot,
    parse_pnl_json,
    parse_positions_json,
)
from dashboard.sample_db import create_sample_state

FIXED_NOW = datetime(2026, 11, 3, 14, 30, 0, tzinfo=UTC)


def _sig_row(exchange_id: str, quantity: str, **overrides: object) -> dict[str, object]:
    """A ``TournamentPosition.model_dump(mode="json")`` row: Decimals dump as strings."""
    row: dict[str, object] = {
        "exchangeId": exchange_id,
        "marketId": "77",
        "marketTitle": "Senate race",
        "option": None,
        "settled": False,
        "quantity": quantity,
        "avgCost": "0.92",
        "currentPrice": "0.93",
        "marketValue": "946.74",
        "costBasis": "936.56",
        "unrealizedPnl": "10.18",
        "unrealizedPnlPct": "1.09",
        "moneyEarned": "0",
        "lots": [
            {
                "lotId": "L1",
                "side": "no",
                "quantity": quantity,
                "entryPrice": "0.92",
                "openedAt": "2026-11-03T14:34:11Z",
            }
        ],
    }
    row.update(overrides)
    return row


def _snap(positions: object, pnl: object = None) -> dict[str, object]:
    return {
        "id": 1411,
        "ts_utc": "2026-11-03T20:05:21Z",
        "positions_json": json.dumps(positions),
        "pnl_json": pnl,
    }


def test_sig_position_rows_parse_with_race_lookup() -> None:
    rows = [
        _sig_row("1001", "-1018"),
        _sig_row("1002", "5", avgCost="0.445", unrealizedPnl="-0.25"),
        _sig_row("1003", "0"),  # closed: skipped
        _sig_row("1004", "12", settled=True),  # settled: skipped
    ]
    lookup = {"1001": ("senate:WY", "R"), "1002": ("senate:IA", "D")}
    res = parse_positions_json(_snap(rows), exchange_lookup=lookup)

    assert res.status == "ok", res.message
    assert len(res.rows) == 2
    by_id = {r.sig_exchange_id: r for r in res.rows}
    no_row = by_id["1001"]
    assert (no_row.race_key, no_row.party, no_row.side) == ("senate:WY", "R", "NO")
    assert no_row.quantity == Decimal(1018)
    assert no_row.total_cost == Decimal("936.56")
    assert no_row.market_value == Decimal("946.74")
    assert by_id["1002"].side == "YES"
    assert by_id["1002"].unrealized_pnl == Decimal("-0.25")


def test_sig_position_row_without_lookup_keeps_exchange_id() -> None:
    res = parse_positions_json(_snap([_sig_row("2001", "-3")]))
    assert res.status == "ok"
    assert res.rows[0].race_key == "—"
    assert res.rows[0].sig_exchange_id == "2001"


def test_sig_position_row_with_unknown_key_is_still_bad_format() -> None:
    res = parse_positions_json(_snap([_sig_row("2001", "-3", surprise="x")]))
    assert res.status == "bad_format"
    assert "surprise" in res.message


def test_only_closed_sig_rows_is_empty_not_bad_format() -> None:
    res = parse_positions_json(_snap([_sig_row("2001", "0")]))
    assert res.status == "empty"


def test_unvalued_positions_payload_is_no_data() -> None:
    payload = {"unvalued": [{"exchange_id": "1", "market_id": "2"}]}
    res = parse_positions_json(_snap(payload))
    assert res.status == "no_data"
    assert "1 holding" in res.message


def test_null_pnl_json_is_no_data_not_bad_format() -> None:
    assert parse_pnl_json(_snap([], pnl=None)).status == "no_data"
    assert parse_pnl_json(_snap([], pnl="")).status == "no_data"


def test_derived_pnl_groups_unrealized_by_race() -> None:
    rows = [
        _sig_row("1001", "-10", unrealizedPnl="4.00"),
        _sig_row("1002", "-10", unrealizedPnl="-1.50"),
        _sig_row("1003", "-10", unrealizedPnl="2.00"),
    ]
    lookup = {"1001": ("senate:WY", "R"), "1002": ("senate:WY", "D")}
    positions = parse_positions_json(_snap(rows), exchange_lookup=lookup)
    derived = derive_pnl_from_positions(positions)

    assert derived is not None
    assert derived.source == "positions_unrealized"
    assert derived.total_pnl == Decimal("4.50")
    assert {r.race_key: r.pnl for r in derived.rows} == {
        "senate:WY": Decimal("2.50"),
        "#1003": Decimal("2.00"),
    }
    html = render_race_pnl_panel_html(derived)
    assert "unrealized, from SIG positions" in html


def test_derived_pnl_refuses_partial_data() -> None:
    rows = [_sig_row("1001", "-10"), _sig_row("1002", "-10", unrealizedPnl=None)]
    positions = parse_positions_json(_snap(rows))
    assert positions.status == "ok"
    assert derive_pnl_from_positions(positions) is None


def _live_like_db(tmp_path: Path) -> tuple[Path, str, str]:
    db_path = create_sample_state(mode="normal", target_dir=tmp_path, now_utc=FIXED_NOW)
    conn = sqlite3.connect(db_path)
    try:
        quote = conn.execute(
            "SELECT * FROM quotes_latest ORDER BY sig_exchange_id LIMIT 1"
        ).fetchone()
        cols = [d[0] for d in conn.execute("SELECT * FROM quotes_latest LIMIT 1").description]
        exchange_id = quote[cols.index("sig_exchange_id")]
        race_key = quote[cols.index("race_key")]

        positions = [_sig_row(str(exchange_id), "-1018", unrealizedPnl="10.18")]
        latest_id = conn.execute("SELECT max(id) FROM snapshots").fetchone()[0]
        conn.execute(
            "UPDATE snapshots SET positions_json = ?, pnl_json = '' WHERE id = ?",
            (json.dumps(positions), latest_id),
        )

        old = dict(zip(cols, quote, strict=True))
        old["sig_exchange_id"] = "1076"
        old["race_key"] = "senate:RI"
        old["ts_utc"] = (FIXED_NOW - timedelta(hours=26)).strftime("%Y-%m-%dT%H:%M:%SZ")
        placeholders = ", ".join("?" for _ in cols)
        conn.execute(
            f"INSERT INTO quotes_latest ({', '.join(cols)}) VALUES ({placeholders})",
            [old[c] for c in cols],
        )
        conn.commit()
    finally:
        conn.close()
    return db_path, str(exchange_id), str(race_key)


def test_live_like_database_has_no_bad_format_and_summarises_old_quotes(tmp_path: Path) -> None:
    db_path, exchange_id, race_key = _live_like_db(tmp_path)
    snap = load_monitor_snapshot(db_path=db_path, state_dir=tmp_path, now_utc=FIXED_NOW)

    assert snap.positions.status == "ok", snap.positions.message
    assert snap.positions.rows[0].race_key == race_key
    assert snap.race_pnl.source == "positions_unrealized"
    assert snap.hero.pnl_available
    assert "Unrealized" in snap.hero.pnl_sub

    items = build_needs_attention_items(snap)
    cats = [it.category for it in items]
    assert "bad_format" not in cats
    assert cats.count("quote_inactive") == 1
    assert not any("1076" in it.title for it in items if it.category == "quote_stale")
    inactive = next(it for it in items if it.category == "quote_inactive")
    assert "1076 (senate:RI)" in inactive.detail

    html = render_positions_panel_html(snap.positions)
    assert "UNREAL P&amp;L" in html
    assert exchange_id in html
