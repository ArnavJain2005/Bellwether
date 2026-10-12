"""M2-A behaviour changes, written against the feed's pre-M2-A public API only.

These tests use nothing but `is_ready`, `blockers`, `last_revision` and the published
events, so they also run against the code before M2-A (with a harness that does not pass
a ledger). There, every `test_regression_*` fails on an assertion: the old feed reported
these markets ready. The `test_control_*` tests pass on both, so the new behaviour is
not "never ready for any reason".

Design: docs/design/market-data-recovery.md, examples F-4, F-9 and F-10, and AT-02,
AT-15, AT-30 and AT-34.
"""

from __future__ import annotations

import asyncio
from typing import Any

import httpx
from realtime_kit import MARKET, Harness, batch, dirty, market_detail, settle
from sig_testkit import Router, json_response

from engine.sig_client.realtime import (
    MarketReady,
    RealtimeFeedPolicy,
    RealtimeMarketFeed,
    SocketEvent,
    SocketState,
)
from engine.sig_client.realtime_topic import StaleReason

QUIET = RealtimeFeedPolicy(tick_s=1e6)
ORDERBOOK = f"/markets/{MARKET}/orderbook"


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

    return asyncio.run(main())


def detail_router(status: str, settled_with: str | None = None) -> Router:
    router = Router()
    body = market_detail(MARKET, status=status, settled_with=settled_with)
    router.routes[f"/api/v1/markets/{MARKET}"] = json_response(200, body)
    return router


def readies(h: Harness) -> int:
    return sum(isinstance(e, MarketReady) for e in h.events)


async def book_trigger(h: Harness) -> None:
    h.transport.batch(h.topic(), dirty(), event="book_dirty")
    await settle()
    await h.clock.advance(2.0)


# --- regressions: ready before M2-A, not ready now -----------------------------------------


def test_regression_a_new_feed_with_default_arguments_is_never_ready() -> None:
    """F-4 / AT-02: a new process cannot show its history, so a restart is not recovery."""

    async def scenario(h: Harness) -> None:
        # The feed exactly as production builds it: no ledger argument.
        h.feed = RealtimeMarketFeed(
            h.rest,
            h.tokens,
            h.transport,
            [MARKET],
            policy=QUIET,
            sink=h.events.append,
            clock=h.clock,
            sleep=h.clock.sleep,
            wall_clock=h.clock.wall,
        )
        await h.start()
        await book_trigger(h)
        assert h.count(ORDERBOOK) >= 2  # the orderbook resyncs succeeded
        assert not h.feed.is_ready(MARKET)
        assert readies(h) == 0
        assert h.feed.blockers(MARKET) != frozenset()
        await h.stop()

    run(scenario, policy=QUIET)


def test_regression_reconnect_on_a_quiet_topic_stays_not_ready() -> None:
    """F-9: a batch lost while disconnected is invisible until a later batch arrives."""

    async def scenario(h: Harness) -> None:
        await h.start()
        h.transport.batch(h.topic(), batch(40, 39))
        await settle()
        assert h.feed.is_ready(MARKET)
        h.transport.push(SocketEvent(SocketState.CLOSED))
        await settle()
        h.transport.subscribed(h.topic())
        await h.clock.advance(2.0)
        assert h.count(ORDERBOOK) == 2  # the books were refreshed ...
        assert not h.feed.is_ready(MARKET)  # ... but continuity is not shown
        assert readies(h) == 1
        await h.stop()

    run(scenario, policy=QUIET)


def test_regression_retried_old_batch_does_not_restore_readiness() -> None:
    """AT-30: a duplicate says nothing about revisions published after the break."""

    async def scenario(h: Harness) -> None:
        await h.start()
        h.transport.batch(h.topic(), batch(40, 39))
        await settle()
        h.transport.push(SocketEvent(SocketState.CLOSED))
        await settle()
        h.transport.subscribed(h.topic())
        h.transport.batch(h.topic(), batch(40, 39))
        await h.clock.advance(2.0)
        assert h.feed.last_revision(MARKET) == 40
        assert not h.feed.is_ready(MARKET)
        await h.stop()

    run(scenario, policy=QUIET)


def test_regression_reconnect_before_any_batch_never_recovers() -> None:
    """AT-34: with no fence, no batch can show what the window missed."""

    async def scenario(h: Harness) -> None:
        await h.start()
        h.transport.push(SocketEvent(SocketState.ERROR))
        await settle()
        h.transport.subscribed(h.topic())
        for revision in range(1, 4):
            h.transport.batch(h.topic(), batch(revision, revision - 1))
        await h.clock.advance(2.0)
        assert not h.feed.is_ready(MARKET)
        await h.stop()

    run(scenario, policy=QUIET)


def test_regression_market_settled_before_subscription_is_never_ready() -> None:
    """Design 1.6 item 2 / AT-15: initial readiness now reads the market state."""

    async def scenario(h: Harness) -> None:
        await h.start()
        await book_trigger(h)
        assert not h.feed.is_ready(MARKET)
        assert readies(h) == 0
        assert h.feed.blockers(MARKET) != frozenset()
        await h.stop()

    run(scenario, policy=QUIET, router=detail_router("settled", "YES"))


def test_regression_settlement_hidden_by_resync_required_is_detected() -> None:
    """F-10: a lifecycle read showing settled quarantines a market blocked for another
    reason, so it is reported with a second reason, not only the batch's own."""

    responses: list[httpx.Response] = [
        json_response(200, market_detail(MARKET)),
        json_response(200, market_detail(MARKET, status="settled", settled_with="YES")),
    ]
    router = Router()
    router.routes[f"/api/v1/markets/{MARKET}"] = responses

    async def scenario(h: Harness) -> None:
        await h.start()
        h.transport.batch(
            h.topic(), {"resyncRequired": True, "delivery": {"revision": 2, "previousRevision": 1}}
        )
        await settle()
        await h.clock.advance(2.0)
        blockers = h.feed.blockers(MARKET)
        assert StaleReason.RESYNC_REQUIRED in blockers
        assert len(blockers) == 2  # the quarantine is visible next to the batch's blocker
        await h.stop()

    run(scenario, policy=QUIET, router=router)


# --- controls: unchanged behaviour ------------------------------------------------------------


def test_control_initial_subscription_then_ready() -> None:
    async def scenario(h: Harness) -> None:
        await h.start()
        assert h.feed.is_ready(MARKET) and readies(h) == 1
        await h.stop()

    run(scenario, policy=QUIET)


def test_control_book_only_trigger_recovers() -> None:
    async def scenario(h: Harness) -> None:
        await h.start()
        await book_trigger(h)
        assert h.feed.is_ready(MARKET) and readies(h) == 2
        assert h.feed.blockers(MARKET) == frozenset()
        await h.stop()

    run(scenario, policy=QUIET)


def test_control_gap_blocks_through_resyncs() -> None:
    async def scenario(h: Harness) -> None:
        await h.start()
        h.transport.batch(h.topic(), batch(1, 0))
        h.transport.batch(h.topic(), batch(3, 2))
        await settle()
        for _ in range(2):
            await book_trigger(h)
        assert not h.feed.is_ready(MARKET)
        assert StaleReason.REVISION_GAP in h.feed.blockers(MARKET)
        await h.stop()

    run(scenario, policy=QUIET)
