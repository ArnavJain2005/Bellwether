"""Read-only offline diagnostics and historical candidate analysis (BOT-DEPLOY-15K).

    python -m bot.diagnose [--db bot/state/bellwether.db] [--risk config/risk.yaml] ...

Never calls SIG or any external API, never reads .env or credentials, and never writes
to the database or state directory. Opens SQLite strictly read-only (`mode=ro`) and
takes an in-memory snapshot via SQLite's backup API so all queries see one consistent
point-in-time state.
"""

from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from collections import Counter, defaultdict
from collections.abc import Callable, Sequence
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime, timedelta
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any

from bot import db, risk, strategy
from bot.exits import DEFAULT_PATH as EXITS_PATH
from bot.exits import ExitsError, ExitSettings, load_exits
from bot.ladder import (
    DEFAULT_PATH as LADDER_PATH,
)
from bot.ladder import (
    HOLD_FILE,
    L0,
    LadderError,
    LoadedLadder,
    caps_text,
    load_ladder,
)
from bot.limits import DEFAULT_PATH as RISK_PATH
from bot.limits import Caps, Limits, LimitsError, load_limits
from bot.mapping import MAP_PATH as DEFAULT_MAP_PATH
from bot.mapping import MappedContract, MappingError, load_map
from bot.refs import RefQuote, fair_value, make_quote
from bot.risk import Intent, RiskState
from bot.strategy import SigQuote

SKIP_EVERY_S = Decimal(60)
ZERO = Decimal(0)


def _dec(value: Any, default: Decimal | None = None) -> Decimal | None:
    if value is None or value == "":
        return default
    try:
        d = Decimal(str(value))
    except InvalidOperation:
        return default
    return d if d.is_finite() else default


def _parse_ts(value: Any) -> datetime | None:
    if not value:
        return None
    try:
        dt = datetime.fromisoformat(str(value))
    except ValueError:
        return None
    return dt if dt.tzinfo is not None else dt.replace(tzinfo=UTC)


def _fmt(value: Decimal | None, places: int = 2) -> str:
    return "-" if value is None else f"{value:.{places}f}"


def _open_readonly_snapshot(db_path: Path) -> sqlite3.Connection:
    """Open `db_path` read-only and copy into an `:memory:` connection for consistency."""
    uri = f"{Path(db_path).resolve().as_uri()}?mode=ro"
    src = sqlite3.connect(uri, uri=True, timeout=5.0)
    try:
        src.execute("PRAGMA busy_timeout=5000")
        src.execute("PRAGMA query_only=ON")
        mem = sqlite3.connect(":memory:")
        src.backup(mem)
    finally:
        src.close()
    mem.row_factory = sqlite3.Row
    mem.execute("PRAGMA query_only=ON")
    return mem


def _has_table(conn: sqlite3.Connection, name: str) -> bool:
    row = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?", (name,)
    ).fetchone()
    return row is not None


def _normalize_skip_reason(reason: str) -> str:
    text = (reason or "").strip()
    if "; not sent: " in text:
        suffix = text.split("; not sent: ", 1)[1]
        if suffix.startswith("PAUSED"):
            return "blocked/skipped: PAUSED file present"
        if suffix.startswith("free cash "):
            return "blocked/skipped: rotation free-cash floor (reinvest_min_free_fraction)"
        if "min cash reserve" in suffix:
            return "blocked/skipped: min_cash_reserve floor"
        if "total committed cap" in suffix:
            return "blocked/skipped: total_committed cap"
        if "per-race cap" in suffix:
            return "blocked/skipped: per_race cap"
        if "max race fraction" in suffix:
            return "blocked/skipped: max_race_fraction cap"
        if "target deployment" in suffix:
            return "blocked/skipped: target_deployment cap"
        if "per-order cap" in suffix:
            return "blocked/skipped: per_order cap"
        if "book depth" in suffix:
            return "blocked/skipped: zero SIG orderbook depth at limit"
        if "SIG read budget" in suffix:
            return "blocked/skipped: SIG read budget headroom"
        if "re-entry block" in suffix:
            return "blocked/skipped: post-exit re-entry cooldown"
        if "already has an order this loop" in suffix:
            return "blocked/skipped: exchange already ordered this loop"
        if "orders this loop" in suffix:
            return "blocked/skipped: MAX_ORDERS_PER_LOOP cap"
        return f"blocked/skipped: {suffix[:80]}"
    if text.startswith("no edge:"):
        return "no edge (SIG quote inside fair +/- required edge)"
    if text.startswith("no complement:"):
        return "no complement (D+R asks > max_sum_ask and bids < min_sum_bid)"
    if text.startswith("no fair value: kalshi stale") or "stale (" in text:
        return "no fair value: reference quote stale"
    if text.startswith("no fair value: kalshi spread") or "spread " in text:
        return "no fair value: reference spread too wide"
    if "venues disagree" in text:
        return "no fair value: Kalshi and Polymarket disagree"
    if text.startswith("no fair value:"):
        return "no fair value: missing/inactive reference quote"
    if "NO orders are disabled" in text:
        return "edge present on NO side, but NO orders are disabled"
    if "already has a live order" in text:
        return "exchange busy (resting/live order)"
    if "cooling down" in text:
        return "exchange 30s post-order cooldown"
    if text.startswith("no SIG quote"):
        return "no SIG quote"
    if text.startswith("exit:"):
        if "exits disabled" in text:
            return "exit: exits disabled"
        if "hold:" in text:
            return "exit: hold (below exit/rotation threshold)"
        return f"exit: {text.removeprefix('exit:').strip()[:60]}"
    return text[:80] or "unspecified"


@dataclass(frozen=True)
class ReasonBucket:
    category: str
    strategy: str
    verdict: str
    throttled_rows: int
    distinct_subjects: int
    est_actual_evaluations: int


@dataclass(frozen=True)
class WindowSummary:
    minutes: int
    start_utc: str
    end_utc: str
    estimated_loops: int
    estimated_total_evaluations: int
    verdict_counts: dict[str, int]
    strategy_counts: dict[str, int]
    buckets: tuple[ReasonBucket, ...]


@dataclass(frozen=True)
class CandidateRow:
    race_key: str
    party: str
    sig_exchange_id: str
    action_side: str  # "yes" | "no" | "none"
    buy_price_side: str
    sig_bid: str
    sig_ask: str
    fair: str
    edge: str
    ref_source: str
    kalshi_quote: str
    kalshi_spread: str
    kalshi_age_s: str
    poly_quote: str
    poly_spread: str
    poly_age_s: str
    venue_disagreement: str
    high_confidence: bool
    high_confidence_detail: str
    passes_thresholds: dict[str, bool]
    passes_high_confidence_gate: bool
    race_committed: str
    active_blockers: tuple[str, ...]
    active_max_shares: int
    active_max_cost: str
    proposed_blockers: tuple[str, ...]
    proposed_max_shares: int
    proposed_max_cost: str


@dataclass
class DiagnosticReport:
    generated_utc: str
    reference_now_utc: str
    db_path: str
    control_files: dict[str, Any]
    heartbeat: dict[str, Any] | None
    config_drift: list[str]
    active_source: str
    effective_limits: dict[str, Any]
    ladder_summary: dict[str, Any]
    account_and_exposure: dict[str, Any]
    rotation_and_exits: dict[str, Any]
    orders_summary: dict[str, Any]
    windows: list[WindowSummary]
    current_quote_outcomes: dict[str, int]
    candidate_thresholds: list[str]
    candidate_counts_by_threshold: dict[str, dict[str, int]]
    candidates: list[CandidateRow] = field(default_factory=list)
    proposed_config: dict[str, Any] = field(default_factory=dict)


def _resolve_active_limits(
    base_limits: Limits,
    loaded_ladder: LoadedLadder | None,
    ladder_row: sqlite3.Row | None,
    beat: sqlite3.Row | None,
) -> tuple[Limits, str, list[str]]:
    """Determine the active Limits and detect any drift between disk YAML and heartbeat."""
    drift: list[str] = []
    disk_step = L0
    disk_limits = base_limits
    if loaded_ladder is not None and ladder_row is not None:
        stored_step = str(ladder_row["step"])
        if stored_step in loaded_ladder.names:
            disk_step = stored_step
            disk_limits = loaded_ladder.limits_for(stored_step)
        if ladder_row["ladder_sha256"] != loaded_ladder.sha256:
            drift.append(
                f"ladder_state sha256 ({str(ladder_row['ladder_sha256'])[:12]}) != "
                f"disk ladder.yaml sha256 ({loaded_ladder.sha256[:12]})"
            )
    if beat is None:
        return disk_limits, f"disk ({disk_step})", drift

    beat_dict = dict(beat)
    hb_step = beat_dict.get("ladder_step")
    hb_scale = _dec(beat_dict.get("scale"))
    hb_total = _dec(beat_dict.get("cap_total"))
    hb_race = _dec(beat_dict.get("cap_per_race"))
    hb_reserve = _dec(beat_dict.get("min_cash_reserve"))
    hb_mode = str(beat_dict.get("mode") or "")

    if hb_mode and hb_mode != base_limits.mode:
        drift.append(f"heartbeat mode={hb_mode!r} != disk risk.yaml mode={base_limits.mode!r}")
    if (hb_step or L0) != disk_step:
        drift.append(f"heartbeat ladder_step={hb_step!r} != stored/disk step={disk_step!r}")
    if hb_total is not None and hb_total != disk_limits.effective_cap("total_committed"):
        drift.append(
            f"heartbeat cap_total={hb_total} != disk effective total_committed="
            f"{disk_limits.effective_cap('total_committed')}"
        )
    if hb_race is not None and hb_race != disk_limits.effective_cap("per_race"):
        drift.append(
            f"heartbeat cap_per_race={hb_race} != disk effective per_race="
            f"{disk_limits.effective_cap('per_race')}"
        )
    if hb_reserve is not None and hb_reserve != disk_limits.effective_cap("min_cash_reserve"):
        drift.append(
            f"heartbeat min_cash_reserve={hb_reserve} != disk min_cash_reserve="
            f"{disk_limits.effective_cap('min_cash_reserve')}"
        )
    if hb_scale is not None and hb_scale != disk_limits.scale:
        drift.append(f"heartbeat scale={hb_scale} != disk scale={disk_limits.scale}")

    # Reconstruct the running in-memory caps from heartbeat when heartbeat is present
    if (
        hb_scale is not None
        and hb_scale > 0
        and hb_total is not None
        and hb_race is not None
        and hb_reserve is not None
    ):
        running_caps = Caps(
            per_order=disk_limits.caps.per_order,
            per_race=hb_race / hb_scale,
            total_committed=hb_total / hb_scale,
            min_cash_reserve=hb_reserve,
            loss_stop=disk_limits.caps.loss_stop,
        )
        running_limits = disk_limits.with_step(hb_scale, running_caps)
        source = f"heartbeat (step {hb_step or L0})" + (" [DRIFT vs disk]" if drift else "")
        return running_limits, source, drift

    return disk_limits, f"disk ({disk_step})", drift


def _check_blockers_for_leg(
    contract: MappedContract,
    side: str,
    price_side: Decimal,
    active_limits: Limits,
    balance: Decimal | None,
    total_committed: Decimal,
    race_committed: Decimal,
    journal_committed: Decimal,
    unknown_orders: int,
    writes_last_min: int,
    state_dir: Path,
    busy_exchanges: set[str],
    last_order: dict[str, datetime],
    reentry_blocked: dict[str, datetime],
    now: datetime,
) -> tuple[tuple[str, ...], int, Decimal]:
    """Return ALL risk/loop blockers that apply to a 1-share buy, plus max_shares & max_cost."""
    blockers: list[str] = []
    if (state_dir / "STOP").exists():
        blockers.append("STOP file present")
    if (state_dir / "PAUSED").exists():
        blockers.append("PAUSED file present")
    ex_block = strategy.exchange_blocked(contract.sig_exchange_id, busy_exchanges, last_order, now)
    if ex_block:
        blockers.append(ex_block)
    if contract.sig_exchange_id in reentry_blocked:
        until = reentry_blocked[contract.sig_exchange_id]
        blockers.append(f"re-entry cooldown until {db.utc_stamp(until)}")
    if side == "no" and not strategy.no_orders_enabled(active_limits):
        blockers.append("NO orders disabled")
    if unknown_orders > 0:
        blockers.append(f"{unknown_orders} unknown order(s)")
    if balance is None:
        blockers.append("balance unknown")
        return tuple(blockers), 0, ZERO

    # Check rotation free-cash gate (`_free_cash_low` in bot/main.py)
    if active_limits.rotating and active_limits.rotation is not None:
        free = balance - active_limits.effective_cap("min_cash_reserve")
        floor = active_limits.rotation.reinvest_min_free_fraction * balance
        if free <= floor:
            blockers.append(
                f"rotation free-cash floor (free {free:.2f} <= "
                f"{active_limits.rotation.reinvest_min_free_fraction:.1%} * balance = {floor:.2f})"
            )

    # Check each individual cap room for 1 share at price_side
    reserve = active_limits.effective_cap("min_cash_reserve")
    cash_room = balance - reserve
    if price_side > cash_room:
        blockers.append(
            f"min_cash_reserve ({reserve}): cash room {cash_room:.2f} < 1 share ({price_side:.3f})"
        )
    total_cap = active_limits.effective_cap("total_committed")
    total_room = total_cap - total_committed
    if price_side > total_room:
        blockers.append(
            f"total_committed cap ({total_cap}): room {total_room:.2f} < 1 share ({price_side:.3f})"
        )
    race_cap = active_limits.effective_cap("per_race")
    race_room = race_cap - race_committed
    if price_side > race_room:
        blockers.append(
            f"per_race cap ({race_cap}): race room {race_room:.2f} < 1 share ({price_side:.3f})"
        )
    equity = balance + total_committed
    if active_limits.max_race_fraction is not None:
        frac_room = active_limits.max_race_fraction * equity - race_committed
        if price_side > frac_room:
            blockers.append(
                f"max_race_fraction ({active_limits.max_race_fraction}): "
                f"room {frac_room:.2f} < 1 share ({price_side:.3f})"
            )
    if active_limits.target_deployment is not None:
        dep_room = active_limits.target_deployment * equity - total_committed
        if price_side > dep_room:
            blockers.append(
                f"target_deployment ({active_limits.target_deployment}): "
                f"room {dep_room:.2f} < 1 share ({price_side:.3f})"
            )

    rstate = RiskState(
        limits=active_limits,
        balance=balance,
        total_committed=total_committed,
        race_committed=race_committed,
        writes_last_min=writes_last_min,
        unknown_orders=unknown_orders,
        journal_committed=journal_committed,
        state_dir=state_dir,
    )
    intent = Intent(
        race_key=contract.race_key,
        sig_market_id=contract.sig_market_id,
        sig_exchange_id=contract.sig_exchange_id,
        side=side,
        price_side=price_side,
        quantity=1,
        strategy="reference",
    )
    res = risk.check(intent, rstate)
    if not res.allowed and res.reason not in blockers:
        blockers.append(res.reason)
    if blockers:
        return tuple(blockers), 0, ZERO
    max_shares = res.max_quantity
    return (), max_shares, price_side * max_shares


def build_report(
    *,
    db_path: Path = db.DB_PATH,
    state_dir: Path | None = None,
    risk_path: Path = RISK_PATH,
    ladder_path: Path = LADDER_PATH,
    exits_path: Path = EXITS_PATH,
    map_path: Path = DEFAULT_MAP_PATH,
    now: datetime | None = None,
    windows: Sequence[int] = (30, 120),
    thresholds: Sequence[Decimal] = (Decimal("0.06"), Decimal("0.04"), Decimal("0.03")),
    target_add_commitment: Decimal = Decimal(15000),
    target_cash_reserve: Decimal = Decimal(10000),
) -> DiagnosticReport:
    """Run all offline diagnostic and candidate analyses on a SQLite DB and YAML files."""
    db_path = Path(db_path)
    state_dir = Path(state_dir) if state_dir is not None else db_path.parent
    base_limits = load_limits(risk_path)
    loaded_ladder = load_ladder(base_limits, ladder_path)
    exits_settings: ExitSettings | None = None
    exits_error: str | None = None
    try:
        exits_settings = load_exits(exits_path)
    except ExitsError as exc:
        exits_error = str(exc)
    contracts = load_map(map_path)

    conn = _open_readonly_snapshot(db_path)
    try:
        beat = (
            conn.execute("SELECT * FROM heartbeat WHERE id = 1").fetchone()
            if _has_table(conn, "heartbeat")
            else None
        )
        ladder_row = (
            db.get_ladder_state(conn) if _has_table(conn, "ladder_state") else None
        )
        snap = (
            conn.execute("SELECT * FROM snapshots ORDER BY id DESC LIMIT 1").fetchone()
            if _has_table(conn, "snapshots")
            else None
        )
        order_rows = (
            conn.execute("SELECT * FROM orders ORDER BY created_utc ASC, rowid ASC").fetchall()
            if _has_table(conn, "orders")
            else []
        )
        quote_rows = (
            conn.execute("SELECT * FROM quotes_latest ORDER BY race_key, party").fetchall()
            if _has_table(conn, "quotes_latest")
            else []
        )
        exposure_rows = (
            conn.execute("SELECT * FROM exposure ORDER BY race_key").fetchall()
            if _has_table(conn, "exposure")
            else []
        )

        # Determine reference `now` from explicit flag, heartbeat, quotes, or current UTC
        ref_now = now
        if ref_now is None and beat is not None:
            ref_now = _parse_ts(beat["ts_utc"])
        if ref_now is None and quote_rows:
            stamps = [_parse_ts(r["ts_utc"]) for r in quote_rows]
            valid_stamps = [s for s in stamps if s is not None]
            if valid_stamps:
                ref_now = max(valid_stamps)
        if ref_now is None and snap is not None:
            ref_now = _parse_ts(snap["ts_utc"])
        if ref_now is None:
            ref_now = datetime.now(UTC)

        active_limits, active_source, drift = _resolve_active_limits(
            base_limits, loaded_ladder, ladder_row, beat
        )

        # Control files
        paused_file = state_dir / "PAUSED"
        pause_text = None
        if paused_file.exists():
            try:
                pause_text = paused_file.read_text(encoding="utf-8").strip()[:200]
            except OSError:
                pause_text = "unreadable"
        control_files = {
            "STOP": (state_dir / "STOP").exists(),
            "PAUSED": paused_file.exists(),
            "pause_reason": pause_text,
            "LADDER_HOLD": (state_dir / HOLD_FILE).exists(),
        }

        # Balance & exposure accounting
        journal_total, journal_by_race = risk.committed(order_rows, active_limits)
        journal_committed = journal_total - active_limits.preexisting_committed
        hb_committed = _dec(beat["committed"]) if beat is not None else None
        # If the live bot also tracked external_committed (unreconciled SIG positions),
        # heartbeat.committed reflects journal_total + external_committed.
        total_committed = (
            max(journal_total, hb_committed) if hb_committed is not None else journal_total
        )
        external_committed = max(ZERO, total_committed - journal_total)

        by_race: dict[str, Decimal] = defaultdict(lambda: ZERO, journal_by_race)
        for er in exposure_rows:
            rk = str(er["race_key"])
            c_val = _dec(er["committed"], ZERO) or ZERO
            by_race[rk] = max(by_race[rk], c_val)

        balance = _dec(beat["balance"]) if beat is not None else None
        if balance is None and snap is not None:
            balance = _dec(snap["balance"])

        reserve = active_limits.effective_cap("min_cash_reserve")
        total_cap = active_limits.effective_cap("total_committed")
        per_race_cap = active_limits.effective_cap("per_race")
        per_order_cap = active_limits.effective_cap("per_order")
        loss_stop_cap = active_limits.effective_cap("loss_stop")

        free_cash_above_reserve = (balance - reserve) if balance is not None else None
        rot_floor = (
            active_limits.rotation.reinvest_min_free_fraction * balance
            if (active_limits.rotating and active_limits.rotation and balance is not None)
            else ZERO
        )
        free_cash_above_rot = (
            (free_cash_above_reserve - rot_floor)
            if free_cash_above_reserve is not None
            else None
        )
        total_cap_room = total_cap - total_committed
        equity = (balance + total_committed) if balance is not None else None
        target_dep_room = (
            (active_limits.target_deployment * equity - total_committed)
            if (active_limits.target_deployment is not None and equity is not None)
            else None
        )
        rooms = [
            r
            for r in (free_cash_above_rot, total_cap_room, target_dep_room)
            if r is not None
        ]
        net_deployable = max(ZERO, min(rooms)) if rooms else ZERO

        binding_gates: list[str] = []
        if free_cash_above_reserve is not None and free_cash_above_reserve <= Decimal(5):
            binding_gates.append(
                f"min_cash_reserve ({reserve}): free cash is {free_cash_above_reserve:.2f} SUSQ"
            )
        if (
            active_limits.rotating
            and active_limits.rotation is not None
            and free_cash_above_reserve is not None
            and free_cash_above_reserve <= rot_floor
        ):
            binding_gates.append(
                f"rotation.reinvest_min_free_fraction "
                f"({active_limits.rotation.reinvest_min_free_fraction:.1%}): "
                f"free cash {free_cash_above_reserve:.2f} <= floor {rot_floor:.2f} SUSQ"
            )
        if total_cap_room <= Decimal(5):
            binding_gates.append(
                f"total_committed cap ({total_cap}): remaining room is {total_cap_room:.2f} SUSQ"
            )

        # Loss-stop proxy
        equity_cost_proxy = (balance + journal_committed) if balance is not None else None
        loss_stop_floor = (
            (active_limits.start_balance - loss_stop_cap)
            if active_limits.start_balance is not None
            else None
        )
        loss_stop_headroom = (
            (equity_cost_proxy - loss_stop_floor)
            if (equity_cost_proxy is not None and loss_stop_floor is not None)
            else None
        )

        # Orders summary
        status_counts: Counter[str] = Counter(str(r["status"]) for r in order_rows)
        unknown_list = [
            {
                "key": r["key"],
                "created_utc": r["created_utc"],
                "race_key": r["race_key"],
                "sig_exchange_id": r["sig_exchange_id"],
                "side": r["side"],
                "price_side": r["price_side"],
                "quantity": r["quantity"],
                "strategy": r["strategy"],
                "error": r["error"],
            }
            for r in order_rows
            if r["status"] == "unknown"
        ]
        recent_fills = [
            {
                "key": r["key"],
                "created_utc": r["created_utc"],
                "race_key": r["race_key"],
                "sig_exchange_id": r["sig_exchange_id"],
                "strategy": r["strategy"],
                "side": r["side"],
                "price_side": r["price_side"],
                "quantity": r["quantity"],
                "quantity_traded": r["quantity_traded"],
                "total_cost": r["total_cost"],
                "reason": r["reason"],
            }
            for r in reversed(order_rows)
            if r["status"] in ("filled", "partial")
        ][:15]

        # Busy exchanges & cooldowns
        live_statuses = {"journaled", "sent", "resting", "partial", "unknown"}
        busy_exchanges = {
            str(r["sig_exchange_id"]) for r in order_rows if r["status"] in live_statuses
        }
        last_order: dict[str, datetime] = {}
        reentry_blocked: dict[str, datetime] = {}
        cooldown_min = (
            active_limits.rotation.reentry_cooldown_minutes
            if (active_limits.rotating and active_limits.rotation)
            else (exits_settings.reentry_block_minutes if exits_settings else 0)
        )
        for r in order_rows:
            ts = _parse_ts(r["created_utc"])
            ex_id = str(r["sig_exchange_id"])
            if ts is not None:
                last_order[ex_id] = ts
                if (
                    r["strategy"] == "exit"
                    and r["status"] != "blocked"
                    and cooldown_min > 0
                ):
                    until = ts + timedelta(minutes=cooldown_min)
                    if until > ref_now:
                        reentry_blocked[ex_id] = until

        # Window skip/decision analysis (30m / 120m)
        races_count = len({c.race_key for c in contracts})
        subjects_per_loop = races_count + len(contracts)
        loop_s = max(1, active_limits.loop_seconds)
        throttle_factor = max(1, int(SKIP_EVERY_S // Decimal(loop_s)))

        window_summaries: list[WindowSummary] = []
        if _has_table(conn, "decisions"):
            for mins in windows:
                w_start = ref_now - timedelta(minutes=mins)
                w_start_iso = db.utc_stamp(w_start)
                w_end_iso = db.utc_stamp(ref_now)
                d_rows = conn.execute(
                    "SELECT * FROM decisions WHERE ts_utc >= ? AND ts_utc <= ? ORDER BY id ASC",
                    (w_start_iso, w_end_iso),
                ).fetchall()
                v_counts: Counter[str] = Counter()
                s_counts: Counter[str] = Counter()
                grouped: dict[tuple[str, str, str], list[sqlite3.Row]] = defaultdict(list)
                for dr in d_rows:
                    verdict = str(dr["verdict"] or "unknown")
                    strat = str(dr["strategy"] or "unknown")
                    v_counts[verdict] += 1
                    s_counts[strat] += 1
                    cat = _normalize_skip_reason(str(dr["reason"] or ""))
                    grouped[(cat, strat, verdict)].append(dr)

                buckets: list[ReasonBucket] = []
                for (cat, strat, verdict), rows_in_group in grouped.items():
                    subj_set = {
                        str(r["sig_exchange_id"] or f"race:{r['race_key']}")
                        for r in rows_in_group
                    }
                    raw_n = len(rows_in_group)
                    # `skip` verdicts are throttled by SKIP_EVERY_S (60s) per subject;
                    # `order`, `would_order`, and `blocked` are unthrottled (1:1).
                    est_evals = raw_n * throttle_factor if verdict == "skip" else raw_n
                    buckets.append(
                        ReasonBucket(
                            category=cat,
                            strategy=strat,
                            verdict=verdict,
                            throttled_rows=raw_n,
                            distinct_subjects=len(subj_set),
                            est_actual_evaluations=est_evals,
                        )
                    )
                buckets.sort(key=lambda b: (b.throttled_rows, b.distinct_subjects), reverse=True)
                est_loops = int((mins * 60) // loop_s)
                window_summaries.append(
                    WindowSummary(
                        minutes=mins,
                        start_utc=w_start_iso,
                        end_utc=w_end_iso,
                        estimated_loops=est_loops,
                        estimated_total_evaluations=est_loops * subjects_per_loop,
                        verdict_counts=dict(v_counts),
                        strategy_counts=dict(s_counts),
                        buckets=tuple(buckets),
                    )
                )

        # Build proposed limits (+15,000 commitment, 10,000 cash reserve, high-confidence 0.04)
        proposed_total_cap = (total_committed + target_add_commitment).quantize(Decimal(1))
        proposed_caps = Caps(
            per_order=active_limits.caps.per_order,
            per_race=max(active_limits.caps.per_race, Decimal(6000)),
            total_committed=proposed_total_cap / active_limits.scale,
            min_cash_reserve=target_cash_reserve,
            loss_stop=active_limits.caps.loss_stop,
        )
        proposed_ref = active_limits.strategies.reference.model_copy(
            update={"high_confidence_edge_election_night": Decimal("0.04")}
        )
        proposed_rot = (
            active_limits.rotation.model_copy(
                update={"reinvest_min_free_fraction": Decimal("0.0")}
            )
            if active_limits.rotation is not None
            else None
        )
        proposed_limits = active_limits.model_copy(
            update={
                "caps": proposed_caps,
                "strategies": active_limits.strategies.model_copy(
                    update={"reference": proposed_ref}
                ),
                "rotation": proposed_rot,
            }
        )

        # Reconstruct latest quotes from `quotes_latest`
        quotes_by_id: dict[str, sqlite3.Row] = {
            str(r["sig_exchange_id"]): r for r in quote_rows
        }
        current_outcomes: Counter[str] = Counter()
        candidates: list[CandidateRow] = []
        threshold_labels = [f"{t:.2f}" for t in thresholds]
        threshold_counts: dict[str, dict[str, int]] = {
            lbl: {"total": 0, "unblocked_active": 0, "unblocked_proposed": 0}
            for lbl in [*threshold_labels, "hc_0.04"]
        }

        ref_cfg = strategy.ref_settings(active_limits)
        min_threshold = min(thresholds) if thresholds else Decimal("0.03")

        for contract in contracts:
            qrow = quotes_by_id.get(contract.sig_exchange_id)
            if qrow is None:
                current_outcomes["no recorded quote in quotes_latest"] += 1
                continue
            sig_bid = _dec(qrow["sig_bid"])
            sig_ask = _dec(qrow["sig_ask"])
            sig_q = SigQuote(sig_bid, sig_ask, _dec(qrow["sig_last"]))

            k_ts = _parse_ts(qrow["kalshi_ts_utc"]) or ref_now
            k_bid = _dec(qrow["kalshi_bid"])
            k_ask = _dec(qrow["kalshi_ask"])
            k_quote: RefQuote | None = None
            if k_bid is not None or k_ask is not None:
                k_quote = make_quote("kalshi", contract.kalshi_ticker, k_bid, k_ask, k_ts)

            p_ts = _parse_ts(qrow["poly_ts_utc"]) or ref_now
            p_bid = _dec(qrow["poly_bid"])
            p_ask = _dec(qrow["poly_ask"])
            p_quote: RefQuote | None = None
            if contract.polymarket_token_id and (p_bid is not None or p_ask is not None):
                p_quote = make_quote(
                    "polymarket", contract.polymarket_token_id, p_bid, p_ask, p_ts
                )

            # Evaluate under active limits for unthrottled current snapshot summary
            ev_now = strategy.evaluate_reference(
                contract,
                sig_q,
                k_quote,
                p_quote,
                active_limits,
                ref_now,
                busy=busy_exchanges,
                last_order=last_order,
            )
            if ev_now.is_skip:
                current_outcomes[_normalize_skip_reason(ev_now.reason)] += 1
            else:
                current_outcomes["qualifies at active threshold"] += 1

            fair, ref_why = fair_value(k_quote, p_quote, ref_cfg, ref_now)
            hc_ok, hc_detail = strategy.check_high_confidence_reference(
                k_quote, p_quote, fair, proposed_limits, ref_now
            )

            # Compute candidate side & edge
            action_side = "none"
            buy_px: Decimal | None = None
            cand_edge: Decimal | None = None
            if fair is not None:
                yes_edge = (fair - sig_ask) if sig_ask is not None else None
                no_edge = (sig_bid - fair) if sig_bid is not None else None
                if yes_edge is not None and (no_edge is None or yes_edge >= no_edge):
                    if yes_edge > ZERO:
                        action_side = "yes"
                        buy_px = strategy.buy_price(sig_ask) if sig_ask is not None else None
                        cand_edge = yes_edge
                elif no_edge is not None and no_edge > ZERO:
                    action_side = "no"
                    buy_px = strategy.buy_price(1 - sig_bid) if sig_bid is not None else None
                    cand_edge = no_edge

            if cand_edge is None or cand_edge < min_threshold or buy_px is None:
                continue

            passes: dict[str, bool] = {}
            for t_val, lbl in zip(thresholds, threshold_labels, strict=True):
                passes[lbl] = cand_edge >= t_val

            req_hc_edge = Decimal("0.04") if hc_ok else Decimal("0.06")
            passes_hc = cand_edge >= req_hc_edge

            race_comm = by_race.get(contract.race_key, ZERO)
            writes_used = int(beat["writes_last_min"] or 0) if beat is not None else 0

            act_blockers, act_shares, act_cost = _check_blockers_for_leg(
                contract,
                action_side,
                buy_px,
                active_limits,
                balance,
                total_committed,
                race_comm,
                journal_committed,
                len(unknown_list),
                writes_used,
                state_dir,
                busy_exchanges,
                last_order,
                reentry_blocked,
                ref_now,
            )
            prop_blockers, prop_shares, prop_cost = _check_blockers_for_leg(
                contract,
                action_side,
                buy_px,
                proposed_limits,
                balance,
                total_committed,
                race_comm,
                journal_committed,
                len(unknown_list),
                writes_used,
                state_dir,
                busy_exchanges,
                last_order,
                reentry_blocked,
                ref_now,
            )

            for lbl, ok in passes.items():
                if ok:
                    threshold_counts[lbl]["total"] += 1
                    if not act_blockers and act_shares > 0:
                        threshold_counts[lbl]["unblocked_active"] += 1
                    if not prop_blockers and prop_shares > 0:
                        threshold_counts[lbl]["unblocked_proposed"] += 1
            if passes_hc:
                threshold_counts["hc_0.04"]["total"] += 1
                if not act_blockers and act_shares > 0:
                    threshold_counts["hc_0.04"]["unblocked_active"] += 1
                if not prop_blockers and prop_shares > 0:
                    threshold_counts["hc_0.04"]["unblocked_proposed"] += 1

            k_age = (
                f"{max(0.0, (ref_now - k_quote.fetched_utc).total_seconds()):.0f}s"
                if k_quote
                else "-"
            )
            p_age = (
                f"{max(0.0, (ref_now - p_quote.fetched_utc).total_seconds()):.0f}s"
                if p_quote
                else "-"
            )
            disagree = (
                _fmt(abs(k_quote.mid - p_quote.mid), 3)
                if (k_quote and k_quote.mid is not None and p_quote and p_quote.mid is not None)
                else "-"
            )

            candidates.append(
                CandidateRow(
                    race_key=contract.race_key,
                    party=contract.party,
                    sig_exchange_id=contract.sig_exchange_id,
                    action_side=action_side,
                    buy_price_side=_fmt(buy_px, 3),
                    sig_bid=_fmt(sig_bid, 3),
                    sig_ask=_fmt(sig_ask, 3),
                    fair=_fmt(fair, 3),
                    edge=_fmt(cand_edge, 3),
                    ref_source=ref_why,
                    kalshi_quote=(
                        f"{_fmt(k_quote.bid, 2)}/{_fmt(k_quote.ask, 2)}" if k_quote else "-"
                    ),
                    kalshi_spread=_fmt(k_quote.spread, 3) if k_quote else "-",
                    kalshi_age_s=k_age,
                    poly_quote=(
                        f"{_fmt(p_quote.bid, 2)}/{_fmt(p_quote.ask, 2)}" if p_quote else "-"
                    ),
                    poly_spread=_fmt(p_quote.spread, 3) if p_quote else "-",
                    poly_age_s=p_age,
                    venue_disagreement=disagree,
                    high_confidence=hc_ok,
                    high_confidence_detail=hc_detail,
                    passes_thresholds=passes,
                    passes_high_confidence_gate=passes_hc,
                    race_committed=_fmt(race_comm, 2),
                    active_blockers=act_blockers,
                    active_max_shares=act_shares,
                    active_max_cost=_fmt(act_cost, 2),
                    proposed_blockers=prop_blockers,
                    proposed_max_shares=prop_shares,
                    proposed_max_cost=_fmt(prop_cost, 2),
                )
            )

        candidates.sort(key=lambda c: Decimal(c.edge), reverse=True)

        # Build ladder summary
        ladder_steps_info = {}
        if loaded_ladder is not None:
            for sname in loaded_ladder.names:
                lim = loaded_ladder.limits_for(sname)
                ladder_steps_info[sname] = caps_text(lim)

        proposed_cfg = {
            "current_total_committed": _fmt(total_committed, 2),
            "current_balance": _fmt(balance, 2),
            "target_additional_commitment": _fmt(target_add_commitment, 2),
            "proposed_total_committed_cap": str(proposed_total_cap),
            "proposed_min_cash_reserve": str(target_cash_reserve),
            "proposed_reinvest_min_free_fraction": "0.0",
            "proposed_high_confidence_edge_election_night": "0.04",
            "exits_enabled": False,
        }

        return DiagnosticReport(
            generated_utc=db.utc_stamp(datetime.now(UTC)),
            reference_now_utc=db.utc_stamp(ref_now),
            db_path=str(db_path),
            control_files=control_files,
            heartbeat=dict(beat) if beat is not None else None,
            config_drift=drift,
            active_source=active_source,
            effective_limits={
                "mode": active_limits.mode,
                "scale": str(active_limits.scale),
                "per_order": str(per_order_cap),
                "per_race": str(per_race_cap),
                "total_committed": str(total_cap),
                "min_cash_reserve": str(reserve),
                "loss_stop": str(loss_stop_cap),
                "target_deployment": (
                    str(active_limits.target_deployment)
                    if active_limits.target_deployment is not None
                    else None
                ),
                "max_race_fraction": (
                    str(active_limits.max_race_fraction)
                    if active_limits.max_race_fraction is not None
                    else None
                ),
                "no_orders_enabled": active_limits.no_orders_enabled,
                "no_price_convention": active_limits.no_price_convention,
                "edge_daytime": str(active_limits.strategies.reference.edge),
                "edge_election_night": str(
                    active_limits.strategies.reference.edge_election_night
                ),
                "high_confidence_edge_election_night": (
                    str(
                        active_limits.strategies.reference.high_confidence_edge_election_night
                    )
                    if active_limits.strategies.reference.high_confidence_edge_election_night
                    is not None
                    else None
                ),
            },
            ladder_summary={
                "present": loaded_ladder is not None,
                "sha256": loaded_ladder.sha256[:12] if loaded_ladder else None,
                "max_step": loaded_ladder.max_step if loaded_ladder else None,
                "stored_step": ladder_row["step"] if ladder_row else None,
                "stored_since_utc": ladder_row["since_utc"] if ladder_row else None,
                "stored_sha256": (
                    str(ladder_row["ladder_sha256"])[:12] if ladder_row else None
                ),
                "steps": ladder_steps_info,
            },
            account_and_exposure={
                "snapshot_id": snap["id"] if snap else None,
                "snapshot_ts_utc": snap["ts_utc"] if snap else None,
                "effective_cash": _fmt(balance, 2),
                "journal_committed": _fmt(journal_committed, 2),
                "preexisting_committed": _fmt(active_limits.preexisting_committed, 2),
                "external_committed": _fmt(external_committed, 2),
                "total_committed": _fmt(total_committed, 2),
                "total_cap": _fmt(total_cap, 2),
                "total_cap_room": _fmt(total_cap_room, 2),
                "min_cash_reserve": _fmt(reserve, 2),
                "free_cash_above_reserve": _fmt(free_cash_above_reserve, 2),
                "rotation_free_cash_floor": _fmt(rot_floor, 2),
                "free_cash_above_rotation_floor": _fmt(free_cash_above_rot, 2),
                "net_deployable_cash": _fmt(net_deployable, 2),
                "binding_gates": binding_gates,
                "loss_stop_equity_proxy": _fmt(equity_cost_proxy, 2),
                "loss_stop_floor": _fmt(loss_stop_floor, 2),
                "loss_stop_headroom": _fmt(loss_stop_headroom, 2),
                "loss_stop_note": (
                    "loss_stop compares (balance + journal_committed) at original entry cost "
                    "against (start_balance - loss_stop); it does NOT capture mark-to-market "
                    "unrealized losses on open positions."
                ),
                "active_races_count": sum(1 for v in by_race.values() if v > ZERO),
            },
            rotation_and_exits={
                "rotation_enabled": active_limits.rotating,
                "take_profit_pct": (
                    str(active_limits.rotation.take_profit_pct)
                    if active_limits.rotation
                    else None
                ),
                "edge_gone": (
                    active_limits.rotation.edge_gone if active_limits.rotation else None
                ),
                "reinvest_min_free_fraction": (
                    str(active_limits.rotation.reinvest_min_free_fraction)
                    if active_limits.rotation
                    else None
                ),
                "reentry_cooldown_minutes": (
                    active_limits.rotation.reentry_cooldown_minutes
                    if active_limits.rotation
                    else None
                ),
                "exits_yaml_enabled": (
                    exits_settings.enabled if exits_settings is not None else False
                ),
                "exits_yaml_error": exits_error,
                "exits_mechanism": (
                    exits_settings.resolved_mechanism if exits_settings is not None else None
                ),
                "exit_edge": (
                    str(exits_settings.exit_edge) if exits_settings is not None else None
                ),
                "exit_edge_election_night": (
                    str(exits_settings.exit_edge_election_night)
                    if exits_settings is not None
                    else None
                ),
                "reentry_blocked_exchanges": {
                    k: db.utc_stamp(v) for k, v in reentry_blocked.items()
                },
            },
            orders_summary={
                "status_counts": dict(status_counts),
                "unknown_orders_count": len(unknown_list),
                "unknown_orders": unknown_list,
                "recent_fills": recent_fills,
            },
            windows=window_summaries,
            current_quote_outcomes=dict(current_outcomes),
            candidate_thresholds=[*threshold_labels, "hc_0.04"],
            candidate_counts_by_threshold=threshold_counts,
            candidates=candidates,
            proposed_config=proposed_cfg,
        )
    finally:
        conn.close()


def format_report(report: DiagnosticReport) -> str:
    """Format a human-readable diagnostic and historical candidate report."""
    lines: list[str] = []
    out = lines.append

    out("=" * 88)
    out("BELLWETHER OFFLINE DIAGNOSTIC & HISTORICAL CANDIDATE REPORT (READ-ONLY, NO NETWORK)")
    out("=" * 88)
    out(f"Database         : {report.db_path}")
    out(f"Reference Now    : {report.reference_now_utc} (Generated: {report.generated_utc})")
    out(f"Active Limits Src: {report.active_source}")
    cf = report.control_files
    out(
        f"Control Files    : STOP={cf['STOP']} | PAUSED={cf['PAUSED']}"
        + (f" ({cf['pause_reason']})" if cf["pause_reason"] else "")
        + f" | LADDER_HOLD={cf['LADDER_HOLD']}"
    )
    if report.config_drift:
        out("\n[!] CONFIG DRIFT DETECTED BETWEEN RUNNING HEARTBEAT AND DISK YAML:")
        for d in report.config_drift:
            out(f"    - {d}")

    out("\n--- 1. EFFECTIVE CASH, EXPOSURE & LIMITS -----------------------------------------------")
    ae = report.account_and_exposure
    el = report.effective_limits
    out(
        f"Effective Cash (balance)      : {ae['effective_cash']} SUSQ "
        f"(snapshot #{ae['snapshot_id']} @ {ae['snapshot_ts_utc']})"
    )
    out(
        f"Committed Exposure            : {ae['total_committed']} / {ae['total_cap']} SUSQ "
        f"(room: {ae['total_cap_room']} SUSQ across {ae['active_races_count']} race(s))"
    )
    out(
        f"  breakdown                   : journal={ae['journal_committed']} + "
        f"preexisting={ae['preexisting_committed']} + external={ae['external_committed']}"
    )
    out(
        f"Cash Reserve Floor            : {ae['min_cash_reserve']} SUSQ "
        f"(free cash above reserve: {ae['free_cash_above_reserve']} SUSQ)"
    )
    out(
        f"Rotation Free-Cash Floor      : {ae['rotation_free_cash_floor']} SUSQ "
        f"(free cash above rotation floor: {ae['free_cash_above_rotation_floor']} SUSQ)"
    )
    out(f"Net Deployable Cash Right Now : {ae['net_deployable_cash']} SUSQ")
    if ae["binding_gates"]:
        out("Binding Gate(s) Blocking Buys :")
        for g in ae["binding_gates"]:
            out(f"  * {g}")
    out(
        f"Effective Caps (scale={el['scale']}) : "
        f"per_order={el['per_order']} | per_race={el['per_race']} | "
        f"total={el['total_committed']} | reserve={el['min_cash_reserve']} | "
        f"loss_stop={el['loss_stop']}"
    )
    out(
        f"Deployment Fractions          : target_deployment={el['target_deployment']} | "
        f"max_race_fraction={el['max_race_fraction']}"
    )
    out(
        f"Reference Edges               : daytime={el['edge_daytime']} | "
        f"election_night={el['edge_election_night']} | "
        f"high_confidence_election_night={el['high_confidence_edge_election_night']}"
    )
    out(
        f"Loss-Stop Proxy               : equity_proxy={ae['loss_stop_equity_proxy']} vs "
        f"floor={ae['loss_stop_floor']} (headroom={ae['loss_stop_headroom']})"
    )
    out(f"  NOTE: {ae['loss_stop_note']}")

    out("\n--- 2. LADDER, ROTATION & EXIT STATUS --------------------------------------------------")
    ls = report.ladder_summary
    out(
        f"Ladder Present={ls['present']} | max_step={ls['max_step']} | "
        f"stored_step={ls['stored_step']} (since {ls['stored_since_utc']}) | "
        f"disk_sha={ls['sha256']} vs stored_sha={ls['stored_sha256']}"
    )
    for sname, stext in ls["steps"].items():
        out(f"  {sname}: {stext}")
    re_info = report.rotation_and_exits
    out(
        f"Rotation : enabled={re_info['rotation_enabled']} | "
        f"take_profit_pct={re_info['take_profit_pct']} | "
        f"edge_gone={re_info['edge_gone']} | "
        f"reinvest_min_free_fraction={re_info['reinvest_min_free_fraction']} | "
        f"cooldown={re_info['reentry_cooldown_minutes']}m"
    )
    out(
        f"Exits    : exits.yaml enabled={re_info['exits_yaml_enabled']} | "
        f"mechanism={re_info['exits_mechanism']} | "
        f"exit_edge={re_info['exit_edge']} (night={re_info['exit_edge_election_night']})"
    )

    out("\n--- 3. ORDERS, RECENT FILLS & UNKNOWN ORDERS -------------------------------------------")
    os_info = report.orders_summary
    out(f"Order Status Counts : {os_info['status_counts']}")
    out(f"Unknown Orders      : {os_info['unknown_orders_count']}")
    for uo in os_info["unknown_orders"]:
        out(
            f"  [UNKNOWN] key={uo['key']} {uo['created_utc']} {uo['race_key']} "
            f"ex={uo['sig_exchange_id']} {uo['side']} {uo['quantity']}@{uo['price_side']} "
            f"err={uo['error']}"
        )
    out(f"Recent Fills ({len(os_info['recent_fills'])} shown):")
    for rf in os_info["recent_fills"]:
        out(
            f"  [FILL] {rf['created_utc']} {rf['race_key']:<18} ex={rf['sig_exchange_id']:<6} "
            f"{rf['strategy']:<10} {str(rf['side']).upper():<3} "
            f"{rf['quantity_traded']}/{rf['quantity']} @ {rf['price_side']} "
            f"(cost={rf['total_cost']})"
        )

    out("\n--- 4. SKIP & DECISION REASONS (THROTTLED DB ROWS vs ACTUAL LOOP EVALUATIONS) ----------")
    out(
        "NOTE: `decisions` throttles `skip` rows to at most 1 per subject every 60s "
        "(SKIP_EVERY_S=60), whereas the loop evaluates all races/contracts every "
        "loop_seconds (10s). `order`, `would_order`, and `blocked` rows are unthrottled."
    )
    for w in report.windows:
        out(
            f"\nWindow Last {w.minutes}m ({w.start_utc} -> {w.end_utc}): "
            f"est_loops={w.estimated_loops}, est_subject_evals={w.estimated_total_evaluations}"
        )
        out(f"  Recorded DB rows by verdict : {w.verdict_counts}")
        out(f"  Recorded DB rows by strategy: {w.strategy_counts}")
        for b in w.buckets[:12]:
            out(
                f"  - [{b.verdict:<11} | {b.strategy:<10}] "
                f"throttled_rows={b.throttled_rows:<5} "
                f"distinct_subjects={b.distinct_subjects:<4} "
                f"est_evals={b.est_actual_evaluations:<6} : {b.category}"
            )
    out("\nCurrent Unthrottled Snapshot Evaluation Across All Mapped Contracts:")
    for k, v in sorted(report.current_quote_outcomes.items(), key=lambda x: -x[1]):
        out(f"  - {v:<4} contract(s): {k}")

    out("\n--- 5. HISTORICAL OFFLINE CANDIDATE ANALYSIS (6c vs 4c vs 3c + HIGH-CONFIDENCE) --------")
    out("WARNING: HISTORICAL OFFLINE ANALYSIS ONLY — BASED ON RECORDED QUOTES IN SQLITE.")
    out("         NOT CURRENT LIVE TRADE RECOMMENDATIONS.")
    for lbl, counts in report.candidate_counts_by_threshold.items():
        out(
            f"  Threshold {lbl:<8}: {counts['total']:<3} qualifying candidate(s) | "
            f"unblocked under active caps: {counts['unblocked_active']:<3} | "
            f"unblocked under proposed (+15k/10k floor) caps: {counts['unblocked_proposed']:<3}"
        )
    if not report.candidates:
        out("  No recorded quotes met the minimum candidate threshold.")
    else:
        out(
            "\n  RACE (PARTY)       EXCH   SIDE BUY_PX FAIR  EDGE  REF_SOURCE / VALIDITY"
            "                 HC_GATE   RACE_EXP  ACTIVE_BLOCKER -> PROPOSED"
        )
        for c in report.candidates[:25]:
            hc_str = "PASS" if c.high_confidence else f"NO({c.high_confidence_detail[:22]})"
            act_b = "OK" if not c.active_blockers else c.active_blockers[0][:34]
            prop_b = (
                f"OK(max {c.proposed_max_shares}sh={c.proposed_max_cost})"
                if not c.proposed_blockers
                else c.proposed_blockers[0][:34]
            )
            ref_info = (
                f"K:{c.kalshi_quote}(sp={c.kalshi_spread},{c.kalshi_age_s}) "
                f"P:{c.poly_quote}(sp={c.poly_spread},{c.poly_age_s}) d={c.venue_disagreement}"
            )
            out(
                f"  {c.race_key:<14} ({c.party}) {c.sig_exchange_id:<6} "
                f"{c.action_side.upper():<4} {c.buy_price_side:<6} {c.fair:<5} {c.edge:<5} "
                f"{ref_info:<37} {hc_str:<9} {c.race_committed:<8}  {act_b} -> {prop_b}"
            )

    out("\n--- 6. PROPOSED RUNNING CONFIGURATION (+15,000 COMMITMENT, 10,000 CASH RESERVE) --------")
    pc = report.proposed_config
    out(
        f"Proposed Caps: total_committed={pc['proposed_total_committed_cap']} "
        f"(current {pc['current_total_committed']} + {pc['target_additional_commitment']}), "
        f"min_cash_reserve={pc['proposed_min_cash_reserve']}, "
        f"reinvest_min_free_fraction={pc['proposed_reinvest_min_free_fraction']}, "
        f"high_confidence_edge_election_night={pc['proposed_high_confidence_edge_election_night']}, "
        f"exits.enabled={pc['exits_enabled']}"
    )
    out("=" * 88)
    return "\n".join(lines)


def main(
    argv: Sequence[str] | None = None,
    *,
    out: Callable[[str], None] = print,
) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m bot.diagnose",
        description="Read-only offline diagnostic and historical candidate analyzer.",
    )
    parser.add_argument("--db", type=Path, default=db.DB_PATH, help="SQLite database path")
    parser.add_argument("--state-dir", type=Path, default=None, help="state directory path")
    parser.add_argument("--risk", type=Path, default=RISK_PATH, help="config/risk.yaml path")
    parser.add_argument(
        "--ladder", type=Path, default=LADDER_PATH, help="config/ladder.yaml path"
    )
    parser.add_argument("--exits", type=Path, default=EXITS_PATH, help="config/exits.yaml path")
    parser.add_argument("--map", type=Path, default=DEFAULT_MAP_PATH, help="bot/map.yaml path")
    parser.add_argument("--now", type=str, default=None, help="ISO UTC timestamp override")
    parser.add_argument(
        "--windows", type=str, default="30,120", help="comma-separated lookback windows (min)"
    )
    parser.add_argument(
        "--thresholds",
        type=str,
        default="0.06,0.04,0.03",
        help="comma-separated hypothetical edges",
    )
    parser.add_argument(
        "--target-add-commitment",
        type=Decimal,
        default=Decimal(15000),
        help="max additional commitment to propose (default 15000)",
    )
    parser.add_argument(
        "--target-cash-reserve",
        type=Decimal,
        default=Decimal(10000),
        help="cash reserve floor to propose (default 10000)",
    )
    parser.add_argument("--json", action="store_true", help="output JSON instead of text")
    args = parser.parse_args(argv)

    if not Path(args.db).exists():
        out(f"error: database not found at {args.db}")
        return 1
    now_dt = _parse_ts(args.now) if args.now else None
    if args.now and now_dt is None:
        out(f"error: invalid --now timestamp {args.now!r}")
        return 2
    try:
        windows = [int(x.strip()) for x in args.windows.split(",") if x.strip()]
        thresholds = [Decimal(x.strip()) for x in args.thresholds.split(",") if x.strip()]
        report = build_report(
            db_path=args.db,
            state_dir=args.state_dir,
            risk_path=args.risk,
            ladder_path=args.ladder,
            exits_path=args.exits,
            map_path=args.map,
            now=now_dt,
            windows=windows,
            thresholds=thresholds,
            target_add_commitment=args.target_add_commitment,
            target_cash_reserve=args.target_cash_reserve,
        )
    except (LimitsError, LadderError, MappingError, sqlite3.Error, ValueError) as exc:
        out(f"error: {exc}")
        return 2

    if args.json:
        out(json.dumps(asdict(report), indent=2, default=str))
    else:
        out(format_report(report))
    return 0


if __name__ == "__main__":
    sys.exit(main())
