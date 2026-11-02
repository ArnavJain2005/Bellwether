"""Unit and Streamlit AppTest suite for Task DASH-EW-1 (`dashboard/`)."""

from __future__ import annotations

import sqlite3
from datetime import UTC, datetime
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
    render_quotes_panel_html,
)
from dashboard.db import (
    CONTRACT_SCHEMA,
    HEARTBEAT_STALE_SECONDS,
    SQLITE_BUSY_TIMEOUT_MS,
    connect_readonly,
    default_repo_root,
    load_monitor_snapshot,
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
    """State 1 (normal data): ALIVE heartbeat, populated hero & 5 panels."""
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

    for table_res in (
        snap.heartbeat_table,
        snap.snapshots_table,
        snap.exposure_table,
        snap.orders_table,
        snap.decisions_table,
        snap.quotes_table,
        snap.events_table,
    ):
        assert table_res.status == "empty"
        assert not table_res.has_data

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
    assert snap.hero.open_orders_display == "NO DATA"
    assert snap.hero.exposure_display == "NO DATA"
    assert snap.hero.last_event_display == "NO DATA"

    for table_res in (
        snap.heartbeat_table,
        snap.snapshots_table,
        snap.exposure_table,
        snap.orders_table,
        snap.decisions_table,
        snap.quotes_table,
        snap.events_table,
    ):
        assert table_res.status == "missing_file"


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


def test_missing_table_and_missing_column_resilience(tmp_path: Path) -> None:
    """Missing table or missing contract column returns explicit no-data without crashing."""
    db_path = tmp_path / "partial.db"
    conn = sqlite3.connect(str(db_path))
    try:
        # Create exposure with missing 'cap' column, and omit all other tables
        conn.execute("CREATE TABLE exposure (race_key TEXT PRIMARY KEY, ts_utc TEXT, committed TEXT)")
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


@pytest.mark.parametrize("mode", ["normal", "empty", "missing", "stale"])
def test_streamlit_apptest_all_four_states(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    mode: str,
) -> None:
    """Streamlit AppTest renders all 4 states cleanly without exceptions."""
    state_dir = tmp_path / mode
    state_dir.mkdir(parents=True, exist_ok=True)
    db_path = create_sample_state(mode=mode, target_dir=state_dir, now_utc=FIXED_NOW)

    monkeypatch.setenv("BELLWETHER_DB_PATH", str(db_path))
    monkeypatch.setenv("BELLWETHER_STATE_DIR", str(state_dir))

    app_file = default_repo_root() / "dashboard" / "app.py"
    at = AppTest.from_file(str(app_file), default_timeout=15)
    at.run()
    assert not at.exception, f"AppTest raised exception in mode={mode}: {at.exception}"

    # Verify pausing auto-refresh when confirm_open is True
    at.session_state["confirm_open"] = True
    at.run()
    assert not at.exception
    markdown_Joined = "\n".join(m.value for m in at.markdown)
    assert "REFRESH: PAUSED (CONFIRM OPEN)" in markdown_Joined
    assert "BELLWETHER EW MONITOR" in markdown_Joined


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

    # Simulate SQLite lock contention on connection open
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
