"""Unit and Streamlit AppTest suite for Tasks DASH-EW-1 and DASH-EW-2 (`dashboard/`)."""

from __future__ import annotations

import json
import sqlite3
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path

import pytest
from streamlit.testing.v1 import AppTest

from dashboard.app import (
    render_db_busy_banner_html,
    render_decisions_panel_html,
    render_events_panel_html,
    render_exposure_panel_html,
    render_heartbeat_banner_html,
    render_hero_row_html,
    render_orders_panel_html,
    render_positions_panel_html,
    render_quotes_panel_html,
    render_race_pnl_panel_html,
)
from dashboard.db import (
    CONTRACT_SCHEMA,
    HEARTBEAT_STALE_SECONDS,
    SQLITE_BUSY_TIMEOUT_MS,
    FilterCriteria,
    connect_readonly,
    default_repo_root,
    load_monitor_snapshot,
    parse_pnl_json,
    parse_positions_json,
)
from dashboard.sample_db import create_sample_state

FIXED_NOW = datetime(2026, 11, 3, 14, 30, 0, tzinfo=UTC)


def test_sample_db_refuses_real_bot_state_dir() -> None:
    """Sample DB generator must refuse to touch the real bot/state directory."""
    real_state = default_repo_root() / "bot" / "state"
    with pytest.raises(ValueError, match="Refusing to write sample database"):
        create_sample_state(mode="normal", target_dir=real_state)


def test_readonly_uri_blocks_writes(tmp_path: Path) -> None:
    """connect_readonly must open SQLite with mode=ro and reject any write."""
    db_path = create_sample_state(mode="normal", target_dir=tmp_path, now_utc=FIXED_NOW)
    conn = connect_readonly(db_path)
    try:
        with pytest.raises(sqlite3.OperationalError, match="readonly"):
            conn.execute("DELETE FROM events")
    finally:
        conn.close()


def test_state_1_normal_data(tmp_path: Path) -> None:
    """State 1 (normal data): ALIVE heartbeat, populated hero, 5 panels, positions, P&L & 6h history."""
    db_path = create_sample_state(mode="normal", target_dir=tmp_path, now_utc=FIXED_NOW)
    snap = load_monitor_snapshot(db_path=db_path, state_dir=tmp_path, now_utc=FIXED_NOW)

    assert snap.heartbeat.status == "ALIVE"
    assert snap.heartbeat.age_seconds == pytest.approx(5.0)
    assert snap.heartbeat.age_display == "5s"
    assert not snap.heartbeat.stop_file_exists
    assert not snap.heartbeat.paused_file_exists

    assert snap.hero.balance_available is True
    assert snap.hero.balance_display == "10,482.50 SUSQ"
    assert snap.hero.pnl_available is True
    assert snap.hero.pnl_display == "+482.50 SUSQ ▲"
    assert snap.hero.pnl_direction == "positive"
    assert snap.hero.open_orders_available is True
    assert snap.hero.open_orders_display == "3"
    assert snap.hero.exposure_available is True
    assert snap.hero.exposure_display == "1,845.00 SUSQ"
    assert snap.hero.last_event_available is True
    assert "IST" in snap.hero.last_event_display

    # Exposure sorted largest committed first
    committed_vals = [float(r["committed"]) for r in snap.exposure_table.rows]
    assert committed_vals == sorted(committed_vals, reverse=True)

    # DASH-EW-2: Positions, per-race P&L, and 6h snapshot history
    assert snap.positions.status == "ok"
    assert len(snap.positions.rows) == 5
    assert snap.race_pnl.status == "ok"
    assert snap.race_pnl.total_pnl == Decimal("482.50")
    assert snap.snapshot_history.status == "ok"
    assert len(snap.snapshot_history.points) == 12

    pos_html = render_positions_panel_html(snap.positions)
    assert "SEN-PA-2026" in pos_html
    assert "SIG-PA-SEN-D" in pos_html

    pnl_html = render_race_pnl_panel_html(snap.race_pnl)
    assert "+215.00 SUSQ ▲" in pnl_html
    assert "−35.00 SUSQ ▼" in pnl_html
    assert "+482.50 SUSQ ▲" in pnl_html

    # Panels render expected highlights
    hb_html = render_heartbeat_banner_html(snap.heartbeat, snap.fetched_at_ist, False)
    assert "ALIVE" in hb_html
    assert "AGE: 5s" in hb_html
    assert "STOP: CLEAR" in hb_html
    assert "PAUSED: CLEAR" in hb_html

    orders_html = render_orders_panel_html(snap.orders_table)
    assert "bw-row-open" in orders_html
    assert "OPEN • RESTING" in orders_html
    assert "OPEN • PARTIAL" in orders_html

    decisions_html = render_decisions_panel_html(snap.decisions_table)
    assert "venue disagreement" in decisions_html
    assert "sum of asks" in decisions_html

    quotes_html = render_quotes_panel_html(snap.quotes_table, FIXED_NOW)
    assert "STALE (&gt;60s)" in quotes_html
    assert "FRESH" in quotes_html

    events_html = render_events_panel_html(snap.events_table)
    assert "bw-badge-warn" in events_html
    assert "bw-badge-error" in events_html


def test_state_2_empty_tables(tmp_path: Path) -> None:
    """State 2 (empty tables): NO HEARTBEAT and explicit NO DATA without fabricated zeros."""
    db_path = create_sample_state(mode="empty", target_dir=tmp_path, now_utc=FIXED_NOW)
    snap = load_monitor_snapshot(db_path=db_path, state_dir=tmp_path, now_utc=FIXED_NOW)

    assert snap.heartbeat.status == "NO HEARTBEAT"
    assert snap.heartbeat.age_seconds is None
    assert snap.hero.balance_available is False
    assert snap.hero.balance_display == "NO DATA"
    assert snap.hero.pnl_available is False
    assert snap.hero.pnl_display == "NO DATA"
    assert snap.hero.open_orders_available is False
    assert snap.hero.open_orders_display == "NO DATA"
    assert snap.hero.exposure_available is False
    assert snap.hero.exposure_display == "NO DATA"
    assert snap.hero.last_event_available is False
    assert snap.hero.last_event_display == "NO DATA"

    assert snap.positions.status == "no_data"
    assert snap.race_pnl.status == "no_data"
    assert snap.snapshot_history.status == "empty"

    hero_html = render_hero_row_html(snap.hero)
    assert hero_html.count("NO DATA") >= 5
    assert "0.00 SUSQ" not in hero_html


def test_state_3_missing_file(tmp_path: Path) -> None:
    """State 3 (missing file): does not create DB file and returns NO HEARTBEAT / NO DATA."""
    db_path = create_sample_state(mode="missing", target_dir=tmp_path, now_utc=FIXED_NOW)
    assert not db_path.exists()

    snap = load_monitor_snapshot(db_path=db_path, state_dir=tmp_path, now_utc=FIXED_NOW)
    assert not db_path.exists(), "Read-only loader must never create a missing DB file"

    assert snap.heartbeat.status == "NO HEARTBEAT"
    assert snap.hero.balance_display == "NO DATA"
    assert snap.hero.pnl_display == "NO DATA"
    assert snap.positions.status == "no_data"
    assert snap.race_pnl.status == "no_data"
    assert snap.snapshot_history.status == "no_data"


def test_state_4_stale_heartbeat_and_flags(tmp_path: Path) -> None:
    """State 4 (stale heartbeat): STALE status (>=30s), STOP & PAUSED files reflected, negative P&L."""
    db_path = create_sample_state(mode="stale", target_dir=tmp_path, now_utc=FIXED_NOW)
    snap = load_monitor_snapshot(db_path=db_path, state_dir=tmp_path, now_utc=FIXED_NOW)

    assert snap.heartbeat.status == "STALE"
    assert snap.heartbeat.age_seconds == pytest.approx(95.0)
    assert snap.heartbeat.age_display == "95s"
    assert snap.heartbeat.stop_file_exists is True
    assert snap.heartbeat.paused_file_exists is True
    assert snap.heartbeat.db_paused is True
    assert snap.heartbeat.db_stop_seen is True

    assert snap.hero.pnl_direction == "negative"
    assert snap.hero.pnl_display == "−184.75 SUSQ ▼"

    hb_html = render_heartbeat_banner_html(snap.heartbeat, snap.fetched_at_ist, False)
    assert "STALE" in hb_html
    assert "AGE: 95s" in hb_html
    assert "STOP: ACTIVE" in hb_html
    assert "PAUSED: ACTIVE" in hb_html


def test_positions_and_pnl_json_malformed_and_unexpected_keys(tmp_path: Path) -> None:
    """DASH-EW-2: Missing, malformed, or unexpected-key JSON renders NO DATA / BAD FORMAT."""
    db_path = create_sample_state(
        mode="malformed_json", target_dir=tmp_path, now_utc=FIXED_NOW
    )
    snap = load_monitor_snapshot(db_path=db_path, state_dir=tmp_path, now_utc=FIXED_NOW)

    assert snap.positions.status == "bad_format"
    assert "NO DATA / BAD FORMAT" in snap.positions.message
    assert "unexpected_key" in snap.positions.message

    assert snap.race_pnl.status == "bad_format"
    assert "NO DATA / BAD FORMAT" in snap.race_pnl.message

    assert snap.hero.pnl_display == "NO DATA / BAD FORMAT"
    assert snap.hero.pnl_available is False

    pos_html = render_positions_panel_html(snap.positions)
    assert "NO DATA / BAD FORMAT" in pos_html
    pnl_html = render_race_pnl_panel_html(snap.race_pnl)
    assert "NO DATA / BAD FORMAT" in pnl_html

    # Also verify guessed summary keys like {"total": "100", "realized": "50"} are rejected
    guessed_pnl_row = {
        "id": 9,
        "ts_utc": "2026-11-03T14:29:00Z",
        "pnl_json": json.dumps({"total": "100.00", "realized": "50.00"}),
    }
    res_pnl = parse_pnl_json(guessed_pnl_row)
    assert res_pnl.status == "bad_format"
    assert "NO DATA / BAD FORMAT" in res_pnl.message

    # Verify missing / null JSON and primitive JSON are rejected
    assert parse_positions_json({"id": 1, "ts_utc": "2026-11-03T14:29:00Z", "positions_json": None}).status == "bad_format"
    assert parse_positions_json({"id": 1, "ts_utc": "2026-11-03T14:29:00Z", "positions_json": "123"}).status == "bad_format"
    assert parse_positions_json({"id": 1, "ts_utc": "2026-11-03T14:29:00Z", "positions_json": json.dumps({"foo": 10})}).status == "bad_format"


def test_single_row_snapshot_history(tmp_path: Path) -> None:
    """DASH-EW-2: snapshots table with only one row returns status='single_row' and 1 point."""
    db_path = create_sample_state(
        mode="single_snapshot", target_dir=tmp_path, now_utc=FIXED_NOW
    )
    snap = load_monitor_snapshot(db_path=db_path, state_dir=tmp_path, now_utc=FIXED_NOW)

    assert snap.snapshot_history.status == "single_row"
    assert snap.snapshot_history.has_data is True
    assert len(snap.snapshot_history.points) == 1
    pt = snap.snapshot_history.points[0]
    assert pt.balance == Decimal("10482.50")
    assert pt.committed == Decimal("1845.00")
    assert "IST" in pt.ts_ist_label


def test_filters_apply_to_orders_decisions_and_positions(tmp_path: Path) -> None:
    """DASH-EW-2: Filters (race_key, strategy, status/verdict) filter Orders, Decisions, and Positions."""
    db_path = create_sample_state(mode="normal", target_dir=tmp_path, now_utc=FIXED_NOW)
    snap = load_monitor_snapshot(db_path=db_path, state_dir=tmp_path, now_utc=FIXED_NOW)

    # 1. Filter by race_key = SEN-MI-2026
    c_race = FilterCriteria(race_key="SEN-MI-2026")
    ord_html = render_orders_panel_html(snap.orders_table, c_race)
    assert "SEN-MI-2026" in ord_html
    assert "SEN-PA-2026" not in ord_html

    dec_html = render_decisions_panel_html(snap.decisions_table, c_race)
    assert "SEN-MI-2026" in dec_html
    assert "SEN-PA-2026" not in dec_html

    pos_html = render_positions_panel_html(snap.positions, c_race)
    assert "SEN-MI-2026" in pos_html
    assert "SEN-PA-2026" not in pos_html

    pnl_html = render_race_pnl_panel_html(snap.race_pnl, c_race)
    assert "SEN-MI-2026" in pnl_html
    assert "SEN-PA-2026" not in pnl_html

    # 2. Filter by strategy = complement
    c_strat = FilterCriteria(strategy="complement")
    ord_comp = render_orders_panel_html(snap.orders_table, c_strat)
    assert "SEN-MI-2026" in ord_comp
    assert "GOV-PA-2026" not in ord_comp

    dec_comp = render_decisions_panel_html(snap.decisions_table, c_strat)
    assert "SEN-MI-2026" in dec_comp
    assert "SEN-OH-2026" not in dec_comp

    # 3. Filter by status/verdict = blocked (decision verdict) and resting (order status)
    c_blocked = FilterCriteria(status_verdict="blocked")
    dec_blocked = render_decisions_panel_html(snap.decisions_table, c_blocked)
    assert "SEN-OH-2026" in dec_blocked
    assert "SEN-PA-2026" not in dec_blocked

    c_resting = FilterCriteria(status_verdict="resting")
    ord_resting = render_orders_panel_html(snap.orders_table, c_resting)
    assert "SEN-PA-2026" in ord_resting
    assert "GOV-PA-2026" not in ord_resting


def test_missing_table_and_missing_column_resilience(tmp_path: Path) -> None:
    """Missing table or missing contract column returns explicit no-data without crashing."""
    db_path = tmp_path / "partial.db"
    conn = sqlite3.connect(str(db_path))
    try:
        conn.execute(
            "CREATE TABLE exposure (race_key TEXT PRIMARY KEY, ts_utc TEXT, committed TEXT)"
        )
        conn.commit()
    finally:
        conn.close()

    snap = load_monitor_snapshot(db_path=db_path, state_dir=tmp_path, now_utc=FIXED_NOW)
    assert snap.exposure_table.status == "missing_columns"
    assert "cap" in snap.exposure_table.missing_columns
    assert snap.orders_table.status == "missing_table"
    assert snap.heartbeat.status == "NO HEARTBEAT"

    exp_html = render_exposure_panel_html(snap.exposure_table)
    assert "NO DATA AVAILABLE" in exp_html
    assert "cap" in exp_html


@pytest.mark.parametrize(
    "mode",
    ["normal", "empty", "missing", "stale", "single_snapshot", "malformed_json"],
)
def test_streamlit_apptest_all_states_and_filter_persistence(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    mode: str,
) -> None:
    """Streamlit AppTest renders all 6 states cleanly and persists filters across reruns."""
    state_dir = tmp_path / mode
    state_dir.mkdir(parents=True, exist_ok=True)
    db_path = create_sample_state(mode=mode, target_dir=state_dir, now_utc=FIXED_NOW)

    monkeypatch.setenv("BELLWETHER_DB_PATH", str(db_path))
    monkeypatch.setenv("BELLWETHER_STATE_DIR", str(state_dir))

    app_file = default_repo_root() / "dashboard" / "app.py"
    at = AppTest.from_file(str(app_file), default_timeout=15)
    at.run()
    assert not at.exception, f"AppTest raised exception in mode={mode}: {at.exception}"

    # Verify default filter state is "All"
    assert at.session_state["filter_race_key"] == "All"
    assert at.session_state["filter_strategy"] == "All"
    assert at.session_state["filter_status_verdict"] == "All"

    # Set a filter in session_state and verify it persists across auto-refresh rerun
    if mode == "normal":
        at.sidebar.selectbox(key="filter_race_key").select("SEN-PA-2026").run()
        assert not at.exception
        assert at.session_state["filter_race_key"] == "SEN-PA-2026"
        joined = "\n".join(m.value for m in at.markdown)
        assert "FILTERS: race=SEN-PA-2026" in joined

    # Verify pausing auto-refresh when confirm_open is True
    at.session_state["confirm_open"] = True
    at.run()
    assert not at.exception
    markdown_joined = "\n".join(m.value for m in at.markdown)
    assert "REFRESH: PAUSED (CONFIRM OPEN)" in markdown_joined
    assert "BELLWETHER EW MONITOR" in markdown_joined


def test_contract_schema_matches_all_seven_tables() -> None:
    """Verify CONTRACT_SCHEMA covers all 7 tables from bot/CONTRACT.md."""
    assert set(CONTRACT_SCHEMA.keys()) == {
        "heartbeat",
        "orders",
        "decisions",
        "quotes_latest",
        "exposure",
        "snapshots",
        "events",
    }


def test_readonly_busy_timeout_and_db_busy_banner(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """connect_readonly sets busy_timeout and locked DB renders DB BUSY banner without crashing."""
    db_path = create_sample_state(mode="normal", target_dir=tmp_path, now_utc=FIXED_NOW)
    conn = connect_readonly(db_path)
    try:
        timeout_row = conn.execute("PRAGMA busy_timeout").fetchone()
        assert timeout_row is not None
        assert int(timeout_row[0]) == SQLITE_BUSY_TIMEOUT_MS
    finally:
        conn.close()

    def _raise_locked(*_args: object, **_kwargs: object) -> sqlite3.Connection:
        raise sqlite3.OperationalError("database is locked")

    monkeypatch.setattr("dashboard.db.connect_readonly", _raise_locked)
    snap = load_monitor_snapshot(db_path=db_path, state_dir=tmp_path, now_utc=FIXED_NOW)
    assert snap.db_busy is True
    assert snap.heartbeat.status == "DB BUSY"
    busy_html = render_db_busy_banner_html(snap)
    assert "DB BUSY" in busy_html
    assert "database is locked" in busy_html
    hb_html = render_heartbeat_banner_html(snap.heartbeat, snap.fetched_at_ist, False)
    assert "DB BUSY" in hb_html


def test_heartbeat_stale_threshold_constant() -> None:
    """HEARTBEAT_STALE_SECONDS is the single constant (30.0s) governing heartbeat staleness."""
    assert HEARTBEAT_STALE_SECONDS == 30.0
