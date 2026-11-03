"""Strategies: pure functions, no I/O. bot/main.py feeds them quotes and sends orders.

Prices are YES-denominated SIG quotes in [0, 1] except `Leg.price_side`, the price of
the side bought (a NO price for a NO buy), as in bot/CONTRACT.md. Every price is snapped
to the 0.005 grid in the less aggressive direction: a buy is floored.

- One live order per exchange, and a 30 s cooldown per exchange after any order.
- Complement: a race with exactly D and R approved. Best asks summing to <= max_sum_ask
  -> buy YES on both at their asks. Best bids summing to >= min_sum_bid -> buy NO on both
  at 1 - bid (NO orders enabled only). Both legs get the same quantity.
- Reference: fair from refs.fair_value; the edge switches to edge_election_night at
  election_night_from_utc. Ask <= fair - edge -> buy YES at the ask. Bid >= fair + edge
  -> buy NO at 1 - bid (NO orders enabled only).
- Size: min(risk max_quantity, book depth at the price); below 1 share -> skip.
- Exit (BOT-EW-5, reduce-only, config/exits.yaml): for a holding on side S with fair
  f_S (f for YES, 1 - f for NO) and SIG's best bid for S, b_S (YES: bid; NO: 1 - ask),
  exit when b_S >= f_S + exit_edge (exit_edge_election_night from
  election_night_from_utc). The exit is a complement buy at 1 - b_S, floored, which
  nets against the holding (bot/exits.py). Same fair-value checks, one-live-order and
  cooldown rules as the reference strategy. Size: min(held x
  max_exit_quantity_fraction, book depth at the price), whole shares.
"""

from __future__ import annotations

import math
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
from decimal import ROUND_CEILING, ROUND_FLOOR, Decimal

from bot.exits import ExitSettings
from bot.limits import Limits
from bot.mapping import MappedContract
from bot.refs import RefQuote, RefSettings, RefSnapshot, fair_value

GRID = Decimal("0.005")
MIN_PRICE = Decimal("0.005")
MAX_PRICE = Decimal("0.995")
COOLDOWN_S = 30


@dataclass(frozen=True)
class SigQuote:
    """SIG best bid/ask and last trade for one exchange, YES-denominated, exact."""

    bid: Decimal | None
    ask: Decimal | None
    last: Decimal | None = None


@dataclass(frozen=True)
class Leg:
    contract: MappedContract
    side: str  # "yes" | "no"
    price_side: Decimal  # on the grid


@dataclass(frozen=True)
class Evaluation:
    """One strategy verdict. No legs means skip; `reason` is plain English."""

    strategy: str
    race_key: str
    subject: str  # skip-throttle key: the exchange id, or "race:<race_key>"
    legs: tuple[Leg, ...]
    reason: str
    party: str | None = None
    sig_exchange_id: str | None = None
    sig_bid: Decimal | None = None
    sig_ask: Decimal | None = None
    kalshi_mid: Decimal | None = None
    poly_mid: Decimal | None = None
    fair: Decimal | None = None
    edge: Decimal | None = None
    exit_of_side: str | None = None  # exits only: the held side the legs close

    @property
    def is_skip(self) -> bool:
        return not self.legs


# --- prices -----------------------------------------------------------------------------


def floor_to_grid(price: Decimal) -> Decimal:
    return (price / GRID).to_integral_value(rounding=ROUND_FLOOR) * GRID


def ceil_to_grid(price: Decimal) -> Decimal:
    return (price / GRID).to_integral_value(rounding=ROUND_CEILING) * GRID


def buy_price(raw: Decimal) -> Decimal | None:
    """A buy limit snapped down to the grid, or None if outside 0.005-0.995."""
    price = floor_to_grid(raw)
    return price if MIN_PRICE <= price <= MAX_PRICE else None


def fmt(value: Decimal | None, places: int = 3) -> str:
    return "-" if value is None else f"{value:.{places}f}"


def ref_settings(limits: Limits) -> RefSettings:
    ref = limits.strategies.reference
    return RefSettings(ref.max_ref_spread, Decimal(ref.max_ref_age_s), ref.max_venue_disagreement)


def is_election_night(limits: Limits, now: datetime) -> bool:
    return now >= limits.strategies.reference.election_night_from_utc


def current_edge(limits: Limits, now: datetime) -> Decimal:
    ref = limits.strategies.reference
    return ref.edge_election_night if is_election_night(limits, now) else ref.edge


def no_orders_enabled(limits: Limits) -> bool:
    return limits.no_orders_enabled and limits.no_price_convention != "unknown"


def exchange_blocked(
    exchange_id: str, busy: Iterable[str], last_order: Mapping[str, datetime], now: datetime
) -> str | None:
    """Why this exchange may not get an order now, or None."""
    if exchange_id in set(busy):
        return f"exchange {exchange_id} already has a live order"
    last = last_order.get(exchange_id)
    if last is not None:
        waited = (now - last).total_seconds()
        if waited < COOLDOWN_S:
            return f"exchange {exchange_id} cooling down ({waited:.0f}s of {COOLDOWN_S}s)"
    return None


# --- reference --------------------------------------------------------------------------


def _ref_text(kalshi: RefQuote | None, poly: RefQuote | None) -> str:
    k = "-" if kalshi is None else f"{fmt(kalshi.bid, 2)}/{fmt(kalshi.ask, 2)}"
    p = "-" if poly is None or poly.mid is None else fmt(poly.mid, 2)
    return f"(Kalshi {k}, Poly {p})"


def evaluate_reference(
    contract: MappedContract,
    sig: SigQuote | None,
    kalshi: RefQuote | None,
    poly: RefQuote | None,
    limits: Limits,
    now: datetime,
    *,
    busy: Iterable[str] = (),
    last_order: Mapping[str, datetime] | None = None,
) -> Evaluation:
    base = {
        "strategy": "reference",
        "race_key": contract.race_key,
        "subject": contract.sig_exchange_id,
        "party": contract.party,
        "sig_exchange_id": contract.sig_exchange_id,
        "sig_bid": sig.bid if sig else None,
        "sig_ask": sig.ask if sig else None,
        "kalshi_mid": kalshi.mid if kalshi else None,
        "poly_mid": poly.mid if poly else None,
    }

    def skip(reason: str, **extra: object) -> Evaluation:
        return Evaluation(legs=(), reason=reason, **base, **extra)  # type: ignore[arg-type]

    if not limits.strategies.reference.enabled:
        return skip("reference strategy disabled")
    blocked = exchange_blocked(contract.sig_exchange_id, busy, last_order or {}, now)
    if blocked:
        return skip(blocked)
    if sig is None or (sig.bid is None and sig.ask is None):
        return skip("no SIG quote")
    fair, why = fair_value(kalshi, poly, ref_settings(limits), now)
    refs = _ref_text(kalshi, poly)
    if fair is None:
        return skip(f"no fair value: {why} {refs}")
    edge = current_edge(limits, now)
    if sig.ask is not None and sig.ask <= fair - edge:
        price = buy_price(sig.ask)
        if price is None:
            return skip(f"SIG ask {fmt(sig.ask)} is outside the tradable range", fair=fair)
        reason = f"SIG ask {fmt(sig.ask)} <= fair {fmt(fair)} - edge {fmt(edge)} {refs}"
        return Evaluation(
            legs=(Leg(contract, "yes", price),),
            reason=reason,
            fair=fair,
            edge=fair - sig.ask,
            **base,  # type: ignore[arg-type]
        )
    if sig.bid is not None and sig.bid >= fair + edge:
        reason = f"SIG bid {fmt(sig.bid)} >= fair {fmt(fair)} + edge {fmt(edge)} {refs}"
        if not no_orders_enabled(limits):
            return skip(f"{reason}, but NO orders are disabled", fair=fair, edge=sig.bid - fair)
        price = buy_price(1 - sig.bid)
        if price is None:
            return skip(f"NO price {fmt(1 - sig.bid)} is outside the tradable range", fair=fair)
        return Evaluation(
            legs=(Leg(contract, "no", price),),
            reason=reason,
            fair=fair,
            edge=sig.bid - fair,
            **base,  # type: ignore[arg-type]
        )
    return skip(
        f"no edge: SIG {fmt(sig.bid)}/{fmt(sig.ask)} vs fair {fmt(fair)} +/- {fmt(edge)} {refs}",
        fair=fair,
    )


# --- exit -------------------------------------------------------------------------------


def other_side(side: str) -> str:
    return "no" if side == "yes" else "yes"


def current_exit_edge(settings: ExitSettings, limits: Limits, now: datetime) -> Decimal:
    if is_election_night(limits, now):
        return settings.exit_edge_election_night
    return settings.exit_edge


def evaluate_exit(
    contract: MappedContract,
    sig: SigQuote | None,
    kalshi: RefQuote | None,
    poly: RefQuote | None,
    held_side: str | None,
    held_qty: Decimal | None,
    settings: ExitSettings | None,
    limits: Limits,
    now: datetime,
    *,
    busy: Iterable[str] = (),
    last_order: Mapping[str, datetime] | None = None,
) -> Evaluation:
    """Whether to close a holding. A leg is the complement buy that nets against it."""
    base = {
        "strategy": "exit",
        "race_key": contract.race_key,
        "subject": f"exit:{contract.sig_exchange_id}",
        "party": contract.party,
        "sig_exchange_id": contract.sig_exchange_id,
        "sig_bid": sig.bid if sig else None,
        "sig_ask": sig.ask if sig else None,
        "kalshi_mid": kalshi.mid if kalshi else None,
        "poly_mid": poly.mid if poly else None,
        "exit_of_side": held_side,
    }

    def skip(reason: str, **extra: object) -> Evaluation:
        return Evaluation(legs=(), reason=f"exit: {reason}", **base, **extra)  # type: ignore[arg-type]

    if settings is None or not settings.enabled:
        return skip("exits disabled")
    if held_side not in ("yes", "no") or held_qty is None or held_qty < 1:
        return skip("nothing held")
    if held_side not in settings.sides:
        return skip(f"exits on {held_side.upper()} holdings are not enabled (sides)")
    blocked = exchange_blocked(contract.sig_exchange_id, busy, last_order or {}, now)
    if blocked:
        return skip(blocked)
    if sig is None:
        return skip("no SIG quote")
    fair, why = fair_value(kalshi, poly, ref_settings(limits), now)
    refs = _ref_text(kalshi, poly)
    if fair is None:
        return skip(f"no fair value: {why} {refs}")
    edge = current_exit_edge(settings, limits, now)
    if held_side == "yes":
        if sig.bid is None:
            return skip(f"no SIG bid for the YES held {refs}", fair=fair)
        fair_side, bid_side = fair, sig.bid
        text = f"SIG bid {fmt(sig.bid)} >= fair {fmt(fair)} + {fmt(edge)} {refs}"
        miss = f"SIG bid {fmt(sig.bid)} < fair {fmt(fair)} + {fmt(edge)} {refs}"
    else:
        if sig.ask is None:
            return skip(f"no SIG ask for the NO held {refs}", fair=fair)
        fair_side, bid_side = 1 - fair, 1 - sig.ask
        text = (
            f"SIG NO bid {fmt(bid_side)} (1 - ask {fmt(sig.ask)}) >= NO fair "
            f"{fmt(fair_side)} + {fmt(edge)} {refs}"
        )
        miss = (
            f"SIG NO bid {fmt(bid_side)} (1 - ask {fmt(sig.ask)}) < NO fair "
            f"{fmt(fair_side)} + {fmt(edge)} {refs}"
        )
    if bid_side < fair_side + edge:
        return skip(f"hold: {miss}", fair=fair, edge=bid_side - fair_side)
    side = other_side(held_side)
    if side == "no" and not no_orders_enabled(limits):
        return skip(f"{text}, but NO orders are disabled", fair=fair, edge=bid_side - fair_side)
    # Floored: the complement buy pays at most 1 - b_S, so the shares close at >= b_S.
    price = buy_price(1 - bid_side)
    if price is None:
        return skip(
            f"complement price {fmt(1 - bid_side)} is outside the tradable range", fair=fair
        )
    return Evaluation(
        legs=(Leg(contract, side, price),),
        reason=f"exit {held_side.upper()}: {text}",
        fair=fair,
        edge=bid_side - fair_side,
        **base,  # type: ignore[arg-type]
    )


def exit_quantity(held_qty: Decimal, fraction: Decimal, depth: Decimal) -> int:
    """min(held x fraction, depth at the price), floored to whole shares; 0 means skip."""
    if held_qty <= 0 or depth <= 0:
        return 0
    return max(0, math.floor(min(held_qty * fraction, depth)))


def exit_text(leg: Leg, quantity: int, held_side: str) -> str:
    return f"{order_text(leg, quantity)} to close {quantity} {held_side.upper()}"


# --- complement -------------------------------------------------------------------------


def evaluate_complement(
    race_key: str,
    contracts: Sequence[MappedContract],
    quotes: Mapping[str, SigQuote],
    limits: Limits,
    now: datetime,
    *,
    busy: Iterable[str] = (),
    last_order: Mapping[str, datetime] | None = None,
) -> Evaluation | None:
    """None when the race does not have exactly D and R approved."""
    by_party = {c.party: c for c in contracts}
    if len(contracts) != 2 or set(by_party) != {"D", "R"}:
        return None
    d, r = by_party["D"], by_party["R"]
    qd, qr = quotes.get(d.sig_exchange_id), quotes.get(r.sig_exchange_id)
    base = {"strategy": "complement", "race_key": race_key, "subject": f"race:{race_key}"}

    def skip(reason: str, edge: Decimal | None = None) -> Evaluation:
        return Evaluation(legs=(), reason=reason, edge=edge, **base)  # type: ignore[arg-type]

    settings = limits.strategies.complement
    if not settings.enabled:
        return skip("complement strategy disabled")
    for contract in (d, r):
        blocked = exchange_blocked(contract.sig_exchange_id, busy, last_order or {}, now)
        if blocked:
            return skip(blocked)
    if qd is None or qr is None:
        return skip("no SIG quote for both D and R")
    text = f"D {fmt(qd.bid)}/{fmt(qd.ask)}, R {fmt(qr.bid)}/{fmt(qr.ask)}"
    if qd.ask is not None and qr.ask is not None:
        total = qd.ask + qr.ask
        if total <= settings.max_sum_ask:
            pd, pr = buy_price(qd.ask), buy_price(qr.ask)
            if pd is None or pr is None:
                return skip(f"asks sum {fmt(total)} but a price is outside the tradable range")
            reason = f"asks D {fmt(qd.ask)} + R {fmt(qr.ask)} = {fmt(total)} <= {fmt(settings.max_sum_ask)}"
            return Evaluation(
                legs=(Leg(d, "yes", pd), Leg(r, "yes", pr)),
                reason=reason,
                edge=1 - total,
                **base,  # type: ignore[arg-type]
            )
    if qd.bid is not None and qr.bid is not None:
        total = qd.bid + qr.bid
        if total >= settings.min_sum_bid:
            reason = f"bids D {fmt(qd.bid)} + R {fmt(qr.bid)} = {fmt(total)} >= {fmt(settings.min_sum_bid)}"
            if not no_orders_enabled(limits):
                return skip(f"{reason}, but NO orders are disabled", edge=total - 1)
            pd, pr = buy_price(1 - qd.bid), buy_price(1 - qr.bid)
            if pd is None or pr is None:
                return skip(f"bids sum {fmt(total)} but a NO price is outside the tradable range")
            return Evaluation(
                legs=(Leg(d, "no", pd), Leg(r, "no", pr)),
                reason=reason,
                edge=total - 1,
                **base,  # type: ignore[arg-type]
            )
    return skip(f"no complement: {text}")


# --- all --------------------------------------------------------------------------------


def evaluate_all(
    contracts: Sequence[MappedContract],
    quotes: Mapping[str, SigQuote],
    refs: RefSnapshot,
    limits: Limits,
    now: datetime,
    *,
    busy: Iterable[str] = (),
    last_order: Mapping[str, datetime] | None = None,
) -> list[Evaluation]:
    """Complement first (per race), then reference (per contract)."""
    busy = set(busy)
    last_order = last_order or {}
    out: list[Evaluation] = []
    races: dict[str, list[MappedContract]] = {}
    for contract in contracts:
        races.setdefault(contract.race_key, []).append(contract)
    for race_key, members in races.items():
        ev = evaluate_complement(
            race_key, members, quotes, limits, now, busy=busy, last_order=last_order
        )
        if ev is not None:
            out.append(ev)
    for contract in contracts:
        kalshi = refs.kalshi.get(contract.kalshi_ticker)
        poly = (
            refs.polymarket.get(contract.polymarket_token_id)
            if contract.polymarket_token_id
            else None
        )
        out.append(
            evaluate_reference(
                contract,
                quotes.get(contract.sig_exchange_id),
                kalshi,
                poly,
                limits,
                now,
                busy=busy,
                last_order=last_order,
            )
        )
    return out


# --- sizing -----------------------------------------------------------------------------


def depth_at(
    bids: Iterable[tuple[Decimal, Decimal]], asks: Iterable[tuple[Decimal, Decimal]], leg: Leg
) -> Decimal:
    """Shares available at or better than the leg's price, from a YES-denominated book.

    A YES buy at p takes asks priced <= p; a NO buy at NO price q takes YES bids priced
    >= 1 - q.
    """
    if leg.side == "yes":
        return sum((qty for price, qty in asks if price <= leg.price_side), Decimal(0))
    yes_equivalent = 1 - leg.price_side
    return sum((qty for price, qty in bids if price >= yes_equivalent), Decimal(0))


def size_order(max_quantity: int, depth: Decimal) -> int:
    """min(risk max_quantity, whole shares of depth); 0 means skip."""
    if depth <= 0 or max_quantity <= 0:
        return 0
    return max(0, min(max_quantity, math.floor(depth)))


def joint_quantity(upper: int, ok: Callable[[int], bool]) -> int:
    """The largest q in [0, upper] with ok(q), assuming ok is monotone (cost grows with q)."""
    lo, hi = 0, max(0, upper)
    while lo < hi:
        mid = (lo + hi + 1) // 2
        if ok(mid):
            lo = mid
        else:
            hi = mid - 1
    return lo


def order_text(leg: Leg, quantity: int) -> str:
    return f"buy {leg.side.upper()} {quantity} @ {fmt(leg.price_side)}"
