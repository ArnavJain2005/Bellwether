"""Bellwether Election-Week Read-Only Monitor (Task DASH-EW-1).

Streamlit single-screen monitor that reads ``bot/state/bellwether.db`` strictly
in read-only mode (`mode=ro`) following ``bot/CONTRACT.md`` and ``AGENTS.md``.
"""

from __future__ import annotations

import html
import sys
from datetime import datetime
from decimal import Decimal
from pathlib import Path
from typing import Any

import streamlit as st

_REPO_ROOT = Path(__file__).resolve().parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from dashboard.db import (
    OPEN_ORDER_STATUSES,
    QUOTE_STALE_SECONDS,
    HeartbeatState,
    HeroMetrics,
    MonitorSnapshot,
    TableResult,
    compute_age_seconds,
    format_edge,
    format_ist_timestamp,
    format_susq,
    load_monitor_snapshot,
    parse_decimal,
)

AUTO_REFRESH_SECONDS = 3


def _esc(value: Any) -> str:
    """HTML-escape a value, rendering None/empty as an em-dash."""
    if value is None:
        return "—"
    text = str(value).strip()
    return html.escape(text) if text else "—"


def _load_css() -> str:
    css_path = Path(__file__).resolve().parent / "style.css"
    if css_path.is_file():
        return css_path.read_text(encoding="utf-8")
    return ""


def render_heartbeat_banner_html(
    hb: HeartbeatState,
    fetched_ist: str,
    refresh_paused: bool,
) -> str:
    """Build the top-of-page Heartbeat Banner HTML."""
    status_slug = hb.status.lower().replace(" ", "-")
    age_text = f"AGE: {hb.age_display}"

    if hb.stop_file_exists or hb.db_stop_seen:
        stop_label = (
            "STOP: FILE PRESENT"
            if hb.stop_file_exists and not hb.db_stop_seen
            else "STOP: ACTIVE (FILE + DB)"
            if hb.stop_file_exists and hb.db_stop_seen
            else "STOP: DB SEEN"
        )
        stop_cls = "bw-flag-pill bw-flag-active-stop"
    else:
        stop_cls = "bw-flag-pill bw-flag-clear"
        stop_label = "STOP: CLEAR"

    pause_reasons = [
        r for r in (hb.paused_file_reason, hb.db_pause_reason) if r
    ]
    pause_suffix = f" ({pause_reasons[0]})" if pause_reasons else ""
    if hb.paused_file_exists or hb.db_paused:
        paused_cls = "bw-flag-pill bw-flag-active-paused"
        paused_label = f"PAUSED: ACTIVE{pause_suffix}"
    else:
        paused_cls = "bw-flag-pill bw-flag-clear"
        paused_label = "PAUSED: CLEAR"

    en_label = "ELECTION NIGHT: ON" if hb.election_night else "ELECTION NIGHT: OFF"
    en_cls = (
        "bw-flag-pill bw-flag-active-paused"
        if hb.election_night
        else "bw-flag-pill bw-flag-clear"
    )
    refresh_label = "REFRESH: PAUSED (CONFIRM OPEN)" if refresh_paused else f"REFRESH: {AUTO_REFRESH_SECONDS}s"

    return (
        f'<div class="bw-hb-banner bw-hb-{_esc(status_slug)}" data-testid="heartbeat-banner">'
        f'  <div class="bw-hb-left">'
        f'    <span class="bw-hb-title">BELLWETHER EW MONITOR</span>'
        f'    <span class="bw-hb-status-pill bw-hb-status-{_esc(status_slug)}">{_esc(hb.status)}</span>'
        f'    <span class="bw-hb-age">{_esc(age_text)}</span>'
        f'    <div class="bw-hb-flags">'
        f'      <span class="{stop_cls}">{_esc(stop_label)}</span>'
        f'      <span class="{paused_cls}">{_esc(paused_label)}</span>'
        f'      <span class="{en_cls}">{_esc(en_label)}</span>'
        f'    </div>'
        f'  </div>'
        f'  <div class="bw-hb-meta">'
        f'    <span>MODE: <strong>{_esc(hb.mode)}</strong></span>'
        f'    <span>LOOP: <strong>{_esc(hb.loop_ms)}</strong></span>'
        f'    <span>R/W 1m: <strong>{_esc(hb.reads_last_min)}</strong> / <strong>{_esc(hb.writes_last_min)}</strong></span>'
        f'    <span>HB TS: <strong>{_esc(hb.ts_ist_display)}</strong></span>'
        f'    <span>POLLED: <strong>{_esc(fetched_ist)}</strong> ({_esc(refresh_label)})</span>'
        f'  </div>'
        f'</div>'
    )


def render_hero_row_html(hero: HeroMetrics) -> str:
    """Build the 5-card translucent Hero Row HTML."""
    bal_cls = "bw-val-neutral" if hero.balance_available else "bw-val-nodata"
    if not hero.pnl_available or hero.pnl_direction == "no_data":
        pnl_cls = "bw-val-nodata"
    elif hero.pnl_direction == "positive":
        pnl_cls = "bw-val-positive"
    elif hero.pnl_direction == "negative":
        pnl_cls = "bw-val-negative"
    else:
        pnl_cls = "bw-val-neutral"

    ord_cls = "bw-val-neutral" if hero.open_orders_available else "bw-val-nodata"
    exp_cls = "bw-val-neutral" if hero.exposure_available else "bw-val-nodata"
    evt_cls = "bw-val-neutral" if hero.last_event_available else "bw-val-nodata"

    cards = [
        ("BALANCE (SNAPSHOT)", hero.balance_display, hero.balance_sub, bal_cls),
        ("NET P&L (SNAPSHOT)", hero.pnl_display, hero.pnl_sub, pnl_cls),
        ("OPEN ORDERS", hero.open_orders_display, hero.open_orders_sub, ord_cls),
        ("TOTAL EXPOSURE", hero.exposure_display, hero.exposure_sub, exp_cls),
        ("LAST EVENT TIME (IST)", hero.last_event_display, hero.last_event_sub, evt_cls),
    ]
    parts = ['<div class="bw-hero-grid" data-testid="hero-row">']
    for label, val, sub, val_cls in cards:
        parts.append(
            f'<div class="bw-hero-card">'
            f'  <div class="bw-hero-label">{_esc(label)}</div>'
            f'  <div class="bw-hero-value {val_cls}">{_esc(val)}</div>'
            f'  <div class="bw-hero-sub">{_esc(sub)}</div>'
            f'</div>'
        )
    parts.append("</div>")
    return "".join(parts)


def _render_nodata_panel(title: str, meta: str, message: str, row_cls: str) -> str:
    return (
        f'<div class="bw-panel {row_cls}">'
        f'  <div class="bw-panel-header">'
        f'    <span class="bw-panel-title">{_esc(title)}</span>'
        f'    <span class="bw-panel-meta">{_esc(meta)}</span>'
        f'  </div>'
        f'  <div class="bw-nodata-box">'
        f'    <div class="bw-nodata-title">NO DATA AVAILABLE</div>'
        f'    <div class="bw-nodata-msg">{_esc(message)}</div>'
        f'  </div>'
        f'</div>'
    )


def render_exposure_panel_html(table: TableResult) -> str:
    """Render Panel 1: EXPOSURE (from ``exposure``, largest committed first)."""
    title = "EXPOSURE BY MARKET"
    if not table.has_data:
        return _render_nodata_panel(title, "exposure", table.message, "bw-panel-row1")

    rows_html: list[str] = []
    for row in table.rows:
        race = _esc(row.get("race_key"))
        comm_dec = parse_decimal(row.get("committed"))
        cap_dec = parse_decimal(row.get("cap"))
        comm_str = format_susq(comm_dec, with_unit=False) if comm_dec is not None else "NO DATA"
        cap_str = format_susq(cap_dec, with_unit=False) if cap_dec is not None else "NO DATA"
        if comm_dec is not None and cap_dec is not None and cap_dec > 0:
            pct = float(min(Decimal(100), max(Decimal(0), (comm_dec / cap_dec) * Decimal(100))))
            pct_label = f"{pct:.1f}%"
        else:
            pct = 0.0
            pct_label = "—"
        ts_str = format_ist_timestamp(row.get("ts_utc"))
        bar_html = (
            f'<div style="display:flex;align-items:center;gap:6px;">'
            f'  <div class="bw-exp-bar-track"><div class="bw-exp-bar-fill" style="width:{pct:.1f}%;"></div></div>'
            f'  <span class="bw-num" style="min-width:38px;">{_esc(pct_label)}</span>'
            f'</div>'
        )
        rows_html.append(
            f"<tr>"
            f"<td><strong>{race}</strong></td>"
            f'<td class="bw-num">{_esc(comm_str)}</td>'
            f'<td class="bw-num">{_esc(cap_str)}</td>'
            f"<td>{bar_html}</td>"
            f'<td class="bw-num">{_esc(ts_str)}</td>'
            f"</tr>"
        )

    meta = f"{len(table.rows)} races | SUSQies | largest first"
    return (
        f'<div class="bw-panel bw-panel-row1" data-testid="panel-exposure">'
        f'  <div class="bw-panel-header">'
        f'    <span class="bw-panel-title">{title}</span>'
        f'    <span class="bw-panel-meta">{_esc(meta)}</span>'
        f'  </div>'
        f'  <div class="bw-table-wrap">'
        f'    <table class="bw-table">'
        f'      <thead><tr>'
        f'        <th>RACE</th>'
        f'        <th class="bw-num">COMMITTED</th>'
        f'        <th class="bw-num">CAP</th>'
        f'        <th>UTILIZATION</th>'
        f'        <th class="bw-num">UPDATED (IST)</th>'
        f'      </tr></thead>'
        f'      <tbody>{"".join(rows_html)}</tbody>'
        f'    </table>'
        f'  </div>'
        f'</div>'
    )


def _format_bid_ask(bid: Any, ask: Any) -> str:
    b_dec = parse_decimal(bid)
    a_dec = parse_decimal(ask)
    b_str = f"{b_dec:.3f}" if b_dec is not None else "—"
    a_str = f"{a_dec:.3f}" if a_dec is not None else "—"
    return f"{b_str} / {a_str}"


def render_quotes_panel_html(table: TableResult, now_utc: datetime) -> str:
    """Render Panel 4: QUOTES (from ``quotes_latest``, flagging age > 60s as STALE)."""
    title = "QUOTES (LATEST)"
    if not table.has_data:
        return _render_nodata_panel(title, "quotes_latest", table.message, "bw-panel-row1")

    stale_count = 0
    rows_html: list[str] = []
    for row in table.rows:
        age_sec = compute_age_seconds(row.get("ts_utc"), now_utc)
        is_stale = age_sec is None or age_sec > QUOTE_STALE_SECONDS
        if is_stale:
            stale_count += 1
        row_cls = ' class="bw-row-stale"' if is_stale else ""
        age_str = f"{round(age_sec)}s" if age_sec is not None else "NO TS"
        state_badge = (
            '<span class="bw-badge bw-badge-stale">STALE (&gt;60s)</span>'
            if is_stale
            else '<span class="bw-badge bw-badge-fresh">FRESH</span>'
        )

        fair_dec = parse_decimal(row.get("fair"))
        fair_str = f"{fair_dec:.3f}" if fair_dec is not None else "—"
        edge_str, edge_dir = format_edge(row.get("edge"))
        edge_cls = (
            "bw-val-positive"
            if edge_dir == "positive"
            else "bw-val-negative"
            if edge_dir == "negative"
            else "bw-val-neutral"
        )
        sig_last_dec = parse_decimal(row.get("sig_last"))
        sig_last_str = f"{sig_last_dec:.3f}" if sig_last_dec is not None else "—"

        rows_html.append(
            f"<tr{row_cls}>"
            f"<td><strong>{_esc(row.get('sig_exchange_id'))}</strong></td>"
            f"<td>{_esc(row.get('race_key'))}</td>"
            f"<td>{_esc(row.get('party'))}</td>"
            f'<td class="bw-num">{_esc(_format_bid_ask(row.get("sig_bid"), row.get("sig_ask")))}</td>'
            f'<td class="bw-num">{_esc(sig_last_str)}</td>'
            f'<td class="bw-num">{_esc(_format_bid_ask(row.get("kalshi_bid"), row.get("kalshi_ask")))}</td>'
            f'<td class="bw-num">{_esc(_format_bid_ask(row.get("poly_bid"), row.get("poly_ask")))}</td>'
            f'<td class="bw-num">{_esc(fair_str)}</td>'
            f'<td class="bw-num {edge_cls}">{_esc(edge_str)}</td>'
            f'<td class="bw-num">{_esc(age_str)}</td>'
            f"<td>{state_badge}</td>"
            f"</tr>"
        )

    meta = f"{len(table.rows)} markets | {stale_count} STALE (>60s)"
    return (
        f'<div class="bw-panel bw-panel-row1" data-testid="panel-quotes">'
        f'  <div class="bw-panel-header">'
        f'    <span class="bw-panel-title">{title}</span>'
        f'    <span class="bw-panel-meta">{_esc(meta)}</span>'
        f'  </div>'
        f'  <div class="bw-table-wrap">'
        f'    <table class="bw-table">'
        f'      <thead><tr>'
        f'        <th>CONTRACT</th>'
        f'        <th>RACE</th>'
        f'        <th>PTY</th>'
        f'        <th class="bw-num">SIG B/A</th>'
        f'        <th class="bw-num">LAST</th>'
        f'        <th class="bw-num">KALSHI B/A</th>'
        f'        <th class="bw-num">POLY B/A</th>'
        f'        <th class="bw-num">FAIR</th>'
        f'        <th class="bw-num">EDGE</th>'
        f'        <th class="bw-num">AGE</th>'
        f'        <th>STATE</th>'
        f'      </tr></thead>'
        f'      <tbody>{"".join(rows_html)}</tbody>'
        f'    </table>'
        f'  </div>'
        f'</div>'
    )


def render_orders_panel_html(table: TableResult) -> str:
    """Render Panel 2: ORDERS (from ``orders``, open orders highlighted)."""
    title = "ORDERS"
    if not table.has_data:
        return _render_nodata_panel(title, "orders", table.message, "bw-panel-row2")

    open_count = 0
    rows_html: list[str] = []
    for row in table.rows:
        status_raw = str(row.get("status") or "").strip().lower()
        is_open = status_raw in OPEN_ORDER_STATUSES
        if is_open:
            open_count += 1
            row_cls = ' class="bw-row-open"'
            badge_cls = "bw-badge bw-badge-open"
            status_label = f"OPEN • {status_raw.upper()}"
        elif status_raw == "filled":
            row_cls = ""
            badge_cls = "bw-badge bw-badge-filled"
            status_label = "FILLED"
        elif status_raw in {"rejected", "unknown"}:
            row_cls = ' class="bw-row-error"'
            badge_cls = "bw-badge bw-badge-error"
            status_label = status_raw.upper()
        else:
            row_cls = ""
            badge_cls = "bw-badge bw-badge-closed"
            status_label = status_raw.upper() or "—"

        ts_str = format_ist_timestamp(
            row.get("updated_utc") or row.get("created_utc")
        ).removesuffix(" IST")
        side_str = (
            f"{str(row.get('action') or 'buy').upper()} {str(row.get('side') or '').upper()}".strip()
        )
        wp_dec = parse_decimal(row.get("wire_price"))
        wp_str = f"{wp_dec:.3f}" if wp_dec is not None else _esc(row.get("wire_price"))
        qty_str = f"{row.get('quantity_traded', 0)}/{row.get('quantity', '—')}"
        cost_dec = parse_decimal(row.get("total_cost"))
        cost_str = format_susq(cost_dec, with_unit=False) if cost_dec is not None else "—"
        strat_str = str(row.get("strategy") or "").strip()
        reason_raw = row.get("error") or row.get("reason") or ""
        reason_text = f"[{strat_str}] {reason_raw}".strip() if strat_str else str(reason_raw)

        rows_html.append(
            f"<tr{row_cls}>"
            f'<td class="bw-num">{_esc(ts_str)}</td>'
            f'<td class="bw-nowrap"><strong>{_esc(row.get("race_key"))}</strong></td>'
            f'<td class="bw-nowrap">{_esc(side_str)}</td>'
            f'<td class="bw-num">{_esc(wp_str)}</td>'
            f'<td class="bw-num">{_esc(qty_str)}</td>'
            f'<td class="bw-num">{_esc(cost_str)}</td>'
            f'<td class="bw-nowrap"><span class="{badge_cls}">{_esc(status_label)}</span></td>'
            f'<td class="bw-reason-cell">{_esc(reason_text)}</td>'
            f"</tr>"
        )

    meta = f"{len(table.rows)} recent | {open_count} OPEN highlighted"
    return (
        f'<div class="bw-panel bw-panel-row2" data-testid="panel-orders">'
        f'  <div class="bw-panel-header">'
        f'    <span class="bw-panel-title">{title}</span>'
        f'    <span class="bw-panel-meta">{_esc(meta)}</span>'
        f'  </div>'
        f'  <div class="bw-table-wrap">'
        f'    <table class="bw-table">'
        f'      <thead><tr>'
        f'        <th class="bw-num">TIME (IST)</th>'
        f'        <th>RACE</th>'
        f'        <th>SIDE</th>'
        f'        <th class="bw-num">PRICE</th>'
        f'        <th class="bw-num">FILL/QTY</th>'
        f'        <th class="bw-num">COST</th>'
        f'        <th>STATUS</th>'
        f'        <th>REASON / ERROR</th>'
        f'      </tr></thead>'
        f'      <tbody>{"".join(rows_html)}</tbody>'
        f'    </table>'
        f'  </div>'
        f'</div>'
    )


def render_decisions_panel_html(table: TableResult) -> str:
    """Render Panel 3: DECISIONS (from ``decisions``, with ``reason`` column)."""
    title = "DECISIONS"
    if not table.has_data:
        return _render_nodata_panel(title, "decisions", table.message, "bw-panel-row2")

    rows_html: list[str] = []
    for row in table.rows:
        ts_str = format_ist_timestamp(row.get("ts_utc")).removesuffix(" IST")
        verdict_raw = str(row.get("verdict") or "").strip().lower()
        if verdict_raw == "order":
            v_badge = '<span class="bw-badge bw-badge-open">ORDER</span>'
        elif verdict_raw == "would_order":
            v_badge = '<span class="bw-badge bw-badge-filled">WOULD_ORDER</span>'
        elif verdict_raw == "blocked":
            v_badge = '<span class="bw-badge bw-badge-warn">BLOCKED</span>'
        else:
            v_badge = f'<span class="bw-badge bw-badge-closed">{_esc(verdict_raw.upper())}</span>'

        fair_dec = parse_decimal(row.get("fair"))
        fair_str = f"{fair_dec:.3f}" if fair_dec is not None else "—"
        edge_dec = parse_decimal(row.get("edge"))
        if edge_dec is None:
            edge_str, edge_cls = "—", "bw-val-neutral"
        elif edge_dec > 0:
            edge_str, edge_cls = f"+{edge_dec:.3f} ▲", "bw-val-positive"
        elif edge_dec < 0:
            edge_str, edge_cls = f"−{abs(edge_dec):.3f} ▼", "bw-val-negative"
        else:
            edge_str, edge_cls = "0.000", "bw-val-neutral"
        qty_val = row.get("quantity")
        qty_str = str(qty_val) if qty_val is not None else "—"
        race_party = (
            f"{row.get('race_key')} ({row.get('party')})"
            if row.get("party")
            else str(row.get("race_key") or "—")
        )

        rows_html.append(
            f"<tr>"
            f'<td class="bw-num">{_esc(ts_str)}</td>'
            f'<td class="bw-nowrap"><strong>{_esc(race_party)}</strong></td>'
            f'<td class="bw-nowrap">{_esc(row.get("strategy"))}</td>'
            f'<td class="bw-nowrap">{v_badge}</td>'
            f'<td class="bw-num">{_esc(fair_str)}</td>'
            f'<td class="bw-num {edge_cls}">{_esc(edge_str)}</td>'
            f'<td class="bw-num">{_esc(qty_str)}</td>'
            f'<td class="bw-reason-cell">{_esc(row.get("reason"))}</td>'
            f"</tr>"
        )

    meta = f"{len(table.rows)} latest | includes reason column"
    return (
        f'<div class="bw-panel bw-panel-row2" data-testid="panel-decisions">'
        f'  <div class="bw-panel-header">'
        f'    <span class="bw-panel-title">{title}</span>'
        f'    <span class="bw-panel-meta">{_esc(meta)}</span>'
        f'  </div>'
        f'  <div class="bw-table-wrap">'
        f'    <table class="bw-table">'
        f'      <thead><tr>'
        f'        <th class="bw-num">TIME (IST)</th>'
        f'        <th>RACE (PTY)</th>'
        f'        <th>STRAT</th>'
        f'        <th>VERDICT</th>'
        f'        <th class="bw-num">FAIR</th>'
        f'        <th class="bw-num">EDGE</th>'
        f'        <th class="bw-num">QTY</th>'
        f'        <th>REASON</th>'
        f'      </tr></thead>'
        f'      <tbody>{"".join(rows_html)}</tbody>'
        f'    </table>'
        f'  </div>'
        f'</div>'
    )


def render_events_panel_html(table: TableResult) -> str:
    """Render Panel 5: EVENTS (from ``events``, newest first, warnings/errors coloured)."""
    title = "EVENTS"
    if not table.has_data:
        return _render_nodata_panel(title, "events", table.message, "bw-panel-row2")

    warn_err_count = 0
    rows_html: list[str] = []
    for row in table.rows:
        level_raw = str(row.get("level") or "info").strip().lower()
        if level_raw == "error":
            warn_err_count += 1
            row_cls = ' class="bw-row-error"'
            badge_html = '<span class="bw-badge bw-badge-error">ERROR</span>'
            msg_style = ' style="color:#FFD7CE;font-weight:600;"'
        elif level_raw in {"warn", "warning"}:
            warn_err_count += 1
            row_cls = ' class="bw-row-warn"'
            badge_html = '<span class="bw-badge bw-badge-warn">WARN</span>'
            msg_style = ' style="color:#FFB000;"'
        else:
            row_cls = ""
            badge_html = '<span class="bw-badge bw-badge-info">INFO</span>'
            msg_style = ""

        ts_str = format_ist_timestamp(row.get("ts_utc"))
        rows_html.append(
            f"<tr{row_cls}>"
            f'<td class="bw-num">{_esc(ts_str)}</td>'
            f"<td>{badge_html}</td>"
            f"<td><strong>{_esc(row.get('kind'))}</strong></td>"
            f"<td{msg_style}>{_esc(row.get('message'))}</td>"
            f"</tr>"
        )

    meta = f"{len(table.rows)} newest first | {warn_err_count} warn/error"
    return (
        f'<div class="bw-panel bw-panel-row2" data-testid="panel-events">'
        f'  <div class="bw-panel-header">'
        f'    <span class="bw-panel-title">{title}</span>'
        f'    <span class="bw-panel-meta">{_esc(meta)}</span>'
        f'  </div>'
        f'  <div class="bw-table-wrap">'
        f'    <table class="bw-table">'
        f'      <thead><tr>'
        f'        <th class="bw-num">TIME (IST)</th>'
        f'        <th>LVL</th>'
        f'        <th>KIND</th>'
        f'        <th>MESSAGE</th>'
        f'      </tr></thead>'
        f'      <tbody>{"".join(rows_html)}</tbody>'
        f'    </table>'
        f'  </div>'
        f'</div>'
    )


def render_db_busy_banner_html(snapshot: MonitorSnapshot) -> str:
    """Render a top-level DB BUSY banner when SQLite is locked beyond busy_timeout."""
    if not snapshot.db_busy:
        return ""
    detail = snapshot.db_busy_message or "SQLite database is temporarily locked by writer."
    return (
        f'<div class="bw-db-busy-banner" data-testid="db-busy-banner">'
        f'  <span><strong>DB BUSY</strong> — {_esc(detail)}</span>'
        f'  <span>AUTO-RETRYING ON NEXT REFRESH</span>'
        f'</div>'
    )


def render_monitor_body(snapshot: MonitorSnapshot, refresh_paused: bool = False) -> None:
    """Render the full monitor content inside the page or auto-refresh fragment."""
    if snapshot.db_busy:
        st.markdown(render_db_busy_banner_html(snapshot), unsafe_allow_html=True)
    st.markdown(
        render_heartbeat_banner_html(
            snapshot.heartbeat,
            snapshot.fetched_at_ist,
            refresh_paused,
        ),
        unsafe_allow_html=True,
    )
    st.markdown(render_hero_row_html(snapshot.hero), unsafe_allow_html=True)

    row1_left, row1_right = st.columns([1.0, 1.55], gap="small")
    with row1_left:
        st.markdown(
            render_exposure_panel_html(snapshot.exposure_table),
            unsafe_allow_html=True,
        )
    with row1_right:
        st.markdown(
            render_quotes_panel_html(snapshot.quotes_table, snapshot.fetched_at_utc),
            unsafe_allow_html=True,
        )

    row2_col1, row2_col2, row2_col3 = st.columns([1.15, 1.15, 0.95], gap="small")
    with row2_col1:
        st.markdown(
            render_orders_panel_html(snapshot.orders_table),
            unsafe_allow_html=True,
        )
    with row2_col2:
        st.markdown(
            render_decisions_panel_html(snapshot.decisions_table),
            unsafe_allow_html=True,
        )
    with row2_col3:
        st.markdown(
            render_events_panel_html(snapshot.events_table),
            unsafe_allow_html=True,
        )


def main() -> None:
    st.set_page_config(
        page_title="Bellwether EW Monitor",
        page_icon="⚡",
        layout="wide",
        initial_sidebar_state="collapsed",
    )

    css = _load_css()
    if css:
        st.markdown(f"<style>{css}</style>", unsafe_allow_html=True)

    if "confirm_open" not in st.session_state:
        st.session_state["confirm_open"] = False

    confirm_open = bool(st.session_state.get("confirm_open", False))
    run_every = None if confirm_open else f"{AUTO_REFRESH_SECONDS}s"

    @st.fragment(run_every=run_every)
    def _live_monitor_fragment() -> None:
        snapshot = load_monitor_snapshot()
        render_monitor_body(snapshot, refresh_paused=confirm_open)

    _live_monitor_fragment()


if __name__ == "__main__":
    main()
