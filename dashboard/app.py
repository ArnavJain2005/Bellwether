"""Bellwether Election-Week Monitor (Tasks DASH-EW-1..3 & DASH-UI-SIMPLIFY).

Streamlit monitor that reads ``bot/state/bellwether.db`` strictly in read-only mode
(`mode=ro`) following ``bot/CONTRACT.md`` and ``AGENTS.md``, and provides the
confirmation-gated ``STOP BOT`` control that atomically creates ``bot/state/STOP``.

Supports both the simplified v2 layout (default) and a one-step rollback to the
legacy layout via ``BW_UI=legacy`` or ``?ui=legacy``.
"""

from __future__ import annotations

import html
import os
import sys
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from pathlib import Path
from typing import Any

import altair as alt
import pandas as pd
import streamlit as st

_REPO_ROOT = Path(__file__).resolve().parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from dashboard.db import (
    DEFAULT_HISTORY_HOURS,
    OPEN_ORDER_STATUSES,
    QUOTE_INACTIVE_SECONDS,
    QUOTE_STALE_SECONDS,
    STOP_CONTRACT_PATH,
    FilterCriteria,
    HeartbeatState,
    HeroMetrics,
    MonitorSnapshot,
    ParsedPnlResult,
    ParsedPositionsResult,
    SnapshotHistoryResult,
    StopWriteResult,
    TableResult,
    collect_filter_options,
    compute_age_seconds,
    filter_decisions_rows,
    filter_orders_rows,
    filter_position_rows,
    filter_race_pnl_rows,
    format_edge,
    format_ist_timestamp,
    format_signed_susq,
    format_susq,
    load_monitor_snapshot,
    parse_decimal,
    write_stop_file,
)

AUTO_REFRESH_SECONDS = 3
ATTENTION_EVENT_WINDOW_SECONDS: float = 15.0 * 60.0  # 15 minutes
ATTENTION_EXPOSURE_CAP_RATIO: Decimal = Decimal("0.90")  # > 90% of a cap


@dataclass(frozen=True)
class AttentionItem:
    """Single actionable problem surfaced in the v2 'Needs attention' box."""

    severity: str  # "error" | "warn"
    category: str  # "db_busy" | "stop" | "paused" | "heartbeat" | "bad_format" | "schema" | "quote_stale" | "exposure_cap" | "event"
    title: str
    detail: str


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


def resolve_ui_mode() -> str:
    """Return ``'legacy'`` when ``BW_UI=legacy`` or ``?ui=legacy`` is set, else ``'v2'``."""
    try:
        qp_val = st.query_params.get("ui", "")
        if isinstance(qp_val, list):
            qp_val = qp_val[0] if qp_val else ""
        qp_clean = str(qp_val).strip().lower()
        if qp_clean == "legacy":
            return "legacy"
        if qp_clean in {"v2", "new"}:
            return "v2"
    except Exception:  # noqa: BLE001, S110
        pass

    env_val = os.environ.get("BW_UI", "").strip().lower()
    if env_val == "legacy":
        return "legacy"
    return "v2"


def _active_filter_summary(criteria: FilterCriteria | None) -> str:
    if criteria is None:
        return ""
    parts: list[str] = []
    if criteria.race_key != "All":
        parts.append(f"race={criteria.race_key}")
    if criteria.strategy != "All":
        parts.append(f"strat={criteria.strategy}")
    if criteria.status_verdict != "All":
        parts.append(f"status/verdict={criteria.status_verdict}")
    return f"FILTERS: {', '.join(parts)}" if parts else ""


def render_heartbeat_banner_html(
    hb: HeartbeatState,
    fetched_ist: str,
    refresh_paused: bool,
    criteria: FilterCriteria | None = None,
) -> str:
    """Build the top-of-page Heartbeat Banner HTML (Legacy UI & unit-test helper)."""
    status_slug = "-".join(hb.status.lower().split())
    age_text = f"AGE: {hb.age_display}"

    if hb.stop_file_exists or hb.db_stop_seen:
        stop_label = (
            "STOP FILE PRESENT"
            if hb.stop_file_exists and not hb.db_stop_seen
            else "STOP FILE PRESENT (STOP: ACTIVE)"
            if hb.stop_file_exists and hb.db_stop_seen
            else "STOP: DB SEEN"
        )
        stop_cls = "bw-flag-pill bw-flag-active-stop"
    else:
        stop_cls = "bw-flag-pill bw-flag-clear"
        stop_label = "STOP: CLEAR"

    pause_reasons = [r for r in (hb.paused_file_reason, hb.db_pause_reason) if r]
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
    filter_summary = _active_filter_summary(criteria)
    filter_pill_html = (
        f'<span class="bw-flag-pill bw-flag-active-paused">{_esc(filter_summary)}</span>'
        if filter_summary
        else ""
    )
    refresh_label = (
        "REFRESH: PAUSED (CONFIRM OPEN)"
        if refresh_paused
        else f"REFRESH: {AUTO_REFRESH_SECONDS}s"
    )

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
        f"      {filter_pill_html}"
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


def render_status_strip_v2_html(
    snapshot: MonitorSnapshot,
    refresh_paused: bool,
    criteria: FilterCriteria | None = None,
) -> str:
    """Build the v2 single-line Status Strip HTML (large state pill, age, mode, flags, last event)."""
    hb = snapshot.heartbeat
    hero = snapshot.hero
    age_text = f"AGE: {hb.age_display}"

    if hb.stop_file_exists or hb.db_stop_seen:
        big_pill_cls = "bw-v2-big-pill bw-v2-pill-down"
        big_pill_text = f"■ STOPPED ({hb.status})"
        stop_label = (
            "STOP FILE PRESENT"
            if hb.stop_file_exists and not hb.db_stop_seen
            else "STOP FILE PRESENT (STOP: ACTIVE)"
            if hb.stop_file_exists and hb.db_stop_seen
            else "STOP: DB SEEN"
        )
        stop_cls = "bw-flag-pill bw-flag-active-stop"
    else:
        stop_cls = "bw-flag-pill bw-flag-clear"
        stop_label = "STOP: CLEAR"
        if hb.status == "ALIVE":
            big_pill_cls = "bw-v2-big-pill bw-v2-pill-alive"
            big_pill_text = "● ALIVE"
        elif hb.status == "STALE":
            big_pill_cls = "bw-v2-big-pill bw-v2-pill-stale"
            big_pill_text = "▲ STALE"
        elif hb.status == "DB BUSY":
            big_pill_cls = "bw-v2-big-pill bw-v2-pill-stale"
            big_pill_text = "▲ DB BUSY"
        else:
            big_pill_cls = "bw-v2-big-pill bw-v2-pill-down"
            big_pill_text = "■ NO HEARTBEAT"

    pause_reasons = [r for r in (hb.paused_file_reason, hb.db_pause_reason) if r]
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
    db_busy_pill = (
        '<span class="bw-flag-pill bw-flag-active-paused">DB BUSY</span>'
        if snapshot.db_busy
        else ""
    )
    filter_summary = _active_filter_summary(criteria)
    filter_pill_html = (
        f'<span class="bw-flag-pill bw-flag-active-paused">{_esc(filter_summary)}</span>'
        if filter_summary
        else ""
    )
    refresh_label = (
        "REFRESH: PAUSED (CONFIRM OPEN)"
        if refresh_paused
        else f"REFRESH: {AUTO_REFRESH_SECONDS}s"
    )

    if hero.last_event_available:
        last_evt_html = (
            f"<span>Last event: <strong>{_esc(hero.last_event_display)}</strong> "
            f"({_esc(hero.last_event_sub)})</span>"
        )
    else:
        last_evt_html = f"<span>Last event: <strong>{_esc(hero.last_event_display)}</strong></span>"

    return (
        f'<div class="bw-v2-status-strip" data-testid="heartbeat-banner">'
        f'  <div class="bw-v2-status-left">'
        f'    <span class="bw-v2-brand">BELLWETHER EW MONITOR</span>'
        f'    <span class="{big_pill_cls}">{_esc(big_pill_text)}</span>'
        f'    <span class="bw-v2-age-chip">{_esc(age_text)}</span>'
        f'    <span class="bw-flag-pill bw-flag-clear">MODE: {_esc(hb.mode)}</span>'
        f'    <span class="{stop_cls}">{_esc(stop_label)}</span>'
        f'    <span class="{paused_cls}">{_esc(paused_label)}</span>'
        f'    <span class="{en_cls}">{_esc(en_label)}</span>'
        f"    {db_busy_pill}"
        f"    {filter_pill_html}"
        f"  </div>"
        f'  <div class="bw-v2-status-right">'
        f"    {last_evt_html}"
        f'    <span>HB: <strong>{_esc(hb.ts_ist_display)}</strong></span>'
        f'    <span>POLLED: <strong>{_esc(snapshot.fetched_at_ist)}</strong> ({_esc(refresh_label)})</span>'
        f"  </div>"
        f"</div>"
    )


def render_hero_row_html(hero: HeroMetrics) -> str:
    """Build the 5-card Legacy Hero Row HTML."""
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


def render_hero_row_v2_html(snapshot: MonitorSnapshot) -> str:
    """Build the v2 Four Big Numbers Hero Row (Balance, Net P&L, Open Orders, Exposure with thin bar)."""
    hero = snapshot.hero
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

    # Compute exposure utilisation bar width vs cap_total if available
    exp_pct = 0.0
    exp_high = False
    if snapshot.heartbeat_table.has_data:
        cap_total = parse_decimal(snapshot.heartbeat_table.rows[0].get("cap_total"))
    else:
        cap_total = None

    used_dec: Decimal | None = None
    if snapshot.exposure_table.has_data:
        valid_exp = [
            d
            for d in (parse_decimal(r.get("committed")) for r in snapshot.exposure_table.rows)
            if d is not None
        ]
        if valid_exp:
            used_dec = sum(valid_exp, Decimal(0))
    elif snapshot.snapshots_table.has_data:
        used_dec = parse_decimal(snapshot.snapshots_table.rows[0].get("committed"))

    if used_dec is not None and cap_total is not None and cap_total > 0:
        ratio = used_dec / cap_total
        exp_high = ratio > ATTENTION_EXPOSURE_CAP_RATIO
        exp_pct = float(min(Decimal(100), max(Decimal(0), ratio * Decimal(100))))

    bar_fill_cls = "bw-v2-exp-bar-fill bw-v2-exp-high" if exp_high else "bw-v2-exp-bar-fill"
    exp_bar_html = (
        f'<div class="bw-v2-exp-bar-track">'
        f'  <div class="{bar_fill_cls}" style="width:{exp_pct:.1f}%;"></div>'
        f"</div>"
        if hero.exposure_available
        else ""
    )

    return (
        f'<div class="bw-v2-hero-grid" data-testid="hero-row">'
        f'  <div class="bw-v2-hero-card">'
        f'    <div class="bw-v2-hero-label">BALANCE</div>'
        f'    <div class="bw-v2-hero-value {bal_cls}">{_esc(hero.balance_display)}</div>'
        f'    <div class="bw-v2-hero-sub">{_esc(hero.balance_sub)}</div>'
        f"  </div>"
        f'  <div class="bw-v2-hero-card">'
        f'    <div class="bw-v2-hero-label">NET P&amp;L</div>'
        f'    <div class="bw-v2-hero-value {pnl_cls}">{_esc(hero.pnl_display)}</div>'
        f'    <div class="bw-v2-hero-sub">{_esc(hero.pnl_sub)}</div>'
        f"  </div>"
        f'  <div class="bw-v2-hero-card">'
        f'    <div class="bw-v2-hero-label">OPEN ORDERS</div>'
        f'    <div class="bw-v2-hero-value {ord_cls}">{_esc(hero.open_orders_display)}</div>'
        f'    <div class="bw-v2-hero-sub">{_esc(hero.open_orders_sub)}</div>'
        f"  </div>"
        f'  <div class="bw-v2-hero-card">'
        f'    <div class="bw-v2-hero-label">EXPOSURE (USED / CAP)</div>'
        f'    <div class="bw-v2-hero-value {exp_cls}">{_esc(hero.exposure_display)}</div>'
        f'    <div class="bw-v2-hero-sub">{_esc(hero.exposure_sub)}</div>'
        f"    {exp_bar_html}"
        f"  </div>"
        f"</div>"
    )


def build_needs_attention_items(snapshot: MonitorSnapshot) -> list[AttentionItem]:
    """Derive the list of current problems from ``snapshot`` without any new data sources.

    Checks:
    1. DB BUSY lock contention
    2. STOP or PAUSED active flags
    3. Stale (>=30s) or missing heartbeat
    4. Bad-format snapshot JSON (`positions_json`, `pnl_json`, `snapshots`)
    5. Missing tables or missing columns
    6. STALE quotes (>60s)
    7. Exposure over 90% of a cap (per-race or total)
    8. Error/warn events in the last 15 minutes
    """
    items: list[AttentionItem] = []
    hb = snapshot.heartbeat

    # 1. DB BUSY
    if snapshot.db_busy or hb.status == "DB BUSY":
        items.append(
            AttentionItem(
                severity="warn",
                category="db_busy",
                title="DB BUSY",
                detail=snapshot.db_busy_message or "SQLite database is locked by writer",
            )
        )

    # 2. STOP / PAUSED flags
    if hb.stop_file_exists or hb.db_stop_seen:
        items.append(
            AttentionItem(
                severity="error",
                category="stop",
                title="STOP FILE PRESENT",
                detail="bot/state/STOP is active; trading is halted",
            )
        )
    if hb.paused_file_exists or hb.db_paused:
        reasons = [r for r in (hb.paused_file_reason, hb.db_pause_reason) if r]
        reason_detail = reasons[0] if reasons else "Bot is paused"
        items.append(
            AttentionItem(
                severity="warn",
                category="paused",
                title="PAUSED ACTIVE",
                detail=reason_detail,
            )
        )

    # 3. Heartbeat liveness
    if hb.status == "STALE":
        items.append(
            AttentionItem(
                severity="warn",
                category="heartbeat",
                title=f"STALE HEARTBEAT (AGE: {hb.age_display})",
                detail=hb.detail_message,
            )
        )
    elif hb.status == "NO HEARTBEAT":
        items.append(
            AttentionItem(
                severity="error",
                category="heartbeat",
                title="NO HEARTBEAT",
                detail=hb.detail_message or "No heartbeat row available",
            )
        )

    # 4. Bad-format snapshot JSON
    if snapshot.positions.status == "bad_format":
        items.append(
            AttentionItem(
                severity="error",
                category="bad_format",
                title="BAD FORMAT (positions_json)",
                detail=snapshot.positions.message,
            )
        )
    if snapshot.race_pnl.status == "bad_format":
        items.append(
            AttentionItem(
                severity="error",
                category="bad_format",
                title="BAD FORMAT (pnl_json)",
                detail=snapshot.race_pnl.message,
            )
        )
    if snapshot.snapshot_history.status == "bad_format":
        items.append(
            AttentionItem(
                severity="error",
                category="bad_format",
                title="BAD FORMAT (snapshots)",
                detail=snapshot.snapshot_history.message,
            )
        )

    # 5. Missing tables / columns
    for tbl in (
        snapshot.heartbeat_table,
        snapshot.snapshots_table,
        snapshot.exposure_table,
        snapshot.orders_table,
        snapshot.decisions_table,
        snapshot.quotes_table,
        snapshot.events_table,
    ):
        if tbl.status in {"missing_table", "missing_columns"}:
            items.append(
                AttentionItem(
                    severity="error",
                    category="schema",
                    title=f"SCHEMA ISSUE ({tbl.table})",
                    detail=tbl.message,
                )
            )

    # 6. STALE quotes (>60s). Rows older than QUOTE_INACTIVE_SECONDS are contracts the
    # bot no longer refreshes; they are summarised in one item.
    inactive_quotes: list[str] = []
    for row in snapshot.quotes_table.rows:
        age_sec = compute_age_seconds(row.get("ts_utc"), snapshot.fetched_at_utc)
        if age_sec is not None and age_sec > QUOTE_INACTIVE_SECONDS:
            inactive_quotes.append(
                f"{row.get('sig_exchange_id') or 'unknown'} ({row.get('race_key') or '—'})"
            )
            continue
        if age_sec is None or age_sec > QUOTE_STALE_SECONDS:
            age_str = f"{round(age_sec)}s" if age_sec is not None else "no timestamp"
            contract = str(row.get("sig_exchange_id") or "unknown")
            race = str(row.get("race_key") or "—")
            items.append(
                AttentionItem(
                    severity="warn",
                    category="quote_stale",
                    title=f"STALE QUOTE: {contract} ({race})",
                    detail=f"Quote age {age_str} (> {int(QUOTE_STALE_SECONDS)}s threshold)",
                )
            )

    if inactive_quotes:
        shown = ", ".join(inactive_quotes[:5])
        more = f" (+{len(inactive_quotes) - 5} more)" if len(inactive_quotes) > 5 else ""
        items.append(
            AttentionItem(
                severity="warn",
                category="quote_inactive",
                title=f"INACTIVE QUOTES ({len(inactive_quotes)})",
                detail=(
                    f"Not refreshed for > {int(QUOTE_INACTIVE_SECONDS // 60)} min "
                    f"(excluded, closed or old-run contracts): {shown}{more}"
                ),
            )
        )

    # 7. Exposure over 90% of a cap
    for row in snapshot.exposure_table.rows:
        comm = parse_decimal(row.get("committed"))
        cap = parse_decimal(row.get("cap"))
        if comm is not None and cap is not None and cap > 0:
            ratio = comm / cap
            if ratio > ATTENTION_EXPOSURE_CAP_RATIO:
                pct = (ratio * Decimal(100)).quantize(Decimal("0.1"))
                race = str(row.get("race_key") or "—")
                items.append(
                    AttentionItem(
                        severity="warn",
                        category="exposure_cap",
                        title=f"EXPOSURE > 90% CAP: {race}",
                        detail=f"{format_susq(comm)} / {format_susq(cap)} ({pct}%)",
                    )
                )

    if snapshot.heartbeat_table.has_data and snapshot.exposure_table.has_data:
        cap_total = parse_decimal(snapshot.heartbeat_table.rows[0].get("cap_total"))
        valid_exp = [
            d
            for d in (parse_decimal(r.get("committed")) for r in snapshot.exposure_table.rows)
            if d is not None
        ]
        if cap_total is not None and cap_total > 0 and valid_exp:
            total_comm = sum(valid_exp, Decimal(0))
            ratio = total_comm / cap_total
            if ratio > ATTENTION_EXPOSURE_CAP_RATIO:
                pct = (ratio * Decimal(100)).quantize(Decimal("0.1"))
                items.append(
                    AttentionItem(
                        severity="warn",
                        category="exposure_cap",
                        title="TOTAL EXPOSURE > 90% CAP",
                        detail=f"{format_susq(total_comm)} / {format_susq(cap_total)} ({pct}%)",
                    )
                )

    # 8. Error/warn events in the last 15 minutes
    for row in snapshot.events_table.rows:
        lvl = str(row.get("level") or "info").strip().lower()
        if lvl in {"error", "warn", "warning"}:
            age_sec = compute_age_seconds(row.get("ts_utc"), snapshot.fetched_at_utc)
            if age_sec is not None and age_sec <= ATTENTION_EVENT_WINDOW_SECONDS:
                sev = "error" if lvl == "error" else "warn"
                kind = str(row.get("kind") or "event")
                ts_ist = format_ist_timestamp(row.get("ts_utc"))
                msg = str(row.get("message") or "")
                items.append(
                    AttentionItem(
                        severity=sev,
                        category="event",
                        title=f"EVENT {lvl.upper()}: {kind}",
                        detail=f"{msg} ({ts_ist}, {round(age_sec)}s ago)",
                    )
                )

    return items


def render_needs_attention_html(snapshot: MonitorSnapshot) -> str:
    """Render the v2 'Needs attention' box directly under the 4 big numbers."""
    items = build_needs_attention_items(snapshot)
    if not items:
        return (
            '<div class="bw-attention-box bw-attention-clear" data-testid="needs-attention-box">'
            '  <div class="bw-attention-clear-row">'
            '    <span class="bw-attention-pill-ok">● All clear</span>'
            '    <span class="bw-attention-clear-detail">'
            "      Heartbeat alive, quotes fresh (≤60s), exposure within 90% caps, no warnings or errors in the last 15m."
            "    </span>"
            "  </div>"
            "</div>"
        )

    has_error = any(item.severity == "error" for item in items)
    box_cls = "bw-attention-error" if has_error else "bw-attention-warn"
    rows_html: list[str] = []
    for item in items:
        badge_cls = (
            "bw-badge bw-badge-error"
            if item.severity == "error"
            else "bw-badge bw-badge-warn"
        )
        badge_label = "ERROR" if item.severity == "error" else "WARN"
        rows_html.append(
            f'<div class="bw-attention-item">'
            f'  <span class="{badge_cls}">{badge_label}</span>'
            f"  <strong>{_esc(item.title)}</strong>"
            f'  <span class="bw-attention-detail">— {_esc(item.detail)}</span>'
            f"</div>"
        )

    return (
        f'<div class="bw-attention-box {box_cls}" data-testid="needs-attention-box">'
        f'  <div class="bw-attention-header">'
        f'    <span class="bw-attention-title">NEEDS ATTENTION ({len(items)})</span>'
        f'    <span class="bw-attention-count">{len(items)} active issue(s)</span>'
        f"  </div>"
        f'  <div class="bw-attention-list">{"".join(rows_html)}</div>'
        f"</div>"
    )


def _render_nodata_panel(
    title: str,
    meta: str,
    message: str,
    row_cls: str,
    heading: str = "NO DATA AVAILABLE",
    testid: str = "",
) -> str:
    tid_attr = f' data-testid="{_esc(testid)}"' if testid else ""
    return (
        f'<div class="bw-panel {row_cls}"{tid_attr}>'
        f'  <div class="bw-panel-header">'
        f'    <span class="bw-panel-title">{_esc(title)}</span>'
        f'    <span class="bw-panel-meta">{_esc(meta)}</span>'
        f'  </div>'
        f'  <div class="bw-nodata-box">'
        f'    <div class="bw-nodata-title">{_esc(heading)}</div>'
        f'    <div class="bw-nodata-msg">{_esc(message)}</div>'
        f'  </div>'
        f'</div>'
    )


def render_exposure_panel_html(
    table: TableResult,
    *,
    panel_cls: str = "bw-panel-row1",
) -> str:
    """Render Panel 1: EXPOSURE (from ``exposure``, largest committed first)."""
    title = "EXPOSURE BY MARKET"
    if not table.has_data:
        return _render_nodata_panel(
            title,
            "exposure",
            table.message,
            panel_cls,
            testid="panel-exposure",
        )

    rows_html: list[str] = []
    for row in table.rows:
        race = _esc(row.get("race_key"))
        comm_dec = parse_decimal(row.get("committed"))
        cap_dec = parse_decimal(row.get("cap"))
        comm_str = (
            format_susq(comm_dec, with_unit=False) if comm_dec is not None else "NO DATA"
        )
        cap_str = (
            format_susq(cap_dec, with_unit=False) if cap_dec is not None else "NO DATA"
        )
        if comm_dec is not None and cap_dec is not None and cap_dec > 0:
            pct = float(
                min(Decimal(100), max(Decimal(0), (comm_dec / cap_dec) * Decimal(100)))
            )
            pct_label = f"{pct:.1f}%"
        else:
            pct = 0.0
            pct_label = "—"
        ts_str = format_ist_timestamp(row.get("ts_utc"))
        bar_html = (
            f'<div style="display:flex;align-items:center;gap:6px;">'
            f'  <div class="bw-exp-bar-track"><div class="bw-exp-bar-fill" style="width:{pct:.1f}%;"></div></div>'
            f'  <span class="bw-num" style="min-width:42px;">{_esc(pct_label)}</span>'
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
        f'<div class="bw-panel {panel_cls}" data-testid="panel-exposure">'
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


def render_quotes_panel_html(
    table: TableResult,
    now_utc: datetime,
    *,
    show_all_columns: bool = False,
    panel_cls: str = "bw-panel-row1",
) -> str:
    """Render Panel 4: QUOTES (from ``quotes_latest``, flagging age > 60s as STALE)."""
    title = "QUOTES (LATEST)"
    if not table.has_data:
        return _render_nodata_panel(
            title,
            "quotes_latest",
            table.message,
            panel_cls,
            testid="panel-quotes",
        )

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
        extra_cells = (
            f"<td>{_esc(row.get('kalshi_ticker'))}</td>"
            f"<td>{_esc(row.get('poly_token'))}</td>"
            if show_all_columns
            else ""
        )

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
            f"{extra_cells}"
            f"</tr>"
        )

    extra_headers = (
        "<th>KALSHI TICKER</th><th>POLY TOKEN</th>" if show_all_columns else ""
    )
    meta = f"{len(table.rows)} markets | {stale_count} STALE (>60s)"
    return (
        f'<div class="bw-panel {panel_cls}" data-testid="panel-quotes">'
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
        f"        {extra_headers}"
        f'      </tr></thead>'
        f'      <tbody>{"".join(rows_html)}</tbody>'
        f'    </table>'
        f'  </div>'
        f'</div>'
    )


def render_orders_panel_html(
    table: TableResult,
    criteria: FilterCriteria | None = None,
    *,
    show_all_columns: bool = False,
    panel_cls: str = "bw-panel-row2",
) -> str:
    """Render Panel 2: ORDERS (from ``orders``, open orders highlighted, filter-aware)."""
    title = "ORDERS"
    if not table.has_data:
        return _render_nodata_panel(
            title,
            "orders",
            table.message,
            panel_cls,
            testid="panel-orders",
        )

    active_criteria = criteria or FilterCriteria()
    rows = filter_orders_rows(table.rows, active_criteria)
    if not rows:
        return _render_nodata_panel(
            title,
            f"0/{len(table.rows)} matched",
            f"No orders match active filters ({_active_filter_summary(active_criteria)})",
            panel_cls,
            heading="NO MATCHING ORDERS",
            testid="panel-orders",
        )

    open_count = 0
    rows_html: list[str] = []
    for row in rows:
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
        side_str = f"{str(row.get('action') or 'buy').upper()} {str(row.get('side') or '').upper()}".strip()
        wp_dec = parse_decimal(row.get("wire_price"))
        wp_str = f"{wp_dec:.3f}" if wp_dec is not None else _esc(row.get("wire_price"))
        qty_str = f"{row.get('quantity_traded', 0)}/{row.get('quantity', '—')}"
        cost_dec = parse_decimal(row.get("total_cost"))
        cost_str = (
            format_susq(cost_dec, with_unit=False) if cost_dec is not None else "—"
        )
        strat_str = str(row.get("strategy") or "").strip()
        reason_raw = row.get("error") or row.get("reason") or ""
        reason_text = (
            f"[{strat_str}] {reason_raw}".strip() if strat_str else str(reason_raw)
        )
        extra_cells = (
            f'<td class="bw-nowrap">{_esc(row.get("key"))}</td>'
            f'<td class="bw-nowrap">{_esc(row.get("sig_exchange_id"))}</td>'
            f'<td class="bw-nowrap">{_esc(row.get("sig_order_id"))}</td>'
            f'<td class="bw-num">{_esc(row.get("http_status"))}</td>'
            if show_all_columns
            else ""
        )

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
            f"{extra_cells}"
            f"</tr>"
        )

    extra_headers = (
        "<th>KEY</th><th>CONTRACT</th><th>SIG ORDER ID</th><th class=\"bw-num\">HTTP</th>"
        if show_all_columns
        else ""
    )
    meta = (
        f"{len(rows)}/{len(table.rows)} shown | {open_count} OPEN highlighted"
        if len(rows) != len(table.rows)
        else f"{len(rows)} recent | {open_count} OPEN highlighted"
    )
    return (
        f'<div class="bw-panel {panel_cls}" data-testid="panel-orders">'
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
        f"        {extra_headers}"
        f'      </tr></thead>'
        f'      <tbody>{"".join(rows_html)}</tbody>'
        f'    </table>'
        f'  </div>'
        f'</div>'
    )


def render_decisions_panel_html(
    table: TableResult,
    criteria: FilterCriteria | None = None,
    *,
    show_all_columns: bool = False,
    panel_cls: str = "bw-panel-row2",
) -> str:
    """Render Panel 3: DECISIONS (from ``decisions``, with ``reason`` column, filter-aware)."""
    title = "DECISIONS"
    if not table.has_data:
        return _render_nodata_panel(
            title,
            "decisions",
            table.message,
            panel_cls,
            testid="panel-decisions",
        )

    active_criteria = criteria or FilterCriteria()
    rows = filter_decisions_rows(table.rows, active_criteria)
    if not rows:
        return _render_nodata_panel(
            title,
            f"0/{len(table.rows)} matched",
            f"No decisions match active filters ({_active_filter_summary(active_criteria)})",
            panel_cls,
            heading="NO MATCHING DECISIONS",
            testid="panel-decisions",
        )

    rows_html: list[str] = []
    for row in rows:
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
        extra_cells = (
            f'<td class="bw-num">{_esc(row.get("id"))}</td>'
            f'<td class="bw-nowrap">{_esc(row.get("sig_exchange_id"))}</td>'
            f'<td class="bw-nowrap">{_esc(row.get("order_key"))}</td>'
            if show_all_columns
            else ""
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
            f"{extra_cells}"
            f"</tr>"
        )

    extra_headers = (
        '<th class="bw-num">ID</th><th>CONTRACT</th><th>ORDER KEY</th>'
        if show_all_columns
        else ""
    )
    meta = (
        f"{len(rows)}/{len(table.rows)} shown | includes reason column"
        if len(rows) != len(table.rows)
        else f"{len(rows)} latest | includes reason column"
    )
    return (
        f'<div class="bw-panel {panel_cls}" data-testid="panel-decisions">'
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
        f"        {extra_headers}"
        f'      </tr></thead>'
        f'      <tbody>{"".join(rows_html)}</tbody>'
        f'    </table>'
        f'  </div>'
        f'</div>'
    )


def render_events_panel_html(
    table: TableResult,
    *,
    show_all_columns: bool = False,
    panel_cls: str = "bw-panel-row2",
) -> str:
    """Render Panel 5: EVENTS (from ``events``, newest first, warnings/errors coloured)."""
    title = "EVENTS"
    if not table.has_data:
        return _render_nodata_panel(
            title,
            "events",
            table.message,
            panel_cls,
            testid="panel-events",
        )

    warn_err_count = 0
    rows_html: list[str] = []
    for row in table.rows:
        level_raw = str(row.get("level") or "info").strip().lower()
        if level_raw == "error":
            warn_err_count += 1
            row_cls = ' class="bw-row-error"'
            badge_html = '<span class="bw-badge bw-badge-error">ERROR</span>'
            msg_style = ' style="color:#FF4D5E;font-weight:600;"'
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
        id_cell = f'<td class="bw-num">{_esc(row.get("id"))}</td>' if show_all_columns else ""
        rows_html.append(
            f"<tr{row_cls}>"
            f"{id_cell}"
            f'<td class="bw-num">{_esc(ts_str)}</td>'
            f"<td>{badge_html}</td>"
            f"<td><strong>{_esc(row.get('kind'))}</strong></td>"
            f"<td{msg_style}>{_esc(row.get('message'))}</td>"
            f"</tr>"
        )

    id_header = '<th class="bw-num">ID</th>' if show_all_columns else ""
    meta = f"{len(table.rows)} newest first | {warn_err_count} warn/error"
    return (
        f'<div class="bw-panel {panel_cls}" data-testid="panel-events">'
        f'  <div class="bw-panel-header">'
        f'    <span class="bw-panel-title">{title}</span>'
        f'    <span class="bw-panel-meta">{_esc(meta)}</span>'
        f'  </div>'
        f'  <div class="bw-table-wrap">'
        f'    <table class="bw-table">'
        f'      <thead><tr>'
        f"        {id_header}"
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


def render_positions_panel_html(
    positions: ParsedPositionsResult,
    criteria: FilterCriteria | None = None,
    *,
    show_all_columns: bool = False,
    panel_cls: str = "bw-panel-tab2-top",
) -> str:
    """Render DASH-EW-2 Panel: POSITIONS from ``snapshots.positions_json``."""
    title = "POSITIONS (LATEST SNAPSHOT)"
    if positions.status == "bad_format":
        return _render_nodata_panel(
            title,
            "snapshots.positions_json",
            positions.message,
            panel_cls,
            heading="NO DATA / BAD FORMAT",
            testid="panel-positions",
        )
    if not positions.has_data:
        return _render_nodata_panel(
            title,
            "snapshots.positions_json",
            positions.message,
            panel_cls,
            heading="NO DATA AVAILABLE",
            testid="panel-positions",
        )

    active_criteria = criteria or FilterCriteria()
    rows = filter_position_rows(positions.rows, active_criteria)
    if not rows:
        return _render_nodata_panel(
            title,
            f"0/{len(positions.rows)} matched",
            f"No positions match race_key={active_criteria.race_key}",
            panel_cls,
            heading="NO MATCHING POSITIONS",
            testid="panel-positions",
        )

    rows_html: list[str] = []
    for row in rows:
        qty_str = (
            f"{int(row.quantity):,}"
            if row.quantity == row.quantity.to_integral_value()
            else f"{row.quantity:,.2f}"
        )
        price_str = f"{row.price_side:.3f}" if row.price_side is not None else "—"
        cost_str = (
            format_susq(row.total_cost, with_unit=False)
            if row.total_cost is not None
            else "—"
        )
        comm_str = (
            format_susq(row.committed, with_unit=False)
            if row.committed is not None
            else "—"
        )
        mv_str = (
            format_susq(row.market_value, with_unit=False)
            if row.market_value is not None
            else "—"
        )
        upnl_str, upnl_dir = (
            format_signed_susq(row.unrealized_pnl, with_unit=False)
            if row.unrealized_pnl is not None
            else ("—", "neutral")
        )
        upnl_cls = (
            "bw-val-positive"
            if upnl_dir == "positive"
            else "bw-val-negative"
            if upnl_dir == "negative"
            else "bw-val-neutral"
        )
        mkt_cell = (
            f'<td class="bw-nowrap">{_esc(row.sig_market_id)}</td>'
            if show_all_columns
            else ""
        )
        rows_html.append(
            f"<tr>"
            f'<td class="bw-nowrap"><strong>{_esc(row.race_key)}</strong></td>'
            f'<td class="bw-nowrap">{_esc(row.sig_exchange_id)}</td>'
            f"{mkt_cell}"
            f'<td class="bw-nowrap">{_esc(row.party)}</td>'
            f'<td class="bw-nowrap">{_esc(row.side)}</td>'
            f'<td class="bw-num">{_esc(qty_str)}</td>'
            f'<td class="bw-num">{_esc(price_str)}</td>'
            f'<td class="bw-num">{_esc(cost_str)}</td>'
            f'<td class="bw-num">{_esc(comm_str)}</td>'
            f'<td class="bw-num">{_esc(mv_str)}</td>'
            f'<td class="bw-num {upnl_cls}">{_esc(upnl_str)}</td>'
            f"</tr>"
        )

    mkt_header = "<th>MKT ID</th>" if show_all_columns else ""
    meta = (
        f"Snapshot #{positions.snapshot_id} @ {positions.snapshot_ts_ist} | "
        f"{len(rows)}/{len(positions.rows)} position(s)"
    )
    return (
        f'<div class="bw-panel {panel_cls}" data-testid="panel-positions">'
        f'  <div class="bw-panel-header">'
        f'    <span class="bw-panel-title">{title}</span>'
        f'    <span class="bw-panel-meta">{_esc(meta)}</span>'
        f'  </div>'
        f'  <div class="bw-table-wrap">'
        f'    <table class="bw-table">'
        f'      <thead><tr>'
        f'        <th>RACE</th>'
        f'        <th>CONTRACT</th>'
        f"        {mkt_header}"
        f'        <th>PTY</th>'
        f'        <th>SIDE</th>'
        f'        <th class="bw-num">QUANTITY</th>'
        f'        <th class="bw-num">PRICE_SIDE</th>'
        f'        <th class="bw-num">TOTAL_COST</th>'
        f'        <th class="bw-num">COMMITTED</th>'
        f'        <th class="bw-num">MKT VALUE</th>'
        f'        <th class="bw-num">UNREAL P&amp;L</th>'
        f'      </tr></thead>'
        f'      <tbody>{"".join(rows_html)}</tbody>'
        f'    </table>'
        f'  </div>'
        f'</div>'
    )


def render_race_pnl_panel_html(
    race_pnl: ParsedPnlResult,
    criteria: FilterCriteria | None = None,
    *,
    panel_cls: str = "bw-panel-tab2-top",
) -> str:
    """Render DASH-EW-2 Panel: PER-RACE P&L from ``snapshots.pnl_json``."""
    title = "PER-RACE P&L (LATEST SNAPSHOT)"
    if race_pnl.status == "bad_format":
        return _render_nodata_panel(
            title,
            "snapshots.pnl_json",
            race_pnl.message,
            panel_cls,
            heading="NO DATA / BAD FORMAT",
            testid="panel-race-pnl",
        )
    if not race_pnl.has_data:
        return _render_nodata_panel(
            title,
            "snapshots.pnl_json",
            race_pnl.message,
            panel_cls,
            heading="NO DATA AVAILABLE",
            testid="panel-race-pnl",
        )

    active_criteria = criteria or FilterCriteria()
    rows = filter_race_pnl_rows(race_pnl.rows, active_criteria)
    if not rows:
        return _render_nodata_panel(
            title,
            f"0/{len(race_pnl.rows)} matched",
            f"No per-race P&L entries match race_key={active_criteria.race_key}",
            panel_cls,
            heading="NO MATCHING P&L ROWS",
            testid="panel-race-pnl",
        )

    rows_html: list[str] = []
    filtered_sum = Decimal(0)
    for row in rows:
        filtered_sum += row.pnl
        pnl_str, pnl_dir = format_signed_susq(row.pnl, with_unit=True)
        pnl_cls = (
            "bw-val-positive"
            if pnl_dir == "positive"
            else "bw-val-negative"
            if pnl_dir == "negative"
            else "bw-val-neutral"
        )
        rows_html.append(
            f"<tr>"
            f'<td class="bw-nowrap"><strong>{_esc(row.race_key)}</strong></td>'
            f'<td class="bw-num {pnl_cls}">{_esc(pnl_str)}</td>'
            f"</tr>"
        )

    tot_str, tot_dir = format_signed_susq(filtered_sum, with_unit=True)
    tot_cls = (
        "bw-val-positive"
        if tot_dir == "positive"
        else "bw-val-negative"
        if tot_dir == "negative"
        else "bw-val-neutral"
    )
    rows_html.append(
        f'<tr style="border-top:2px solid #2B323D;">'
        f"<td><strong>TOTAL ({len(rows)} RACE(S))</strong></td>"
        f'<td class="bw-num {tot_cls}"><strong>{_esc(tot_str)}</strong></td>'
        f"</tr>"
    )

    meta = (
        f"Snapshot #{race_pnl.snapshot_id} @ {race_pnl.snapshot_ts_ist} | "
        f"{len(rows)}/{len(race_pnl.rows)} race(s)"
    )
    if race_pnl.source == "positions_unrealized":
        meta += " | unrealized, from SIG positions"
    return (
        f'<div class="bw-panel {panel_cls}" data-testid="panel-race-pnl">'
        f'  <div class="bw-panel-header">'
        f'    <span class="bw-panel-title">{title}</span>'
        f'    <span class="bw-panel-meta">{_esc(meta)}</span>'
        f'  </div>'
        f'  <div class="bw-table-wrap">'
        f'    <table class="bw-table">'
        f'      <thead><tr>'
        f'        <th>RACE_KEY</th>'
        f'        <th class="bw-num">P&amp;L (SUSQ)</th>'
        f'      </tr></thead>'
        f'      <tbody>{"".join(rows_html)}</tbody>'
        f'    </table>'
        f'  </div>'
        f'</div>'
    )


def render_snapshot_history_panel(
    history: SnapshotHistoryResult,
    *,
    compact_height: bool = False,
) -> None:
    """Render DASH-EW-2 Panel: History chart of Balance and Total Exposure over time (IST)."""
    title = f"SNAPSHOT HISTORY (LAST {history.window_hours:g}H — BALANCE & TOTAL EXPOSURE)"
    empty_cls = "bw-v2-panel-short" if compact_height else "bw-panel-tab2-bottom"
    if history.status == "bad_format":
        st.markdown(
            _render_nodata_panel(
                title,
                "snapshots",
                history.message,
                empty_cls,
                heading="NO DATA / BAD FORMAT",
                testid="panel-snapshot-history",
            ),
            unsafe_allow_html=True,
        )
        return

    if not history.has_data:
        st.markdown(
            _render_nodata_panel(
                title,
                "snapshots",
                history.message,
                empty_cls,
                heading="NO DATA AVAILABLE",
                testid="panel-snapshot-history",
            ),
            unsafe_allow_html=True,
        )
        return

    st.markdown(
        f'<div class="bw-panel-header" data-testid="panel-snapshot-history">'
        f'  <span class="bw-panel-title">{_esc(title)}</span>'
        f'  <span class="bw-panel-meta">{_esc(history.message)}</span>'
        f'</div>',
        unsafe_allow_html=True,
    )

    if history.status == "single_row" and len(history.points) == 1:
        pt0 = history.points[0]
        st.markdown(
            f'<div class="bw-single-snap-banner" data-testid="single-snapshot-notice">'
            f'  <span><strong>SINGLE SNAPSHOT RECORDED</strong> — Snapshot #{pt0.id} @ {_esc(pt0.ts_ist_label)} IST</span>'
            f'  <span>BALANCE: <strong>{_esc(format_susq(pt0.balance))}</strong> &nbsp;|&nbsp; '
            f'TOTAL EXPOSURE: <strong>{_esc(format_susq(pt0.committed))}</strong></span>'
            f'</div>',
            unsafe_allow_html=True,
        )

    records: list[dict[str, Any]] = []
    for idx, pt in enumerate(history.points):
        ist_label = f"{pt.ts_ist_label} (#{pt.id})" if len(history.points) <= 3 else pt.ts_ist_label
        records.append(
            {
                "order": idx,
                "Time (IST)": ist_label,
                "Metric": "Balance (SUSQ)",
                "SUSQies": float(pt.balance),
                "Snapshot ID": pt.id,
            }
        )
        records.append(
            {
                "order": idx,
                "Time (IST)": ist_label,
                "Metric": "Total Exposure / Committed (SUSQ)",
                "SUSQies": float(pt.committed),
                "Snapshot ID": pt.id,
            }
        )

    df = pd.DataFrame(records)
    color_scale = alt.Scale(
        domain=["Balance (SUSQ)", "Total Exposure / Committed (SUSQ)"],
        range=["#2BD67B", "#FFB000"],
    )
    if compact_height:
        chart_height = 175 if history.status == "single_row" else 210
    else:
        chart_height = 240 if history.status == "single_row" else 280

    chart = (
        alt.Chart(df)
        .mark_line(point=alt.OverlayMarkDef(size=70, filled=True), strokeWidth=2.4)
        .encode(
            x=alt.X(
                "Time (IST):N",
                sort=alt.EncodingSortField(field="order", op="min", order="ascending"),
                title="Time (IST)",
                axis=alt.Axis(
                    labelAngle=0,
                    labelLimit=240,
                    labelColor="#8A94A6",
                    titleColor="#FFB000",
                    gridColor="#1E232B",
                ),
            ),
            y=alt.Y(
                "SUSQies:Q",
                title="SUSQies",
                scale=alt.Scale(zero=False),
                axis=alt.Axis(
                    labelColor="#8A94A6",
                    titleColor="#FFB000",
                    gridColor="#1E232B",
                ),
            ),
            color=alt.Color(
                "Metric:N",
                scale=color_scale,
                legend=alt.Legend(
                    orient="top",
                    title=None,
                    labelLimit=360,
                    labelColor="#E8ECF2",
                ),
            ),
            tooltip=["Snapshot ID:Q", "Time (IST):N", "Metric:N", "SUSQies:Q"],
        )
        .properties(height=chart_height)
        .configure(background="#0C0E11")
        .configure_view(stroke="#1E232B")
    )
    st.altair_chart(chart, width="stretch")


def render_db_busy_banner_html(snapshot: MonitorSnapshot) -> str:
    """Render a top-level DB BUSY banner when SQLite is locked beyond busy_timeout."""
    if not snapshot.db_busy:
        return ""
    detail = (
        snapshot.db_busy_message
        or "SQLite database is temporarily locked by writer."
    )
    return (
        f'<div class="bw-db-busy-banner" data-testid="db-busy-banner">'
        f'  <span><strong>DB BUSY</strong> — {_esc(detail)}</span>'
        f'  <span>AUTO-RETRYING ON NEXT REFRESH</span>'
        f'</div>'
    )


def render_stop_confirmation_html() -> str:
    """Render the confirmation prompt banner shown when STOP BOT is clicked."""
    return (
        f'<div class="bw-stop-confirm-banner" data-testid="stop-confirm-banner">'
        f'  <span class="bw-stop-confirm-badge">CONFIRM STOP</span>'
        f'  <span class="bw-stop-confirm-msg">'
        f"    This creates <code>{_esc(STOP_CONTRACT_PATH)}</code> and halts trading. Confirm?"
        f"  </span>"
        f'  <span class="bw-stop-confirm-note">3s AUTO-REFRESH PAUSED</span>'
        f"</div>"
    )


def render_stop_result_banner_html(result: StopWriteResult) -> str:
    """Render the success or failure result banner after a STOP write attempt."""
    if result.ok:
        return (
            f'<div class="bw-stop-result-banner bw-stop-result-ok" data-testid="stop-result-banner">'
            f'  <span><strong>STOP FILE PRESENT</strong> — Created <code>{_esc(result.stop_path)}</code> '
            f'at <strong>{_esc(result.ts_ist)}</strong> (payload: <code>{_esc(result.content)}</code>)</span>'
            f'  <span>MANUAL REMOVAL OF bot/state/STOP REQUIRED TO CLEAR</span>'
            f"</div>"
        )
    if result.already_existed:
        return (
            f'<div class="bw-stop-result-banner bw-stop-result-warn" data-testid="stop-result-banner">'
            f'  <span><strong>STOP FILE PRESENT</strong> — {_esc(result.message)}</span>'
            f'  <span>Checked at {_esc(result.ts_ist)}</span>'
            f"</div>"
        )
    return (
        f'<div class="bw-stop-result-banner bw-stop-result-err" data-testid="stop-result-banner">'
        f'  <span><strong>{_esc(result.message)}</strong></span>'
        f'  <span>Target: <code>{_esc(result.stop_path)}</code> | Attempted at {_esc(result.ts_ist)}</span>'
        f"</div>"
    )


def render_ui_error_banner_html(exc: BaseException) -> str:
    """Render the top-level red fallback banner if v2 UI rendering raises an exception."""
    return (
        f'<div class="bw-ui-error-banner" data-testid="ui-error-banner">'
        f'  <span><strong>UI ERROR, switch to legacy</strong> — {_esc(type(exc).__name__)}: {_esc(exc)}</span>'
        f'  <a href="?ui=legacy" class="bw-ui-error-link">Switch to Legacy UI (?ui=legacy)</a>'
        f"</div>"
    )


def render_ui_footer_html(ui_mode: str) -> str:
    """Render the small user-visible footer identifying 'UI v2' or 'UI legacy'."""
    if ui_mode == "legacy":
        return (
            '<div class="bw-ui-footer" data-testid="ui-mode-footer">'
            "  <span><strong>UI legacy</strong> &nbsp;•&nbsp; Read-only SQLite (mode=ro)</span>"
            '  <span>Switch to simplified view: <a href="?ui=v2">?ui=v2</a></span>'
            "</div>"
        )
    return (
        '<div class="bw-ui-footer" data-testid="ui-mode-footer">'
        "  <span><strong>UI v2</strong> &nbsp;•&nbsp; Read-only SQLite (mode=ro)</span>"
        '  <span>One-step rollback: <a href="?ui=legacy">?ui=legacy</a> or <code>BW_UI=legacy</code></span>'
        "</div>"
    )


def _open_stop_confirmation() -> None:
    """Open the STOP confirmation step and pause auto-refresh without writing anything."""
    st.session_state["confirm_open"] = True
    st.session_state["stop_write_result"] = None


def _cancel_stop_action() -> None:
    """Cancel the STOP confirmation step without writing anything."""
    st.session_state["confirm_open"] = False


def _confirm_stop_action() -> None:
    """Execute the confirmed STOP file creation once and store the result."""
    if not st.session_state.get("confirm_open", False):
        return
    st.session_state["confirm_open"] = False
    st.session_state["stop_write_result"] = write_stop_file()


def _render_sidebar_filters(snapshot: MonitorSnapshot) -> FilterCriteria:
    """Render sidebar filter controls (`race_key`, `strategy`, `status/verdict`) and column toggle."""
    for key in ("filter_race_key", "filter_strategy", "filter_status_verdict"):
        if key not in st.session_state:
            st.session_state[key] = "All"
    if "show_all_columns" not in st.session_state:
        st.session_state["show_all_columns"] = False

    current = FilterCriteria(
        race_key=str(st.session_state.get("filter_race_key", "All")),
        strategy=str(st.session_state.get("filter_strategy", "All")),
        status_verdict=str(st.session_state.get("filter_status_verdict", "All")),
    )
    race_opts, strat_opts, sv_opts = collect_filter_options(snapshot, current)

    with st.sidebar:
        st.markdown("### MONITOR FILTERS")
        race_val = st.selectbox(
            "race_key",
            options=race_opts,
            key="filter_race_key",
        )
        strat_val = st.selectbox(
            "strategy",
            options=strat_opts,
            key="filter_strategy",
        )
        sv_val = st.selectbox(
            "status / verdict",
            options=sv_opts,
            key="filter_status_verdict",
        )
        st.toggle(
            "Show all columns",
            key="show_all_columns",
        )

    return FilterCriteria(
        race_key=str(race_val),
        strategy=str(strat_val),
        status_verdict=str(sv_val),
    )


def render_monitor_body_legacy(
    snapshot: MonitorSnapshot,
    refresh_paused: bool = False,
    criteria: FilterCriteria | None = None,
) -> None:
    """Render the exact pre-simplification Legacy UI (`BW_UI=legacy` or `?ui=legacy`)."""
    active_criteria = criteria or FilterCriteria()
    stop_result = st.session_state.get("stop_write_result")
    stop_active = bool(
        snapshot.heartbeat.stop_file_exists
        or (
            isinstance(stop_result, StopWriteResult)
            and (stop_result.ok or stop_result.already_existed)
            and stop_result.stop_path.is_file()
        )
    )

    if snapshot.db_busy:
        st.markdown(render_db_busy_banner_html(snapshot), unsafe_allow_html=True)

    banner_col, stop_col = st.columns([6.4, 1.0], gap="small")
    with banner_col:
        st.markdown(
            render_heartbeat_banner_html(
                snapshot.heartbeat,
                snapshot.fetched_at_ist,
                refresh_paused,
                criteria=active_criteria,
            ),
            unsafe_allow_html=True,
        )
    with stop_col:
        if stop_active:
            st.button(
                "STOP ACTIVE",
                key="stop_bot_btn",
                disabled=True,
                width="stretch",
            )
        else:
            st.button(
                "STOP BOT",
                key="stop_bot_btn",
                disabled=refresh_paused,
                type="primary",
                on_click=_open_stop_confirmation,
                width="stretch",
            )

    if refresh_paused and not stop_active:
        msg_col, conf_col, canc_col = st.columns([4.8, 0.8, 0.8], gap="small")
        with msg_col:
            st.markdown(render_stop_confirmation_html(), unsafe_allow_html=True)
        with conf_col:
            st.button(
                "Confirm",
                key="confirm_stop_btn",
                type="primary",
                on_click=_confirm_stop_action,
                width="stretch",
            )
        with canc_col:
            st.button(
                "Cancel",
                key="cancel_stop_btn",
                on_click=_cancel_stop_action,
                width="stretch",
            )

    if isinstance(stop_result, StopWriteResult):
        st.markdown(
            render_stop_result_banner_html(stop_result),
            unsafe_allow_html=True,
        )

    st.markdown(render_hero_row_html(snapshot.hero), unsafe_allow_html=True)

    tab_live, tab_pos_hist = st.tabs(
        ["LIVE MONITOR", "POSITIONS, P&L & HISTORY (6H)"]
    )

    with tab_live:
        row1_left, row1_right = st.columns([1.0, 1.55], gap="small")
        with row1_left:
            st.markdown(
                render_exposure_panel_html(snapshot.exposure_table),
                unsafe_allow_html=True,
            )
        with row1_right:
            st.markdown(
                render_quotes_panel_html(
                    snapshot.quotes_table, snapshot.fetched_at_utc
                ),
                unsafe_allow_html=True,
            )

        row2_col1, row2_col2, row2_col3 = st.columns([1.15, 1.15, 0.95], gap="small")
        with row2_col1:
            st.markdown(
                render_orders_panel_html(snapshot.orders_table, active_criteria),
                unsafe_allow_html=True,
            )
        with row2_col2:
            st.markdown(
                render_decisions_panel_html(snapshot.decisions_table, active_criteria),
                unsafe_allow_html=True,
            )
        with row2_col3:
            st.markdown(
                render_events_panel_html(snapshot.events_table),
                unsafe_allow_html=True,
            )

    with tab_pos_hist:
        pos_col, pnl_col = st.columns([1.45, 1.0], gap="small")
        with pos_col:
            st.markdown(
                render_positions_panel_html(
                    snapshot.positions,
                    active_criteria,
                    show_all_columns=True,
                ),
                unsafe_allow_html=True,
            )
        with pnl_col:
            st.markdown(
                render_race_pnl_panel_html(snapshot.race_pnl, active_criteria),
                unsafe_allow_html=True,
            )
        render_snapshot_history_panel(snapshot.snapshot_history)


def render_monitor_body_v2(
    snapshot: MonitorSnapshot,
    refresh_paused: bool = False,
    criteria: FilterCriteria | None = None,
    show_all_columns: bool = False,
) -> None:
    """Render the simplified v2 layout: Status Strip -> 4 Big Numbers -> Needs Attention -> 4 Detail Tabs."""
    active_criteria = criteria or FilterCriteria()
    stop_result = st.session_state.get("stop_write_result")
    stop_active = bool(
        snapshot.heartbeat.stop_file_exists
        or (
            isinstance(stop_result, StopWriteResult)
            and (stop_result.ok or stop_result.already_existed)
            and stop_result.stop_path.is_file()
        )
    )

    if snapshot.db_busy:
        st.markdown(render_db_busy_banner_html(snapshot), unsafe_allow_html=True)

    # 1. Status Strip with STOP BOT button visually separated at the far right
    strip_col, stop_col = st.columns([6.5, 1.0], gap="medium")
    with strip_col:
        st.markdown(
            render_status_strip_v2_html(
                snapshot,
                refresh_paused,
                criteria=active_criteria,
            ),
            unsafe_allow_html=True,
        )
    with stop_col:
        if stop_active:
            st.button(
                "STOP ACTIVE",
                key="stop_bot_btn",
                disabled=True,
                width="stretch",
            )
        else:
            st.button(
                "STOP BOT",
                key="stop_bot_btn",
                disabled=refresh_paused,
                type="primary",
                on_click=_open_stop_confirmation,
                width="stretch",
            )

    if refresh_paused and not stop_active:
        msg_col, conf_col, canc_col = st.columns([4.8, 0.8, 0.8], gap="small")
        with msg_col:
            st.markdown(render_stop_confirmation_html(), unsafe_allow_html=True)
        with conf_col:
            st.button(
                "Confirm",
                key="confirm_stop_btn",
                type="primary",
                on_click=_confirm_stop_action,
                width="stretch",
            )
        with canc_col:
            st.button(
                "Cancel",
                key="cancel_stop_btn",
                on_click=_cancel_stop_action,
                width="stretch",
            )

    if isinstance(stop_result, StopWriteResult):
        st.markdown(
            render_stop_result_banner_html(stop_result),
            unsafe_allow_html=True,
        )

    # 2. Four Big Numbers Only (Balance, Net P&L, Open Orders, Exposure with thin bar)
    st.markdown(render_hero_row_v2_html(snapshot), unsafe_allow_html=True)

    # 3. Needs Attention Box (directly under the numbers)
    st.markdown(render_needs_attention_html(snapshot), unsafe_allow_html=True)

    # 4. Four Detail Tabs below ("Orders & Decisions", "Quotes & Exposure", "Positions & History", "Events")
    tab_ord_dec, tab_quo_exp, tab_pos_hist, tab_events = st.tabs(
        [
            "Orders & Decisions",
            "Quotes & Exposure",
            "Positions & History",
            "Events",
        ]
    )

    with tab_ord_dec:
        ord_col, dec_col = st.columns([1.0, 1.0], gap="medium")
        with ord_col:
            st.markdown(
                render_orders_panel_html(
                    snapshot.orders_table,
                    active_criteria,
                    show_all_columns=show_all_columns,
                    panel_cls="bw-v2-panel",
                ),
                unsafe_allow_html=True,
            )
        with dec_col:
            st.markdown(
                render_decisions_panel_html(
                    snapshot.decisions_table,
                    active_criteria,
                    show_all_columns=show_all_columns,
                    panel_cls="bw-v2-panel",
                ),
                unsafe_allow_html=True,
            )

    with tab_quo_exp:
        quo_col, exp_col = st.columns([1.45, 1.0], gap="medium")
        with quo_col:
            st.markdown(
                render_quotes_panel_html(
                    snapshot.quotes_table,
                    snapshot.fetched_at_utc,
                    show_all_columns=show_all_columns,
                    panel_cls="bw-v2-panel",
                ),
                unsafe_allow_html=True,
            )
        with exp_col:
            st.markdown(
                render_exposure_panel_html(
                    snapshot.exposure_table,
                    panel_cls="bw-v2-panel",
                ),
                unsafe_allow_html=True,
            )

    with tab_pos_hist:
        pos_col, pnl_col = st.columns([1.4, 1.0], gap="medium")
        with pos_col:
            st.markdown(
                render_positions_panel_html(
                    snapshot.positions,
                    active_criteria,
                    show_all_columns=show_all_columns,
                    panel_cls="bw-v2-panel-short",
                ),
                unsafe_allow_html=True,
            )
        with pnl_col:
            st.markdown(
                render_race_pnl_panel_html(
                    snapshot.race_pnl,
                    active_criteria,
                    panel_cls="bw-v2-panel-short",
                ),
                unsafe_allow_html=True,
            )
        render_snapshot_history_panel(
            snapshot.snapshot_history,
            compact_height=True,
        )

    with tab_events:
        st.markdown(
            render_events_panel_html(
                snapshot.events_table,
                show_all_columns=show_all_columns,
                panel_cls="bw-v2-panel",
            ),
            unsafe_allow_html=True,
        )


def render_monitor_body(
    snapshot: MonitorSnapshot,
    refresh_paused: bool = False,
    criteria: FilterCriteria | None = None,
    ui_mode: str | None = None,
) -> None:
    """Render the monitor in ``v2`` (default) or ``legacy`` mode with a top-level error boundary."""
    mode = ui_mode or resolve_ui_mode()
    show_all = bool(st.session_state.get("show_all_columns", False))

    if mode == "legacy":
        render_monitor_body_legacy(
            snapshot,
            refresh_paused=refresh_paused,
            criteria=criteria,
        )
        st.markdown(render_ui_footer_html("legacy"), unsafe_allow_html=True)
        return

    try:
        render_monitor_body_v2(
            snapshot,
            refresh_paused=refresh_paused,
            criteria=criteria,
            show_all_columns=show_all,
        )
    except Exception as exc:  # noqa: BLE001
        st.markdown(render_ui_error_banner_html(exc), unsafe_allow_html=True)

    st.markdown(render_ui_footer_html("v2"), unsafe_allow_html=True)


def _current_filter_criteria() -> FilterCriteria:
    """Read current FilterCriteria from st.session_state."""
    return FilterCriteria(
        race_key=str(st.session_state.get("filter_race_key", "All")),
        strategy=str(st.session_state.get("filter_strategy", "All")),
        status_verdict=str(st.session_state.get("filter_status_verdict", "All")),
    )


def main() -> None:
    st.set_page_config(
        page_title="Bellwether EW Monitor",
        page_icon="⚡",
        layout="wide",
        initial_sidebar_state="expanded",
    )

    css = _load_css()
    if css:
        st.markdown(f"<style>{css}</style>", unsafe_allow_html=True)

    if "confirm_open" not in st.session_state:
        st.session_state["confirm_open"] = False
    if "stop_write_result" not in st.session_state:
        st.session_state["stop_write_result"] = None

    initial_snapshot = load_monitor_snapshot(history_hours=DEFAULT_HISTORY_HOURS)
    _render_sidebar_filters(initial_snapshot)

    confirm_open = bool(st.session_state.get("confirm_open", False))
    st.session_state["_fragment_confirm_bound"] = confirm_open
    run_every = None if confirm_open else f"{AUTO_REFRESH_SECONDS}s"

    @st.fragment(run_every=run_every)
    def _live_monitor_fragment() -> None:
        live_confirm = bool(st.session_state.get("confirm_open", False))
        if st.session_state.get("_fragment_confirm_bound") != live_confirm:
            st.session_state["_fragment_confirm_bound"] = live_confirm
            st.rerun()
        snapshot = load_monitor_snapshot(history_hours=DEFAULT_HISTORY_HOURS)
        criteria = _current_filter_criteria()
        render_monitor_body(
            snapshot,
            refresh_paused=live_confirm,
            criteria=criteria,
        )

    _live_monitor_fragment()


if __name__ == "__main__":
    main()
