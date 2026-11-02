"""Read-only SQLite data layer for the Bellwether election-week monitor.

Follows ``bot/CONTRACT.md`` and ``AGENTS.md``:
- Opens ``bot/state/bellwether.db`` strictly in read-only URI mode (``mode=ro``).
- Uses the exact table and column names from ``bot/CONTRACT.md``.
- Never creates, modifies, or deletes database or state files.
- Never imports ``engine/`` or ``bot/``, never reads ``.env``, and makes no network calls.
- Returns explicit ``no data`` states when the file, a table, a column, or rows are missing.
"""

from __future__ import annotations

import json
import os
import sqlite3
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any
from urllib.parse import quote

HEARTBEAT_STALE_SECONDS: float = 30.0
QUOTE_STALE_SECONDS: float = 60.0
IST_TZ = timezone(timedelta(hours=5, minutes=30), name="IST")

OPEN_ORDER_STATUSES: frozenset[str] = frozenset(
    {"journaled", "sent", "resting", "partial"}
)

CONTRACT_SCHEMA: dict[str, tuple[str, ...]] = {
    "heartbeat": (
        "id",
        "ts_utc",
        "mode",
        "version",
        "loop_ms",
        "reads_last_min",
        "writes_last_min",
        "paused",
        "pause_reason",
        "stop_seen",
        "balance",
        "start_balance",
        "committed",
        "scale",
        "cap_total",
        "cap_per_race",
        "min_cash_reserve",
        "edge_now",
        "election_night",
    ),
    "orders": (
        "key",
        "created_utc",
        "updated_utc",
        "race_key",
        "sig_market_id",
        "sig_exchange_id",
        "side",
        "action",
        "price_side",
        "wire_price",
        "quantity",
        "expires_utc",
        "strategy",
        "reason",
        "status",
        "sig_order_id",
        "quantity_traded",
        "total_cost",
        "http_status",
        "error",
        "response_json",
    ),
    "decisions": (
        "id",
        "ts_utc",
        "race_key",
        "sig_exchange_id",
        "party",
        "strategy",
        "sig_bid",
        "sig_ask",
        "kalshi_mid",
        "poly_mid",
        "fair",
        "edge",
        "verdict",
        "side",
        "price_side",
        "quantity",
        "reason",
        "order_key",
    ),
    "quotes_latest": (
        "sig_exchange_id",
        "race_key",
        "party",
        "ts_utc",
        "sig_bid",
        "sig_ask",
        "sig_last",
        "kalshi_ticker",
        "kalshi_bid",
        "kalshi_ask",
        "kalshi_ts_utc",
        "poly_token",
        "poly_bid",
        "poly_ask",
        "poly_ts_utc",
        "fair",
        "edge",
    ),
    "exposure": (
        "race_key",
        "ts_utc",
        "committed",
        "cap",
    ),
    "snapshots": (
        "id",
        "ts_utc",
        "balance",
        "committed",
        "open_orders",
        "positions_json",
        "pnl_json",
    ),
    "events": (
        "id",
        "ts_utc",
        "level",
        "kind",
        "message",
    ),
}


@dataclass(frozen=True)
class TableResult:
    """Result of querying a contract table in read-only mode."""

    table: str
    status: str  # "ok" | "empty" | "missing_file" | "missing_table" | "missing_columns" | "db_error"
    rows: list[dict[str, Any]] = field(default_factory=list)
    missing_columns: tuple[str, ...] = ()
    message: str = ""

    @property
    def has_data(self) -> bool:
        return self.status == "ok" and len(self.rows) > 0


@dataclass(frozen=True)
class HeartbeatState:
    """Evaluated heartbeat banner state and control flags."""

    status: str  # "ALIVE" | "STALE" | "NO HEARTBEAT"
    age_seconds: float | None
    age_display: str
    ts_ist_display: str
    stop_file_exists: bool
    paused_file_exists: bool
    paused_file_reason: str | None
    db_paused: bool
    db_pause_reason: str | None
    db_stop_seen: bool
    mode: str
    version: str
    loop_ms: str
    reads_last_min: str
    writes_last_min: str
    election_night: bool
    detail_message: str


@dataclass(frozen=True)
class HeroMetrics:
    """Hero row metrics derived from snapshots, exposure, orders, and events."""

    balance_display: str
    balance_sub: str
    balance_available: bool
    pnl_display: str
    pnl_sub: str
    pnl_direction: str  # "positive" | "negative" | "neutral" | "no_data"
    pnl_available: bool
    open_orders_display: str
    open_orders_sub: str
    open_orders_available: bool
    exposure_display: str
    exposure_sub: str
    exposure_available: bool
    last_event_display: str
    last_event_sub: str
    last_event_available: bool


@dataclass(frozen=True)
class MonitorSnapshot:
    """Complete read-only snapshot of all tables and derived dashboard states."""

    db_path: Path
    state_dir: Path
    fetched_at_utc: datetime
    fetched_at_ist: str
    heartbeat_table: TableResult
    snapshots_table: TableResult
    exposure_table: TableResult
    orders_table: TableResult
    decisions_table: TableResult
    quotes_table: TableResult
    events_table: TableResult
    heartbeat: HeartbeatState
    hero: HeroMetrics


def default_repo_root() -> Path:
    """Return the repository root containing ``dashboard/``."""
    return Path(__file__).resolve().parent.parent


def resolve_paths(
    db_path: str | Path | None = None,
    state_dir: str | Path | None = None,
) -> tuple[Path, Path]:
    """Resolve the SQLite DB path and state directory without touching disk."""
    env_db = os.environ.get("BELLWETHER_DB_PATH", "").strip()
    env_state = os.environ.get("BELLWETHER_STATE_DIR", "").strip()

    resolved_state: Path | None = None
    if state_dir is not None and str(state_dir).strip():
        resolved_state = Path(state_dir).expanduser().resolve()
    elif env_state:
        resolved_state = Path(env_state).expanduser().resolve()

    if db_path is not None and str(db_path).strip():
        resolved_db = Path(db_path).expanduser().resolve()
    elif env_db:
        resolved_db = Path(env_db).expanduser().resolve()
    elif resolved_state is not None:
        resolved_db = (resolved_state / "bellwether.db").resolve()
    else:
        resolved_db = (default_repo_root() / "bot" / "state" / "bellwether.db").resolve()

    if resolved_state is None:
        resolved_state = resolved_db.parent

    return resolved_db, resolved_state


def build_readonly_uri(db_path: Path) -> str:
    """Construct a strict SQLite ``mode=ro`` URI for a local filesystem path."""
    posix_path = db_path.resolve().as_posix()
    if not posix_path.startswith("/"):
        posix_path = "/" + posix_path
    encoded_path = quote(posix_path, safe="/:")
    return f"file:{encoded_path}?mode=ro"


def connect_readonly(db_path: Path) -> sqlite3.Connection:
    """Open ``db_path`` in read-only mode (`mode=ro`) without creating the file."""
    uri = build_readonly_uri(db_path)
    conn = sqlite3.connect(uri, uri=True, timeout=2.0)
    conn.row_factory = sqlite3.Row
    return conn


def parse_utc_timestamp(value: Any) -> datetime | None:
    """Parse an ISO-8601 UTC timestamp string into an aware UTC datetime."""
    if value is None:
        return None
    raw = str(value).strip()
    if not raw:
        return None
    normalized = raw[:-1] + "+00:00" if raw.endswith(("Z", "z")) else raw
    try:
        dt = datetime.fromisoformat(normalized)
    except ValueError:
        return None
    if dt.tzinfo is None:
        return dt.replace(tzinfo=UTC)
    return dt.astimezone(UTC)


def format_ist_timestamp(value: Any, *, include_date: bool = False) -> str:
    """Format an ISO-8601 UTC timestamp in IST (UTC+05:30)."""
    dt = parse_utc_timestamp(value)
    if dt is None:
        return "—"
    ist_dt = dt.astimezone(IST_TZ)
    if include_date:
        return ist_dt.strftime("%Y-%m-%d %H:%M:%S IST")
    return ist_dt.strftime("%H:%M:%S IST")


def compute_age_seconds(value: Any, now_utc: datetime) -> float | None:
    """Return age in non-negative seconds for a UTC timestamp string."""
    dt = parse_utc_timestamp(value)
    if dt is None:
        return None
    return max(0.0, (now_utc - dt).total_seconds())


def parse_decimal(value: Any) -> Decimal | None:
    """Safely parse a string/number into a ``Decimal``, returning ``None`` on invalid input."""
    if value is None or isinstance(value, bool):
        return None
    raw = str(value).strip()
    if not raw:
        return None
    try:
        dec = Decimal(raw)
    except InvalidOperation:
        return None
    if not dec.is_finite():
        return None
    return dec


def format_susq(value: Any, *, decimals: int = 2, with_unit: bool = True) -> str:
    """Format a SUSQies decimal string without fabricating zeros for missing values."""
    dec = value if isinstance(value, Decimal) else parse_decimal(value)
    if dec is None:
        return "NO DATA"
    quant = Decimal(1) if decimals <= 0 else Decimal(f"1.{'0' * decimals}")
    rounded = dec.quantize(quant)
    unit = " SUSQ" if with_unit else ""
    return f"{rounded:,.{decimals}f}{unit}"


def format_signed_susq(
    value: Decimal | None,
    *,
    decimals: int = 2,
    with_unit: bool = True,
) -> tuple[str, str]:
    """Format a signed P&L value with explicit +/- and ▲/▼ indicators."""
    if value is None:
        return "NO DATA", "no_data"
    quant = Decimal(1) if decimals <= 0 else Decimal(f"1.{'0' * decimals}")
    rounded = value.quantize(quant)
    unit = " SUSQ" if with_unit else ""
    if rounded > 0:
        return f"+{rounded:,.{decimals}f}{unit} ▲", "positive"
    if rounded < 0:
        return f"−{abs(rounded):,.{decimals}f}{unit} ▼", "negative"
    return f"0.{'0' * decimals}{unit}", "neutral"


def format_price_prob(value: Any) -> str:
    """Format a [0, 1] contract price string (e.g. ``0.435`` -> ``0.435 (43.5¢)``)."""
    dec = parse_decimal(value)
    if dec is None:
        return "—"
    cents = (dec * Decimal(100)).quantize(Decimal("0.1"))
    return f"{dec.normalize():f} ({cents}¢)" if dec != 0 else "0.000 (0.0¢)"


def format_edge(value: Any) -> tuple[str, str]:
    """Format edge as signed probability / cents with direction."""
    dec = parse_decimal(value)
    if dec is None:
        return "—", "neutral"
    cents = (dec * Decimal(100)).quantize(Decimal("0.1"))
    if dec > 0:
        return f"+{dec:.3f} (+{cents}¢) ▲", "positive"
    if dec < 0:
        return f"−{abs(dec):.3f} (−{abs(cents)}¢) ▼", "negative"
    return "0.000 (0.0¢)", "neutral"


def _read_paused_file_reason(paused_path: Path) -> str | None:
    """Read optional pause note from ``bot/state/PAUSED`` safely."""
    if not paused_path.is_file():
        return None
    try:
        content = paused_path.read_text(encoding="utf-8", errors="replace").strip()
    except OSError:
        return None
    if not content:
        return None
    first_line = content.splitlines()[0].strip()
    return first_line[:160] if first_line else None


def _inspect_and_query_table(
    conn: sqlite3.Connection,
    table: str,
    query_sql: str,
    params: tuple[Any, ...] = (),
) -> TableResult:
    """Verify table & column existence against ``CONTRACT_SCHEMA`` before querying."""
    expected_cols = CONTRACT_SCHEMA[table]
    try:
        cur = conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name = ?",
            (table,),
        )
        if cur.fetchone() is None:
            return TableResult(
                table=table,
                status="missing_table",
                message=f"NO DATA — Table '{table}' is missing from bellwether.db",
            )

        pragma_cur = conn.execute(f"PRAGMA table_info({table})")
        present_cols = {str(row["name"]) for row in pragma_cur.fetchall()}
        missing = tuple(col for col in expected_cols if col not in present_cols)
        if missing:
            missing_list = ", ".join(missing)
            return TableResult(
                table=table,
                status="missing_columns",
                missing_columns=missing,
                message=f"NO DATA — Table '{table}' is missing contract columns: {missing_list}",
            )

        data_cur = conn.execute(query_sql, params)
        rows = [dict(row) for row in data_cur.fetchall()]
        if not rows:
            return TableResult(
                table=table,
                status="empty",
                rows=[],
                message=f"NO DATA — No rows recorded in '{table}'",
            )
        return TableResult(
            table=table,
            status="ok",
            rows=rows,
            message=f"{len(rows)} row(s)",
        )
    except sqlite3.Error as exc:
        return TableResult(
            table=table,
            status="db_error",
            message=f"NO DATA — SQLite read error on '{table}': {exc}",
        )


def _evaluate_heartbeat(
    heartbeat_table: TableResult,
    state_dir: Path,
    now_utc: datetime,
    stale_threshold_seconds: float = HEARTBEAT_STALE_SECONDS,
) -> HeartbeatState:
    """Evaluate heartbeat liveness (`ALIVE` / `STALE` / `NO HEARTBEAT`) and flags."""
    stop_path = state_dir / "STOP"
    paused_path = state_dir / "PAUSED"
    stop_file_exists = stop_path.exists()
    paused_file_exists = paused_path.exists()
    paused_file_reason = _read_paused_file_reason(paused_path)

    if not heartbeat_table.has_data:
        return HeartbeatState(
            status="NO HEARTBEAT",
            age_seconds=None,
            age_display="NO DATA",
            ts_ist_display="NO DATA",
            stop_file_exists=stop_file_exists,
            paused_file_exists=paused_file_exists,
            paused_file_reason=paused_file_reason,
            db_paused=False,
            db_pause_reason=None,
            db_stop_seen=False,
            mode="NO DATA",
            version="—",
            loop_ms="—",
            reads_last_min="—",
            writes_last_min="—",
            election_night=False,
            detail_message=heartbeat_table.message or "NO DATA — No heartbeat row",
        )

    row = heartbeat_table.rows[0]
    age = compute_age_seconds(row.get("ts_utc"), now_utc)
    ts_ist = format_ist_timestamp(row.get("ts_utc"), include_date=False)
    mode_val = str(row.get("mode") or "").strip().upper() or "NO DATA"
    version_val = str(row.get("version") or "").strip() or "—"
    loop_ms_val = f"{row['loop_ms']} ms" if row.get("loop_ms") is not None else "—"
    reads_val = (
        f"{row['reads_last_min']}/60" if row.get("reads_last_min") is not None else "—"
    )
    writes_val = (
        f"{row['writes_last_min']}/15"
        if row.get("writes_last_min") is not None
        else "—"
    )
    db_paused = bool(int(row["paused"])) if row.get("paused") is not None else False
    db_pause_reason = (
        str(row["pause_reason"]).strip()
        if row.get("pause_reason") is not None and str(row["pause_reason"]).strip()
        else None
    )
    db_stop_seen = (
        bool(int(row["stop_seen"])) if row.get("stop_seen") is not None else False
    )
    election_night = (
        bool(int(row["election_night"]))
        if row.get("election_night") is not None
        else False
    )

    if age is None:
        return HeartbeatState(
            status="NO HEARTBEAT",
            age_seconds=None,
            age_display="INVALID TS",
            ts_ist_display=ts_ist,
            stop_file_exists=stop_file_exists,
            paused_file_exists=paused_file_exists,
            paused_file_reason=paused_file_reason,
            db_paused=db_paused,
            db_pause_reason=db_pause_reason,
            db_stop_seen=db_stop_seen,
            mode=mode_val,
            version=version_val,
            loop_ms=loop_ms_val,
            reads_last_min=reads_val,
            writes_last_min=writes_val,
            election_night=election_night,
            detail_message="Heartbeat timestamp missing or unparseable",
        )

    age_int = round(age)
    age_display = f"{age_int}s"
    status = "ALIVE" if age < stale_threshold_seconds else "STALE"
    detail = (
        f"Heartbeat age {age:.1f}s (< {int(stale_threshold_seconds)}s threshold)"
        if status == "ALIVE"
        else f"Heartbeat age {age:.1f}s (≥ {int(stale_threshold_seconds)}s threshold)"
    )
    return HeartbeatState(
        status=status,
        age_seconds=age,
        age_display=age_display,
        ts_ist_display=ts_ist,
        stop_file_exists=stop_file_exists,
        paused_file_exists=paused_file_exists,
        paused_file_reason=paused_file_reason,
        db_paused=db_paused,
        db_pause_reason=db_pause_reason,
        db_stop_seen=db_stop_seen,
        mode=mode_val,
        version=version_val,
        loop_ms=loop_ms_val,
        reads_last_min=reads_val,
        writes_last_min=writes_val,
        election_night=election_night,
        detail_message=detail,
    )


def _extract_pnl_from_snapshot(
    snapshot_row: dict[str, Any],
    heartbeat_row: dict[str, Any] | None,
) -> tuple[Decimal | None, str]:
    """Extract P&L from ``snapshots.pnl_json`` or ``balance - start_balance``."""
    raw_pnl_json = snapshot_row.get("pnl_json")
    if raw_pnl_json is not None and str(raw_pnl_json).strip():
        try:
            parsed = json.loads(str(raw_pnl_json))
        except (ValueError, TypeError):
            parsed = None

        if isinstance(parsed, (int, float, str)):
            dec = parse_decimal(parsed)
            if dec is not None:
                return dec, "from snapshots.pnl_json"
        elif isinstance(parsed, dict) and parsed:
            for key in ("total", "total_pnl", "pnl", "net", "net_pnl"):
                if key in parsed:
                    dec = parse_decimal(parsed[key])
                    if dec is not None:
                        return dec, f"from snapshots.pnl_json[{key}]"
            if "realized" in parsed or "unrealized" in parsed:
                r_dec = parse_decimal(parsed.get("realized")) or Decimal(0)
                u_dec = parse_decimal(parsed.get("unrealized")) or Decimal(0)
                if (
                    parse_decimal(parsed.get("realized")) is not None
                    or parse_decimal(parsed.get("unrealized")) is not None
                ):
                    return r_dec + u_dec, "realized + unrealized"
            numeric_vals = [parse_decimal(v) for v in parsed.values()]
            valid_vals = [v for v in numeric_vals if v is not None]
            if valid_vals and len(valid_vals) == len(parsed):
                return sum(valid_vals, Decimal(0)), f"sum across {len(valid_vals)} races"

    snap_bal = parse_decimal(snapshot_row.get("balance"))
    start_bal = (
        parse_decimal(heartbeat_row.get("start_balance"))
        if heartbeat_row is not None
        else None
    )
    if snap_bal is not None and start_bal is not None:
        return snap_bal - start_bal, f"vs start {format_susq(start_bal)}"

    return None, "pnl_json empty & no start_balance"


def _compute_hero_metrics(
    snapshots_table: TableResult,
    heartbeat_table: TableResult,
    exposure_table: TableResult,
    orders_table: TableResult,
    events_table: TableResult,
    now_utc: datetime,
) -> HeroMetrics:
    """Compute the 5 hero cards without fabricating zeros for missing data."""
    hb_row = heartbeat_table.rows[0] if heartbeat_table.has_data else None
    snap_row = snapshots_table.rows[0] if snapshots_table.has_data else None

    # 1 & 2. Balance and P&L from latest snapshots row
    if snap_row is not None:
        bal_dec = parse_decimal(snap_row.get("balance"))
        snap_ts = format_ist_timestamp(snap_row.get("ts_utc"))
        if bal_dec is not None:
            balance_display = format_susq(bal_dec)
            balance_sub = f"Snapshot #{snap_row.get('id')} @ {snap_ts}"
            balance_available = True
        else:
            balance_display = "NO DATA"
            balance_sub = f"Snapshot #{snap_row.get('id')} balance null"
            balance_available = False

        pnl_dec, pnl_source = _extract_pnl_from_snapshot(snap_row, hb_row)
        if pnl_dec is not None:
            pnl_display, pnl_direction = format_signed_susq(pnl_dec)
            pnl_sub = pnl_source
            pnl_available = True
        else:
            pnl_display = "NO DATA"
            pnl_direction = "no_data"
            pnl_sub = pnl_source
            pnl_available = False
    else:
        balance_display = "NO DATA"
        balance_sub = snapshots_table.message or "No snapshots row"
        balance_available = False
        pnl_display = "NO DATA"
        pnl_direction = "no_data"
        pnl_sub = snapshots_table.message or "No snapshots row"
        pnl_available = False

    # 3. Open order count
    live_open_count: int | None = None
    if orders_table.status == "ok":
        live_open_count = sum(
            1
            for r in orders_table.rows
            if str(r.get("status") or "").strip().lower() in OPEN_ORDER_STATUSES
        )

    snap_open_raw = snap_row.get("open_orders") if snap_row is not None else None
    snap_open_count: int | None = None
    if snap_open_raw is not None and not isinstance(snap_open_raw, bool):
        try:
            snap_open_count = int(snap_open_raw)
        except (ValueError, TypeError):
            snap_open_count = None

    if snap_open_count is not None:
        open_orders_display = str(snap_open_count)
        if live_open_count is not None:
            open_orders_sub = f"Snapshot: {snap_open_count} | Orders table open: {live_open_count}"
        else:
            open_orders_sub = f"From snapshot #{snap_row.get('id')}"
        open_orders_available = True
    elif live_open_count is not None:
        open_orders_display = str(live_open_count)
        open_orders_sub = "Active in orders table (no snapshot row)"
        open_orders_available = True
    else:
        open_orders_display = "NO DATA"
        open_orders_sub = snapshots_table.message or orders_table.message or "No order data"
        open_orders_available = False

    # 4. Total exposure
    cap_total_dec = parse_decimal(hb_row.get("cap_total")) if hb_row is not None else None
    if exposure_table.status == "ok":
        exp_decimals = [parse_decimal(r.get("committed")) for r in exposure_table.rows]
        valid_exp = [d for d in exp_decimals if d is not None]
        if valid_exp:
            total_exp = sum(valid_exp, Decimal(0))
            exposure_display = format_susq(total_exp)
            if cap_total_dec is not None and cap_total_dec > 0:
                pct = (total_exp / cap_total_dec * Decimal(100)).quantize(Decimal("0.1"))
                exposure_sub = f"{len(valid_exp)} race(s) | Cap {format_susq(cap_total_dec)} ({pct}%)"
            else:
                exposure_sub = f"Across {len(valid_exp)} active race(s)"
            exposure_available = True
        else:
            exposure_display = "NO DATA"
            exposure_sub = "Exposure rows have unparseable committed values"
            exposure_available = False
    elif snap_row is not None and parse_decimal(snap_row.get("committed")) is not None:
        snap_comm = parse_decimal(snap_row.get("committed"))
        exposure_display = format_susq(snap_comm)
        exposure_sub = "From latest snapshot (exposure table empty)"
        exposure_available = True
    elif hb_row is not None and parse_decimal(hb_row.get("committed")) is not None:
        hb_comm = parse_decimal(hb_row.get("committed"))
        exposure_display = format_susq(hb_comm)
        exposure_sub = "From heartbeat (exposure table empty)"
        exposure_available = True
    else:
        exposure_display = "NO DATA"
        exposure_sub = exposure_table.message or "No exposure data"
        exposure_available = False

    # 5. Last event time
    if events_table.has_data:
        latest_event = events_table.rows[0]
        ev_ts_ist = format_ist_timestamp(latest_event.get("ts_utc"))
        ev_age = compute_age_seconds(latest_event.get("ts_utc"), now_utc)
        ev_age_str = f"{round(ev_age)}s ago" if ev_age is not None else "age unknown"
        ev_level = str(latest_event.get("level") or "info").upper()
        ev_kind = str(latest_event.get("kind") or "event")
        last_event_display = ev_ts_ist
        last_event_sub = f"[{ev_level}] {ev_kind} ({ev_age_str})"
        last_event_available = ev_ts_ist != "—"
    else:
        last_event_display = "NO DATA"
        last_event_sub = events_table.message or "No events recorded"
        last_event_available = False

    return HeroMetrics(
        balance_display=balance_display,
        balance_sub=balance_sub,
        balance_available=balance_available,
        pnl_display=pnl_display,
        pnl_sub=pnl_sub,
        pnl_direction=pnl_direction,
        pnl_available=pnl_available,
        open_orders_display=open_orders_display,
        open_orders_sub=open_orders_sub,
        open_orders_available=open_orders_available,
        exposure_display=exposure_display,
        exposure_sub=exposure_sub,
        exposure_available=exposure_available,
        last_event_display=last_event_display,
        last_event_sub=last_event_sub,
        last_event_available=last_event_available,
    )


def load_monitor_snapshot(
    db_path: str | Path | None = None,
    state_dir: str | Path | None = None,
    now_utc: datetime | None = None,
    order_limit: int = 50,
    decision_limit: int = 50,
    event_limit: int = 60,
) -> MonitorSnapshot:
    """Load all contract tables and compute banner & hero states in read-only mode."""
    resolved_db, resolved_state = resolve_paths(db_path=db_path, state_dir=state_dir)
    current_utc = now_utc.astimezone(UTC) if now_utc else datetime.now(UTC)
    fetched_ist = current_utc.astimezone(IST_TZ).strftime("%H:%M:%S IST")

    if not resolved_db.is_file():
        missing_msg = f"NO DATA — Database file not found at {resolved_db.name}"
        tables = {
            name: TableResult(table=name, status="missing_file", message=missing_msg)
            for name in CONTRACT_SCHEMA
        }
        hb_state = _evaluate_heartbeat(tables["heartbeat"], resolved_state, current_utc)
        hero_state = _compute_hero_metrics(
            tables["snapshots"],
            tables["heartbeat"],
            tables["exposure"],
            tables["orders"],
            tables["events"],
            current_utc,
        )
        return MonitorSnapshot(
            db_path=resolved_db,
            state_dir=resolved_state,
            fetched_at_utc=current_utc,
            fetched_at_ist=fetched_ist,
            heartbeat_table=tables["heartbeat"],
            snapshots_table=tables["snapshots"],
            exposure_table=tables["exposure"],
            orders_table=tables["orders"],
            decisions_table=tables["decisions"],
            quotes_table=tables["quotes_latest"],
            events_table=tables["events"],
            heartbeat=hb_state,
            hero=hero_state,
        )

    try:
        conn = connect_readonly(resolved_db)
    except sqlite3.Error as exc:
        err_msg = f"NO DATA — Cannot open {resolved_db.name} in read-only mode: {exc}"
        tables = {
            name: TableResult(table=name, status="missing_file", message=err_msg)
            for name in CONTRACT_SCHEMA
        }
        hb_state = _evaluate_heartbeat(tables["heartbeat"], resolved_state, current_utc)
        hero_state = _compute_hero_metrics(
            tables["snapshots"],
            tables["heartbeat"],
            tables["exposure"],
            tables["orders"],
            tables["events"],
            current_utc,
        )
        return MonitorSnapshot(
            db_path=resolved_db,
            state_dir=resolved_state,
            fetched_at_utc=current_utc,
            fetched_at_ist=fetched_ist,
            heartbeat_table=tables["heartbeat"],
            snapshots_table=tables["snapshots"],
            exposure_table=tables["exposure"],
            orders_table=tables["orders"],
            decisions_table=tables["decisions"],
            quotes_table=tables["quotes_latest"],
            events_table=tables["events"],
            heartbeat=hb_state,
            hero=hero_state,
        )

    with conn:
        heartbeat_table = _inspect_and_query_table(
            conn,
            "heartbeat",
            "SELECT * FROM heartbeat WHERE id = 1 LIMIT 1",
        )
        snapshots_table = _inspect_and_query_table(
            conn,
            "snapshots",
            "SELECT * FROM snapshots ORDER BY id DESC LIMIT 1",
        )
        exposure_table = _inspect_and_query_table(
            conn,
            "exposure",
            "SELECT * FROM exposure ORDER BY CAST(committed AS REAL) DESC, race_key ASC",
        )
        orders_table = _inspect_and_query_table(
            conn,
            "orders",
            "SELECT * FROM orders ORDER BY updated_utc DESC, created_utc DESC LIMIT ?",
            (order_limit,),
        )
        decisions_table = _inspect_and_query_table(
            conn,
            "decisions",
            "SELECT * FROM decisions ORDER BY id DESC LIMIT ?",
            (decision_limit,),
        )
        quotes_table = _inspect_and_query_table(
            conn,
            "quotes_latest",
            "SELECT * FROM quotes_latest ORDER BY race_key ASC, party ASC, sig_exchange_id ASC",
        )
        events_table = _inspect_and_query_table(
            conn,
            "events",
            "SELECT * FROM events ORDER BY id DESC LIMIT ?",
            (event_limit,),
        )

    conn.close()

    hb_state = _evaluate_heartbeat(heartbeat_table, resolved_state, current_utc)
    hero_state = _compute_hero_metrics(
        snapshots_table,
        heartbeat_table,
        exposure_table,
        orders_table,
        events_table,
        current_utc,
    )

    return MonitorSnapshot(
        db_path=resolved_db,
        state_dir=resolved_state,
        fetched_at_utc=current_utc,
        fetched_at_ist=fetched_ist,
        heartbeat_table=heartbeat_table,
        snapshots_table=snapshots_table,
        exposure_table=exposure_table,
        orders_table=orders_table,
        decisions_table=decisions_table,
        quotes_table=quotes_table,
        events_table=events_table,
        heartbeat=hb_state,
        hero=hero_state,
    )
