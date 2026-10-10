"""S1-T3-A-F2 review repairs: expiry crossed during a REST read, dropped-event recovery,
and the closing boundary.

Offline: REST through httpx.MockTransport, FakeTransport for the socket, ManualClock for
time; every await that could hang is bounded by a real-time watchdog. All realtime
payloads are synthetic. Times are relative to T0, the wall clock when each test starts.
"""

from __future__ import annotations

import asyncio
import copy
from datetime import timedelta
from typing import Any

import httpx
import pytest
from realtime_kit import (
    MARKET,
    T0,
    FakeTransport,
    Harness,
    batch,
    book,
    settle,
    settled,
    trade,
    watchdog,
)
from sig_testkit import TOURNAMENT, fixture, json_response

from engine.sig_client.realtime import (
    BroadcastEvent,
    MarketReady,
    RealtimeFeedPolicy,
    TradeReceived,
)
from engine.sig_client.realtime_topic import BookSource, StaleReason

ORDERBOOK = f"/markets/{MARKET}/orderbook"
EX = "36"
QUIET = RealtimeFeedPolicy(tick_s=1e6)  # the timer never fires within these tests
DIRTY = {"exchangeId": 36, "marketId": MARKET, "tournamentId": TOURNAMENT.id, "at": "x"}


def run(scenario: Any, **kwargs: Any) -> Harness:
    async def main() -> Harness:
        h = Harness(**kwargs)
        h.baseline_tasks = set(asyncio.all_tasks())
        try:
            await scenario(h)
        finally:
            if h.task is not None and not h.task.done():
                await h.feed.aclose()
                await asyncio.gather(h.task, return_exceptions=True)
        return h

    return asyncio.run(watchdog(main(), 20.0))


def iso(delta_s: float) -> str:
    return (T0 + timedelta(seconds=delta_s)).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


def readies(h: Harness) -> int:
    return sum(isinstance(e, MarketReady) for e in h.events)


def rest_book(sequence: int | None, read_at: float = 0.0) -> httpx.Response:
    """A market orderbook whose single exchange was read by the engine at T0 + read_at."""
    body = copy.deepcopy(fixture("market_orderbook"))
    exchange = body["contexts"][0]["orderbook"]["exchanges"][0]
    exchange["asOf"] = None if sequence is None else {"sequence": sequence, "at": iso(read_at)}
    return json_response(200, body)


class Gate:
    """A REST route answered by `responses` in order; the first waits for `open()`."""

    def __init__(self, *responses: httpx.Response) -> None:
        self.responses = list(responses)
        self.event = asyncio.Event()
        self.first = True

    async def __call__(self, request: httpx.Request) -> httpx.Response:
        if self.first:
            self.first = False
            await self.event.wait()
        return self.responses.pop(0)


async def push_expiring_book(h: Harness, expiry_s: float) -> None:
    """Revision 1 carries a pushed book (version 9) with an order expiring at T0+expiry_s."""
    h.transport.batch(h.topic(), batch(1, 0, books=[book(9, next_expiry=iso(expiry_s))]))
    await settle()
    held = h.feed.books(MARKET)[EX]
    assert (held.source, held.as_of.sequence, held.expiry_known) == (BookSource.PUSH, 9, True)


async def trigger_resync(h: Harness) -> None:
    """A standalone book_dirty: a refetch trigger that does not block readiness."""
    h.transport.batch(h.topic(), DIRTY, event="book_dirty")
    await settle()


# --- 1. expiry crossed during a REST read ------------------------------------------------------


def test_versioned_read_made_before_expiry_cannot_clear_it() -> None:
    """Expiry at T0+5. The read is made at T0+4 (asOf.at) and arrives at T0+6."""
    gate = Gate(rest_book(9, read_at=4.0), rest_book(9, read_at=6.0))

    async def scenario(h: Harness) -> None:
        await h.start()
        await push_expiring_book(h, 5.0)
        h.router.routes[f"/api/v1{ORDERBOOK}"] = gate
        h.clock.now += 4  # T0+4
        await trigger_resync(h)
        assert h.count(ORDERBOOK) == 2  # in flight
        h.clock.now += 2  # T0+6: the order expired while the read was in flight
        before = readies(h)
        gate.event.set()
        await settle()
        # The first read predates the expiry, so it is not applied; a follow-up read was
        # made at once (the minimum resync interval had already passed).
        assert h.count(ORDERBOOK) == 3
        # Control: the follow-up was read at T0+6, after the expiry, and recovers.
        held = h.feed.books(MARKET)[EX]
        assert (held.source, held.as_of.at) == (BookSource.REST, iso(6.0))
        assert held.expiry_epoch is None and h.feed.is_ready(MARKET)
        assert readies(h) == before + 1
        await h.stop()

    run(scenario, policy=QUIET)


def test_versioned_read_before_expiry_publishes_no_ready_until_a_later_read() -> None:
    """Same race, with the follow-up held back: nothing is ready in between."""
    second = Gate(rest_book(9, read_at=7.0))

    async def first_then_hold(request: httpx.Request) -> httpx.Response:
        if state["calls"] == 0:
            state["calls"] += 1
            await first_gate.wait()
            return rest_book(9, read_at=4.0)
        return await second(request)

    state = {"calls": 0}
    first_gate = asyncio.Event()

    async def scenario(h: Harness) -> None:
        await h.start()
        await push_expiring_book(h, 5.0)
        h.router.routes[f"/api/v1{ORDERBOOK}"] = first_then_hold
        h.clock.now += 4
        await trigger_resync(h)
        h.clock.now += 2
        before = readies(h)
        first_gate.set()
        await settle()
        assert readies(h) == before and not h.feed.is_ready(MARKET)
        held = h.feed.books(MARKET)[EX]
        assert (held.source, held.expiry_known) == (BookSource.PUSH, True)  # kept, expired
        second.event.set()  # control: a read made after the expiry recovers
        await settle()
        assert readies(h) == before + 1 and h.feed.is_ready(MARKET)
        await h.stop()

    run(scenario, policy=QUIET)


def test_unversioned_read_requested_before_expiry_cannot_clear_it() -> None:
    first_gate = asyncio.Event()
    state = {"calls": 0}

    async def route(request: httpx.Request) -> httpx.Response:
        state["calls"] += 1
        if state["calls"] == 1:
            await first_gate.wait()  # requested at T0+4, answered at T0+6
        return rest_book(None)

    async def scenario(h: Harness) -> None:
        await h.start()
        await push_expiring_book(h, 5.0)
        h.router.routes[f"/api/v1{ORDERBOOK}"] = route
        h.clock.now += 4
        await trigger_resync(h)
        h.clock.now += 2
        before = readies(h)
        first_gate.set()
        await settle()
        # The first read cannot be shown to be after the expiry; the immediate follow-up
        # was requested at T0+6, past T0+5 plus the 1 s skew margin, and recovers.
        assert state["calls"] == 2
        held = h.feed.books(MARKET)[EX]
        assert (held.source, held.as_of, held.expiry_epoch) == (BookSource.REST, None, None)
        assert h.feed.is_ready(MARKET) and readies(h) == before + 1
        await h.stop()

    run(scenario, policy=QUIET)


def test_replacement_before_expiry_keeps_the_expiry_outstanding() -> None:
    """A newer read made before a known expiry replaces the book but must not erase it."""

    async def scenario(h: Harness) -> None:
        await h.start()
        await push_expiring_book(h, 30.0)
        h.router.routes[f"/api/v1{ORDERBOOK}"] = rest_book(10, read_at=2.0)
        h.clock.now += 2
        await trigger_resync(h)
        held = h.feed.books(MARKET)[EX]
        assert (held.source, held.as_of.sequence) == (BookSource.REST, 10)
        assert held.next_expiry_at == iso(30.0) and held.expiry_known is False
        assert h.feed.is_ready(MARKET)
        h.clock.now += 29  # T0+31: past the carried expiry; no timer runs
        assert not h.feed.is_ready(MARKET)
        await h.stop()

    run(scenario, policy=QUIET)


# --- 2. dropped events block action readiness ---------------------------------------------------


def test_gap_hiding_a_settlement_is_never_cleared_by_book_refreshes() -> None:
    async def scenario(h: Harness) -> None:
        await h.start()
        h.transport.batch(h.topic(), batch(1, 0))
        await settle()
        mark = len(h.events)
        h.transport.batch(
            h.topic(),
            batch(5, 3, trades=[trade("t5", 55)], books=[book(55)], market_settled=[settled()]),
        )
        await settle()
        new = h.events[mark:]
        # The gap batch's arrays stay discarded: no settlement or trade callbacks.
        assert {type(e).__name__ for e in new} <= {"MarketStale", "BookUpdated"}
        assert not any(isinstance(e, TradeReceived) for e in new)
        for _ in range(3):  # successful book refreshes, then a re-join and another
            await h.clock.advance(2.0)
            h.transport.subscribed(h.topic())
            await settle()
        await h.clock.advance(2.0)
        assert h.count(ORDERBOOK) >= 4
        assert readies(h) == 1 and not h.feed.is_ready(MARKET)
        assert h.feed.blockers(MARKET) == {StaleReason.REVISION_GAP}
        await h.stop()

    run(scenario, policy=QUIET)


@pytest.mark.parametrize(
    ("payload", "reason"),
    [
        ({"resyncRequired": True, "delivery": {"revision": 2, "previousRevision": 1}},
         StaleReason.RESYNC_REQUIRED),
        ('{"delivery": {"revision": 2, "previousRevision": 1}, "trades": "x"}',
         StaleReason.MALFORMED),
    ],
)
def test_compact_resync_required_and_malformed_stay_blocked(payload: Any, reason: Any) -> None:
    async def scenario(h: Harness) -> None:
        await h.start()
        h.transport.batch(h.topic(), batch(1, 0))
        await settle()
        if isinstance(payload, str):
            h.transport.push(BroadcastEvent(h.topic(), "market_batch", payload))
        else:
            h.transport.batch(h.topic(), payload)
        await settle()
        await h.clock.advance(2.0)
        h.transport.subscribed(h.topic())
        await h.clock.advance(2.0)
        assert h.count(ORDERBOOK) == 3  # two successful book refreshes
        assert readies(h) == 1 and not h.feed.is_ready(MARKET)
        assert h.feed.blockers(MARKET) == {reason}
        assert h.feed.last_revision(MARKET) == 2
        await h.stop()

    run(scenario, policy=QUIET)


def test_non_blocking_trigger_still_recovers() -> None:
    """Control: a book-only trigger is fully recovered by a book read."""

    async def scenario(h: Harness) -> None:
        await h.start()
        await trigger_resync(h)
        assert not h.feed.is_ready(MARKET)
        await h.clock.advance(2.0)
        assert h.feed.is_ready(MARKET) and readies(h) == 2
        assert h.feed.blockers(MARKET) == frozenset()
        await h.stop()

    run(scenario, policy=QUIET)


# --- 3. closing boundary -------------------------------------------------------------------------


def test_not_ready_as_soon_as_aclose_returns() -> None:
    async def scenario(h: Harness) -> None:
        await h.start()
        assert h.feed.is_ready(MARKET)
        await watchdog(h.feed.aclose())
        assert not h.feed.is_ready(MARKET)  # before run() has even finished
        await watchdog(h.task)
        assert not h.feed.is_ready(MARKET)

    run(scenario, policy=QUIET)


class SlowCloseTransport(FakeTransport):
    """close() waits for `release` before it stops delivering events."""

    def __init__(self) -> None:
        super().__init__()
        self.release = asyncio.Event()

    async def close(self) -> None:
        await self.release.wait()
        await super().close()


def test_response_completing_while_closing_publishes_nothing() -> None:
    transport = SlowCloseTransport()
    state: dict[str, Any] = {}

    async def scenario(h: Harness) -> None:
        async def route(request: httpx.Request) -> httpx.Response:
            # Closing begins while this read is in flight: aclose() runs up to the slow
            # close(), then the response completes in the same turn.
            state["closer"] = asyncio.create_task(h.feed.aclose())
            await asyncio.sleep(0)
            state["ready_while_closing"] = h.feed.is_ready(MARKET)
            return rest_book(None)

        await h.start()
        h.router.routes[f"/api/v1{ORDERBOOK}"] = route
        mark = len(h.events)
        try:
            await trigger_resync(h)
            await h.clock.advance(2.0)  # min_resync_interval_s: the read starts now
            await settle()
            assert h.count(ORDERBOOK) == 2 and "closer" in state
            assert state["ready_while_closing"] is False
            assert not any(isinstance(e, MarketReady) for e in h.events[mark:])
            assert not h.feed.is_ready(MARKET)
        finally:
            transport.release.set()  # never leave cleanup waiting on the slow close
        await watchdog(state["closer"])
        await watchdog(h.task)
        assert h.task.exception() is None
        assert not any(isinstance(e, MarketReady) for e in h.events[mark:])
        assert set(asyncio.all_tasks()) - h.baseline_tasks - {asyncio.current_task()} == set()

    run(scenario, policy=QUIET, transport=transport)
