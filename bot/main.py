"""The election-week trading loop.

    python -m bot.main [--dry-run | --live]

Live only when config/risk.yaml says `mode: live` AND --live is passed; anything else
is a dry run, where no order and no cancel is ever sent: strategies still run and
every would-be order is a 'would_order' decision.

Every SIG read goes through one SigRestClient; reads and writes share one RateLimiter.
New orders go through bot/orders.py (risk.check, journal, retries) and nothing else.

Startup: refuse an empty map; verify the tournament; resolve journal rows a previous
run left mid-send; live: cancel every open order; read balance, positions and open
orders; heartbeat and a start event.

Each loop (every loop_seconds):
  a. bot/state/STOP -> live: cancel all; stop event; stop_seen = 1; exit 0.
  b. SIG bulk prices (<= 100 ids per call) and reference quotes, concurrently.
  c. quotes_latest and exposure; strategies; at most 5 orders per loop.
  d. every 60 s: balance, positions, open orders -> snapshot; reconcile the journal;
     cancel our resting orders older than order_expiry_seconds + 30 s. Anything that
     cannot be explained -> PAUSED (reason 'reconcile').
  e. every 300 s: tournament markets; a market no longer open stops trading.
  f. heartbeat, every loop, whatever happened.
An unexpected exception in a loop writes an error event and creates PAUSED; the loop
keeps reading and writing the heartbeat. SIGTERM/SIGINT: live -> cancel all, then exit.
"""

from __future__ import annotations

import argparse
import asyncio
import dataclasses
import json
import logging
import os
import signal
import sqlite3
import sys
import time
from collections.abc import Awaitable, Callable, Sequence
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any

from bot import db, risk, strategy
from bot.db import STATE_DIR
from bot.limits import Limits, LimitsError, load_limits
from bot.mapping import MappedContract, MappingError, load_map
from bot.orders import OrderPath
from bot.refs import RefCollector, RefSnapshot, fair_value
from bot.risk import Intent
from bot.sig_writer import SigWriter
from bot.strategy import Evaluation, Leg, SigQuote
from engine.config import MIDTERM_ELECTIONS, ConfigError, load_sig_config
from engine.sig_client.account_reads import PositionsRead
from engine.sig_client.errors import SigClientError
from engine.sig_client.rate_limiter import RateLimiter, RequestKind
from engine.sig_client.rest import SigRestClient

logger = logging.getLogger(__name__)

VERSION = "bot-ew-3"
MAX_ORDERS_PER_LOOP = 5
PRICE_CHUNK = 100  # GET /exchanges/prices takes at most 100 ids
ACCOUNT_EVERY_S = 60.0
MARKETS_EVERY_S = 300.0
STALE_GRACE_S = 30
SKIP_EVERY_S = 60.0  # skip decisions at most once per subject per minute
EVENT_EVERY_S = 60.0  # repeated warnings at most once per minute
READ_HEADROOM = 70  # no optional (orderbook) read once this many reads are in the window
BOOK_DEPTH = 20
LIVE_STATUSES = ("journaled", "sent", "resting", "unknown")
SENT_STATUSES = ("sent", "resting", "filled", "partial", "unknown")


class StartupError(Exception):
    """The bot must not start (empty map, failed verification)."""


def _text(value: Decimal | None) -> str | None:
    return None if value is None else str(value)


def _json(value: Any) -> str:
    return json.dumps(value, default=str, sort_keys=True)


class Bot:
    def __init__(
        self,
        *,
        limits: Limits,
        contracts: Sequence[MappedContract],
        conn: sqlite3.Connection,
        rest: SigRestClient,
        writer: SigWriter,
        refs: RefCollector,
        limiter: RateLimiter,
        live: bool,
        state_dir: Path = STATE_DIR,
        clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], Awaitable[None]] | None = None,
        now: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
        self.limits = limits
        self.contracts = {c.sig_exchange_id: c for c in contracts}
        self.conn = conn
        self.rest = rest
        self.writer = writer
        self.refs = refs
        self.limiter = limiter
        self.live = live and limits.mode == "live"
        self.state_dir = Path(state_dir)
        self.clock = clock
        self.now = now
        self.stopping = asyncio.Event()
        # The loop's wait between iterations ends early on a stop signal; the order
        # path's waits (90 s in flight, backoffs) never do.
        self._sleep = sleep or self._interruptible_sleep
        self.path = OrderPath(
            limits=limits,
            conn=conn,
            writer=writer,
            reader=rest,
            limiter=limiter,
            live=self.live,
            state_dir=self.state_dir,
            balance=self._order_balance,
            extra_committed=lambda: self.external_committed,
            clock=clock,
            sleep=sleep or asyncio.sleep,
            now=now,
        )
        self.balance: Decimal | None = None
        self.loop_balance: Decimal | None = None
        self.batch_spent = Decimal(0)
        self.external_committed = Decimal(0)
        self.last_order: dict[str, datetime] = {}
        self._skip_logged: dict[str, float] = {}
        self._event_logged: dict[str, float] = {}
        self._next_account = 0.0
        self._next_markets = 0.0
        self._stop_seen = False
        self._loop_ms = 0
        self._quotes: dict[str, SigQuote] = {}
        self._ordered_this_loop: set[str] = set()

    @property
    def mode(self) -> str:
        return "live" if self.live else "dry_run"

    # --- small helpers --------------------------------------------------------------

    async def _interruptible_sleep(self, seconds: float) -> None:
        try:
            await asyncio.wait_for(self.stopping.wait(), timeout=max(0.0, seconds))
        except TimeoutError:
            pass

    def event(self, level: str, kind: str, message: str, *, throttle: str | None = None) -> None:
        """Write an event; with `throttle`, at most once per minute for that key."""
        if throttle is not None:
            last = self._event_logged.get(throttle)
            if last is not None and self.clock() - last < EVENT_EVERY_S:
                return
            self._event_logged[throttle] = self.clock()
        db.insert_event(self.conn, level, kind, message)

    def paused(self) -> bool:
        return (self.state_dir / "PAUSED").exists()

    def pause(self, reason: str, kind: str = "pause") -> None:
        self.path.pause(reason, kind=kind)

    def _redacted(self, exc: BaseException) -> str:
        return self.writer.redact(f"{type(exc).__name__}: {exc}")[:300]

    async def _order_balance(self) -> Decimal | None:
        """Balance for risk: this loop's fresh read minus what this loop already spent."""
        if self.loop_balance is None:
            return None
        return self.loop_balance - self.batch_spent

    def committed(self) -> tuple[Decimal, dict[str, Decimal]]:
        rows = self.conn.execute("SELECT * FROM orders").fetchall()
        total, by_race = risk.committed(rows, self.limits)
        return total + self.external_committed, by_race

    def busy_exchanges(self) -> set[str]:
        rows = db.orders_with_status(self.conn, *LIVE_STATUSES)
        return {row["sig_exchange_id"] for row in rows}

    # --- startup --------------------------------------------------------------------

    async def startup(self) -> None:
        if self.live and self.limits.start_balance is None:
            raise StartupError(
                "live mode requires start_balance in config/risk.yaml for the loss stop"
            )
        if not self.contracts:
            raise StartupError("bot/map.yaml has no approved contracts: refusing to start")
        await self.rest.verify_tournament()
        self._resolve_interrupted_rows()
        if self.live:
            await self.path.cancel_all("startup")
        await self.account_cycle()
        if self.limits.start_balance is None:
            self.event("warn", "start", "start_balance is not set: the loss stop is disabled")
        self.write_heartbeat()
        self.event(
            "info",
            "start",
            f"{VERSION} started in {self.mode} with {len(self.contracts)} approved contracts",
        )

    def _resolve_interrupted_rows(self) -> None:
        """Rows a previous run left mid-placement. 'sent' may have landed -> unknown."""
        for row in db.orders_with_status(self.conn, "journaled"):
            db.update_order(
                self.conn, row["key"], status="rejected", error="not sent: process stopped"
            )
        for row in db.orders_with_status(self.conn, "sent"):
            why = "process stopped while sending: outcome unknown"
            db.update_order(self.conn, row["key"], status="unknown", error=why)
            db.insert_event(self.conn, "error", "order_unknown", f"order {row['key']}: {why}")
            self.pause(f"order {row['key']} outcome unknown after a restart")

    # --- the loop -------------------------------------------------------------------

    async def run(self) -> int:
        try:
            await self.startup()
        except StartupError as exc:
            self.event("error", "stop", str(exc))
            logger.error("%s", exc)
            return 2
        except SigClientError as exc:
            self.event("error", "stop", f"startup failed: {self._redacted(exc)}")
            logger.error("startup failed: %s", type(exc).__name__)
            return 1
        while True:
            started = self.clock()
            if not await self.iteration():
                return 0
            elapsed = self.clock() - started
            await self._sleep(max(0.0, self.limits.loop_seconds - elapsed))

    async def iteration(self) -> bool:
        """One loop. False means stop (STOP file or signal)."""
        started = self.clock()
        try:
            if (self.state_dir / "STOP").exists():
                await self.stop("stop file")
                return False
            if self.stopping.is_set():
                await self.stop("signal")
                return False
            if self.clock() >= self._next_markets:
                await self.markets_cycle()
            quotes, refs = await self.read_quotes()
            if quotes is not None:
                self.write_quotes(quotes, refs)
                await self.act(quotes, refs)
            if self.clock() >= self._next_account:
                await self.account_cycle()
        except Exception as exc:  # keep reading and beating; PAUSED stops new orders
            logger.exception("iteration failed")
            self.event(
                "error",
                "pause",
                f"iteration failed: {self._redacted(exc)}",
                throttle=f"iteration:{type(exc).__name__}",
            )
            if not self.paused():
                self.pause(f"unexpected error: {type(exc).__name__}")
        finally:
            self._loop_ms = int((self.clock() - started) * 1000)
            try:
                self.write_heartbeat()
            except Exception:
                logger.exception("heartbeat write failed")
        return True

    async def stop(self, reason: str) -> None:
        if self.live:
            await self.path.cancel_all(reason)
        self._stop_seen = reason == "stop file" or self._stop_seen
        self.event("info", "stop", f"stopping ({reason}) in {self.mode}")

    # --- reads ----------------------------------------------------------------------

    async def read_quotes(self) -> tuple[dict[str, SigQuote] | None, RefSnapshot]:
        ids = list(self.contracts)
        chunks = [ids[i : i + PRICE_CHUNK] for i in range(0, len(ids), PRICE_CHUNK)]
        tickers = [c.kalshi_ticker for c in self.contracts.values()]
        tokens = [c.polymarket_token_id for c in self.contracts.values() if c.polymarket_token_id]
        results = await asyncio.gather(
            self.refs.fetch_all(tickers, tokens),
            *(self.rest.get_exchange_prices(chunk) for chunk in chunks),
            return_exceptions=True,
        )
        refs, prices = results[0], results[1:]
        if isinstance(refs, BaseException):
            raise refs  # unexpected: the collector reports venue errors in the snapshot
        for venue, error in refs.errors.items():
            self.event("warn", "ref_stale", f"{venue}: {error}"[:300], throttle=f"ref:{venue}")
        quotes: dict[str, SigQuote] = {}
        for result in prices:
            if isinstance(result, SigClientError):
                self.event(
                    "warn",
                    "ref_stale",
                    f"SIG prices read failed: {type(result).__name__}; no trading this loop",
                    throttle="sig_prices",
                )
                return None, refs
            if isinstance(result, BaseException):
                raise result
            for price in result.data:
                quotes[price.exchangeId] = SigQuote(price.bestBid, price.bestAsk, price.latestPrice)
        return quotes, refs

    def write_quotes(self, quotes: dict[str, SigQuote], refs: RefSnapshot) -> None:
        now = self.now()
        settings = strategy.ref_settings(self.limits)
        for exchange_id, c in self.contracts.items():
            q = quotes.get(exchange_id)
            k = refs.kalshi.get(c.kalshi_ticker)
            p = refs.polymarket.get(c.polymarket_token_id) if c.polymarket_token_id else None
            fair, _ = fair_value(k, p, settings, now)
            # edge: the better of buying YES (fair - ask) and buying NO (bid - fair).
            edge = None
            if fair is not None and q is not None:
                sides = []
                if q.ask is not None:
                    sides.append(fair - q.ask)
                if q.bid is not None:
                    sides.append(q.bid - fair)
                edge = max(sides, default=None)
            db.upsert_quote(
                self.conn,
                sig_exchange_id=exchange_id,
                race_key=c.race_key,
                party=c.party,
                ts_utc=db.utc_stamp(now),
                sig_bid=_text(q.bid if q else None),
                sig_ask=_text(q.ask if q else None),
                sig_last=_text(q.last if q else None),
                kalshi_ticker=c.kalshi_ticker,
                kalshi_bid=_text(k.bid if k else None),
                kalshi_ask=_text(k.ask if k else None),
                kalshi_ts_utc=db.utc_stamp(k.fetched_utc) if k else None,
                poly_token=c.polymarket_token_id,
                poly_bid=_text(p.bid if p else None),
                poly_ask=_text(p.ask if p else None),
                poly_ts_utc=db.utc_stamp(p.fetched_utc) if p else None,
                fair=_text(fair),
                edge=_text(edge),
            )
        self.write_exposure()

    def write_exposure(self) -> None:
        _, by_race = self.committed()
        cap = self.limits.effective_cap("per_race")
        for race_key in {c.race_key for c in self.contracts.values()} | set(by_race):
            db.upsert_exposure(
                self.conn,
                race_key=race_key,
                committed=str(by_race.get(race_key, Decimal(0))),
                cap=str(cap),
            )

    # --- acting ---------------------------------------------------------------------

    def _skip(self, ev: Evaluation, reason: str | None = None) -> None:
        last = self._skip_logged.get(ev.subject)
        if last is not None and self.clock() - last < SKIP_EVERY_S:
            return
        self._skip_logged[ev.subject] = self.clock()
        db.insert_decision(self.conn, verdict="skip", **self._decision_fields(ev, reason))

    @staticmethod
    def _decision_fields(ev: Evaluation, reason: str | None = None) -> dict[str, Any]:
        return {
            "race_key": ev.race_key,
            "sig_exchange_id": ev.sig_exchange_id,
            "party": ev.party,
            "strategy": ev.strategy,
            "sig_bid": _text(ev.sig_bid),
            "sig_ask": _text(ev.sig_ask),
            "kalshi_mid": _text(ev.kalshi_mid),
            "poly_mid": _text(ev.poly_mid),
            "fair": _text(ev.fair),
            "edge": _text(ev.edge),
            "reason": reason or ev.reason,
        }

    async def act(self, quotes: dict[str, SigQuote], refs: RefSnapshot) -> None:
        now = self.now()
        self._quotes = quotes
        evaluations = strategy.evaluate_all(
            list(self.contracts.values()),
            quotes,
            refs,
            self.limits,
            now,
            busy=self.busy_exchanges(),
            last_order=self.last_order,
        )
        self.loop_balance = None
        self.batch_spent = Decimal(0)
        self._ordered_this_loop = set()
        paused = self.paused()
        sent = 0
        for ev in evaluations:
            if ev.is_skip:
                self._skip(ev)
                continue
            if paused:  # no reads spent on orders that risk.check would refuse
                self._skip(ev, f"{ev.reason}; not sent: PAUSED")
                continue
            # Evaluations were made before this loop's orders: one order per exchange.
            if any(leg.contract.sig_exchange_id in self._ordered_this_loop for leg in ev.legs):
                self._skip(ev, f"{ev.reason}; not sent: exchange already has an order this loop")
                continue
            if sent + len(ev.legs) > MAX_ORDERS_PER_LOOP:
                self._skip(ev, f"{ev.reason}; not sent: {MAX_ORDERS_PER_LOOP} orders this loop")
                continue
            if self.limiter.used(RequestKind.READ) + len(ev.legs) + 1 > READ_HEADROOM:
                self._skip(ev, f"{ev.reason}; not sent: SIG read budget nearly used")
                continue
            sent += await self.send(ev, now)

    async def send(self, ev: Evaluation, now: datetime) -> int:
        """Size, risk-check every leg, then place. Returns the orders placed or simulated."""
        if self.loop_balance is None:
            try:
                read = await self.rest.read_tournament_balance()
                self.loop_balance = self.balance = read.tournament.myBalance
            except SigClientError as exc:
                self._skip(ev, f"{ev.reason}; not sent: balance read failed ({type(exc).__name__})")
                return 0
        depths = []
        for leg in ev.legs:
            # A failed read here happens before any leg is sent, so nothing is at risk:
            # skip this trade and try again next loop. A transient 429 must not turn
            # into a PAUSED that waits for Arnav.
            try:
                book = await self.rest.get_exchange_orderbook(
                    leg.contract.sig_exchange_id, depth=BOOK_DEPTH
                )
            except SigClientError as exc:
                self._skip(
                    ev, f"{ev.reason}; not sent: orderbook read failed ({type(exc).__name__})"
                )
                return 0
            depths.append(
                strategy.depth_at(
                    [(lvl.price, lvl.quantity) for lvl in book.bids],
                    [(lvl.price, lvl.quantity) for lvl in book.asks],
                    leg,
                )
            )
        quantity, why = self.size(ev, depths)
        if quantity < 1:
            self._skip(ev, f"{ev.reason}; not sent: {why}")
            return 0
        placed = 0
        for index, leg in enumerate(ev.legs):
            intent = self._intent(ev, leg, quantity)
            record = await self.path.place(intent, context=self._leg_context(ev, leg))
            if record.status != "blocked":
                self.last_order[leg.contract.sig_exchange_id] = now
                self._ordered_this_loop.add(leg.contract.sig_exchange_id)
                placed += 1
            # Only journaled orders lower the balance risk sees: the journal adds the
            # same cost back in the loss-stop proxy. A dry-run 'would_order' has no
            # journal row, so subtracting it would make the proxy fall and trip the
            # loss stop on simulated buys.
            if record.status in SENT_STATUSES:
                self.batch_spent += leg.price_side * quantity
            if record.status not in ("resting", "filled", "partial", "would_order"):
                if index == 0 and len(ev.legs) > 1:
                    self.event(
                        "warn",
                        "order_rejected",
                        f"complement {ev.race_key}: first leg {record.status}; second leg not sent",
                    )
                elif index > 0:
                    self.event(
                        "warn",
                        "order_rejected",
                        f"complement {ev.race_key}: second leg {record.status} after the first "
                        "was sent; not chasing",
                    )
                break
        return placed

    def _leg_context(self, ev: Evaluation, leg: Leg) -> dict[str, Any]:
        """Decision columns for one leg: the evaluation's, with this leg's SIG quote."""
        quote = self._quotes.get(leg.contract.sig_exchange_id)
        return {
            **self._decision_fields(ev),
            "party": leg.contract.party,
            "sig_bid": _text(quote.bid if quote else None),
            "sig_ask": _text(quote.ask if quote else None),
        }

    def _intent(self, ev: Evaluation, leg: Leg, quantity: int) -> Intent:
        c = leg.contract
        return Intent(
            race_key=c.race_key,
            sig_market_id=c.sig_market_id,
            sig_exchange_id=c.sig_exchange_id,
            side=leg.side,
            price_side=leg.price_side,
            quantity=quantity,
            strategy=ev.strategy,
            reason=f"{ev.reason} -> {strategy.order_text(leg, quantity)}",
        )

    def _state(self, race_key: str, balance: Decimal | None) -> risk.RiskState:
        return risk.build_state(
            self.conn,
            self.limits,
            race_key=race_key,
            balance=balance,
            writes_last_min=self.limiter.used(RequestKind.WRITE),
            state_dir=self.state_dir,
            extra_committed=self.external_committed,
        )

    def size(self, ev: Evaluation, depths: list[Decimal]) -> tuple[int, str]:
        """Equal quantity for every leg that passes risk for ALL legs together."""
        balance = None if self.loop_balance is None else self.loop_balance - self.batch_spent
        state = self._state(ev.race_key, balance)
        upper = None
        for leg, depth in zip(ev.legs, depths, strict=True):
            result = risk.check(self._intent(ev, leg, 1), state)
            if not result.allowed:
                return 0, f"risk: {result.reason}"
            q = strategy.size_order(result.max_quantity, depth)
            upper = q if upper is None else min(upper, q)
        if not upper:
            return 0, f"no depth at the price (depth {', '.join(str(d) for d in depths)})"

        def all_legs_pass(q: int) -> bool:
            s = state
            for leg in ev.legs:
                if not risk.check(self._intent(ev, leg, q), s).allowed:
                    return False
                cost = leg.price_side * q
                # The leg becomes a journaled order: cash down, journal up by the same
                # cost, so the loss-stop proxy (balance + journal) is unchanged.
                s = dataclasses.replace(
                    s,
                    total_committed=s.total_committed + cost,
                    race_committed=s.race_committed + cost,
                    journal_committed=s.journal_committed + cost,
                    balance=None if s.balance is None else s.balance - cost,
                )
            return True

        quantity = strategy.joint_quantity(upper, all_legs_pass)
        return quantity, "" if quantity else "risk refuses every leg together"

    # --- periodic cycles ------------------------------------------------------------

    async def account_cycle(self) -> None:
        """Balance, positions, open orders -> snapshot; reconcile; stale own orders."""
        self._next_account = self.clock() + ACCOUNT_EVERY_S
        try:
            balance_read = await self.rest.read_tournament_balance()
            self.balance = balance_read.tournament.myBalance
            positions = await self.rest.read_tournament_positions()
            walk = await self.rest.read_open_orders()
        except SigClientError as exc:
            self.balance = None
            self.event(
                "warn",
                "reconcile",
                f"account read failed: {type(exc).__name__}",
                throttle="account",
            )
            return
        await self.path.refresh_statuses(walk)
        self.reconcile(walk.orders)
        await self.cancel_stale(walk.orders)
        total, _ = self.committed()
        if isinstance(positions, PositionsRead):
            positions_json = _json([row.model_dump(mode="json") for row in positions.rows])
        else:
            positions_json = _json({"unvalued": [dataclasses.asdict(m) for m in positions.missing]})
        db.insert_snapshot(
            self.conn,
            balance=_text(self.balance),
            committed=str(total),
            open_orders=len(walk.orders),
            positions_json=positions_json,
            pnl_json=None,
        )
        self.write_exposure()

    def reconcile(self, open_orders: Sequence[Any]) -> None:
        rows = self.conn.execute("SELECT * FROM orders").fetchall()
        by_id = {row["sig_order_id"]: row for row in rows if row["sig_order_id"]}
        foreign = [o for o in open_orders if str(o.id) not in by_id]
        problems = []
        for order in open_orders:
            row = by_id.get(str(order.id))
            if row is None:
                continue
            if (order.exchangeId, order.side, order.action) != (
                row["sig_exchange_id"],
                row["side"],
                "buy",
            ):
                problems.append(f"open order {order.id} differs from journal row {row['key']}")
            if row["status"] not in LIVE_STATUSES:
                problems.append(f"order {order.id} is open on SIG but {row['status']} here")
        for row in rows:
            traded = row["quantity_traded"]
            if traded is not None and Decimal(traded) > int(row["quantity"]):
                problems.append(f"order {row['key']} traded more than its quantity")
        # Open orders we did not journal (manual or lost) count at their limit, the worse
        # side for a NO order (OQ-W6: priceLimit denomination is open).
        extra = Decimal(0)
        for order in foreign:
            p = order.priceLimit if order.priceLimit is not None else Decimal(1)
            per_share = max(p, 1 - p) if order.side == "no" else p
            extra += per_share * Decimal(order.quantity)
        self.external_committed = extra
        if foreign:
            message = f"{len(foreign)} open order(s) on SIG are not in the journal"
            if self.live:
                problems.append(message)
            else:
                self.event("warn", "reconcile", message, throttle="foreign")
        if problems and not self.paused():
            text = "; ".join(problems)[:500]
            self.event("error", "reconcile", text)
            self.pause(f"reconcile: {text}", kind="reconcile")

    async def cancel_stale(self, open_orders: Sequence[Any]) -> None:
        if not self.live:
            return
        open_ids = {str(o.id) for o in open_orders}
        limit = timedelta(seconds=self.limits.order_expiry_seconds + STALE_GRACE_S)
        now = self.now()
        for row in db.orders_with_status(self.conn, "resting"):
            created = datetime.fromisoformat(row["created_utc"])
            if row["sig_order_id"] in open_ids and now - created > limit:
                self.event("warn", "reconcile", f"order {row['key']} outlived its expiry")
                await self.path.cancel(row["key"])

    async def markets_cycle(self) -> None:
        self._next_markets = self.clock() + MARKETS_EVERY_S
        try:
            markets = await self.rest.list_all_tournament_markets(status="any")
        except SigClientError as exc:
            self.event(
                "warn",
                "market_closed",
                f"markets read failed: {type(exc).__name__}",
                throttle="markets",
            )
            return
        status = {m.id: m.status for m in markets}
        for exchange_id, c in list(self.contracts.items()):
            state = status.get(c.sig_market_id, "missing")
            if state != "open":
                del self.contracts[exchange_id]
                self.event(
                    "warn",
                    "market_closed",
                    f"{c.race_key} {c.party} market {c.sig_market_id} is {state}: "
                    "dropped from trading",
                )

    # --- heartbeat ------------------------------------------------------------------

    def write_heartbeat(self) -> None:
        now = self.now()
        pause_reason = None
        if self.paused():
            try:
                pause_reason = (self.state_dir / "PAUSED").read_text(encoding="utf-8").strip()[:300]
            except OSError:
                pause_reason = "unreadable"
        total, _ = self.committed()
        db.upsert_heartbeat(
            self.conn,
            ts_utc=db.utc_stamp(now),
            mode=self.mode,
            version=VERSION,
            loop_ms=self._loop_ms,
            reads_last_min=self.limiter.used(RequestKind.READ),
            writes_last_min=self.limiter.used(RequestKind.WRITE),
            paused=int(pause_reason is not None),
            pause_reason=pause_reason,
            stop_seen=int(self._stop_seen),
            balance=_text(self.balance),
            start_balance=_text(self.limits.start_balance),
            committed=str(total),
            scale=str(self.limits.scale),
            cap_total=str(self.limits.effective_cap("total_committed")),
            cap_per_race=str(self.limits.effective_cap("per_race")),
            min_cash_reserve=str(self.limits.effective_cap("min_cash_reserve")),
            edge_now=str(strategy.current_edge(self.limits, now)),
            election_night=int(strategy.is_election_night(self.limits, now)),
        )


def _install_signals(bot: Bot) -> None:
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGTERM, signal.SIGINT):
        try:
            loop.add_signal_handler(sig, bot.stopping.set)
        except (NotImplementedError, RuntimeError):  # Windows
            signal.signal(sig, lambda *_: loop.call_soon_threadsafe(bot.stopping.set))


async def _main(live_flag: bool) -> int:
    limits = load_limits()
    contracts = load_map()
    live = live_flag and limits.mode == "live"
    if live_flag and not live:
        print("--live ignored: config/risk.yaml says mode: dry_run. Running a dry run.")
    config = load_sig_config(os.environ, tournament=MIDTERM_ELECTIONS)
    limiter = RateLimiter()
    conn = db.connect()
    rest = SigRestClient.create(config, limiter=limiter)
    writer = SigWriter(config, limiter)
    refs = RefCollector()
    bot = Bot(
        limits=limits,
        contracts=contracts,
        conn=conn,
        rest=rest,
        writer=writer,
        refs=refs,
        limiter=limiter,
        live=live,
    )
    _install_signals(bot)
    try:
        return await bot.run()
    finally:
        await refs.aclose()
        await writer.aclose()
        await rest.aclose()
        conn.close()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m bot.main", description=__doc__.split("\n")[0])
    group = parser.add_mutually_exclusive_group()
    group.add_argument("--dry-run", action="store_true", help="never send orders (default)")
    group.add_argument("--live", action="store_true", help="live, if risk.yaml says mode: live")
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    try:
        return asyncio.run(_main(args.live))
    except (LimitsError, MappingError, ConfigError) as exc:
        print(f"refusing to start: {exc}")
        return 2


if __name__ == "__main__":
    sys.exit(main())
