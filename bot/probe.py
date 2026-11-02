"""NO-price convention probe (OQ-W3). Arnav runs it live; agents never do.

    python -m bot.probe --exchange <id> --i-am-arnav

Places two 1-share buys with a 60 s expiry on one exchange: YES at 0.010, then NO with
request price 0.020 sent raw. Both go through bot/orders.py (risk.check with the probe
flag, which caps each order's cost at 1 SUSQie, the journal and the retry rules). Under
the yes_price convention the NO request means a NO limit of 0.980, which will probably
fill: the cost is at most 0.98 SUSQie.

It prints the canonical response fields, waits 90 s, reports whether expiry removed both
orders, cancels anything left and prints which no_price_convention line to set in
config/risk.yaml. Everything is recorded in the orders and events tables.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
from decimal import Decimal

from bot import db
from bot.limits import load_limits
from bot.orders import OrderPath, OrderRecord
from bot.risk import Intent
from bot.sig_writer import SigWriter
from engine.config import MIDTERM_ELECTIONS, load_sig_config
from engine.sig_client.rate_limiter import RateLimiter
from engine.sig_client.rest import SigRestClient

YES_PRICE = Decimal("0.010")
NO_REQUEST_PRICE = Decimal("0.020")
PROBE_EXPIRY_S = 60
WAIT_S = 90
CANONICAL_FIELDS = (
    "orderId",
    "exchangeId",
    "open",
    "action",
    "side",
    "price",
    "quantity",
    "remainingQuantity",
    "quantityTraded",
    "totalCost",
    "fillPrice",
    "terminalReasonCode",
)


def verdict(record: OrderRecord) -> str:
    """The NO order's canonical price -> the convention line, or UNCLEAR."""
    try:
        body = json.loads(record.response_json or "", parse_float=Decimal)
        price = Decimal(body["price"])
    except (ValueError, KeyError, TypeError, ArithmeticError):
        return "UNCLEAR: keep NO orders disabled"
    if price == NO_REQUEST_PRICE:
        return "no_price"
    if price == 1 - NO_REQUEST_PRICE:
        return "yes_price"
    return "UNCLEAR: keep NO orders disabled"


def _show(label: str, record: OrderRecord) -> None:
    print(f"\n{label}: status={record.status} key={record.key} http={record.http_status}")
    print(f"  reason: {record.reason}")
    if record.error:
        print(f"  error: {record.error}")
    try:
        body = json.loads(record.response_json or "", parse_float=Decimal)
    except ValueError:
        body = None
    if isinstance(body, dict):
        for name in CANONICAL_FIELDS:
            print(f"  {name}: {body.get(name, '<absent>')}")


async def run(exchange_id: str) -> int:
    limits = load_limits()
    config = load_sig_config(os.environ, tournament=MIDTERM_ELECTIONS)
    limiter = RateLimiter()
    conn = db.connect()
    rest = SigRestClient.create(config, limiter=limiter)
    writer = SigWriter(config, limiter)
    path = OrderPath(
        limits=limits, conn=conn, writer=writer, reader=rest, limiter=limiter, live=True
    )
    try:
        db.insert_event(conn, "info", "probe", f"probe start on exchange {exchange_id}")
        prices = await rest.get_exchange_prices([exchange_id])
        if not prices.data:
            print(f"exchange {exchange_id} is not in the tournament")
            return 2
        quote = prices.data[0]
        print(
            f"exchange {exchange_id} (market {quote.marketId}): latest={quote.latestPrice} "
            f"bid={quote.bestBid} ask={quote.bestAsk}"
        )

        def intent(side: str, price: Decimal) -> Intent:
            return Intent(
                race_key="probe",
                sig_market_id=quote.marketId,
                sig_exchange_id=exchange_id,
                side=side,
                price_side=price,
                quantity=1,
                strategy="probe",
                reason=f"OQ-W3 probe: 1 {side.upper()} at request price {price}",
            )

        yes = await path.place(intent("yes", YES_PRICE), probe=True, expiry_seconds=PROBE_EXPIRY_S)
        no = await path.place(
            intent("no", NO_REQUEST_PRICE), probe=True, expiry_seconds=PROBE_EXPIRY_S
        )
        _show("YES 0.010", yes)
        _show("NO request 0.020", no)

        print(f"\nwaiting {WAIT_S} s for both orders to expire ...")
        await asyncio.sleep(WAIT_S)
        walk = await rest.read_open_orders()
        open_ids = {str(order.id) for order in walk.orders}
        for label, record in (("YES", yes), ("NO", no)):
            if record.sig_order_id is None:
                print(f"{label}: no SIG order id (status {record.status})")
            elif record.sig_order_id in open_ids:
                print(f"{label}: still open after {WAIT_S} s: expiry did NOT remove it; cancelling")
                await path.cancel(record.key or "")
            else:
                print(f"{label}: not open (expired, filled or cancelled)")
        await path.refresh_statuses()

        result = verdict(no)
        db.insert_event(conn, "info", "probe", f"probe verdict: {result}")
        print(f"\nVERDICT: {result}")
        if result in ("no_price", "yes_price"):
            print(f"Set in config/risk.yaml:  no_price_convention: {result}")
            print("(and no_orders_enabled: true only if you want NO orders on)")
        else:
            print("Leave config/risk.yaml as it is: no_price_convention: unknown")
        return 0
    finally:
        await writer.aclose()
        await rest.aclose()
        conn.close()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m bot.probe", description=__doc__.split("\n")[0])
    parser.add_argument("--exchange", required=True, help="numeric SIG exchange id")
    parser.add_argument("--i-am-arnav", action="store_true", help="required: places live orders")
    args = parser.parse_args(argv)
    if not args.i_am_arnav:
        print("refused: the probe places live orders; only Arnav runs it (--i-am-arnav)")
        return 2
    if not args.exchange.isdigit():
        print("refused: --exchange must be a numeric exchange id")
        return 2
    return asyncio.run(run(args.exchange))


if __name__ == "__main__":
    sys.exit(main())
