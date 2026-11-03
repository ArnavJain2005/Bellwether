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

Loss stop: buying moves cash into positions, so cash alone falls with every ordinary
buy. The loss check compares an equity proxy, balance + journal_committed (what the
journal's own orders commit, without preexisting_committed or orders outside the
journal), against start_balance - effective loss_stop. It is only a proxy: the journal
counts what was paid, so it does not catch mark-to-market losses on open positions,
nor a position that settles at zero while its filled cost still counts. The reserve and
the per-order, per-race and total caps still use cash and committed as before.
BOT-ROTATE adds two optional rooms on equity (balance + total committed):
target_deployment x equity - total committed, and max_race_fraction x equity - race
committed.

Exits (BOT-EW-5) pass check_exit, never check: a complement buy that nets against a
holding frees capital, so the caps do not apply, but the holding must come from a fresh
positions read and the exit may never exceed it (an exit larger than the holding would
open the opposite position, as a sell would under SIG's canonicalisation). Their
commitment is in exit_commitments: a closed exit releases the average journal cost of
the shares it closed (not its proceeds), so balance + journal_committed moves by the
realised P&L.
"""

from __future__ import annotations

import math
import sqlite3
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from pathlib import Path

from bot.db import STATE_DIR
from bot.exits import ExitSettings
from bot.limits import Limits

TICK = Decimal("0.005")
MIN_PRICE = Decimal("0.005")
MAX_PRICE = Decimal("0.995")
PROBE_MAX_COST = Decimal(1)  # SUSQies

STRATEGIES = frozenset({"complement", "reference", "probe"})
EXIT_STRATEGIES = frozenset({"exit", "exit_probe"})
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
    exit_of_side: str | None = None  # exits only: the held side this buy closes


@dataclass(frozen=True)
class HeldPosition:
    """One exchange's holding from a SIG positions read (positive YES, negative NO).

    `fresh` is True only for a read taken in this loop's exit stage; the 60 s account
    snapshot is never fresh.
    """

    exchange_id: str
    side: str  # "yes" | "no"
    quantity: Decimal  # > 0, as SIG sent it (OQ-W5: whole shares are checked, not assumed)
    fresh: bool


@dataclass(frozen=True)
class RiskState:
    """Everything check() needs, gathered by the caller (see build_state)."""

    limits: Limits
    balance: Decimal | None  # tournament cash; None when it could not be read
    total_committed: Decimal  # includes preexisting_committed
    race_committed: Decimal  # this intent's race only
    writes_last_min: int
    unknown_orders: int
    # The journal's own orders only (no preexisting, no external): the loss-stop proxy.
    journal_committed: Decimal
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


def _strategy(row: Mapping[str, object]) -> str:
    return str(row["strategy"])


def _filled(row: Mapping[str, object]) -> tuple[Decimal, Decimal] | None:
    """(filled cost, filled shares) of one buy; None when its fill is not established.

    Filled cost is the larger of totalCost and price_side x quantity_traded, as in
    order_commitment.
    """
    if row["status"] == "rejected":
        return Decimal(0), Decimal(0)
    price, quantity = _dec(row["price_side"]), _dec(row["quantity"])
    traded = _dec(row["quantity_traded"])
    if price is None or quantity is None or traded is None or traded < 0 or traded > quantity:
        return None
    cost = _dec(row["total_cost"])
    price = effective_price(str(row["side"]), price, _strategy(row))
    return max(cost if cost is not None and cost > 0 else Decimal(0), price * traded), traded


def exit_commitments(rows: Iterable[Mapping[str, object]]) -> dict[str, Decimal]:
    """What each exit row commits (negative = capital released), by order key.

    For each exchange and held side, the basis is the journal's own filled buys of that
    side (exits excluded): average cost = filled cost / filled shares, over all of them.
    An exit that closed t shares releases average cost x t, taken in creation order and
    never beyond the basis's shares (shares the journal did not buy, such as manual
    holdings, release nothing). The unfilled part of a live exit (journaled, sent,
    resting, unknown) counts at its buy price in full, since with the holding gone it
    would open the opposite position; an exit whose fill is not established counts in
    full and releases nothing. Rejected exits count nothing.
    """
    rows = list(rows)
    basis: dict[tuple[str, str], tuple[Decimal, Decimal]] = {}  # (exchange, side) -> cost, shares
    for row in rows:
        if _strategy(row) in EXIT_STRATEGIES:
            continue
        filled = _filled(row)
        if filled is None:
            continue
        group = (str(row["sig_exchange_id"]), str(row["side"]))
        cost, shares = basis.get(group, (Decimal(0), Decimal(0)))
        basis[group] = (cost + filled[0], shares + filled[1])
    closed: dict[tuple[str, str], Decimal] = {}
    out: dict[str, Decimal] = {}
    exits = [row for row in rows if _strategy(row) in EXIT_STRATEGIES]
    for row in sorted(exits, key=lambda r: (str(r["created_utc"]), str(r["key"]))):
        key = str(row["key"])
        if row["status"] == "rejected":
            out[key] = Decimal(0)
            continue
        held = row["exit_of_side"]
        if held not in ("yes", "no"):
            raise ValueError(f"exit order {key} has no exit_of_side")
        price, quantity = _dec(row["price_side"]), _dec(row["quantity"])
        if price is None or quantity is None:
            raise ValueError(f"order {key} has an unreadable price or quantity")
        traded = _dec(row["quantity_traded"])
        if traded is None or traded < 0 or traded > quantity:
            out[key] = price * quantity  # fill not established: count it in full
            continue
        group = (str(row["sig_exchange_id"]), str(held))
        cost, shares = basis.get(group, (Decimal(0), Decimal(0)))
        done = closed.get(group, Decimal(0))
        released = max(Decimal(0), min(traded, shares - done))
        closed[group] = done + released
        release = cost * released / shares if shares > 0 else Decimal(0)
        if row["status"] in LIVE_STATUSES:
            out[key] = price * (quantity - traded) - release
        elif row["status"] in CLOSED_STATUSES:
            out[key] = -release
        else:
            raise ValueError(f"order {key} has an unknown status")
    return out


def committed(rows: Iterable[Mapping[str, object]], limits: Limits) -> tuple[Decimal, dict]:
    """(total committed including preexisting, committed per race_key)."""
    rows = list(rows)
    exits = exit_commitments(rows)
    by_race: dict[str, Decimal] = {}
    total = limits.preexisting_committed
    for row in rows:
        if _strategy(row) in EXIT_STRATEGIES:
            amount = exits[str(row["key"])]
        else:
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
        journal_committed=total - limits.preexisting_committed,
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
    equity = state.balance + state.journal_committed  # a proxy: see the module docstring
    if limits.start_balance is not None and equity < limits.start_balance - loss_stop:
        return _deny(
            f"loss stop: balance {state.balance} + journal committed "
            f"{state.journal_committed} = {equity} is below start {limits.start_balance} "
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
    # BOT-ROTATE: fractions of equity, balance + total committed (cash + what is invested).
    equity = state.balance + state.total_committed
    if limits.target_deployment is not None:
        rooms.append(
            (
                f"target deployment {limits.target_deployment}",
                limits.target_deployment * equity - state.total_committed,
            )
        )
    if limits.max_race_fraction is not None:
        rooms.append(
            (
                f"max race fraction {limits.max_race_fraction} for {intent.race_key}",
                limits.max_race_fraction * equity - state.race_committed,
            )
        )
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


# --- exits ------------------------------------------------------------------------------


def live_exit_quantity(rows: Iterable[Mapping[str, object]], exchange_id: str) -> Decimal:
    """Unfilled shares of live exit orders on one exchange (in full when unestablished)."""
    total = Decimal(0)
    for row in rows:
        if (
            _strategy(row) not in EXIT_STRATEGIES
            or str(row["sig_exchange_id"]) != exchange_id
            or row["status"] not in LIVE_STATUSES
        ):
            continue
        quantity = _dec(row["quantity"]) or Decimal(0)
        traded = _dec(row["quantity_traded"])
        if traded is None or traded < 0 or traded > quantity:
            traded = Decimal(0)
        total += quantity - traded
    return total


def check_exit(
    intent: Intent,
    state: RiskState,
    *,
    held: HeldPosition | None,
    settings: ExitSettings | None,
    live_exit_quantity: Decimal,
    live_orders_on_exchange: int,
    require_fresh: bool,
    probe: bool = False,
) -> RiskResult:
    """Allow or deny one exit: a complement buy that closes held shares.

    The caps do not apply (an exit frees capital). Refused when: STOP, PAUSED or
    LADDER_HOLD exists; any order is unknown; the holding is not from a fresh read
    (`require_fresh`, i.e. live) or is for another exchange or side; the quantity
    exceeds the holding minus live exits there; any order is live on the exchange (its
    fills could change the holding after the read); exits are disabled or the side is
    not in `sides`. `probe` is for bot/exit_probe.py only: strategy 'exit_probe',
    exactly 1 share, `enabled` and `sides` not required.
    """
    limits = state.limits
    for name in ("STOP", "PAUSED", "LADDER_HOLD"):
        if (state.state_dir / name).exists():
            return _deny(f"{name} file exists: no exits")
    if state.unknown_orders > 0:
        return _deny(
            f"{state.unknown_orders} order(s) have an unknown outcome: no exits until Arnav "
            "resolves them"
        )
    if intent.strategy not in EXIT_STRATEGIES:
        return _deny(f"strategy {intent.strategy!r} is not an exit")
    if probe != (intent.strategy == "exit_probe"):
        return _deny("the probe flag and strategy 'exit_probe' must go together")
    if intent.action != "buy":
        return _deny(f"action {intent.action!r} refused: exits are complement buys, never sells")
    held_side = intent.exit_of_side
    if held_side not in ("yes", "no"):
        return _deny("an exit must name the held side it closes")
    if intent.side != ("no" if held_side == "yes" else "yes"):
        return _deny(f"an exit of {held_side.upper()} must buy the other side")
    if settings is None:
        return _deny("exits are off (no valid config/exits.yaml)")
    if settings.resolved_mechanism != "complement_buy":
        return _deny("only the complement_buy mechanism is built")
    if not probe:
        if not settings.enabled:
            return _deny("exits are disabled (config/exits.yaml enabled: false)")
        if held_side not in settings.sides:
            return _deny(f"exits on {held_side.upper()} holdings are not enabled (sides)")
    if intent.side == "no":
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
    if probe and intent.quantity != 1:
        return _deny("the exit probe closes exactly 1 share")
    if held is None:
        return _deny("no holding read for this exchange: nothing to exit")
    if held.exchange_id != intent.sig_exchange_id:
        return _deny("the holding read is for another exchange")
    if require_fresh and not held.fresh:
        return _deny("the holding is not from a fresh positions read in this loop")
    if held.side != held_side:
        return _deny(
            f"the holding is {held.side.upper()}, not the {held_side.upper()} being exited"
        )
    quantity = _dec(held.quantity)
    if quantity is None or quantity <= 0 or quantity != quantity.to_integral_value():
        return _deny(f"held quantity {held.quantity} is not a whole number of shares (OQ-W5)")
    if live_orders_on_exchange > 0:
        return _deny(
            f"{live_orders_on_exchange} order(s) live on exchange {intent.sig_exchange_id}: "
            "their fills could change the holding"
        )
    room = quantity - live_exit_quantity
    if intent.quantity > room:
        return _deny(
            f"exit of {intent.quantity} exceeds held {quantity} minus live exits "
            f"{live_exit_quantity}"
        )
    if state.writes_last_min >= limits.max_writes_per_min:
        return _deny(
            f"{state.writes_last_min} writes in the last minute, limit {limits.max_writes_per_min}"
        )
    return RiskResult(
        allowed=True,
        reason=(
            f"exit: buy {intent.quantity} {intent.side.upper()} at {intent.price_side} closes "
            f"{intent.quantity} of {quantity} {held_side.upper()} held"
        ),
        max_quantity=int(room),
    )


def journal_position(
    rows: Iterable[Mapping[str, object]], exchange_id: str
) -> tuple[Decimal, Decimal, Decimal]:
    """(signed shares the journal filled, lowest and highest pending change) on one exchange.

    YES fills count +, NO fills -, exits included: with netting, SIG's signed position
    moves by exactly these. A live order, or one whose fill is not established, may
    still add its remainder in its own direction, so the position is explained anywhere
    in [filled + low, filled + high].
    """
    filled = low = high = Decimal(0)
    for row in rows:
        if str(row["sig_exchange_id"]) != exchange_id or row["status"] == "rejected":
            continue
        sign = 1 if row["side"] == "yes" else -1
        quantity = _dec(row["quantity"]) or Decimal(0)
        traded = _dec(row["quantity_traded"])
        established = traded is not None and 0 <= traded <= quantity
        if established:
            filled += sign * traded
        if row["status"] in LIVE_STATUSES or not established:
            rest = quantity - (traded if established and traded is not None else Decimal(0))
            if sign > 0:
                high += rest
            else:
                low -= rest
    return filled, low, high


def entry_price(
    rows: Iterable[Mapping[str, object]], exchange_id: str, side: str, held: Decimal
) -> Decimal | None:
    """Average entry price per share of the `held` shares on one exchange and side.

    SIG nets FIFO, so the shares still held are the newest lots. The lots are the
    journal's own filled buys of that side (exits excluded), newest first, each at its
    filled cost per share (the larger of totalCost and price_side x quantity_traded, an
    upper bound). None (unknown: no take-profit or edge-gone exit) when the journal's
    lots do not cover `held` (manual or pre-journal shares), or a buy there has a fill
    that is not established.
    """
    if held <= 0:
        return None
    lots: list[tuple[str, str, Decimal, Decimal]] = []
    for row in rows:
        if (
            str(row["sig_exchange_id"]) != exchange_id
            or str(row["side"]) != side
            or _strategy(row) in EXIT_STRATEGIES
            or row["status"] == "rejected"
        ):
            continue
        filled = _filled(row)
        if filled is None:
            return None
        cost, shares = filled
        if shares > 0:
            lots.append((str(row["created_utc"]), str(row["key"]), cost, shares))
    need, total = held, Decimal(0)
    for _, _, cost, shares in sorted(lots, reverse=True):
        take = min(need, shares)
        total += cost * take / shares
        need -= take
        if need == 0:
            return total / held
    return None
