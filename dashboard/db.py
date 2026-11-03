"""Read-only SQLite data layer and single STOP file writer for the Bellwether monitor.

Follows ``bot/CONTRACT.md`` and ``AGENTS.md``:
- Opens ``bot/state/bellwether.db`` strictly in read-only URI mode (``mode=ro``).
- Uses the exact table and column names from ``bot/CONTRACT.md``.
- Writes nothing except ``bot/state/STOP`` via ``write_stop_file`` (Task DASH-EW-3),
  using atomic exclusive file creation (`mode="x"`) that never overwrites or deletes.
- Never imports ``engine/`` or ``bot/``, never reads ``.env``, and makes no network calls.
- Returns explicit ``no data`` / ``bad format`` states when the file, a table, a column,
  rows, or JSON payloads are missing or malformed.
"""

from __future__ import annotations

import errno
import json
import os
import re
import sqlite3
import stat
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any
from urllib.parse import quote

# Exact STOP control filename and contract path from `bot/CONTRACT.md`.
STOP_FILENAME: str = "STOP"
STOP_CONTRACT_PATH: str = "bot/state/STOP"

# Heartbeat STALE threshold in seconds (single source of truth).
# Source: Task DASH-EW-1 specification ("ALIVE (age < a threshold from CONTRACT.md
# or 30s if not given)"). `bot/CONTRACT.md` specifies that `heartbeat` is rewritten
# every loop (and `snapshots` appended every 60 s) without a numeric heartbeat stale
# threshold, so this 30.0s constant is the authoritative threshold.
HEARTBEAT_STALE_SECONDS: float = 30.0
QUOTE_STALE_SECONDS: float = 60.0
# A `quotes_latest` row not refreshed for this long belongs to a contract the bot no
# longer evaluates (excluded from the map, closed, or left from an earlier run). Such
# rows are summarised as one attention item instead of one item per contract.
QUOTE_INACTIVE_SECONDS: float = 3600.0
SQLITE_BUSY_TIMEOUT_MS: int = 2000
DEFAULT_HISTORY_HOURS: float = 6.0
DEFAULT_SNAPSHOT_HISTORY_LIMIT: int = 360
IST_TZ = timezone(timedelta(hours=5, minutes=30), name="IST")

OPEN_ORDER_STATUSES: frozenset[str] = frozenset(
    {"journaled", "sent", "resting", "partial"}
)
ALL_ORDER_STATUSES: tuple[str, ...] = (
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
ALL_DECISION_VERDICTS: tuple[str, ...] = (
    "order",
    "would_order",
    "blocked",
    "skip",
)
ALL_STRATEGIES: tuple[str, ...] = (
    "complement",
    "reference",
    "probe",
    "exit",
    "exit_probe",
)

# Exact position fields from `bot/CONTRACT.md` (no guessed keys).
ALLOWED_POSITION_KEYS: frozenset[str] = frozenset(
    {
        "race_key",
        "sig_exchange_id",
        "sig_market_id",
        "party",
        "side",
        "quantity",
        "price_side",
        "total_cost",
        "committed",
    }
)
# What the bot actually writes today (`bot/main.py`): each element of
# `positions_json` is SIG's `TournamentPosition` row dumped as sent
# (`engine/sig_client/wire.py`). `quantity`: positive = YES shares, negative = NO.
# Units of the money fields are unresolved upstream, so they are shown as sent.
SIG_POSITION_KEYS: frozenset[str] = frozenset(
    {
        "exchangeId",
        "marketId",
        "marketTitle",
        "option",
        "settled",
        "quantity",
        "avgCost",
        "currentPrice",
        "marketValue",
        "costBasis",
        "unrealizedPnl",
        "unrealizedPnlPct",
        "moneyEarned",
        "lots",
    }
)
ALLOWED_PARTIES: frozenset[str] = frozenset({"D", "R", "I"})
ALLOWED_SIDES: frozenset[str] = frozenset({"yes", "no"})

# Valid `race_key` patterns per `bot/CONTRACT.md` (`senate:PA`, `governor:AZ`,
# `house:PA-08`, `house_control`, `senate_control`, or `SEN-PA-2026` style).
_RACE_KEY_RE = re.compile(
    r"^(?:"
    r"(?:senate|governor|house):[A-Za-z0-9_-]+"
    r"|(?:house|senate)_control"
    r"|(?:SEN|GOV|HOUSE)-[A-Z0-9-]+"
    r")$"
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
    status: str  # "ok" | "empty" | "missing_file" | "missing_table" | "missing_columns" | "db_busy" | "db_error"
    rows: list[dict[str, Any]] = field(default_factory=list)
    missing_columns: tuple[str, ...] = ()
    message: str = ""

    @property
    def has_data(self) -> bool:
        return self.status == "ok" and len(self.rows) > 0


@dataclass(frozen=True)
class HeartbeatState:
    """Evaluated heartbeat banner state and control flags."""

    status: str  # "ALIVE" | "STALE" | "NO HEARTBEAT" | "DB BUSY"
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
class PositionRow:
    """Validated position entry from ``snapshots.positions_json``."""

    race_key: str
    sig_exchange_id: str
    sig_market_id: str
    party: str
    side: str
    quantity: Decimal
    price_side: Decimal | None
    total_cost: Decimal | None
    committed: Decimal | None
    market_value: Decimal | None = None
    unrealized_pnl: Decimal | None = None


@dataclass(frozen=True)
class ParsedPositionsResult:
    """Result of parsing ``snapshots.positions_json`` against ``bot/CONTRACT.md``."""

    status: str  # "ok" | "empty" | "no_data" | "bad_format"
    rows: list[PositionRow] = field(default_factory=list)
    snapshot_id: int | None = None
    snapshot_ts_ist: str = "—"
    message: str = ""

    @property
    def has_data(self) -> bool:
        return self.status == "ok" and len(self.rows) > 0


@dataclass(frozen=True)
class RacePnlRow:
    """Validated per-race P&L entry from ``snapshots.pnl_json``."""

    race_key: str
    pnl: Decimal


@dataclass(frozen=True)
class ParsedPnlResult:
    """Result of parsing ``snapshots.pnl_json`` against ``bot/CONTRACT.md``."""

    status: str  # "ok" | "empty" | "no_data" | "bad_format"
    rows: list[RacePnlRow] = field(default_factory=list)
    total_pnl: Decimal | None = None
    snapshot_id: int | None = None
    snapshot_ts_ist: str = "—"
    message: str = ""
    source: str = "pnl_json"  # "pnl_json" | "positions_unrealized"

    @property
    def has_data(self) -> bool:
        return self.status == "ok" and len(self.rows) > 0


@dataclass(frozen=True)
class SnapshotHistoryPoint:
    """One chronological point from ``snapshots`` for the history chart."""

    id: int
    ts_utc: str
    ts_dt_utc: datetime
    ts_ist_dt: datetime
    ts_ist_label: str
    balance: Decimal
    committed: Decimal
    open_orders: int | None


@dataclass(frozen=True)
class SnapshotHistoryResult:
    """Time-series history of balance and committed exposure from ``snapshots``."""

    status: str  # "ok" | "single_row" | "empty" | "no_data" | "bad_format"
    points: list[SnapshotHistoryPoint] = field(default_factory=list)
    window_hours: float = DEFAULT_HISTORY_HOURS
    message: str = ""

    @property
    def has_data(self) -> bool:
        return self.status in {"ok", "single_row"} and len(self.points) > 0


@dataclass(frozen=True)
class FilterCriteria:
    """Active user filter selections applied to Orders, Decisions, and Positions."""

    race_key: str = "All"
    strategy: str = "All"
    status_verdict: str = "All"


@dataclass(frozen=True)
class StopWriteResult:
    """Result of the single permitted dashboard write (`bot/state/STOP`)."""

    ok: bool
    already_existed: bool
    stop_path: Path
    ts_utc: str
    ts_ist: str
    content: str
    error_kind: str  # "" | "already_exists" | "folder_missing" | "permission_denied" | "os_error" | "verification_failed"
    message: str


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
    positions: ParsedPositionsResult = field(
        default_factory=lambda: ParsedPositionsResult(status="no_data")
    )
    race_pnl: ParsedPnlResult = field(
        default_factory=lambda: ParsedPnlResult(status="no_data")
    )
    snapshot_history: SnapshotHistoryResult = field(
        default_factory=lambda: SnapshotHistoryResult(status="no_data")
    )
    db_busy: bool = False
    db_busy_message: str | None = None


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


def resolve_stop_path(state_dir: str | Path | None = None) -> Path:
    """Return the resolved path to ``bot/state/STOP`` (or ``<state_dir>/STOP`` in tests)."""
    _, resolved_state = resolve_paths(state_dir=state_dir)
    return resolved_state / STOP_FILENAME


def write_stop_file(
    state_dir: str | Path | None = None,
    now_utc: datetime | None = None,
) -> StopWriteResult:
    """Atomically create ``bot/state/STOP`` (the only file write allowed in ``dashboard/``).

    - Uses ``STOP_FILENAME`` (``bot/state/STOP`` per ``bot/CONTRACT.md``).
    - Writes ``"<UTC ISO-8601 timestamp> dashboard\\n"``.
    - Uses exclusive creation (``mode="x"``) so an existing ``STOP`` file is never
      overwritten, truncated, or deleted.
    - Never creates parent directories; fails clearly if the folder is missing or
      read-only.
    - Verifies ``stop_path.is_file()`` after writing before claiming success.
    """
    stop_path = resolve_stop_path(state_dir=state_dir)
    parent_dir = stop_path.parent
    current_utc = now_utc.astimezone(UTC) if now_utc else datetime.now(UTC)
    ts_utc = current_utc.strftime("%Y-%m-%dT%H:%M:%SZ")
    ts_ist = format_ist_timestamp(ts_utc)
    payload = f"{ts_utc} dashboard\n"

    if not parent_dir.exists() or not parent_dir.is_dir():
        return StopWriteResult(
            ok=False,
            already_existed=False,
            stop_path=stop_path,
            ts_utc=ts_utc,
            ts_ist=ts_ist,
            content="",
            error_kind="folder_missing",
            message=f"STOP WRITE FAILED (folder missing): State directory does not exist ({parent_dir})",
        )

    if stop_path.exists():
        return StopWriteResult(
            ok=False,
            already_existed=True,
            stop_path=stop_path,
            ts_utc=ts_utc,
            ts_ist=ts_ist,
            content="",
            error_kind="already_exists",
            message=f"STOP ALREADY PRESENT: {stop_path} already exists; no second write performed.",
        )

    try:
        dir_mode = parent_dir.stat().st_mode
        if not (dir_mode & stat.S_IWUSR) or not os.access(parent_dir, os.W_OK):
            raise PermissionError(
                errno.EACCES,
                "Permission denied: state directory is read-only",
                str(parent_dir),
            )

        with stop_path.open("x", encoding="utf-8") as fh:
            fh.write(payload)
            fh.flush()
    except FileExistsError:
        return StopWriteResult(
            ok=False,
            already_existed=True,
            stop_path=stop_path,
            ts_utc=ts_utc,
            ts_ist=ts_ist,
            content="",
            error_kind="already_exists",
            message=f"STOP ALREADY PRESENT: {stop_path} already exists; no second write performed.",
        )
    except FileNotFoundError as exc:
        return StopWriteResult(
            ok=False,
            already_existed=False,
            stop_path=stop_path,
            ts_utc=ts_utc,
            ts_ist=ts_ist,
            content="",
            error_kind="folder_missing",
            message=f"STOP WRITE FAILED (folder missing): {exc}",
        )
    except PermissionError as exc:
        return StopWriteResult(
            ok=False,
            already_existed=False,
            stop_path=stop_path,
            ts_utc=ts_utc,
            ts_ist=ts_ist,
            content="",
            error_kind="permission_denied",
            message=f"STOP WRITE FAILED (permission denied): {exc}",
        )
    except OSError as exc:
        is_perm = exc.errno in {errno.EACCES, errno.EPERM, errno.EROFS}
        kind = "permission_denied" if is_perm else "os_error"
        label = "permission denied" if is_perm else type(exc).__name__
        return StopWriteResult(
            ok=False,
            already_existed=False,
            stop_path=stop_path,
            ts_utc=ts_utc,
            ts_ist=ts_ist,
            content="",
            error_kind=kind,
            message=f"STOP WRITE FAILED ({label}): {exc}",
        )

    if not stop_path.is_file():
        return StopWriteResult(
            ok=False,
            already_existed=False,
            stop_path=stop_path,
            ts_utc=ts_utc,
            ts_ist=ts_ist,
            content="",
            error_kind="verification_failed",
            message=f"STOP WRITE FAILED (verification failed): {stop_path} does not exist after write.",
        )

    return StopWriteResult(
        ok=True,
        already_existed=False,
        stop_path=stop_path,
        ts_utc=ts_utc,
        ts_ist=ts_ist,
        content=payload.strip(),
        error_kind="",
        message=f"STOP CREATED: {stop_path} at {ts_ist}",
    )


def build_readonly_uri(db_path: Path) -> str:
    """Construct a strict SQLite ``mode=ro`` URI for a local filesystem path."""
    posix_path = db_path.resolve().as_posix()
    if not posix_path.startswith("/"):
        posix_path = "/" + posix_path
    encoded_path = quote(posix_path, safe="/:")
    return f"file:{encoded_path}?mode=ro"


def is_sqlite_busy_error(exc: BaseException) -> bool:
    """Return True when ``exc`` indicates SQLite lock / busy contention."""
    if not isinstance(exc, sqlite3.Error):
        return False
    code = getattr(exc, "sqlite_errorcode", None)
    if code in {
        getattr(sqlite3, "SQLITE_BUSY", 5),
        getattr(sqlite3, "SQLITE_LOCKED", 6),
    }:
        return True
    msg = str(exc).lower()
    return "database is locked" in msg or "database table is locked" in msg or "busy" in msg


def connect_readonly(
    db_path: Path,
    busy_timeout_ms: int = SQLITE_BUSY_TIMEOUT_MS,
) -> sqlite3.Connection:
    """Open ``db_path`` in read-only mode (`mode=ro`) with an explicit busy timeout."""
    uri = build_readonly_uri(db_path)
    timeout_sec = max(0.001, busy_timeout_ms / 1000.0)
    conn = sqlite3.connect(uri, uri=True, timeout=timeout_sec)
    conn.row_factory = sqlite3.Row
    conn.execute(f"PRAGMA busy_timeout = {int(busy_timeout_ms)}")
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
        return datetime.combine(dt.date(), dt.time(), tzinfo=UTC)
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
    if value is None or isinstance(value, (bool, list, dict)):
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


def is_valid_race_key(value: Any, known_race_keys: set[str] | None = None) -> bool:
    """Return True if ``value`` is a valid ``race_key`` per ``bot/CONTRACT.md``."""
    if not isinstance(value, str):
        return False
    key = value.strip()
    if not key:
        return False
    if known_race_keys and key in known_race_keys:
        return True
    return bool(_RACE_KEY_RE.match(key))


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


def parse_positions_json(
    snapshot_row: dict[str, Any] | None,
    known_race_keys: set[str] | None = None,
    exchange_lookup: dict[str, tuple[str, str]] | None = None,
) -> ParsedPositionsResult:
    """Parse ``snapshots.positions_json`` strictly following ``bot/CONTRACT.md``.

    Never guesses undocumented keys: any unexpected object key or invalid value
    returns ``status="bad_format"`` with a ``NO DATA / BAD FORMAT`` message.

    Two shapes are accepted: the snake_case contract fields, and SIG's
    ``TournamentPosition`` rows exactly as ``bot/main.py`` writes them (keys in
    ``SIG_POSITION_KEYS``). For SIG rows, ``exchange_lookup`` maps an exchange id to
    ``(race_key, party)`` from the bot's own tables. ``{"unvalued": [...]}`` (SIG's
    409 for holdings without a valuation) is reported as no data, not bad format.
    """
    if snapshot_row is None:
        return ParsedPositionsResult(
            status="no_data",
            message="NO DATA — No snapshots row available",
        )

    snap_id_raw = snapshot_row.get("id")
    snap_id = int(snap_id_raw) if isinstance(snap_id_raw, int) else None
    snap_ts_ist = format_ist_timestamp(snapshot_row.get("ts_utc"))
    raw = snapshot_row.get("positions_json")

    if raw is None or not str(raw).strip():
        return ParsedPositionsResult(
            status="bad_format",
            snapshot_id=snap_id,
            snapshot_ts_ist=snap_ts_ist,
            message="NO DATA / BAD FORMAT — positions_json is missing or empty",
        )

    try:
        parsed = json.loads(str(raw))
    except (ValueError, TypeError):
        return ParsedPositionsResult(
            status="bad_format",
            snapshot_id=snap_id,
            snapshot_ts_ist=snap_ts_ist,
            message="NO DATA / BAD FORMAT — positions_json is malformed JSON",
        )

    if not isinstance(parsed, (list, dict)):
        return ParsedPositionsResult(
            status="bad_format",
            snapshot_id=snap_id,
            snapshot_ts_ist=snap_ts_ist,
            message="NO DATA / BAD FORMAT — positions_json must be a JSON array or object",
        )

    if len(parsed) == 0:
        return ParsedPositionsResult(
            status="empty",
            rows=[],
            snapshot_id=snap_id,
            snapshot_ts_ist=snap_ts_ist,
            message="NO DATA — No positions recorded in latest snapshot",
        )

    if isinstance(parsed, dict) and set(parsed.keys()) == {"unvalued"}:
        unvalued = parsed.get("unvalued")
        count = len(unvalued) if isinstance(unvalued, list) else 0
        return ParsedPositionsResult(
            status="no_data",
            snapshot_id=snap_id,
            snapshot_ts_ist=snap_ts_ist,
            message=(
                f"NO DATA — SIG reported {count} holding(s) without a valuation "
                "(positions read returned 409)"
            ),
        )

    rows: list[PositionRow] = []
    lookup = exchange_lookup or {}

    def _validate_sig_item(item: dict[str, Any]) -> PositionRow | None | str:
        """Map one SIG ``TournamentPosition`` row; ``None`` skips a closed row."""
        unexpected = set(item.keys()) - SIG_POSITION_KEYS
        if unexpected:
            bad_list = ", ".join(sorted(str(k) for k in unexpected))
            return f"NO DATA / BAD FORMAT — Unexpected keys in SIG positions_json: {bad_list}"
        qty_signed = parse_decimal(item.get("quantity"))
        if qty_signed is None:
            return "NO DATA / BAD FORMAT — Invalid 'quantity' value in positions_json"
        exch_val = str(item.get("exchangeId") or "").strip()
        if not exch_val:
            return "NO DATA / BAD FORMAT — SIG position row missing 'exchangeId'"
        if qty_signed == 0 or item.get("settled") is True:
            return None
        race_val, party_val = lookup.get(exch_val, ("", ""))
        avg_cost = parse_decimal(item.get("avgCost"))
        return PositionRow(
            race_key=race_val or "—",
            sig_exchange_id=exch_val,
            sig_market_id=str(item.get("marketId") or "").strip() or "—",
            party=party_val or "—",
            side="YES" if qty_signed > 0 else "NO",
            quantity=abs(qty_signed),
            price_side=avg_cost if avg_cost is not None and 0 <= avg_cost <= 1 else None,
            total_cost=parse_decimal(item.get("costBasis")),
            committed=None,
            market_value=parse_decimal(item.get("marketValue")),
            unrealized_pnl=parse_decimal(item.get("unrealizedPnl")),
        )

    def _validate_dict_item(
        item: dict[str, Any],
        fallback_race_key: str = "",
        fallback_party: str = "",
        fallback_exchange_id: str = "",
    ) -> PositionRow | str:
        if not item:
            return "NO DATA / BAD FORMAT — Empty position object in positions_json"
        unexpected = set(item.keys()) - ALLOWED_POSITION_KEYS
        if unexpected:
            bad_list = ", ".join(sorted(str(k) for k in unexpected))
            return f"NO DATA / BAD FORMAT — Unexpected keys in positions_json: {bad_list}"

        if "quantity" not in item:
            return "NO DATA / BAD FORMAT — Position object missing required 'quantity' key"
        qty_dec = parse_decimal(item.get("quantity"))
        if qty_dec is None:
            return "NO DATA / BAD FORMAT — Invalid 'quantity' value in positions_json"

        race_val = str(item.get("race_key") or fallback_race_key).strip()
        exch_val = str(item.get("sig_exchange_id") or fallback_exchange_id).strip()
        mkt_val = str(item.get("sig_market_id") or "").strip()
        party_val = str(item.get("party") or fallback_party).strip()
        side_val = str(item.get("side") or "").strip().lower()

        if not race_val and not exch_val:
            return "NO DATA / BAD FORMAT — Position requires 'race_key' or 'sig_exchange_id'"
        if race_val and not is_valid_race_key(race_val, known_race_keys):
            return f"NO DATA / BAD FORMAT — Invalid race_key '{race_val}' in positions_json"
        if party_val and party_val not in ALLOWED_PARTIES:
            return f"NO DATA / BAD FORMAT — Invalid party '{party_val}' in positions_json"
        if side_val and side_val not in ALLOWED_SIDES:
            return f"NO DATA / BAD FORMAT — Invalid side '{side_val}' in positions_json"

        price_dec: Decimal | None = None
        if item.get("price_side") is not None and str(item.get("price_side")).strip():
            price_dec = parse_decimal(item.get("price_side"))
            if price_dec is None or price_dec < 0 or price_dec > 1:
                return "NO DATA / BAD FORMAT — Invalid 'price_side' in positions_json"

        cost_dec: Decimal | None = None
        if item.get("total_cost") is not None and str(item.get("total_cost")).strip():
            cost_dec = parse_decimal(item.get("total_cost"))
            if cost_dec is None:
                return "NO DATA / BAD FORMAT — Invalid 'total_cost' in positions_json"

        comm_dec: Decimal | None = None
        if item.get("committed") is not None and str(item.get("committed")).strip():
            comm_dec = parse_decimal(item.get("committed"))
            if comm_dec is None:
                return "NO DATA / BAD FORMAT — Invalid 'committed' in positions_json"

        return PositionRow(
            race_key=race_val or "—",
            sig_exchange_id=exch_val or "—",
            sig_market_id=mkt_val or "—",
            party=party_val or "—",
            side=side_val.upper() if side_val else "—",
            quantity=qty_dec,
            price_side=price_dec,
            total_cost=cost_dec,
            committed=comm_dec,
        )

    if isinstance(parsed, list):
        for elem in parsed:
            if not isinstance(elem, dict):
                return ParsedPositionsResult(
                    status="bad_format",
                    snapshot_id=snap_id,
                    snapshot_ts_ist=snap_ts_ist,
                    message="NO DATA / BAD FORMAT — positions_json array items must be objects",
                )
            if "exchangeId" in elem:
                sig_res = _validate_sig_item(elem)
                if isinstance(sig_res, str):
                    return ParsedPositionsResult(
                        status="bad_format",
                        snapshot_id=snap_id,
                        snapshot_ts_ist=snap_ts_ist,
                        message=sig_res,
                    )
                if sig_res is not None:
                    rows.append(sig_res)
                continue
            res = _validate_dict_item(elem)
            if isinstance(res, str):
                return ParsedPositionsResult(
                    status="bad_format",
                    snapshot_id=snap_id,
                    snapshot_ts_ist=snap_ts_ist,
                    message=res,
                )
            rows.append(res)
    else:
        # Dict payload: check if it looks like a single record with contract keys
        if any(k in ALLOWED_POSITION_KEYS for k in parsed):
            res = _validate_dict_item(parsed)
            if isinstance(res, str):
                return ParsedPositionsResult(
                    status="bad_format",
                    snapshot_id=snap_id,
                    snapshot_ts_ist=snap_ts_ist,
                    message=res,
                )
            rows.append(res)
        else:
            # Map of `{race_key[:party] | sig_exchange_id -> quantity | dict}`
            for map_key, val in parsed.items():
                key_str = str(map_key).strip()
                inferred_race = ""
                inferred_party = ""
                inferred_exch = ""

                if ":" in key_str:
                    parts = key_str.rsplit(":", 1)
                    if parts[1] in ALLOWED_PARTIES and is_valid_race_key(
                        parts[0], known_race_keys
                    ):
                        inferred_race = parts[0]
                        inferred_party = parts[1]
                    elif is_valid_race_key(key_str, known_race_keys):
                        inferred_race = key_str
                elif is_valid_race_key(key_str, known_race_keys):
                    inferred_race = key_str
                elif key_str.startswith("SIG-") or key_str.isdigit():
                    inferred_exch = key_str

                if isinstance(val, dict):
                    if not inferred_race and not inferred_exch and "race_key" not in val and "sig_exchange_id" not in val:
                        return ParsedPositionsResult(
                            status="bad_format",
                            snapshot_id=snap_id,
                            snapshot_ts_ist=snap_ts_ist,
                            message=f"NO DATA / BAD FORMAT — Unexpected key '{key_str}' in positions_json",
                        )
                    res = _validate_dict_item(
                        val,
                        fallback_race_key=inferred_race,
                        fallback_party=inferred_party,
                        fallback_exchange_id=inferred_exch,
                    )
                    if isinstance(res, str):
                        return ParsedPositionsResult(
                            status="bad_format",
                            snapshot_id=snap_id,
                            snapshot_ts_ist=snap_ts_ist,
                            message=res,
                        )
                    rows.append(res)
                else:
                    if not inferred_race and not inferred_exch:
                        return ParsedPositionsResult(
                            status="bad_format",
                            snapshot_id=snap_id,
                            snapshot_ts_ist=snap_ts_ist,
                            message=f"NO DATA / BAD FORMAT — Unexpected key '{key_str}' in positions_json",
                        )
                    qty_dec = parse_decimal(val)
                    if qty_dec is None:
                        return ParsedPositionsResult(
                            status="bad_format",
                            snapshot_id=snap_id,
                            snapshot_ts_ist=snap_ts_ist,
                            message=f"NO DATA / BAD FORMAT — Invalid quantity for '{key_str}' in positions_json",
                        )
                    rows.append(
                        PositionRow(
                            race_key=inferred_race or "—",
                            sig_exchange_id=inferred_exch or "—",
                            sig_market_id="—",
                            party=inferred_party or "—",
                            side="—",
                            quantity=qty_dec,
                            price_side=None,
                            total_cost=None,
                            committed=None,
                        )
                    )

    if not rows:
        return ParsedPositionsResult(
            status="empty",
            rows=[],
            snapshot_id=snap_id,
            snapshot_ts_ist=snap_ts_ist,
            message="NO DATA — No open positions in latest snapshot",
        )

    rows.sort(key=lambda r: (abs(r.quantity), r.race_key), reverse=True)
    return ParsedPositionsResult(
        status="ok",
        rows=rows,
        snapshot_id=snap_id,
        snapshot_ts_ist=snap_ts_ist,
        message=f"{len(rows)} position(s) in snapshot #{snap_id}",
    )


def parse_pnl_json(
    snapshot_row: dict[str, Any] | None,
    known_race_keys: set[str] | None = None,
) -> ParsedPnlResult:
    """Parse ``snapshots.pnl_json`` as a per-race P&L table following ``bot/CONTRACT.md``.

    Requires a JSON object mapping valid ``race_key`` strings to finite decimal P&L
    strings/numbers (or a JSON array of ``{"race_key": ..., "pnl": ...}`` objects).
    Never guesses summary keys (`total`, `realized`, `unrealized`, etc.); unexpected
    keys or malformed values return ``status="bad_format"``.
    """
    if snapshot_row is None:
        return ParsedPnlResult(
            status="no_data",
            message="NO DATA — No snapshots row available",
        )

    snap_id_raw = snapshot_row.get("id")
    snap_id = int(snap_id_raw) if isinstance(snap_id_raw, int) else None
    snap_ts_ist = format_ist_timestamp(snapshot_row.get("ts_utc"))
    raw = snapshot_row.get("pnl_json")

    if raw is None or not str(raw).strip():
        # The bot writes pnl_json = NULL today (bot/main.py); that is absent data,
        # not a malformed payload.
        return ParsedPnlResult(
            status="no_data",
            snapshot_id=snap_id,
            snapshot_ts_ist=snap_ts_ist,
            message="NO DATA — pnl_json not written by the bot",
        )

    try:
        parsed = json.loads(str(raw))
    except (ValueError, TypeError):
        return ParsedPnlResult(
            status="bad_format",
            snapshot_id=snap_id,
            snapshot_ts_ist=snap_ts_ist,
            message="NO DATA / BAD FORMAT — pnl_json is malformed JSON",
        )

    if not isinstance(parsed, dict):
        return ParsedPnlResult(
            status="bad_format",
            snapshot_id=snap_id,
            snapshot_ts_ist=snap_ts_ist,
            message="NO DATA / BAD FORMAT — pnl_json must be a JSON object keyed by race_key",
        )

    if len(parsed) == 0:
        return ParsedPnlResult(
            status="empty",
            rows=[],
            total_pnl=None,
            snapshot_id=snap_id,
            snapshot_ts_ist=snap_ts_ist,
            message="NO DATA — No per-race P&L entries in latest snapshot",
        )

    rows: list[RacePnlRow] = []
    for key, val in parsed.items():
        race_str = str(key).strip()
        if not is_valid_race_key(race_str, known_race_keys):
            return ParsedPnlResult(
                status="bad_format",
                snapshot_id=snap_id,
                snapshot_ts_ist=snap_ts_ist,
                message=f"NO DATA / BAD FORMAT — Unexpected key '{race_str}' in pnl_json",
            )
        pnl_dec = parse_decimal(val)
        if pnl_dec is None:
            return ParsedPnlResult(
                status="bad_format",
                snapshot_id=snap_id,
                snapshot_ts_ist=snap_ts_ist,
                message=f"NO DATA / BAD FORMAT — Invalid P&L value for '{race_str}' in pnl_json",
            )
        rows.append(RacePnlRow(race_key=race_str, pnl=pnl_dec))

    rows.sort(key=lambda r: (abs(r.pnl), r.race_key), reverse=True)
    total_pnl = sum((r.pnl for r in rows), Decimal(0))
    return ParsedPnlResult(
        status="ok",
        rows=rows,
        total_pnl=total_pnl,
        snapshot_id=snap_id,
        snapshot_ts_ist=snap_ts_ist,
        message=f"{len(rows)} race(s) in snapshot #{snap_id}",
    )


def derive_pnl_from_positions(positions: ParsedPositionsResult) -> ParsedPnlResult | None:
    """Per-race unrealized P&L from SIG's ``unrealizedPnl`` on each position row.

    Used only when ``pnl_json`` is absent. Returns ``None`` unless every open position
    carries an ``unrealizedPnl`` value (no partial sums). Rows without a known
    race are grouped under ``#<exchange id>``.
    """
    if not positions.has_data:
        return None
    if any(r.unrealized_pnl is None for r in positions.rows):
        return None
    by_race: dict[str, Decimal] = {}
    for r in positions.rows:
        key = r.race_key if r.race_key != "—" else f"#{r.sig_exchange_id}"
        by_race[key] = by_race.get(key, Decimal(0)) + (r.unrealized_pnl or Decimal(0))
    rows = [RacePnlRow(race_key=k, pnl=v) for k, v in by_race.items()]
    rows.sort(key=lambda r: (abs(r.pnl), r.race_key), reverse=True)
    return ParsedPnlResult(
        status="ok",
        rows=rows,
        total_pnl=sum((r.pnl for r in rows), Decimal(0)),
        snapshot_id=positions.snapshot_id,
        snapshot_ts_ist=positions.snapshot_ts_ist,
        message=f"unrealized P&L from {len(positions.rows)} SIG position(s)",
        source="positions_unrealized",
    )


def build_snapshot_history(
    snapshots_table: TableResult,
    window_hours: float = DEFAULT_HISTORY_HOURS,
) -> SnapshotHistoryResult:
    """Build chronological balance and exposure history from the last N snapshots."""
    if not snapshots_table.has_data:
        return SnapshotHistoryResult(
            status="no_data" if snapshots_table.status != "empty" else "empty",
            points=[],
            window_hours=window_hours,
            message=snapshots_table.message or "NO DATA — No snapshots available",
        )

    valid_points: list[SnapshotHistoryPoint] = []
    for row in snapshots_table.rows:
        dt_utc = parse_utc_timestamp(row.get("ts_utc"))
        bal_dec = parse_decimal(row.get("balance"))
        comm_dec = parse_decimal(row.get("committed"))
        if dt_utc is None or bal_dec is None or comm_dec is None:
            continue
        ist_dt = dt_utc.astimezone(IST_TZ)
        open_raw = row.get("open_orders")
        open_int: int | None = None
        if open_raw is not None and not isinstance(open_raw, bool):
            try:
                open_int = int(open_raw)
            except (ValueError, TypeError):
                open_int = None
        valid_points.append(
            SnapshotHistoryPoint(
                id=int(row.get("id") or 0),
                ts_utc=str(row.get("ts_utc")),
                ts_dt_utc=dt_utc,
                ts_ist_dt=ist_dt,
                ts_ist_label=ist_dt.strftime("%H:%M:%S IST"),
                balance=bal_dec,
                committed=comm_dec,
                open_orders=open_int,
            )
        )

    if not valid_points:
        return SnapshotHistoryResult(
            status="bad_format",
            points=[],
            window_hours=window_hours,
            message="NO DATA / BAD FORMAT — Snapshots missing valid ts_utc, balance, or committed",
        )

    valid_points.sort(key=lambda p: (p.ts_dt_utc, p.id))
    latest_ts = valid_points[-1].ts_dt_utc
    cutoff = latest_ts - timedelta(hours=max(0.1, window_hours))
    windowed = [p for p in valid_points if p.ts_dt_utc >= cutoff]
    if not windowed:
        windowed = [valid_points[-1]]

    if len(windowed) == 1:
        return SnapshotHistoryResult(
            status="single_row",
            points=windowed,
            window_hours=window_hours,
            message="1 snapshot recorded in window (awaiting additional 60s snapshots for trend line)",
        )

    return SnapshotHistoryResult(
        status="ok",
        points=windowed,
        window_hours=window_hours,
        message=f"{len(windowed)} snapshots over last {window_hours:g}h (IST axis)",
    )


def collect_filter_options(
    snapshot: MonitorSnapshot,
    current_criteria: FilterCriteria | None = None,
) -> tuple[list[str], list[str], list[str]]:
    """Return ``(race_options, strategy_options, status_verdict_options)`` starting with ``'All'``."""
    races: set[str] = set()
    for tbl in (
        snapshot.exposure_table,
        snapshot.orders_table,
        snapshot.decisions_table,
        snapshot.quotes_table,
    ):
        for row in tbl.rows:
            rk = str(row.get("race_key") or "").strip()
            if rk:
                races.add(rk)
    for pos in snapshot.positions.rows:
        if pos.race_key and pos.race_key != "—":
            races.add(pos.race_key)
    for rp in snapshot.race_pnl.rows:
        if rp.race_key:
            races.add(rp.race_key)

    strategies: set[str] = set(ALL_STRATEGIES)
    for tbl in (snapshot.orders_table, snapshot.decisions_table):
        for row in tbl.rows:
            st_val = str(row.get("strategy") or "").strip()
            if st_val:
                strategies.add(st_val)

    status_verdicts: list[str] = [
        "All",
        "OPEN (orders)",
        *ALL_ORDER_STATUSES,
        *ALL_DECISION_VERDICTS,
    ]

    if current_criteria is not None:
        if current_criteria.race_key and current_criteria.race_key != "All":
            races.add(current_criteria.race_key)
        if current_criteria.strategy and current_criteria.strategy != "All":
            strategies.add(current_criteria.strategy)
        if (
            current_criteria.status_verdict
            and current_criteria.status_verdict not in status_verdicts
        ):
            status_verdicts.append(current_criteria.status_verdict)

    return (
        ["All", *sorted(races)],
        ["All", *sorted(strategies)],
        status_verdicts,
    )


def filter_orders_rows(
    rows: list[dict[str, Any]],
    criteria: FilterCriteria,
) -> list[dict[str, Any]]:
    """Filter ``orders`` rows by ``race_key``, ``strategy``, and ``status_verdict``."""
    filtered = rows
    if criteria.race_key != "All":
        filtered = [
            r for r in filtered if str(r.get("race_key") or "").strip() == criteria.race_key
        ]
    if criteria.strategy != "All":
        filtered = [
            r for r in filtered if str(r.get("strategy") or "").strip() == criteria.strategy
        ]
    sv = criteria.status_verdict
    if sv != "All":
        if sv == "OPEN (orders)":
            filtered = [
                r
                for r in filtered
                if str(r.get("status") or "").strip().lower() in OPEN_ORDER_STATUSES
            ]
        elif sv in ALL_ORDER_STATUSES or sv not in ALL_DECISION_VERDICTS:
            filtered = [
                r
                for r in filtered
                if str(r.get("status") or "").strip().lower() == sv.lower()
            ]
    return filtered


def filter_decisions_rows(
    rows: list[dict[str, Any]],
    criteria: FilterCriteria,
) -> list[dict[str, Any]]:
    """Filter ``decisions`` rows by ``race_key``, ``strategy``, and ``status_verdict``."""
    filtered = rows
    if criteria.race_key != "All":
        filtered = [
            r for r in filtered if str(r.get("race_key") or "").strip() == criteria.race_key
        ]
    if criteria.strategy != "All":
        filtered = [
            r for r in filtered if str(r.get("strategy") or "").strip() == criteria.strategy
        ]
    sv = criteria.status_verdict
    if sv != "All" and (
        sv in ALL_DECISION_VERDICTS
        or (sv != "OPEN (orders)" and sv not in ALL_ORDER_STATUSES)
    ):
        filtered = [
            r
            for r in filtered
            if str(r.get("verdict") or "").strip().lower() == sv.lower()
        ]
    return filtered


def filter_position_rows(
    rows: list[PositionRow],
    criteria: FilterCriteria,
) -> list[PositionRow]:
    """Filter ``PositionRow`` entries by ``race_key``."""
    if criteria.race_key == "All":
        return rows
    return [r for r in rows if r.race_key == criteria.race_key]


def filter_race_pnl_rows(
    rows: list[RacePnlRow],
    criteria: FilterCriteria,
) -> list[RacePnlRow]:
    """Filter ``RacePnlRow`` entries by ``race_key``."""
    if criteria.race_key == "All":
        return rows
    return [r for r in rows if r.race_key == criteria.race_key]


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
        if is_sqlite_busy_error(exc):
            return TableResult(
                table=table,
                status="db_busy",
                message=f"DB BUSY — SQLite lock contention on '{table}': {exc}",
            )
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
    """Evaluate heartbeat liveness (`ALIVE` / `STALE` / `NO HEARTBEAT` / `DB BUSY`) and flags."""
    stop_path = state_dir / "STOP"
    paused_path = state_dir / "PAUSED"
    stop_file_exists = stop_path.exists()
    paused_file_exists = paused_path.exists()
    paused_file_reason = _read_paused_file_reason(paused_path)

    if not heartbeat_table.has_data:
        no_data_status = (
            "DB BUSY" if heartbeat_table.status == "db_busy" else "NO HEARTBEAT"
        )
        no_data_age = (
            "DB BUSY" if heartbeat_table.status == "db_busy" else "NO DATA"
        )
        return HeartbeatState(
            status=no_data_status,
            age_seconds=None,
            age_display=no_data_age,
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


def _compute_hero_metrics(
    snapshots_table: TableResult,
    heartbeat_table: TableResult,
    exposure_table: TableResult,
    orders_table: TableResult,
    events_table: TableResult,
    parsed_pnl: ParsedPnlResult,
    now_utc: datetime,
) -> HeroMetrics:
    """Compute the 5 hero cards without fabricating zeros or guessing keys."""
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

        if parsed_pnl.status == "ok" and parsed_pnl.total_pnl is not None:
            pnl_display, pnl_direction = format_signed_susq(parsed_pnl.total_pnl)
            if parsed_pnl.source == "positions_unrealized":
                pnl_sub = (
                    f"Unrealized, SIG-reported, {len(parsed_pnl.rows)} race(s)"
                )
            else:
                pnl_sub = f"sum across {len(parsed_pnl.rows)} race(s) in pnl_json"
            pnl_available = True
        elif parsed_pnl.status == "bad_format":
            pnl_display = "NO DATA / BAD FORMAT"
            pnl_direction = "no_data"
            pnl_sub = parsed_pnl.message
            pnl_available = False
        else:
            start_bal = (
                parse_decimal(hb_row.get("start_balance"))
                if hb_row is not None
                else None
            )
            if bal_dec is not None and start_bal is not None:
                pnl_display, pnl_direction = format_signed_susq(bal_dec - start_bal)
                pnl_sub = f"vs start {format_susq(start_bal)}"
                pnl_available = True
            else:
                pnl_display = "NO DATA"
                pnl_direction = "no_data"
                pnl_sub = parsed_pnl.message or "pnl_json empty & no start_balance"
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
            open_orders_sub = (
                f"Snapshot: {snap_open_count} | Orders table open: {live_open_count}"
            )
        else:
            open_orders_sub = f"From snapshot #{snap_row.get('id')}"
        open_orders_available = True
    elif live_open_count is not None:
        open_orders_display = str(live_open_count)
        open_orders_sub = "Active in orders table (no snapshot row)"
        open_orders_available = True
    else:
        open_orders_display = "NO DATA"
        open_orders_sub = (
            snapshots_table.message or orders_table.message or "No order data"
        )
        open_orders_available = False

    # 4. Total exposure
    cap_total_dec = (
        parse_decimal(hb_row.get("cap_total")) if hb_row is not None else None
    )
    if exposure_table.status == "ok":
        exp_decimals = [parse_decimal(r.get("committed")) for r in exposure_table.rows]
        valid_exp = [d for d in exp_decimals if d is not None]
        if valid_exp:
            total_exp = sum(valid_exp, Decimal(0))
            exposure_display = format_susq(total_exp)
            if cap_total_dec is not None and cap_total_dec > 0:
                pct = (total_exp / cap_total_dec * Decimal(100)).quantize(
                    Decimal("0.1")
                )
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
    snapshot_history_limit: int = DEFAULT_SNAPSHOT_HISTORY_LIMIT,
    history_hours: float = DEFAULT_HISTORY_HOURS,
    busy_timeout_ms: int = SQLITE_BUSY_TIMEOUT_MS,
) -> MonitorSnapshot:
    """Load all contract tables and compute banner, hero, positions, P&L, and history states."""
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
        empty_pos = ParsedPositionsResult(status="no_data", message=missing_msg)
        empty_pnl = ParsedPnlResult(status="no_data", message=missing_msg)
        empty_hist = SnapshotHistoryResult(
            status="no_data", window_hours=history_hours, message=missing_msg
        )
        hero_state = _compute_hero_metrics(
            tables["snapshots"],
            tables["heartbeat"],
            tables["exposure"],
            tables["orders"],
            tables["events"],
            empty_pnl,
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
            positions=empty_pos,
            race_pnl=empty_pnl,
            snapshot_history=empty_hist,
            db_busy=False,
            db_busy_message=None,
        )

    try:
        conn = connect_readonly(resolved_db, busy_timeout_ms=busy_timeout_ms)
    except sqlite3.Error as exc:
        is_busy = is_sqlite_busy_error(exc)
        err_status = "db_busy" if is_busy else "missing_file"
        err_msg = (
            f"DB BUSY — {resolved_db.name} is locked by writer ({exc})"
            if is_busy
            else f"NO DATA — Cannot open {resolved_db.name} in read-only mode: {exc}"
        )
        tables = {
            name: TableResult(table=name, status=err_status, message=err_msg)
            for name in CONTRACT_SCHEMA
        }
        hb_state = _evaluate_heartbeat(tables["heartbeat"], resolved_state, current_utc)
        empty_pos = ParsedPositionsResult(status="no_data", message=err_msg)
        empty_pnl = ParsedPnlResult(status="no_data", message=err_msg)
        empty_hist = SnapshotHistoryResult(
            status="no_data", window_hours=history_hours, message=err_msg
        )
        hero_state = _compute_hero_metrics(
            tables["snapshots"],
            tables["heartbeat"],
            tables["exposure"],
            tables["orders"],
            tables["events"],
            empty_pnl,
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
            positions=empty_pos,
            race_pnl=empty_pnl,
            snapshot_history=empty_hist,
            db_busy=is_busy,
            db_busy_message=err_msg if is_busy else None,
        )

    try:
        heartbeat_table = _inspect_and_query_table(
            conn,
            "heartbeat",
            "SELECT * FROM heartbeat WHERE id = 1 LIMIT 1",
        )
        snapshots_table = _inspect_and_query_table(
            conn,
            "snapshots",
            "SELECT * FROM snapshots ORDER BY id DESC LIMIT ?",
            (snapshot_history_limit,),
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
    finally:
        conn.close()

    all_tables = (
        heartbeat_table,
        snapshots_table,
        exposure_table,
        orders_table,
        decisions_table,
        quotes_table,
        events_table,
    )
    busy_tables = [t for t in all_tables if t.status == "db_busy"]
    db_busy = len(busy_tables) > 0
    db_busy_message = busy_tables[0].message if busy_tables else None

    known_races: set[str] = set()
    for tbl in (exposure_table, orders_table, decisions_table, quotes_table):
        for r in tbl.rows:
            rk = str(r.get("race_key") or "").strip()
            if rk:
                known_races.add(rk)

    exchange_lookup: dict[str, tuple[str, str]] = {}
    for tbl in (orders_table, decisions_table, quotes_table):
        for r in tbl.rows:
            exch = str(r.get("sig_exchange_id") or "").strip()
            rk = str(r.get("race_key") or "").strip()
            if not exch or not rk:
                continue
            party = str(r.get("party") or "").strip()
            prev_party = exchange_lookup.get(exch, ("", ""))[1]
            exchange_lookup[exch] = (rk, party or prev_party)

    latest_snap = snapshots_table.rows[0] if snapshots_table.has_data else None
    positions_res = (
        parse_positions_json(
            latest_snap, known_race_keys=known_races, exchange_lookup=exchange_lookup
        )
        if latest_snap is not None
        else ParsedPositionsResult(
            status="no_data", message=snapshots_table.message or "No snapshots row"
        )
    )
    pnl_res = (
        parse_pnl_json(latest_snap, known_race_keys=known_races)
        if latest_snap is not None
        else ParsedPnlResult(
            status="no_data", message=snapshots_table.message or "No snapshots row"
        )
    )
    if pnl_res.status in {"no_data", "empty"}:
        derived = derive_pnl_from_positions(positions_res)
        if derived is not None:
            pnl_res = derived
    history_res = build_snapshot_history(snapshots_table, window_hours=history_hours)

    hb_state = _evaluate_heartbeat(heartbeat_table, resolved_state, current_utc)
    hero_state = _compute_hero_metrics(
        snapshots_table,
        heartbeat_table,
        exposure_table,
        orders_table,
        events_table,
        pnl_res,
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
        positions=positions_res,
        race_pnl=pnl_res,
        snapshot_history=history_res,
        db_busy=db_busy,
        db_busy_message=db_busy_message,
    )
