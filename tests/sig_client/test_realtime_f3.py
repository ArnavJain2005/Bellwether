"""S1-T3-A-F3: fail closed when the local clock's offset from SIG's is unknown.

The local wall clock (ManualClock.wall) runs SKEW seconds ahead of SIG's. An order's
`nextExpiryAt` and every `asOf.at` are SIG times; `sig(x)` writes SIG time T0 + x. When
the local clock reads T0 + x, SIG's clock reads T0 + x - SKEW.

Offline: REST through httpx.MockTransport, FakeTransport, ManualClock, real-time
watchdogs. All realtime payloads are synthetic.
"""

from __future__ import annotations

import asyncio
import copy
import math
from datetime import timedelta
from decimal import Decimal
from typing import Any

import httpx
import pytest
from realtime_kit import MARKET, T0, Harness, batch, book, dumps, settle, watchdog
from sig_testkit import TOURNAMENT, fixture, json_response

from engine.sig_client.realtime import MarketReady, RealtimeFeedPolicy
from engine.sig_client.realtime_topic import BookSource, MarketTopic
from engine.sig_client.realtime_wire import parse_instant
from engine.sig_client.wire import CombinedOrderbookExchange

ORDERBOOK = f"/markets/{MARKET}/orderbook"
EX = "36"
SKEW = 2.0  # the local clock is two seconds ahead of SIG's
DIRTY = {"exchangeId": 36, "marketId": MARKET, "tournamentId": TOURNAMENT.id, "at": "x"}


def sig(delta_s: float) -> str:
    return (T0 + timedelta(seconds=delta_s)).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


def run(scenario: Any, **kwargs: Any) -> Harness:
    async def main() -> Harness:
        h = Harness(**kwargs)
        try:
            await scenario(h)
        finally:
            if h.task is not None and not h.task.done():
                await h.feed.aclose()
                await asyncio.gather(h.task, return_exceptions=True)
        return h

    return asyncio.run(watchdog(main(), 20.0))


def rest_body(sequence: int | None, read_at_sig: float = 0.0) -> dict[str, Any]:
    body = copy.deepcopy(fixture("market_orderbook"))
    exchange = body["contexts"][0]["orderbook"]["exchanges"][0]
    exchange["asOf"] = (
        None if sequence is None else {"sequence": sequence, "at": sig(read_at_sig)}
    )
    return body


def readies(h: Harness) -> int:
    return sum(isinstance(e, MarketReady) for e in h.events)


class Route:
    """Answers REST reads in order (the last answer repeats) and counts them."""

    def __init__(self, *bodies: dict[str, Any]) -> None:
        self.bodies = list(bodies)
        self.calls = 0

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.calls += 1
        body = self.bodies.pop(0) if len(self.bodies) > 1 else self.bodies[0]
        return json_response(200, body)


async def expiring_push_then_local_expiry(h: Harness, route: Route) -> None:
    """A pushed book whose order expires at SIG T0+5; the local clock then reads T0+6
    (SIG T0+4: the order has not expired yet) and a book_dirty asks for a refetch."""
    await h.start()
    h.router.routes[f"/api/v1{ORDERBOOK}"] = route
    h.transport.batch(h.topic(), batch(1, 0, books=[book(9, next_expiry=sig(5))]))
    await settle()
    h.clock.now += 6  # local T0+6; no timer runs
    assert not h.feed.is_ready(MARKET)  # locally the expiry has passed
    h.transport.batch(h.topic(), DIRTY, event="book_dirty")
    await settle()


# --- the default: unknown bound -----------------------------------------------------------------


def test_default_bound_is_unknown() -> None:
    assert RealtimeFeedPolicy().expiry_skew_margin_s is None


def test_unknown_bound_keeps_the_expiry_with_a_clock_two_seconds_ahead() -> None:
    route = Route(rest_body(None))  # unversioned: no read time from SIG

    async def scenario(h: Harness) -> None:
        await expiring_push_then_local_expiry(h, route)
        before = readies(h)
        assert route.calls == 1
        held = h.feed.books(MARKET)[EX]
        assert held.source is BookSource.PUSH  # the unversioned read did not replace it
        assert held.expiry_epoch == parse_instant(sig(5))
        assert not h.feed.is_ready(MARKET)
        # Reads continue on the normal schedule (2 s apart) and never clear the expiry.
        await h.clock.advance(10.0)
        assert 2 <= route.calls <= 1 + 10 / 2 + 1
        held = h.feed.books(MARKET)[EX]
        assert held.expiry_epoch == parse_instant(sig(5))
        assert not h.feed.is_ready(MARKET) and readies(h) == before
        await h.stop()

    run(scenario, policy=RealtimeFeedPolicy(tick_s=1e6))


def test_unknown_bound_carries_the_expiry_through_an_unversioned_replacement() -> None:
    """Before the expiry, an unversioned read still replaces the book (it is newer by the
    unversioned rule) but must keep the outstanding expiry rather than erase it."""
    route = Route(rest_body(None))

    async def scenario(h: Harness) -> None:
        await h.start()
        h.router.routes[f"/api/v1{ORDERBOOK}"] = route
        h.transport.batch(h.topic(), batch(1, 0, books=[book(9, next_expiry=sig(30))]))
        await settle()
        h.clock.now += 2
        h.transport.batch(h.topic(), DIRTY, event="book_dirty")
        await settle()
        held = h.feed.books(MARKET)[EX]
        assert (held.source, held.as_of) == (BookSource.REST, None)
        assert held.expiry_epoch == parse_instant(sig(30)) and held.expiry_known is False
        h.clock.now += 29  # local T0+31
        assert not h.feed.is_ready(MARKET)
        await h.stop()

    run(scenario, policy=RealtimeFeedPolicy(tick_s=1e6))


# --- an explicit bound ----------------------------------------------------------------------------


def test_explicit_bound_covering_the_skew_recovers_only_after_sig_expiry() -> None:
    route = Route(rest_body(None))

    async def scenario(h: Harness) -> None:
        await expiring_push_then_local_expiry(h, route)
        before = readies(h)
        # Requested at local T0+6: 6 - 3 = 3 < 5, so not shown after the expiry.
        assert route.calls == 1 and h.feed.books(MARKET)[EX].source is BookSource.PUSH
        assert not h.feed.is_ready(MARKET)
        await h.clock.advance(2.0)  # follow-up requested at local T0+8: 8 - 3 >= 5
        assert route.calls == 2
        held = h.feed.books(MARKET)[EX]
        assert (held.source, held.expiry_epoch) == (BookSource.REST, None)
        assert h.feed.is_ready(MARKET) and readies(h) == before + 1
        await h.stop()

    run(scenario, policy=RealtimeFeedPolicy(tick_s=1e6, expiry_skew_margin_s=3.0))


@pytest.mark.parametrize("bound", [0, 0.0, 2, 2.5, 3600.0])
def test_explicit_bound_accepts_finite_non_negative_numbers(bound: float) -> None:
    assert RealtimeFeedPolicy(expiry_skew_margin_s=bound).expiry_skew_margin_s == bound


@pytest.mark.parametrize("bound", [-0.001, -1, math.nan, math.inf, -math.inf, True, False, "1"])
def test_explicit_bound_rejects_invalid_values(bound: Any) -> None:
    with pytest.raises(ValueError):
        RealtimeFeedPolicy(expiry_skew_margin_s=bound)


# --- versioned recovery needs no bound ------------------------------------------------------------


def test_versioned_read_after_sig_expiry_recovers_with_the_bound_unknown() -> None:
    # The first read is made at SIG T0+4 (before the expiry); the follow-up at SIG T0+5.5.
    route = Route(rest_body(9, read_at_sig=4.0), rest_body(9, read_at_sig=5.5))

    async def scenario(h: Harness) -> None:
        await expiring_push_then_local_expiry(h, route)
        before = readies(h)
        assert route.calls == 1
        held = h.feed.books(MARKET)[EX]
        assert held.source is BookSource.PUSH and held.expiry_epoch is not None
        assert not h.feed.is_ready(MARKET)
        await h.clock.advance(2.0)
        assert route.calls == 2
        held = h.feed.books(MARKET)[EX]
        assert (held.source, held.as_of.at, held.expiry_epoch) == (
            BookSource.REST,
            sig(5.5),
            None,
        )
        assert h.feed.is_ready(MARKET) and readies(h) == before + 1
        await h.stop()

    run(scenario, policy=RealtimeFeedPolicy(tick_s=1e6))


# --- topic rule ---------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("skew_margin", "covered"),
    [(None, False), (Decimal(3600), False), (Decimal(1), True), (Decimal(0), True)],
)
def test_unversioned_snapshot_needs_an_explicit_bound(skew_margin: Any, covered: bool) -> None:
    t = MarketTopic(TOURNAMENT.id, MARKET)
    t.on_message("market_batch", dumps(batch(1, 0, books=[book(9, next_expiry=sig(-60))])))
    exchange = rest_body(None)["contexts"][0]["orderbook"]["exchanges"][0]
    snapshot = CombinedOrderbookExchange.model_validate(
        {**exchange, "impliedProbability": None, "bids": [], "asks": []}
    )
    now = parse_instant(sig(0))
    changed = t.apply_snapshot(
        [snapshot],
        depth=200,
        started_at=t.stamp(),
        now_epoch=now,
        requested_epoch=now,  # a minute after the expiry, by the local clock
        skew_margin=skew_margin,
    )
    assert bool(changed) is covered
    assert t.has_expired_book(now) is (not covered)
