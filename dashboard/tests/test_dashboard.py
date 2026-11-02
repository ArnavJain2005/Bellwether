"""Unit and Streamlit AppTest suite for Tasks DASH-EW-1, DASH-EW-2, and DASH-EW-3 (`dashboard/`)."""

from __future__ import annotations

import ast
import json
import os
import sqlite3
import stat
from datetime import UTC, datetime, timedelta
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
    render_stop_confirmation_html,
    render_stop_result_banner_html,
)
from dashboard.db import (
    CONTRACT_SCHEMA,
    HEARTBEAT_STALE_SECONDS,
    SQLITE_BUSY_TIMEOUT_MS,
    STOP_FILENAME,
    FilterCriteria,
    connect_readonly,
    default_repo_root,
    load_monitor_snapshot,
    parse_pnl_json,
    parse_positions_json,
    write_stop_file,
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


def test_stop_button_confirm_then_write_creates_file(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """DASH-EW-3: Clicking STOP BOT opens confirmation (no write), survives refresh, and Confirm creates STOP."""
    state_dir = tmp_path / "stop_confirm_flow"
    state_dir.mkdir(parents=True, exist_ok=True)
    db_path = create_sample_state(mode="normal", target_dir=state_dir, now_utc=FIXED_NOW)
    stop_path = state_dir / STOP_FILENAME
    assert not stop_path.exists()

    monkeypatch.setenv("BELLWETHER_DB_PATH", str(db_path))
    monkeypatch.setenv("BELLWETHER_STATE_DIR", str(state_dir))

    app_file = default_repo_root() / "dashboard" / "app.py"
    at = AppTest.from_file(str(app_file), default_timeout=15)
    at.run()
    assert not at.exception

    stop_btn = at.button(key="stop_bot_btn")
    assert stop_btn.label == "STOP BOT"
    assert stop_btn.disabled is False
    assert not stop_path.exists()

    # Step 1: Click STOP BOT -> opens confirmation, pauses refresh, writes nothing
    stop_btn.click().run()
    assert not at.exception
    assert not stop_path.exists(), "Clicking STOP BOT must not create STOP before Confirm"
    assert at.session_state["confirm_open"] is True
    md_open = "\n".join(m.value for m in at.markdown)
    assert "This creates" in md_open and "bot/state/STOP" in md_open and "halts trading. Confirm?" in md_open
    assert "REFRESH: PAUSED (CONFIRM OPEN)" in md_open

    # Step 2: Survival across refresh without premature write
    at.run()
    assert not at.exception
    assert at.session_state["confirm_open"] is True
    assert not stop_path.exists()

    # Step 3: Click Confirm -> atomically creates STOP with UTC timestamp + "dashboard"
    at.button(key="confirm_stop_btn").click().run()
    assert not at.exception
    assert at.session_state["confirm_open"] is False
    assert stop_path.is_file(), "Confirm must create bot/state/STOP in the target state directory"

    file_content = stop_path.read_text(encoding="utf-8")
    assert file_content.endswith("Z dashboard\n")

    md_after = "\n".join(m.value for m in at.markdown)
    assert "STOP FILE PRESENT" in md_after
    assert str(stop_path) in md_after
    assert "IST" in md_after

    stop_btn_after = at.button(key="stop_bot_btn")
    assert stop_btn_after.label == "STOP ACTIVE"
    assert stop_btn_after.disabled is True

    # Step 4: Subsequent refresh does not double-submit or overwrite STOP
    mtime_first = stop_path.stat().st_mtime_ns
    content_first = stop_path.read_text(encoding="utf-8")
    at.run()
    assert not at.exception
    assert stop_path.read_text(encoding="utf-8") == content_first
    assert stop_path.stat().st_mtime_ns == mtime_first


def test_stop_button_cancel_writes_nothing(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """DASH-EW-3: Clicking STOP BOT then Cancel closes confirmation and writes nothing."""
    state_dir = tmp_path / "stop_cancel_flow"
    state_dir.mkdir(parents=True, exist_ok=True)
    db_path = create_sample_state(mode="normal", target_dir=state_dir, now_utc=FIXED_NOW)
    stop_path = state_dir / STOP_FILENAME

    monkeypatch.setenv("BELLWETHER_DB_PATH", str(db_path))
    monkeypatch.setenv("BELLWETHER_STATE_DIR", str(state_dir))

    app_file = default_repo_root() / "dashboard" / "app.py"
    at = AppTest.from_file(str(app_file), default_timeout=15)
    at.run()
    assert not at.exception

    at.button(key="stop_bot_btn").click().run()
    assert at.session_state["confirm_open"] is True
    assert not stop_path.exists()

    at.button(key="cancel_stop_btn").click().run()
    assert not at.exception
    assert at.session_state["confirm_open"] is False
    assert not stop_path.exists(), "Cancel must never create bot/state/STOP"
    assert at.button(key="stop_bot_btn").label == "STOP BOT"
    assert at.button(key="stop_bot_btn").disabled is False


def test_stop_already_present_means_no_second_write(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """DASH-EW-3: If STOP already exists, write_stop_file and UI never write a second time."""
    state_dir = tmp_path / "stop_existing"
    state_dir.mkdir(parents=True, exist_ok=True)
    db_path = create_sample_state(mode="normal", target_dir=state_dir, now_utc=FIXED_NOW)
    stop_path = state_dir / STOP_FILENAME

    # First write creates STOP
    res1 = write_stop_file(state_dir=state_dir, now_utc=FIXED_NOW)
    assert res1.ok is True
    assert res1.already_existed is False
    assert stop_path.is_file()
    original_content = stop_path.read_text(encoding="utf-8")
    assert original_content == "2026-11-03T14:30:00Z dashboard\n"

    # Second write attempt must not overwrite or touch existing STOP
    later_now = FIXED_NOW + timedelta(minutes=5)
    res2 = write_stop_file(state_dir=state_dir, now_utc=later_now)
    assert res2.ok is False
    assert res2.already_existed is True
    assert res2.error_kind == "already_exists"
    assert "STOP ALREADY PRESENT" in res2.message
    assert stop_path.read_text(encoding="utf-8") == original_content

    # Also verify UI shows disabled STOP ACTIVE when STOP already exists
    monkeypatch.setenv("BELLWETHER_DB_PATH", str(db_path))
    monkeypatch.setenv("BELLWETHER_STATE_DIR", str(state_dir))
    app_file = default_repo_root() / "dashboard" / "app.py"
    at = AppTest.from_file(str(app_file), default_timeout=15)
    at.run()
    assert not at.exception
    assert at.button(key="stop_bot_btn").label == "STOP ACTIVE"
    assert at.button(key="stop_bot_btn").disabled is True
    assert "STOP FILE PRESENT" in "\n".join(m.value for m in at.markdown)


def test_stop_readonly_folder_shows_failure_and_no_success(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """DASH-EW-3: Read-only folder fails cleanly with exact error and never claims success."""
    ro_dir = tmp_path / "readonly_state"
    ro_dir.mkdir(parents=True, exist_ok=True)
    db_path = create_sample_state(mode="normal", target_dir=ro_dir, now_utc=FIXED_NOW)
    stop_path = ro_dir / STOP_FILENAME

    os.chmod(ro_dir, stat.S_IREAD)
    try:
        res = write_stop_file(state_dir=ro_dir, now_utc=FIXED_NOW)
        assert res.ok is False
        assert res.already_existed is False
        assert res.error_kind == "permission_denied"
        assert "STOP WRITE FAILED" in res.message
        assert "permission denied" in res.message.lower()
        assert not stop_path.exists()

        banner_html = render_stop_result_banner_html(res)
        assert "STOP WRITE FAILED" in banner_html
        assert "STOP CREATED" not in banner_html
        assert "STOP FILE PRESENT" not in banner_html

        # Also test full AppTest flow against read-only folder
        monkeypatch.setenv("BELLWETHER_DB_PATH", str(db_path))
        monkeypatch.setenv("BELLWETHER_STATE_DIR", str(ro_dir))
        app_file = default_repo_root() / "dashboard" / "app.py"
        at = AppTest.from_file(str(app_file), default_timeout=15)
        at.run()
        at.button(key="stop_bot_btn").click().run()
        at.button(key="confirm_stop_btn").click().run()
        assert not at.exception
        assert not stop_path.exists()
        md_err = "\n".join(m.value for m in at.markdown)
        assert "STOP WRITE FAILED (permission denied)" in md_err
        assert "STOP CREATED" not in md_err
    finally:
        os.chmod(ro_dir, stat.S_IWRITE | stat.S_IREAD)


def test_stop_missing_folder_shows_failure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """DASH-EW-3: Missing state folder fails cleanly without creating directories or claiming success."""
    missing_dir = tmp_path / "nonexistent_state_folder"
    stop_path = missing_dir / STOP_FILENAME
    assert not missing_dir.exists()

    res = write_stop_file(state_dir=missing_dir, now_utc=FIXED_NOW)
    assert res.ok is False
    assert res.already_existed is False
    assert res.error_kind == "folder_missing"
    assert "STOP WRITE FAILED (folder missing)" in res.message
    assert not missing_dir.exists()
    assert not stop_path.exists()

    # Also test AppTest flow when state folder is missing
    monkeypatch.setenv("BELLWETHER_DB_PATH", str(missing_dir / "bellwether.db"))
    monkeypatch.setenv("BELLWETHER_STATE_DIR", str(missing_dir))
    app_file = default_repo_root() / "dashboard" / "app.py"
    at = AppTest.from_file(str(app_file), default_timeout=15)
    at.run()
    at.button(key="stop_bot_btn").click().run()
    at.button(key="confirm_stop_btn").click().run()
    assert not at.exception
    assert not missing_dir.exists()
    md_err = "\n".join(m.value for m in at.markdown)
    assert "STOP WRITE FAILED (folder missing)" in md_err
    assert "STOP CREATED" not in md_err


def _scan_source_for_forbidden_io(
    source_code: str,
    rel_file: str,
    allowed_writer: tuple[str, str] = ("dashboard/db.py", "write_stop_file"),
) -> list[str]:
    """Return any forbidden write, open-in-write-mode, or delete calls found in ``source_code``."""
    tree = ast.parse(source_code, filename=rel_file)
    violations: list[str] = []

    write_method_names = {"write", "writelines", "write_text", "write_bytes", "mkdir", "makedirs"}
    delete_method_names = {
        "unlink",
        "rmdir",
        "remove",
        "removedirs",
        "rmtree",
        "move",
        "rename",
        "replace",
    }

    def _visit(node: ast.AST, current_func: str | None) -> None:
        next_func = current_func
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            next_func = node.name

        if isinstance(node, ast.Call):
            is_allowed_writer = (rel_file, current_func or "") == allowed_writer
            func_name = ""
            is_method = False
            if isinstance(node.func, ast.Name):
                func_name = node.func.id
            elif isinstance(node.func, ast.Attribute):
                func_name = node.func.attr
                is_method = True

            # 1. Delete / remove / rename calls are forbidden EVERYWHERE in dashboard/ (even in write_stop_file)
            if func_name in delete_method_names:
                violations.append(
                    f"{rel_file}:{node.lineno} ({current_func}): forbidden delete/modify call '{func_name}'"
                )

            # 2. Write calls outside the single STOP writer function
            if func_name in write_method_names and not is_allowed_writer:
                violations.append(
                    f"{rel_file}:{node.lineno} ({current_func}): forbidden write call '{func_name}' outside {allowed_writer}"
                )

            # 3. Any open() or .open() call
            if func_name == "open":
                mode_arg: str | None = None
                if not is_method and len(node.args) >= 2 and isinstance(node.args[1], ast.Constant):
                    mode_arg = str(node.args[1].value)
                elif is_method and len(node.args) >= 1 and isinstance(node.args[0], ast.Constant):
                    mode_arg = str(node.args[0].value)
                for kw in node.keywords:
                    if kw.arg == "mode" and isinstance(kw.value, ast.Constant):
                        mode_arg = str(kw.value.value)

                if not is_allowed_writer:
                    if mode_arg is None or any(ch in mode_arg for ch in ("w", "a", "x", "+")):
                        violations.append(
                            f"{rel_file}:{node.lineno} ({current_func}): forbidden open(mode={mode_arg!r}) outside {allowed_writer}"
                        )
                else:
                    # Inside write_stop_file: only atomic exclusive creation ("x") is allowed, never overwrite ("w", "a", "+")
                    if mode_arg != "x":
                        violations.append(
                            f"{rel_file}:{node.lineno} ({current_func}): STOP writer must use atomic mode='x', found {mode_arg!r}"
                        )

        for child in ast.iter_child_nodes(node):
            _visit(child, next_func)

    _visit(tree, None)
    return violations


def test_static_no_other_writes_or_deletes_in_dashboard() -> None:
    """DASH-EW-3: Fail if any write call, open in write mode, or delete call appears in dashboard/ outside write_stop_file."""
    dashboard_dir = default_repo_root() / "dashboard"
    py_files = sorted(
        p for p in dashboard_dir.rglob("*.py") if "tests" not in p.parts
    )
    assert len(py_files) >= 3

    all_violations: list[str] = []
    for path in py_files:
        rel_path = path.relative_to(default_repo_root()).as_posix()
        source = path.read_text(encoding="utf-8")
        all_violations.extend(_scan_source_for_forbidden_io(source, rel_path))

    assert all_violations == [], f"Forbidden I/O calls found in dashboard/: {all_violations}"

    # Self-verify that the static scanner catches write, open-in-write-mode, and delete calls
    assert _scan_source_for_forbidden_io("def f(p):\n    p.write_text('x')\n", "dashboard/app.py")
    assert _scan_source_for_forbidden_io("def f(p):\n    open(p, 'w')\n", "dashboard/app.py")
    assert _scan_source_for_forbidden_io("def f(p):\n    p.unlink()\n", "dashboard/db.py")
    assert _scan_source_for_forbidden_io(
        "def write_stop_file(p):\n    p.unlink()\n", "dashboard/db.py"
    )
    assert _scan_source_for_forbidden_io(
        "def write_stop_file(p):\n    p.open('w')\n", "dashboard/db.py"
    )
    assert not _scan_source_for_forbidden_io(
        "def write_stop_file(p):\n    with p.open('x') as fh:\n        fh.write('ok')\n",
        "dashboard/db.py",
    )
    assert render_stop_confirmation_html()


# ---------------------------------------------------------------------------
# DASH-UI-SIMPLIFY (UI v2) parity, Needs Attention, STOP flow & safety nets
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "mode",
    ["normal", "empty", "missing", "stale", "single_snapshot", "malformed_json", "db_busy"],
)
def test_v2_feature_parity_across_all_states(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, mode: str
) -> None:
    """DASH-UI-SIMPLIFY: Parity test across all 7 states asserting every checklist item appears in v2."""
    from dataclasses import replace as dc_replace

    from dashboard.app import (
        render_hero_row_v2_html,
        render_needs_attention_html,
        render_status_strip_v2_html,
        render_ui_footer_html,
    )

    state_dir = tmp_path / f"v2_parity_{mode}"
    sample_mode = "normal" if mode == "db_busy" else mode
    db_path = create_sample_state(
        mode=sample_mode,  # type: ignore[arg-type]
        target_dir=state_dir,
        now_utc=FIXED_NOW,
    )

    snap = load_monitor_snapshot(db_path=db_path, state_dir=state_dir, now_utc=FIXED_NOW)
    if mode == "db_busy":
        snap = dc_replace(
            snap,
            db_busy=True,
            db_busy_message="database is locked (SQLITE_BUSY after 2500ms timeout)",
        )

    strip_html = render_status_strip_v2_html(snap, refresh_paused=False)
    hero_html = render_hero_row_v2_html(snap)
    attn_html = render_needs_attention_html(snap)
    footer_html = render_ui_footer_html("v2")

    # 1. Status strip parity items
    assert "bw-v2-status-strip" in strip_html
    assert "Last event:" in strip_html
    assert "MODE:" in strip_html
    assert "STOP" in strip_html
    assert "PAUSED:" in strip_html
    assert "ELECTION NIGHT:" in strip_html
    if mode == "db_busy":
        assert "DB BUSY" in strip_html
    if mode == "stale":
        assert "STOPPED" in strip_html
        assert "STOP FILE PRESENT" in strip_html
        assert "PAUSED: ACTIVE" in strip_html

    # 2. Four big numbers only + thin exposure bar (when exposure available)
    assert "bw-v2-hero-grid" in hero_html
    assert ">BALANCE<" in hero_html
    assert ">NET P&amp;L<" in hero_html
    assert ">OPEN ORDERS<" in hero_html
    assert ">EXPOSURE (USED / CAP)<" in hero_html
    assert "LAST EVENT TIME" not in hero_html  # moved into status strip
    if snap.hero.exposure_available:
        assert "bw-v2-exp-bar-track" in hero_html

    # 3. Needs attention box + UI v2 footer
    assert 'data-testid="needs-attention-box"' in attn_html
    assert "UI v2" in footer_html

    # 4. Full Streamlit AppTest render in v2 mode for each state
    if mode != "db_busy":
        monkeypatch.setenv("BELLWETHER_DB_PATH", str(db_path))
        monkeypatch.setenv("BELLWETHER_STATE_DIR", str(state_dir))
        monkeypatch.delenv("BW_UI", raising=False)

        app_file = Path(__file__).resolve().parents[1] / "app.py"
        at = AppTest.from_file(str(app_file), default_timeout=30)
        at.run(timeout=30)
        assert not at.exception

        tab_labels = [t.label for t in at.tabs]
        assert tab_labels == [
            "Orders & Decisions",
            "Quotes & Exposure",
            "Positions & History",
            "Events",
        ]

        all_md = "\n".join(m.value for m in at.markdown)
        assert "bw-v2-status-strip" in all_md
        assert "bw-v2-hero-grid" in all_md
        assert 'data-testid="needs-attention-box"' in all_md
        assert "UI v2" in all_md
        assert "ORDERS" in all_md
        assert "DECISIONS" in all_md
        assert "QUOTES" in all_md
        assert "EXPOSURE" in all_md
        assert "POSITIONS" in all_md
        assert 'data-testid="panel-race-pnl"' in all_md
        assert "SNAPSHOT HISTORY" in all_md
        assert "EVENTS" in all_md

        # Sidebar controls: 3 filters + Show all columns toggle
        assert at.sidebar.selectbox(key="filter_race_key").value == "All"
        assert at.sidebar.selectbox(key="filter_strategy").value == "All"
        assert at.sidebar.selectbox(key="filter_status_verdict").value == "All"
        show_all_toggle = at.sidebar.toggle(key="show_all_columns")
        assert show_all_toggle.value is False

        # Toggle Show all columns and verify raw IDs / venue tokens appear on normal data
        if mode == "normal":
            assert "mkt-pa-sen" not in all_md
            assert "sig-992014" not in all_md
            show_all_toggle.set_value(True).run(timeout=30)
            assert not at.exception
            expanded_md = "\n".join(m.value for m in at.markdown)
            assert "mkt-pa-sen" in expanded_md
            assert "sig-992014" in expanded_md
    else:
        busy_banner = render_db_busy_banner_html(snap)
        assert "DB BUSY" in busy_banner


def test_v2_needs_attention_lists_problems_and_shows_all_clear(tmp_path: Path) -> None:
    """DASH-UI-SIMPLIFY: Needs attention box lists stale heartbeat, stale quote, and error event; shows All clear on healthy data."""
    from dataclasses import replace as dc_replace

    from dashboard.app import build_needs_attention_items, render_needs_attention_html

    # 1. Sample 'normal' state has 1 stale quote (78s > 60s) and recent warn/error events
    normal_dir = tmp_path / "attn_normal"
    normal_db = create_sample_state(mode="normal", target_dir=normal_dir, now_utc=FIXED_NOW)
    normal_snap = load_monitor_snapshot(db_path=normal_db, state_dir=normal_dir, now_utc=FIXED_NOW)

    normal_items = build_needs_attention_items(normal_snap)
    categories = [it.category for it in normal_items]
    assert "quote_stale" in categories
    assert "event" in categories
    normal_html = render_needs_attention_html(normal_snap)
    assert "NEEDS ATTENTION" in normal_html
    assert "SIG-AZ-SEN-D" in normal_html

    # 2. Stale heartbeat + STOP/PAUSED + DB BUSY + >90% exposure cap + bad JSON format
    stale_dir = tmp_path / "attn_stale"
    stale_db = create_sample_state(mode="stale", target_dir=stale_dir, now_utc=FIXED_NOW)
    stale_snap = load_monitor_snapshot(db_path=stale_db, state_dir=stale_dir, now_utc=FIXED_NOW)
    high_exp_rows = [
        {"race_key": "SEN-PA-2026", "committed": "950.00", "cap": "1000.00", "updated_at_utc": FIXED_NOW.isoformat()}
    ]
    busy_stale_snap = dc_replace(
        stale_snap,
        db_busy=True,
        db_busy_message="database is locked",
        exposure_table=dc_replace(
            stale_snap.exposure_table,
            status="ok",
            rows=high_exp_rows,
            message="1 row(s)",
        ),
    )
    stale_items = build_needs_attention_items(busy_stale_snap)
    stale_cats = {it.category for it in stale_items}
    assert "heartbeat" in stale_cats
    assert "stop" in stale_cats
    assert "paused" in stale_cats
    assert "db_busy" in stale_cats
    assert "exposure_cap" in stale_cats

    # 3. Healthy snapshot -> "All clear"
    fresh_quotes = [
        {**r, "ts_utc": (FIXED_NOW - timedelta(seconds=5)).isoformat()}
        for r in normal_snap.quotes_table.rows
    ]
    info_events = [
        r for r in normal_snap.events_table.rows if str(r.get("level")).lower() == "info"
    ]
    healthy_snap = dc_replace(
        normal_snap,
        quotes_table=dc_replace(normal_snap.quotes_table, rows=fresh_quotes),
        events_table=dc_replace(normal_snap.events_table, rows=info_events),
    )
    healthy_items = build_needs_attention_items(healthy_snap)
    assert healthy_items == []
    healthy_html = render_needs_attention_html(healthy_snap)
    assert "All clear" in healthy_html
    assert "bw-attention-clear" in healthy_html


def test_v2_stop_flow_and_safety_nets(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """DASH-UI-SIMPLIFY: Verify STOP flow in v2 (Confirm/Cancel/read-only failure), BW_UI=legacy fallback, and UI error boundary."""
    from dashboard.app import render_monitor_body, render_ui_error_banner_html, resolve_ui_mode

    state_dir = tmp_path / "v2_stop_state"
    db_path = create_sample_state(mode="normal", target_dir=state_dir, now_utc=FIXED_NOW)
    stop_file = state_dir / STOP_FILENAME

    monkeypatch.setenv("BELLWETHER_DB_PATH", str(db_path))
    monkeypatch.setenv("BELLWETHER_STATE_DIR", str(state_dir))
    monkeypatch.delenv("BW_UI", raising=False)

    app_file = Path(__file__).resolve().parents[1] / "app.py"
    at = AppTest.from_file(str(app_file), default_timeout=30)
    at.run(timeout=30)
    assert not at.exception
    assert not stop_file.exists()

    # 1. Click STOP BOT -> opens confirmation, writes nothing yet
    stop_btns = [b for b in at.button if b.label == "STOP BOT"]
    assert len(stop_btns) == 1
    stop_btns[0].click().run(timeout=30)
    assert not stop_file.exists()
    assert bool(at.session_state["confirm_open"]) is True
    md_open = "\n".join(m.value for m in at.markdown)
    assert "This creates" in md_open and "bot/state/STOP" in md_open and "halts trading. Confirm?" in md_open

    # 2. Click Cancel -> closes confirmation, writes nothing
    cancel_btns = [b for b in at.button if b.label == "Cancel"]
    assert len(cancel_btns) == 1
    cancel_btns[0].click().run(timeout=30)
    assert not stop_file.exists()
    assert bool(at.session_state["confirm_open"]) is False

    # 3. Click STOP BOT -> Confirm -> writes bot/state/STOP once and shows STOP ACTIVE
    stop_btns = [b for b in at.button if b.label == "STOP BOT"]
    stop_btns[0].click().run(timeout=30)
    confirm_btns = [b for b in at.button if b.label == "Confirm"]
    assert len(confirm_btns) == 1
    confirm_btns[0].click().run(timeout=30)
    assert stop_file.exists()
    assert "dashboard" in stop_file.read_text(encoding="utf-8")
    active_btns = [b for b in at.button if b.label == "STOP ACTIVE"]
    assert len(active_btns) == 1
    assert active_btns[0].disabled is True

    # 4. Read-only folder failure in temp dir
    ro_dir = tmp_path / "v2_ro_dir"
    ro_dir.mkdir(parents=True, exist_ok=True)
    ro_dir.chmod(stat.S_IREAD | stat.S_IXUSR)
    try:
        res = write_stop_file(state_dir=ro_dir, now_utc=FIXED_NOW)
        assert res.ok is False
        assert not (ro_dir / STOP_FILENAME).exists()
    finally:
        ro_dir.chmod(stat.S_IWRITE | stat.S_IREAD | stat.S_IXUSR)

    # 5. Legacy mode fallback via BW_UI=legacy
    monkeypatch.setenv("BW_UI", "legacy")
    assert resolve_ui_mode() == "legacy"
    at_legacy = AppTest.from_file(str(app_file), default_timeout=30)
    at_legacy.run(timeout=30)
    assert not at_legacy.exception
    legacy_tabs = [t.label for t in at_legacy.tabs]
    assert legacy_tabs == [
        "LIVE MONITOR",
        "POSITIONS, P&L & HISTORY (6H)",
    ]
    legacy_md = "\n".join(m.value for m in at_legacy.markdown)
    assert "UI legacy" in legacy_md

    # 6. Top-level error boundary in v2 mode renders 'UI ERROR, switch to legacy'
    err_html = render_ui_error_banner_html(RuntimeError("synthetic v2 failure"))
    assert "UI ERROR, switch to legacy" in err_html
    assert "?ui=legacy" in err_html
    assert "synthetic v2 failure" in err_html
    assert callable(render_monitor_body)

