"""Exit probe (BOT-EW-5). Arnav runs it live, with the bot stopped; agents never do.

    python -m bot.exit_probe --exchange <id> --i-am-arnav

Shows that the exit mechanism (a complement buy that nets against the holding,
bot/exits.py) closes exactly one held share before exits are switched on:

  1. Reads the tournament position on that exchange; refuses unless it holds at least
     2 shares of one side (and the exchange is approved in bot/map.yaml).
  2. Buys 1 share of the other side at a marketable price: 1 - SIG bid (floored) to
     close YES, SIG ask (floored) to close NO. It goes through bot/orders.py
     (place_exit: risk.check_exit with the probe flag, the journal, the retry rules).
  3. Cancels any remainder at once and confirms the order with a read (place_exit).
  4. Re-reads the position, the cash and the order.
  5. VERDICT: OK only if the position fell by exactly 1 share on the same side, no
     opposite-side position appeared and cash rose by about the share's sale price
     (1 - the complement's fill price). Otherwise VERDICT: NOT OK with the facts.

Worst case it costs one share's spread. exits.yaml need not be enabled, but it must be
valid; NO orders must be enabled for the probe of a YES holding (its exit is a NO buy).
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
from dataclasses import dataclass
from decimal import Decimal

from bot import db
from bot.exits import ExitsError, load_exits
from bot.limits import load_limits
from bot.main import holdings, signed_positions
from bot.mapping import load_map
from bot.orders import OrderPath, OrderRecord
from bot.risk import HeldPosition, Intent
from bot.sig_writer import SigWriter
from bot.strategy import buy_price, other_side
from engine.config import MIDTERM_ELECTIONS, load_sig_config
from engine.sig_client.errors import SigClientError
from engine.sig_client.rate_limiter import RateLimiter
from engine.sig_client.rest import SigRestClient

CASH_TOLERANCE = Decimal("0.02")  # SUSQies per share: rounding and any fee


@dataclass(frozen=True)
class Facts:
    """What the probe saw before and after its one-share exit."""

    held_before: HeldPosition
    signed_after: Decimal | None  # None when the position could not be re-read
    cash_before: Decimal | None
    cash_after: Decimal | None
    complement_price: Decimal  # the complement buy's limit (fill denominations: OQ-W4)
    record: OrderRecord
    fill_price: Decimal | None = None  # SIG's fillPrice as sent, for the record only


def judge(facts: Facts) -> tuple[bool, list[str]]:
    """(OK?, the facts as lines). OK needs all three checks to pass."""
    held = facts.held_before
    sign = 1 if held.side == "yes" else -1
    before = sign * held.quantity
    lines = [
        (
            f"order: status={facts.record.status} key={facts.record.key} "
            f"traded={facts.record.quantity_traded} total_cost={facts.record.total_cost} "
            f"fillPrice={facts.fill_price}"
        ),
        f"position before: {before} ({held.quantity} {held.side.upper()})",
        f"position after: {facts.signed_after}",
        f"cash before: {facts.cash_before}  after: {facts.cash_after}",
    ]
    ok = True
    if facts.record.quantity_traded is None or Decimal(facts.record.quantity_traded) != 1:
        lines.append("NOT OK: the exit did not fill exactly 1 share")
        ok = False
    if facts.signed_after is None:
        lines.append("NOT OK: the position could not be re-read")
        return False, lines
    expected = before - sign
    if facts.signed_after != expected:
        lines.append(f"NOT OK: position should be {expected}, is {facts.signed_after}")
        ok = False
    if facts.signed_after * sign < 0:
        lines.append("NOT OK: an opposite-side position appeared")
        ok = False
    if facts.cash_before is None or facts.cash_after is None:
        lines.append("NOT OK: cash could not be read before and after")
        return False, lines
    sale = 1 - facts.complement_price
    rise = facts.cash_after - facts.cash_before
    lines.append(f"cash rose by {rise}; a 1-share sale at {sale} would raise it by {sale}")
    if abs(rise - sale) > CASH_TOLERANCE:
        lines.append(f"NOT OK: cash change {rise} is not about {sale}")
        ok = False
    return ok, lines


def _fill_price(record: OrderRecord) -> Decimal | None:
    try:
        body = json.loads(record.response_json or "", parse_float=Decimal)
        price = body.get("fillPrice")
        return None if price is None else Decimal(str(price))
    except (ValueError, AttributeError, ArithmeticError):
        return None


async def _held(rest: SigRestClient, exchange_id: str) -> tuple[HeldPosition | None, Decimal]:
    parsed = signed_positions(await rest.read_tournament_positions())
    if parsed is None:
        raise RuntimeError("positions read returned unvalued holdings (409)")
    signed, settled = parsed
    if exchange_id in settled:
        raise RuntimeError(f"exchange {exchange_id} is settled")
    return holdings(signed, fresh=True).get(exchange_id), signed.get(exchange_id, Decimal(0))


async def run(exchange_id: str) -> int:
    limits = load_limits()
    try:
        settings = load_exits()
    except ExitsError as exc:
        print(f"refused: {exc}")
        return 2
    if settings is None:
        print("refused: config/exits.yaml is absent; the probe needs a valid one")
        return 2
    contract = next((c for c in load_map() if c.sig_exchange_id == exchange_id), None)
    if contract is None:
        print(f"refused: exchange {exchange_id} is not approved in bot/map.yaml")
        return 2
    config = load_sig_config(os.environ, tournament=MIDTERM_ELECTIONS)
    limiter = RateLimiter()
    conn = db.connect()
    rest = SigRestClient.create(config, limiter=limiter)
    writer = SigWriter(config, limiter)
    path = OrderPath(
        limits=limits, conn=conn, writer=writer, reader=rest, limiter=limiter, live=True
    )
    try:
        await rest.verify_tournament()
        db.insert_event(conn, "info", "probe", f"exit probe start on exchange {exchange_id}")
        held, _ = await _held(rest, exchange_id)
        if held is None or held.quantity < 2:
            print(f"refused: exchange {exchange_id} holds {held}: the probe needs >= 2 shares")
            return 2
        cash_before = (await rest.read_tournament_balance()).tournament.myBalance
        prices = await rest.get_exchange_prices([exchange_id])
        if not prices.data:
            print(f"exchange {exchange_id} is not in the tournament")
            return 2
        quote = prices.data[0]
        print(
            f"exchange {exchange_id} ({contract.race_key} {contract.party}): "
            f"held {held.quantity} {held.side.upper()}; bid={quote.bestBid} ask={quote.bestAsk}"
        )
        sale = (
            quote.bestBid
            if held.side == "yes"
            else (None if quote.bestAsk is None else 1 - quote.bestAsk)
        )
        if sale is None:
            print("refused: SIG has no bid for the side held")
            return 2
        price = buy_price(1 - sale)
        if price is None:
            print(f"refused: complement price {1 - sale} is outside 0.005-0.995")
            return 2
        side = other_side(held.side)
        intent = Intent(
            race_key=contract.race_key,
            sig_market_id=contract.sig_market_id,
            sig_exchange_id=exchange_id,
            side=side,
            price_side=price,
            quantity=1,
            strategy="exit_probe",
            reason=f"exit probe: buy 1 {side.upper()} at {price} to close 1 {held.side.upper()}",
            exit_of_side=held.side,
        )
        record = await path.place_exit(intent, held=held, settings=settings, probe=True)
        print(f"\nexit: status={record.status} key={record.key} http={record.http_status}")
        print(f"  reason: {record.reason}")
        if record.error:
            print(f"  error: {record.error}")
        if record.key is None:
            print("VERDICT: NOT OK: nothing was sent")
            return 1
        try:
            _, signed_after = await _held(rest, exchange_id)
        except (SigClientError, RuntimeError) as exc:  # report the facts whatever failed
            print(f"position re-read failed: {type(exc).__name__}")
            signed_after = None
        cash_after = (await rest.read_tournament_balance()).tournament.myBalance
        row = db.get_order(conn, record.key)
        final = OrderRecord.from_row(row)
        facts = Facts(
            held_before=held,
            signed_after=signed_after,
            cash_before=cash_before,
            cash_after=cash_after,
            complement_price=price,
            fill_price=_fill_price(final),
            record=final,
        )
        ok, lines = judge(facts)
        for line in lines:
            print(f"  {line}")
        verdict = "OK" if ok else "NOT OK"
        db.insert_event(conn, "info" if ok else "warn", "probe", f"exit probe verdict: {verdict}")
        print(f"\nVERDICT: {verdict}")
        if ok:
            print("Exits may be switched on: set enabled: true in config/exits.yaml.")
        else:
            print("Leave config/exits.yaml at enabled: false and send these facts to the PR.")
        return 0 if ok else 1
    finally:
        await writer.aclose()
        await rest.aclose()
        conn.close()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m bot.exit_probe", description=__doc__.split("\n")[0]
    )
    parser.add_argument("--exchange", required=True, help="numeric SIG exchange id")
    parser.add_argument("--i-am-arnav", action="store_true", help="required: places a live order")
    args = parser.parse_args(argv)
    if not args.i_am_arnav:
        print("refused: the exit probe places a live order; only Arnav runs it (--i-am-arnav)")
        return 2
    if not args.exchange.isdigit():
        print("refused: --exchange must be a numeric exchange id")
        return 2
    return asyncio.run(run(args.exchange))


if __name__ == "__main__":
    sys.exit(main())
