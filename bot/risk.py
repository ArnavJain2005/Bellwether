"""risk.check(intent, state): the one gate every new order passes (hard rule 3).

The bot only buys. Cost of a buy = price_side x quantity in SUSQies (bot/CONTRACT.md).

Committed (conservative where the wire is unresolved):
- a live order (journaled, sent, resting, unknown) counts its filled part plus
  price_side x its unfilled quantity; with no established fill quantity it counts in
  full (C1: an unknown outcome counts at full size);
- a closed order (filled, partial, cancelled, expired) counts its filled part; with no
  established fill quantity it counts in full;
- the filled part is the larger of SIG's totalCost and price_side x quantity_traded. A
  buy never fills above its limit, so the second is an upper bound whatever totalCost's
  units turn out to be (OQ-W4);
- rejected orders count nothing; preexisting_committed is added once to the total.
A probe NO order's price convention is unknown (OQ-W3), so it counts at
max(p, 1 - p) per share.
"""

from __future__ import annotations

import math
import sqlite3
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from pathlib import Path

from bot.db import STATE_DIR
from bot.limits import Limits

TICK = Decimal("0.005")
MIN_PRICE = Decimal("0.005")
MAX_PRICE = Decimal("0.995")
PROBE_MAX_COST = Decimal(1)  # SUSQies

STRATEGIES = frozenset({"complement", "reference", "probe"})
LIVE_STATUSES = frozenset({"journaled", "sent", "resting", "unknown"})
CLOSED_STATUSES = frozenset({"filled", "partial", "cancelled", "expired"})


@dataclass(frozen=True)
class Intent:
    """One buy the bot wants to make. price_side is the price of the side bought."""

    race_key: str
    sig_market_id: str
    sig_exchange_id: str
    side: str
    price_side: Decimal
    quantity: int
    strategy: str
    action: str = "buy"
    reason: str = ""


@dataclass(frozen=True)
class RiskState:
    """Everything check() needs, gathered by the caller (see build_state)."""

    limits: Limits
    balance: Decimal | None  # tournament cash; None when it could not be read
    total_committed: Decimal  # includes preexisting_committed
    race_committed: Decimal  # this intent's race only
    writes_last_min: int
    unknown_orders: int
    state_dir: Path = STATE_DIR


@dataclass(frozen=True)
class RiskResult:
    allowed: bool
    reason: str
    max_quantity: int


def on_grid(price: object) -> bool:
    """A finite Decimal on the 0.005 tick within 0.005-0.995."""
    return (
        isinstance(price, Decimal)
        and price.is_finite()
        and MIN_PRICE <= price <= MAX_PRICE
        and price % TICK == 0
    )


def _dec(value: object) -> Decimal | None:
    if value is None:
        return None
    try:
        out = Decimal(str(value))
    except InvalidOperation:
        return None
    return out if out.is_finite() else None


def effective_price(side: str, price_side: Decimal, strategy: str) -> Decimal:
    """Per-share cost used for limits; a probe NO uses the worse of both conventions."""
    if strategy == "probe" and side == "no":
        return max(price_side, 1 - price_side)
    return price_side


def order_commitment(row: Mapping[str, object]) -> Decimal:
    """What one journaled order commits, per the rules in the module docstring."""
    status = row["status"]
    if status == "rejected":
        return Decimal(0)
    price = _dec(row["price_side"])
    quantity = _dec(row["quantity"])
    if price is None or quantity is None:
        raise ValueError(f"order {row['key']} has an unreadable price or quantity")
    price = effective_price(str(row["side"]), price, str(row["strategy"]))
    traded = _dec(row["quantity_traded"])
    if traded is None or traded < 0 or traded > quantity:
        return price * quantity  # fill state not established: count it in full
    cost = _dec(row["total_cost"])
    filled = max(cost if cost is not None and cost > 0 else Decimal(0), price * traded)
    if status in LIVE_STATUSES:
        return filled + price * (quantity - traded)
    if status in CLOSED_STATUSES:
        return filled
    raise ValueError(f"order {row['key']} has an unknown status")


def committed(rows: Iterable[Mapping[str, object]], limits: Limits) -> tuple[Decimal, dict]:
    """(total committed including preexisting, committed per race_key)."""
    by_race: dict[str, Decimal] = {}
    total = limits.preexisting_committed
    for row in rows:
        amount = order_commitment(row)
        total += amount
        race = str(row["race_key"])
        by_race[race] = by_race.get(race, Decimal(0)) + amount
    return total, by_race


def build_state(
    conn: sqlite3.Connection,
    limits: Limits,
    *,
    race_key: str,
    balance: Decimal | None,
    writes_last_min: int,
    state_dir: Path = STATE_DIR,
    extra_committed: Decimal = Decimal(0),
) -> RiskState:
    """`extra_committed` adds to the total only (open SIG orders not in the journal)."""
    rows = conn.execute("SELECT * FROM orders").fetchall()
    total, by_race = committed(rows, limits)
    unknown = sum(1 for row in rows if row["status"] == "unknown")
    return RiskState(
        limits=limits,
        balance=balance,
        total_committed=total + extra_committed,
        race_committed=by_race.get(race_key, Decimal(0)),
        writes_last_min=writes_last_min,
        unknown_orders=unknown,
        state_dir=state_dir,
    )


def _deny(reason: str) -> RiskResult:
    return RiskResult(allowed=False, reason=reason, max_quantity=0)


def _floor_shares(room: Decimal, price: Decimal) -> int:
    return max(0, math.floor(room / price)) if room > 0 else 0


def check(intent: Intent, state: RiskState, *, probe: bool = False) -> RiskResult:
    """Allow or deny one buy. Every result carries a plain-English reason.

    `probe` is for bot/probe.py only: it permits a NO buy whatever the NO settings say,
    requires strategy 'probe' and caps the order's cost at 1 SUSQie.
    """
    limits = state.limits
    if (state.state_dir / "STOP").exists():
        return _deny("STOP file exists: no new orders")
    if (state.state_dir / "PAUSED").exists():
        return _deny("PAUSED file exists: no new orders until Arnav deletes it")
    if state.unknown_orders > 0:
        return _deny(
            f"{state.unknown_orders} order(s) have an unknown outcome: no new orders "
            "until Arnav resolves them"
        )
    if intent.action != "buy":
        return _deny(f"action {intent.action!r} refused: the bot only buys")
    if intent.strategy not in STRATEGIES:
        return _deny(f"unknown strategy {intent.strategy!r}")
    if probe != (intent.strategy == "probe"):
        return _deny("the probe flag and strategy 'probe' must go together")
    if intent.side not in ("yes", "no"):
        return _deny(f"side {intent.side!r} is neither 'yes' nor 'no'")
    if intent.side == "no" and not probe:
        if not limits.no_orders_enabled:
            return _deny("NO orders are disabled (no_orders_enabled is false)")
        if limits.no_price_convention == "unknown":
            return _deny("NO orders refused: no_price_convention is unknown (OQ-W3)")
    if not isinstance(intent.price_side, Decimal):
        return _deny("price must be an exact Decimal, never a float")
    if not on_grid(intent.price_side):
        return _deny(f"price {intent.price_side} is off the 0.005 grid or outside 0.005-0.995")
    if (
        isinstance(intent.quantity, bool)
        or not isinstance(intent.quantity, int)
        or intent.quantity < 1
    ):
        return _deny("quantity must be a whole number of shares above 0")
    if state.balance is None:
        return _deny("tournament balance is unknown: no new orders")
    loss_stop = limits.effective_cap("loss_stop")
    if limits.start_balance is not None and state.balance < limits.start_balance - loss_stop:
        return _deny(
            f"loss stop: balance {state.balance} is below start {limits.start_balance} "
            f"minus {loss_stop}"
        )
    if state.writes_last_min >= limits.max_writes_per_min:
        return _deny(
            f"{state.writes_last_min} writes in the last minute, limit {limits.max_writes_per_min}"
        )

    price = effective_price(intent.side, intent.price_side, intent.strategy)
    per_order = limits.effective_cap("per_order")
    if probe:
        per_order = min(per_order, PROBE_MAX_COST)
    per_race = limits.effective_cap("per_race")
    total_cap = limits.effective_cap("total_committed")
    reserve = limits.effective_cap("min_cash_reserve")
    rooms = [
        ("per-order cap", per_order),
        (f"per-race cap for {intent.race_key}", per_race - state.race_committed),
        ("total committed cap", total_cap - state.total_committed),
        ("minimum cash reserve", state.balance - reserve),
    ]
    max_quantity = min(_floor_shares(room, price) for _, room in rooms)
    cost = price * intent.quantity
    for name, room in rooms:
        if cost > room:
            return RiskResult(
                allowed=False,
                reason=(
                    f"cost {cost} exceeds the room of {max(room, Decimal(0))} under the "
                    f"{name}; at most {max_quantity} share(s) would pass"
                ),
                max_quantity=max_quantity,
            )
    return RiskResult(
        allowed=True,
        reason=(
            f"buy {intent.quantity} {intent.side.upper()} at {intent.price_side} costs {cost}, "
            f"within every cap; at most {max_quantity} share(s) would pass"
        ),
        max_quantity=max_quantity,
    )
