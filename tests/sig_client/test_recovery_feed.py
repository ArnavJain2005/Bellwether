"""M2-A acceptance tests: the feed with its recovery ledger, offline.

Design: docs/design/market-data-recovery.md, section 8 (AT-xx ids) and 9.1. REST goes
through httpx.MockTransport, the socket is FakeTransport, time is ManualClock. Realtime
payloads are synthetic. Unless a test says otherwise the feed has a TEST-ONLY bootstrap
ledger (realtime_kit.test_bootstrap); production's default is unknown history.

Race tests are deterministic: a held market-detail route keeps a read in flight until the
test releases it, and seams around `get_market` place closing or the deadline exactly
between a response and the observe/commit step.
"""

from __future__ import annotations

import asyncio
import copy
import itertools
import logging
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
    dirty,
    market_detail,
    settle,
    settled,
    token_body,
    trade,
)
from sig_testkit import TOURNAMENT, Router, error_body, fixture, json_response

from engine.config import TournamentRef
from engine.sig_client.errors import SigServiceUnavailableError
from engine.sig_client.rate_limiter import RequestKind
from engine.sig_client.readiness import DOES_NOT_COVER, ReadinessStatus
from engine.sig_client.realtime import (
    BroadcastEvent,
    ChannelStatus,
    ChannelStatusEvent,
    MarketReady,
    MarketStale,
    RealtimeFeedPolicy,
    RealtimeMarketFeed,
    SocketEvent,
    SocketState,
)
from engine.sig_client.realtime_topic import BLOCKING_REASONS, StaleReason
from engine.sig_client.recovery import (
    COVERAGE_NOTE,
    InstanceState,
    LedgerOrigin,
    RecoveryLedger,
    RecoveryPolicy,
    WindowCause,
    WindowState,
)

ORDERBOOK = f"/markets/{MARKET}/orderbook"
DETAIL = f"/markets/{MARKET}"
DETAIL_ROUTE = f"/api/v1{DETAIL}"
QUIET = RealtimeFeedPolicy(tick_s=1e6)  # the timer never fires within these tests
SETTLED_ON = "2026-10-10T12:00:01.000Z"

# Messages that raise each message-borne blocking reason on a freshly started market.
CASES: dict[StaleReason, list[tuple[str, Any]]] = {
    StaleReason.MARKET_SETTLED: [("market_batch", batch(1, 0, market_settled=[settled()]))],
    StaleReason.NULL_TRADE_SEQUENCE: [
        ("market_batch", batch(1, 0, trades=[trade(None, None)], books=[book(5)]))
    ],
    StaleReason.REVISION_GAP: [("market_batch", batch(1, 0)), ("market_batch", batch(3, 2))],
    StaleReason.RESYNC_REQUIRED: [
        ("market_batch", {"resyncRequired": True, "delivery": {"revision": 1, "previousRevision": 0}})
    ],
    StaleReason.MALFORMED: [("market_batch", {"delivery": "broken"})],
    StaleReason.MISSING_BOOK: [("market_batch", batch(1, 0, book_dirty=[dirty()]))],
}
REASONS = list(CASES)


def detail(
    status: str = "open", settled_with: str | None = None, tournament: TournamentRef = TOURNAMENT
) -> httpx.Response:
    settled_on = SETTLED_ON if status == "settled" else None
    body = market_detail(
        MARKET,
        status=status,
        settled_with=settled_with,
        settled_on=settled_on,
        tournament=tournament,
    )
    return json_response(200, body)


OPEN = detail()


def _router(*details: httpx.Response) -> Router:
    """The standard routes, with market detail answered by `details` (the last repeats)."""
    router = Router()
    router.routes[DETAIL_ROUTE] = list(details)
    return router


class Held:
    """A market-detail route: `immediate` responses are served at once, in order; every
    later request waits until the test releases it (or the read is cancelled)."""

    def __init__(self, *immediate: httpx.Response) -> None:
        self.immediate = list(immediate)
        self.waiting: list[asyncio.Future[httpx.Response]] = []
        self.cancelled = 0
        self.answered = 0

    async def __call__(self, request: httpx.Request) -> httpx.Response:
        if self.immediate:
            return self.immediate.pop(0)
        future: asyncio.Future[httpx.Response] = asyncio.get_running_loop().create_future()
        self.waiting.append(future)
        try:
            response = await future
        except asyncio.CancelledError:
            self.cancelled += 1
            raise
        self.answered += 1
        return response

    def pending(self) -> int:
        return sum(not f.done() for f in self.waiting)

    def release(self, response: httpx.Response) -> bool:
        for future in self.waiting:
            if not future.done():
                future.set_result(response)
                return True
        return False


class SlowClose(FakeTransport):
    """A transport whose close() does not return until the test allows it."""

    def __init__(self) -> None:
        super().__init__()
        self.allow_close = asyncio.Event()

    async def close(self) -> None:
        if not self.closed:
            self.closed = True
            self.queue.put_nowait(None)
        await self.allow_close.wait()


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


async def send(h: Harness, reason: StaleReason) -> None:
    for event, payload in CASES[reason]:
        h.transport.batch(h.topic(), payload, event=event)
        await settle()


async def book_trigger(h: Harness) -> None:
    """A standalone book_dirty and the resync it starts (a book-only trigger)."""
    h.transport.batch(h.topic(), {**dirty(), "exchangeId": 36}, event="book_dirty")
    await settle()
    await h.clock.advance(2.0)


def readies(h: Harness) -> int:
    return sum(isinstance(e, MarketReady) for e in h.events)


def state(h: Harness) -> tuple[Any, ...]:
    ledger = h.ledger
    return (
        ledger.instances(MARKET),
        ledger.generation(MARKET),
        ledger.windows(MARKET),
        ledger.lifecycle(MARKET),
        ledger.last_revision(MARKET),
        ledger.initial_evidence(MARKET),
    )


def instance(h: Harness, reason: StaleReason) -> Any:
    (found,) = [i for i in h.ledger.instances(MARKET) if i.reason is reason]
    return found


def reasons_text(h: Harness) -> str:
    return "\n".join(h.feed.readiness(MARKET).reasons)


def assert_consistent(h: Harness) -> None:
    """is_ready() is exactly readiness().status is READY, for every market."""
    for market_id in h.market_ids:
        status = h.feed.readiness(market_id).status
        assert h.feed.is_ready(market_id) is (status is ReadinessStatus.READY)


# --- positive control: the ready path still works with a (test) bootstrap ledger -------------


def test_control_bootstrap_market_becomes_ready_after_lifecycle_read_and_resync() -> None:
    async def scenario(h: Harness) -> None:
        await h.start()
        assert h.feed.is_ready(MARKET) and readies(h) == 1
        assert h.count(DETAIL) == 1 and h.count(ORDERBOOK) == 1
        result = h.feed.readiness(MARKET)
        assert result.status is ReadinessStatus.READY and result.reasons == ()
        assert result.history.origin is LedgerOrigin.BOOTSTRAP
        assert result.history.initial_evidence is not None
        assert result.history.initial_evidence.status == "open"
        assert h.feed.blockers(MARKET) == frozenset()
        # A book-only trigger is still fully recovered by a book read.
        await book_trigger(h)
        assert h.feed.is_ready(MARKET) and readies(h) == 2
        assert h.count(DETAIL) == 1  # nothing to recover: no lifecycle read spent
        assert_consistent(h)
        await h.stop()

    run(scenario, policy=QUIET)


# --- AT-01: blockers survive any number of successful orderbook resyncs ---------------------


@pytest.mark.parametrize("reason", REASONS)
def test_at01_blocker_survives_successful_resyncs(reason: StaleReason) -> None:
    async def scenario(h: Harness) -> None:
        await h.start()
        await send(h, reason)
        reads = h.count(ORDERBOOK)
        for _ in range(3):
            await book_trigger(h)
        assert h.count(ORDERBOOK) >= reads + 3  # the resyncs ran and succeeded
        assert not h.feed.is_ready(MARKET) and readies(h) == 1
        assert reason in h.feed.blockers(MARKET)
        assert instance(h, reason).state is InstanceState.OPEN
        result = h.feed.readiness(MARKET)
        assert result.status is ReadinessStatus.NOT_READY
        assert f"blocker: {reason.value}" in reasons_text(h)
        assert_consistent(h)
        await h.stop()

    run(scenario, policy=QUIET)


# --- AT-02: a restart with unknown history is never ready -----------------------------------


@pytest.mark.parametrize("reason", REASONS)
def test_at02_restart_without_retained_history_is_never_ready(reason: StaleReason) -> None:
    async def main() -> None:
        first = Harness(policy=QUIET)
        await first.start()
        await send(first, reason)
        assert reason in first.feed.blockers(MARKET)
        await first.stop()
        # A new process: a new feed and a new default ledger (unknown history).
        h = Harness(policy=QUIET, ledger=RecoveryLedger())
        try:
            await h.start()
            await book_trigger(h)
            await h.clock.advance(300.0)
            lifecycle = h.feed.readiness(MARKET).lifecycle
            assert lifecycle is not None and lifecycle.status == "open"  # the read showed open
            assert h.count(ORDERBOOK) >= 2  # and the orderbook resyncs succeeded
            assert h.feed.blockers(MARKET) == {StaleReason.HISTORY_UNKNOWN}
            assert not h.feed.is_ready(MARKET)
            result = h.feed.readiness(MARKET)
            assert result.status is ReadinessStatus.NOT_READY
            assert result.history.origin is LedgerOrigin.UNKNOWN
            assert result.history.history_unknown_open
            assert "history unknown" in reasons_text(h)
            assert not any(isinstance(e, MarketReady) for e in h.events)
        finally:
            await h.stop()

    asyncio.run(main())


def test_at02_feed_built_without_a_ledger_has_unknown_history() -> None:
    """Production's default: no ledger argument at all."""

    async def main() -> None:
        h = Harness(policy=QUIET)
        feed = RealtimeMarketFeed(h.rest, h.tokens, FakeTransport(), [MARKET], clock=h.clock)
        assert feed.ledger.origin is LedgerOrigin.UNKNOWN
        assert feed.blockers(MARKET) == {StaleReason.HISTORY_UNKNOWN}
        assert feed.readiness(MARKET).status is ReadinessStatus.NOT_READY
        await feed.aclose()
        await h.rest.aclose()
        await h.tokens.aclose()

    asyncio.run(main())


# --- AT-03: a replacement feed sharing the ledger inherits every blocker -------------------


@pytest.mark.parametrize("reason", REASONS)
def test_at03_replacement_feed_inherits_blockers_and_opens_a_window(reason: StaleReason) -> None:
    async def main() -> None:
        first = Harness(policy=QUIET)
        ledger = first.ledger
        await first.start()
        await send(first, reason)
        fence = ledger.last_revision(MARKET)
        # Refused while the first feed is live: two feeds may not share one ledger.
        with pytest.raises(RuntimeError, match="not closing"):
            RealtimeMarketFeed(first.rest, first.tokens, FakeTransport(), [MARKET], ledger=ledger)
        await first.stop()
        generation = ledger.generation(MARKET)
        h = Harness(policy=QUIET, ledger=ledger, clock=first.clock)
        # Synchronously, at construction: a window with the retained revision as fence.
        (window,) = ledger.open_windows(MARKET)
        assert window.causes == (WindowCause.FEED_REPLACED,) and window.fence == fence
        assert ledger.generation(MARKET) == generation + 1
        assert h.feed.last_revision(MARKET) == fence  # gap checks continue from it
        try:
            await h.start()
            await book_trigger(h)
            assert reason in h.feed.blockers(MARKET)
            assert instance(h, reason).state is InstanceState.OPEN
            assert not h.feed.is_ready(MARKET) and readies(h) == 0
            assert h.feed.readiness(MARKET).continuity.windows[0].fence == fence
        finally:
            await h.stop()

    asyncio.run(main())


def test_at03_replacement_feed_detects_a_gap_against_the_retained_revision() -> None:
    async def main() -> None:
        first = Harness(policy=QUIET)
        await first.start()
        first.transport.batch(first.topic(), batch(5, 4))
        await settle()
        await first.stop()
        h = Harness(policy=QUIET, ledger=first.ledger, clock=first.clock)
        try:
            await h.start()
            h.transport.batch(h.topic(), batch(8, 7))  # revisions 6-7 were never seen
            await settle()
            gap = instance(h, StaleReason.REVISION_GAP)
            assert gap.revisions == (5, 8)
        finally:
            await h.stop()

    asyncio.run(main())


# --- AT-04 to AT-06: settlement against market detail ---------------------------------------


def test_at04_matching_settlement_is_confirmed_then_quarantined() -> None:
    async def scenario(h: Harness) -> None:
        await h.start()
        await send(h, StaleReason.MARKET_SETTLED)
        settlement = instance(h, StaleReason.MARKET_SETTLED)
        not_open = instance(h, StaleReason.LIFECYCLE_NOT_OPEN)
        assert settlement.state is InstanceState.QUARANTINED
        assert not_open.state is InstanceState.QUARANTINED
        assert settlement.evidence[-1].finding.startswith("confirmed")
        assert h.feed.blockers(MARKET) == {
            StaleReason.MARKET_SETTLED,
            StaleReason.LIFECYCLE_NOT_OPEN,
        }
        assert h.feed.readiness(MARKET).status is ReadinessStatus.QUARANTINED
        reads = h.count(DETAIL)
        for _ in range(3):
            await book_trigger(h)
        h.transport.subscribed(h.topic())
        await h.clock.advance(600.0)
        assert not h.feed.is_ready(MARKET) and readies(h) == 1
        assert h.count(DETAIL) == reads  # a quarantined market is not retried
        assert_consistent(h)
        await h.stop()

    run(scenario, policy=QUIET, router=_router(OPEN, detail("settled", "YES")))


def test_at05_market_detail_showing_open_is_inconclusive() -> None:
    async def scenario(h: Harness) -> None:
        await h.start()
        await send(h, StaleReason.MARKET_SETTLED)
        settlement = instance(h, StaleReason.MARKET_SETTLED)
        assert settlement.state is InstanceState.OPEN
        assert "(Q1)" in settlement.evidence[-1].finding
        result = h.feed.readiness(MARKET)
        assert result.status is ReadinessStatus.NOT_READY
        assert "inconclusive" in reasons_text(h) and "(Q1)" in reasons_text(h)
        # Bounded: an applied, inconclusive attempt is not repeated until something new.
        reads = h.count(DETAIL)
        await h.clock.advance(600.0)
        assert h.count(DETAIL) == reads == 2
        await h.stop()

    run(scenario, policy=QUIET)


def test_at06_settlement_label_mismatch_stays_open_and_quarantines_the_market() -> None:
    async def scenario(h: Harness) -> None:
        await h.start()
        await send(h, StaleReason.MARKET_SETTLED)  # the signal says YES
        settlement = instance(h, StaleReason.MARKET_SETTLED)
        assert settlement.state is InstanceState.OPEN
        assert "(Q2)" in settlement.evidence[-1].finding
        assert instance(h, StaleReason.LIFECYCLE_NOT_OPEN).state is InstanceState.QUARANTINED
        assert h.feed.readiness(MARKET).status is ReadinessStatus.QUARANTINED
        assert "(Q2)" in reasons_text(h)
        await h.stop()

    run(scenario, policy=QUIET, router=_router(OPEN, detail("settled", "NO")))


# --- AT-07 to AT-11: events during an attempt -----------------------------------------------


def test_at07_second_settlement_supersedes_the_first_attempt() -> None:
    held = Held(OPEN)

    async def scenario(h: Harness) -> None:
        h.router.routes[DETAIL_ROUTE] = held
        await h.start()
        h.transport.batch(h.topic(), batch(1, 0, market_settled=[settled()]))
        await settle()
        assert held.pending() == 1  # attempt 1 is reading
        h.transport.batch(h.topic(), batch(2, 1, market_settled=[settled()]))
        await settle()
        generation = h.ledger.generation(MARKET)
        first, second = h.ledger.instances(MARKET)
        assert held.release(detail("settled", "YES"))
        await settle()
        # Superseded: it may only restrict, so the settled read quarantines the market
        # with LIFECYCLE_NOT_OPEN, but confirms neither settlement.
        lifecycle = h.feed.readiness(MARKET).lifecycle
        assert lifecycle is not None and not lifecycle.current
        assert [i.state for i in h.ledger.instances(MARKET)[:2]] == [InstanceState.OPEN] * 2
        assert all(not i.evidence for i in h.ledger.instances(MARKET)[:2])
        assert h.ledger.generation(MARKET) == generation + 1
        assert first.reason is second.reason is StaleReason.MARKET_SETTLED
        await h.stop()

    run(scenario, policy=QUIET)


def test_at07_follow_up_attempt_targets_both_settlements() -> None:
    held = Held(OPEN)

    async def scenario(h: Harness) -> None:
        h.router.routes[DETAIL_ROUTE] = held
        await h.start()
        h.transport.batch(h.topic(), batch(1, 0, market_settled=[settled()]))
        await settle()
        h.transport.batch(h.topic(), batch(2, 1, market_settled=[settled()]))
        await settle()
        generation = h.ledger.generation(MARKET)
        assert held.release(OPEN)  # attempt 1: superseded, shows open: changes nothing
        await settle()
        assert h.ledger.generation(MARKET) == generation
        assert all(i.state is InstanceState.OPEN and not i.evidence
                   for i in h.ledger.instances(MARKET))
        assert held.pending() == 1  # the coalesced follow-up started
        assert held.release(detail("settled", "YES"))
        await settle()
        first, second = (i for i in h.ledger.instances(MARKET)
                         if i.reason is StaleReason.MARKET_SETTLED)
        assert first.state is second.state is InstanceState.QUARANTINED
        assert first.evidence[-1].observation.attempt_id == second.evidence[-1].observation.attempt_id
        assert first.evidence[-1].observation.current
        await h.stop()

    run(scenario, policy=QUIET)


async def _disturb(h: Harness, kind: str) -> None:
    if kind == "new blocker":
        h.transport.batch(h.topic(), batch(5, 4))  # a gap after revision 1
    elif kind == "re-join":
        h.transport.subscribed(h.topic())
    await settle()


@pytest.mark.parametrize("kind", ["new blocker", "re-join"])
@pytest.mark.parametrize("status", ["open", "settled", "closed"])
def test_at08_at09_at11_disturbance_during_an_attempt(kind: str, status: str) -> None:
    held = Held(OPEN)

    async def scenario(h: Harness) -> None:
        h.router.routes[DETAIL_ROUTE] = held
        await h.start()
        h.transport.batch(h.topic(), batch(1, 0, market_settled=[settled()]))
        await settle()
        assert held.pending() == 1
        await _disturb(h, kind)
        generation = h.ledger.generation(MARKET)
        before = h.ledger.instances(MARKET)
        assert held.release(detail(status, "YES" if status == "settled" else None))
        await settle()
        settlement = instance(h, StaleReason.MARKET_SETTLED)
        assert settlement.state is InstanceState.OPEN and not settlement.evidence  # no confirmation
        if status == "open":
            assert h.ledger.instances(MARKET) == before  # nothing changes
            assert h.ledger.generation(MARKET) == generation
        else:
            # AT-11: the live, superseded response restricts; the generation rises once.
            assert instance(h, StaleReason.LIFECYCLE_NOT_OPEN).state is InstanceState.QUARANTINED
            assert h.ledger.generation(MARKET) == generation + 1
            assert h.feed.readiness(MARKET).status is ReadinessStatus.QUARANTINED
        assert readies(h) == 1  # no ready published, ever
        await h.stop()

    run(scenario, policy=QUIET)


def _after_superseded(h: Harness, status: str, generation: int, before: Any) -> None:
    """AT-08's expectations: `open` changes nothing; `settled` quarantines only."""
    assert instance(h, StaleReason.MARKET_SETTLED).state is InstanceState.OPEN
    if status == "open":
        assert h.ledger.instances(MARKET) == before
        assert h.ledger.generation(MARKET) == generation
    else:
        assert instance(h, StaleReason.LIFECYCLE_NOT_OPEN).state is InstanceState.QUARANTINED
        assert h.ledger.generation(MARKET) == generation + 1


@pytest.mark.parametrize("status", ["open", "settled"])
def test_at10_token_refresh_during_an_attempt_supersedes_it(status: str) -> None:
    held = Held(OPEN)

    async def scenario(h: Harness) -> None:
        h.router.routes[DETAIL_ROUTE] = held
        await h.start()
        await h.clock.advance(10199.0)  # refresh due at 11200 (10 minutes before expiry)
        h.transport.batch(h.topic(), batch(1, 0, market_settled=[settled()]))
        await settle()
        assert held.pending() == 1
        await h.clock.advance(2.0)  # the refresh opens a window on every market
        assert h.count("/realtime/token") == 2
        assert WindowCause.TOKEN_REFRESH in h.ledger.open_windows(MARKET)[0].causes
        generation, before = h.ledger.generation(MARKET), h.ledger.instances(MARKET)
        assert held.release(detail(status, "YES" if status == "settled" else None))
        await settle()
        _after_superseded(h, status, generation, before)
        await h.stop()

    run(
        scenario,
        policy=RealtimeFeedPolicy(tick_s=1.0),
        token_responses=[
            json_response(200, token_body()),
            json_response(
                200, token_body(token="rt-sentinel-token-2", expires=T0 + timedelta(hours=6))
            ),
        ],
    )


@pytest.mark.parametrize("status", ["open", "settled"])
def test_at10_token_expiry_during_an_attempt_supersedes_it(status: str) -> None:
    held = Held(OPEN)
    unavailable = json_response(503, error_body("SERVICE_UNAVAILABLE"))

    async def scenario(h: Harness) -> None:
        # Minted at 1000 for 3 h: expiry at 11800. Refresh mints fail at 11200, 11320 and
        # 11560; the next is due at 12040, after the expiry.
        h.router.routes[DETAIL_ROUTE] = held
        await h.start()
        await h.clock.advance(10795.0)  # now 11795
        h.transport.batch(h.topic(), batch(1, 0, market_settled=[settled()]))
        await settle()
        assert held.pending() == 1
        await h.clock.advance(10.0)  # now 11805: the token expired during the read
        assert WindowCause.TOKEN_EXPIRED in h.ledger.open_windows(MARKET)[0].causes
        generation, before = h.ledger.generation(MARKET), h.ledger.instances(MARKET)
        assert held.release(detail(status, "YES" if status == "settled" else None))
        await settle()
        _after_superseded(h, status, generation, before)
        await h.stop()

    run(
        scenario,
        policy=RealtimeFeedPolicy(tick_s=1.0, mint_retry_s=120.0, max_retry_s=600.0),
        token_responses=[json_response(200, token_body()), unavailable, unavailable, unavailable],
    )


# --- AT-12, AT-36, AT-37: lifetime guards ---------------------------------------------------


def test_at12_close_during_an_attempt_cancels_it_and_changes_nothing() -> None:
    held = Held(OPEN)
    transport = SlowClose()

    async def scenario(h: Harness) -> None:
        h.router.routes[DETAIL_ROUTE] = held
        await h.start()
        h.transport.batch(h.topic(), batch(1, 0, market_settled=[settled()]))
        await settle()
        assert held.pending() == 1
        before, events = state(h), list(h.events)
        closer = asyncio.create_task(h.feed.aclose())
        await settle()
        assert not closer.done()  # transport.close() is still pending
        result = h.feed.readiness(MARKET)
        assert not h.feed.is_ready(MARKET)
        assert result.status is ReadinessStatus.NOT_READY and result.closing
        assert result.reasons[0] == "feed closing"
        assert held.cancelled == 1  # the attempt was cancelled
        held.release(detail("settled", "YES"))  # the response arrives too late
        await settle()
        assert state(h) == before and h.events == events
        transport.allow_close.set()
        await closer
        assert h.task is not None
        await h.task
        assert state(h) == before and h.events == events
        await h.rest.aclose()
        await h.tokens.aclose()

    run(scenario, policy=QUIET, transport=transport)


def test_at12_closing_reports_not_ready_even_for_a_quarantined_market() -> None:
    transport = SlowClose()

    async def scenario(h: Harness) -> None:
        await h.start()
        await send(h, StaleReason.MARKET_SETTLED)
        assert h.feed.readiness(MARKET).status is ReadinessStatus.QUARANTINED
        closer = asyncio.create_task(h.feed.aclose())
        await settle()
        assert h.feed.readiness(MARKET).status is ReadinessStatus.NOT_READY
        assert h.feed.readiness(MARKET).reasons[0] == "feed closing"
        transport.allow_close.set()
        await closer
        assert h.task is not None
        await h.task
        await h.rest.aclose()
        await h.tokens.aclose()

    run(scenario, policy=QUIET, transport=transport, router=_router(OPEN, detail("settled", "YES")))


def test_at36_response_reaching_the_step_after_the_deadline_is_dropped(
    caplog: pytest.LogCaptureFixture,
) -> None:
    async def scenario(h: Harness) -> None:
        await h.start()
        real = h.rest.get_market
        snapshots: list[tuple[Any, ...]] = []

        async def late(market_id: str) -> Any:
            market = await real(market_id)  # the response arrived in time ...
            h.clock.now += 31.0  # ... but the step runs after the 30 s deadline
            snapshots.append(state(h))  # the ledger as the step finds it
            return market

        h.rest.get_market = late  # type: ignore[method-assign]
        h.transport.batch(h.topic(), batch(1, 0, market_settled=[settled()]))
        await settle()
        assert h.count(DETAIL) == 2  # the read itself succeeded
        assert state(h) == snapshots[0]  # but the step applied nothing
        assert instance(h, StaleReason.MARKET_SETTLED).state is InstanceState.OPEN
        assert h.feed.readiness(MARKET).consecutive_failures == 1
        await h.stop()

    with caplog.at_level(logging.INFO, logger="engine.sig_client.realtime"):
        run(scenario, policy=QUIET, router=_router(OPEN, detail("settled", "YES")))
    assert "dropped: past its deadline" in caplog.text


def test_at37_completed_superseded_response_after_close_begins_is_dropped(
    caplog: pytest.LogCaptureFixture,
) -> None:
    transport = SlowClose()
    held = Held(OPEN)

    async def scenario(h: Harness) -> None:
        h.router.routes[DETAIL_ROUTE] = held
        await h.start()
        real = h.rest.get_market
        closers: list[asyncio.Task[None]] = []

        async def close_in_flight(market_id: str) -> Any:
            market = await real(market_id)  # completed while the feed was live
            closers.append(asyncio.ensure_future(h.feed.aclose()))
            await asyncio.sleep(0)  # aclose() begins before the step runs
            return market

        h.rest.get_market = close_in_flight  # type: ignore[method-assign]
        h.transport.batch(h.topic(), batch(1, 0, market_settled=[settled()]))
        await settle()
        h.transport.batch(h.topic(), batch(5, 4))  # supersede the attempt (a gap)
        await settle()
        before, events = state(h), list(h.events)
        assert held.release(detail("settled", "YES"))  # would quarantine if applied
        await settle()
        assert h.feed.closing
        assert state(h) == before and h.events == events  # closing beats restriction
        transport.allow_close.set()
        await closers[0]
        assert h.task is not None
        await h.task
        await h.rest.aclose()
        await h.tokens.aclose()

    with caplog.at_level(logging.INFO, logger="engine.sig_client.realtime"):
        run(scenario, policy=QUIET, transport=transport)
    assert "dropped: feed closing" in caplog.text


# --- AT-13, AT-14: failed reads ---------------------------------------------------------------


def _times(h: Harness, responses: list[httpx.Response]) -> list[float]:
    """Serve `responses` (the last one repeats) and record when each request was made."""
    times: list[float] = []

    def route(request: httpx.Request) -> httpx.Response:
        times.append(h.clock.now)
        return responses.pop(0) if len(responses) > 1 else responses[0]

    h.router.routes[DETAIL_ROUTE] = route
    return times


def test_at13_unavailable_until_the_deadline_fails_and_honours_retry_after() -> None:
    async def scenario(h: Harness) -> None:
        busy = json_response(503, error_body("SERVICE_UNAVAILABLE"), {"Retry-After": "10"})
        times = _times(h, [OPEN, busy])
        await h.start()
        h.transport.batch(h.topic(), batch(1, 0, market_settled=[settled()]))
        await settle()
        start = times[1]
        await h.clock.advance(20.0)
        # Reads at +0, +10, +20; a fourth would end at the 30 s read deadline: it fails.
        assert times[1:] == [start, start + 10, start + 20]
        assert instance(h, StaleReason.MARKET_SETTLED).state is InstanceState.OPEN
        assert h.feed.readiness(MARKET).consecutive_failures == 1
        await h.clock.advance(9.9)
        assert len(times) == 4  # the backoff (5 s) is floored by Retry-After (10 s)
        await h.clock.advance(0.1)
        assert len(times) == 5 and times[-1] == pytest.approx(start + 30)
        await h.stop()

    run(scenario, policy=QUIET)


def test_at13_retry_after_beyond_the_backoff_cap_is_never_shortened() -> None:
    async def scenario(h: Harness) -> None:
        limited = json_response(429, error_body("RATE_LIMITED"), {"Retry-After": "200"})
        times = _times(h, [OPEN, limited])
        await h.start()
        h.transport.batch(h.topic(), batch(1, 0, market_settled=[settled()]))
        await settle()
        start = times[1]
        await h.clock.advance(199.0)
        assert times[1:] == [start]  # not retried at the 120 s cap
        await h.clock.advance(1.0)
        assert times[1:] == [start, start + 200]
        await h.stop()

    run(scenario, policy=QUIET)


def test_at13_attempt_deadline_cancels_a_read_that_never_returns() -> None:
    held = Held(OPEN)

    async def scenario(h: Harness) -> None:
        h.router.routes[DETAIL_ROUTE] = held
        await h.start()
        h.transport.batch(h.topic(), batch(1, 0, market_settled=[settled()]))
        await settle()
        before = state(h)
        await h.clock.advance(29.9)
        assert held.pending() == 1 and held.cancelled == 0
        await h.clock.advance(0.1)
        assert held.cancelled == 1  # cancelled at the 30 s deadline
        assert state(h) == before
        assert h.feed.readiness(MARKET).consecutive_failures == 1
        await h.clock.advance(5.0)
        assert held.pending() == 1  # the next attempt, after the 5 s backoff
        await h.stop()

    run(scenario, policy=QUIET)


def test_at13_oversized_retry_after_stops_the_feed_like_a_resync() -> None:
    async def scenario(h: Harness) -> None:
        huge = json_response(
            503, error_body("SERVICE_UNAVAILABLE"), {"Retry-After": "99999999999999"}
        )
        _times(h, [OPEN, huge])
        await h.start()
        h.transport.batch(h.topic(), batch(1, 0, market_settled=[settled()]))
        await settle()
        assert h.task is not None and h.task.done()
        with pytest.raises(SigServiceUnavailableError):
            h.task.result()
        await h.rest.aclose()
        await h.tokens.aclose()

    run(scenario, policy=QUIET)


def test_at14_context_for_another_tournament_fails_the_attempt() -> None:
    other = TournamentRef(slug="beta-tournament", id="660e8400-e29b-41d4-a716-446655440000")

    async def scenario(h: Harness) -> None:
        await h.start()
        before = state(h)
        h.transport.batch(h.topic(), batch(1, 0, market_settled=[settled()]))
        await settle()
        after_trigger = state(h)
        assert after_trigger != before  # the settlement itself was recorded
        await settle()
        assert state(h) == after_trigger  # nothing from the out-of-scope read was applied
        assert h.feed.readiness(MARKET).lifecycle == before[3]
        assert h.feed.blockers(MARKET) == {StaleReason.MARKET_SETTLED}  # no quarantine
        assert h.feed.readiness(MARKET).consecutive_failures == 1
        assert h.task is not None and not h.task.done()  # the feed keeps running
        await h.stop()

    run(scenario, policy=QUIET, router=_router(OPEN, detail("settled", "YES", tournament=other)))


# --- AT-15, AT-16: the (test) bootstrap's first subscription ---------------------------------


@pytest.mark.parametrize("status", ["closed", "settled"])
def test_at15_bootstrap_lifecycle_read_not_open_quarantines(status: str) -> None:
    async def scenario(h: Harness) -> None:
        await h.start()
        await h.clock.advance(60.0)
        assert h.feed.blockers(MARKET) == {StaleReason.LIFECYCLE_NOT_OPEN}
        assert h.feed.readiness(MARKET).status is ReadinessStatus.QUARANTINED
        assert h.feed.readiness(MARKET).history.initial_evidence is None
        assert readies(h) == 0 and not h.feed.is_ready(MARKET)
        await h.stop()

    run(scenario, policy=QUIET, router=_router(detail(status, "YES")))


def test_at16_bootstrap_not_ready_until_a_lifecycle_read_succeeds() -> None:
    async def scenario(h: Harness) -> None:
        busy = json_response(503, error_body("SERVICE_UNAVAILABLE"), {"Retry-After": "10"})
        responses = [busy, busy, busy, OPEN]
        _times(h, responses)
        await h.start()
        assert h.count(ORDERBOOK) == 1  # the books were synced ...
        assert not h.feed.is_ready(MARKET)  # ... but there is no initial-state evidence
        assert "initial-state evidence pending" in reasons_text(h)
        await h.clock.advance(20.0)  # the first attempt fails at its read deadline
        assert not h.feed.is_ready(MARKET) and readies(h) == 0
        await h.clock.advance(10.0)  # the second attempt succeeds
        await h.clock.advance(2.0)  # and the publication resync runs
        assert h.feed.is_ready(MARKET) and readies(h) == 1
        assert h.count(ORDERBOOK) == 2
        await h.stop()

    run(scenario, policy=QUIET)


# --- AT-17, AT-18: book expiry is unchanged by recovery --------------------------------------


def iso(delta_s: float) -> str:
    return (T0 + timedelta(seconds=delta_s)).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


def rest_book(sequence: int | None, read_at: float = 0.0) -> httpx.Response:
    body = copy.deepcopy(fixture("market_orderbook"))
    exchange = body["contexts"][0]["orderbook"]["exchanges"][0]
    exchange["asOf"] = None if sequence is None else {"sequence": sequence, "at": iso(read_at)}
    return json_response(200, body)


@pytest.mark.parametrize("bound", [None, 1.0])
def test_at17_at18_expiry_crossed_while_a_recovery_read_is_in_flight(bound: float | None) -> None:
    held = Held(OPEN)
    books = {"next": rest_book(None)}

    async def scenario(h: Harness) -> None:
        h.router.routes[DETAIL_ROUTE] = held
        h.router.routes[f"/api/v1{ORDERBOOK}"] = lambda request: books["next"]
        await h.start()
        assert h.feed.is_ready(MARKET)
        # Revision 1 pushes a book (version 9) with an order expiring at T0 + 10.
        h.transport.batch(h.topic(), batch(1, 0, books=[book(9, next_expiry=iso(10.0))]))
        await settle()
        h.transport.subscribed(h.topic())  # a re-join: a window and a lifecycle read
        await settle()
        assert held.pending() == 1
        generation = h.ledger.generation(MARKET)
        await h.clock.advance(11.0)  # the expiry passes while the read is in flight
        assert h.ledger.generation(MARKET) == generation  # expiry raises no generation
        assert held.release(OPEN)
        await settle()
        lifecycle = h.feed.readiness(MARKET).lifecycle
        assert lifecycle is not None and lifecycle.current  # it passed the step
        h.transport.batch(h.topic(), batch(2, 1))  # a clean batch confirms the window
        await h.clock.advance(4.0)
        if bound is None:
            # Unversioned reads cannot cover the expiry without a skew bound.
            assert not h.feed.is_ready(MARKET)
            assert "order expiry has passed" in reasons_text(h)
            books["next"] = rest_book(10, read_at=12.0)  # versioned, read after it
            await book_trigger(h)
            await h.clock.advance(2.0)
        # AT-18: with a bound, an unversioned read sent >= 1 s after the expiry covers it.
        assert h.feed.is_ready(MARKET)
        assert not any(b.expired for b in h.feed.readiness(MARKET).books)
        await h.stop()

    run(scenario, policy=RealtimeFeedPolicy(tick_s=1.0, expiry_skew_margin_s=bound))


# --- AT-19: the readiness result explains every state ---------------------------------------


def test_at19_readiness_result_content_in_every_state() -> None:
    async def scenario(h: Harness) -> None:
        await h.start()
        ready = h.feed.readiness(MARKET)
        assert (ready.status, ready.reasons, ready.closing) == (ReadinessStatus.READY, (), False)
        assert ready.does_not_cover == DOES_NOT_COVER
        assert ready.continuity.note == COVERAGE_NOTE
        assert ready.transport.subscribed and ready.transport.token_valid
        assert [b.exchange_id for b in ready.books] == ["36"]
        assert ready.lifecycle is not None and "unversioned" in ready.lifecycle.note
        assert ready.history.origin is LedgerOrigin.BOOTSTRAP and ready.history.bootstrap_declared
        assert ready.evaluated_utc.tzinfo is not None
        # NOT_READY: a gap, with its revisions and missing guarantees.
        h.transport.batch(h.topic(), batch(1, 0))
        h.transport.batch(h.topic(), batch(4, 3))
        await settle()
        gap = h.feed.readiness(MARKET)
        assert gap.status is ReadinessStatus.NOT_READY
        assert any("revision gap" in r and "revisions 2-4" in r and "Q8" in r for r in gap.reasons)
        assert gap.blockers[0].missing == ("Q1", "Q3", "Q4", "Q6", "Q7", "Q8")
        assert gap.generation == h.ledger.generation(MARKET)
        # An unobserved window, with its fence and decision id.
        h.transport.subscribed(h.topic())
        await settle()
        window = h.feed.readiness(MARKET)
        assert any("continuity" in r and "fence revision 4" in r and "D-1" in r
                   for r in window.reasons)
        assert window.continuity.windows[0].state is WindowState.OPEN
        await h.stop()
        closed = h.feed.readiness(MARKET)
        assert closed.closing and closed.reasons[0] == "feed closing"

    run(scenario, policy=QUIET)


def test_at19_quarantined_and_unknown_history_reasons() -> None:
    async def main() -> None:
        h = Harness(policy=QUIET, router=_router(OPEN, detail("settled", "YES")))
        await h.start()
        await send(h, StaleReason.MARKET_SETTLED)
        quarantined = h.feed.readiness(MARKET)
        assert quarantined.status is ReadinessStatus.QUARANTINED
        assert any("quarantined" in r and "Q9" in r for r in quarantined.reasons)
        await h.stop()
        u = Harness(policy=QUIET, ledger=RecoveryLedger())
        await u.start()
        unknown = u.feed.readiness(MARKET)
        assert any(r.startswith("history unknown") and "D-3" in r for r in unknown.reasons)
        assert unknown.history.history_unknown_open
        await u.stop()

    asyncio.run(main())


# --- AT-25: a null-sequence trade is never cleared --------------------------------------------


def test_at25_null_sequence_trade_is_never_cleared_and_no_tape_is_read() -> None:
    async def scenario(h: Harness) -> None:
        await h.start()
        await send(h, StaleReason.NULL_TRADE_SEQUENCE)
        for _ in range(3):
            await book_trigger(h)
        h.transport.subscribed(h.topic())
        await h.clock.advance(600.0)
        assert StaleReason.NULL_TRADE_SEQUENCE in h.feed.blockers(MARKET)
        assert instance(h, StaleReason.NULL_TRADE_SEQUENCE).state is InstanceState.OPEN
        assert h.feed.readiness(MARKET).lifecycle.status == "open"  # shown open: no matter
        # The tape and price routes exist, but M2-A never spends budget on them.
        assert h.count("/exchanges/36/trades") == 0
        assert h.count("/exchanges/prices") == 0
        assert not h.feed.is_ready(MARKET)
        await h.stop()

    run(scenario, policy=QUIET)


# --- AT-27: the recovery gate ---------------------------------------------------------------


MANY = tuple(str(26 + i) for i in range(12))


def _many_routes(h: Harness) -> None:
    for market_id in MANY:
        body = copy.deepcopy(fixture("market_orderbook"))
        h.router.routes[f"/api/v1/markets/{market_id}/orderbook"] = json_response(200, body)


def _counts(h: Harness) -> tuple[int, int]:
    details = sum(h.count(f"/markets/{m}") for m in MANY)
    books = sum(h.count(f"/markets/{m}/orderbook") for m in MANY)
    return details, books


def test_at27_attempt_starts_wait_for_the_recovery_gate() -> None:
    async def main() -> None:
        h = Harness(policy=QUIET, market_ids=MANY, ledger=RecoveryLedger())
        _many_routes(h)
        try:
            await h.start()
            await settle(200)  # twelve markets take more loop turns than one
            assert _counts(h) == (10, 12)  # 10 attempt starts; every resync ran
            # Every read, recovery included, went through the shared account limiter.
            assert h.limiter.used(RequestKind.READ) == 1 + 10 + 12
            await h.clock.advance(59.9)
            assert _counts(h)[0] == 10
            await h.clock.advance(0.2)  # the rolling window frees (plus the limiter's pad)
            assert _counts(h)[0] == 20
            await h.clock.advance(60.0)
            assert _counts(h)[0] == 22  # 12 initial reads, then 10 after the re-joins
            await h.clock.advance(600.0)
            assert _counts(h)[0] == 22
        finally:
            await h.stop()

    asyncio.run(main())


def test_at27_resync_and_shared_limiter_behaviour_unchanged_by_the_gate() -> None:
    async def books_with(recovery: RecoveryPolicy) -> int:
        h = Harness(policy=QUIET, market_ids=MANY, ledger=RecoveryLedger(), recovery=recovery)
        _many_routes(h)
        try:
            await h.start()
            await settle(200)
            return _counts(h)[1]
        finally:
            await h.stop()

    async def main() -> None:
        narrow = await books_with(RecoveryPolicy(attempts_per_window=1))
        wide = await books_with(RecoveryPolicy(attempts_per_window=1000))
        assert narrow == wide == len(MANY)

    asyncio.run(main())


# --- AT-28: escalation --------------------------------------------------------------------------


def test_at28_escalation_after_five_failures_keeps_retrying_at_the_cap() -> None:
    async def scenario(h: Harness) -> None:
        busy = json_response(503, error_body("SERVICE_UNAVAILABLE"))
        times = _times(h, [OPEN, busy])
        await h.start()
        h.transport.batch(h.topic(), batch(1, 0, market_settled=[settled()]))
        await settle()
        await h.clock.advance(4.9)  # the first attempt: 4 reads over 1.75 s, then it fails
        result = h.feed.readiness(MARKET)
        assert result.consecutive_failures == 1 and not result.escalated
        await h.clock.advance(1000.0)
        starts = times[1::4]  # each attempt makes 4 HTTP attempts (ReadRetryPolicy)
        gaps = [round(b - a, 2) for a, b in itertools.pairwise(starts)]
        assert gaps[:7] == [6.75, 11.75, 21.75, 41.75, 81.75, 121.75, 121.75]
        result = h.feed.readiness(MARKET)
        assert result.escalated and result.consecutive_failures >= 5
        assert result.status is ReadinessStatus.NOT_READY
        assert any("escalated" in r for r in result.reasons)
        assert instance(h, StaleReason.MARKET_SETTLED).state is InstanceState.OPEN
        await h.stop()

    run(scenario, policy=QUIET)


# --- AT-30 to AT-35: revision continuity across an unobserved window ------------------------


async def _fence_40(h: Harness) -> None:
    await h.start()
    h.transport.batch(h.topic(), batch(40, 39))
    await settle()
    assert h.feed.is_ready(MARKET)
    h.transport.push(SocketEvent(SocketState.CLOSED))
    await settle()
    h.transport.subscribed(h.topic())
    await h.clock.advance(2.0)
    (window,) = h.ledger.open_windows(MARKET)
    assert window.fence == 40 and window.state is WindowState.OPEN
    assert window.causes == (WindowCause.SOCKET_CLOSED, WindowCause.REJOIN)


def test_at30_retried_old_batch_after_reconnect_is_a_duplicate() -> None:
    async def scenario(h: Harness) -> None:
        await _fence_40(h)
        h.transport.batch(h.topic(), batch(40, 39))
        await h.clock.advance(2.0)
        assert h.ledger.open_windows(MARKET)[0].state is WindowState.OPEN
        assert not h.feed.is_ready(MARKET)
        assert h.feed.last_revision(MARKET) == 40
        await h.stop()

    run(scenario, policy=QUIET)


def test_at31_clean_batch_past_the_fence_confirms_the_window() -> None:
    async def scenario(h: Harness) -> None:
        await _fence_40(h)
        h.transport.batch(h.topic(), batch(40, 39))  # the duplicate first (AT-30)
        h.transport.batch(h.topic(), batch(41, 40))
        await settle()
        assert h.ledger.open_windows(MARKET) == ()
        (window,) = h.feed.readiness(MARKET).continuity.windows
        assert (window.state, window.confirmed_through) == (WindowState.CONFIRMED, 41)
        assert h.feed.readiness(MARKET).continuity.note == COVERAGE_NOTE
        await h.clock.advance(2.0)  # the publication resync
        assert h.feed.is_ready(MARKET)
        await h.stop()

    run(scenario, policy=QUIET)


def test_at32_gap_past_the_fence_opens_a_gap_and_leaves_the_window_open() -> None:
    async def scenario(h: Harness) -> None:
        await _fence_40(h)
        h.transport.batch(h.topic(), batch(43, 42))
        await settle()
        assert StaleReason.REVISION_GAP in h.feed.blockers(MARKET)
        (window,) = h.ledger.open_windows(MARKET)
        assert window.unconfirmable is not None
        h.transport.batch(h.topic(), batch(44, 43))  # later clean batches cannot confirm it
        await h.clock.advance(2.0)
        assert h.ledger.open_windows(MARKET) == (window,)
        assert not h.feed.is_ready(MARKET)
        await h.stop()

    run(scenario, policy=QUIET)


@pytest.mark.parametrize(
    ("payload", "reason"),
    [
        ({"resyncRequired": True, "delivery": {"revision": 41, "previousRevision": 40}},
         StaleReason.RESYNC_REQUIRED),
        ({"delivery": {"revision": 41, "previousRevision": 40}, "trades": "x"},
         StaleReason.MALFORMED),
        (batch(41, 40, book_dirty=[dirty()]), StaleReason.MISSING_BOOK),
    ],
)
def test_at33_unclean_advance_past_the_fence_never_confirms(payload: Any, reason: Any) -> None:
    async def scenario(h: Harness) -> None:
        await _fence_40(h)
        h.transport.batch(h.topic(), payload)
        await settle()
        assert h.feed.last_revision(MARKET) == 41  # the revision advanced ...
        (window,) = h.ledger.open_windows(MARKET)  # ... the window stays open
        assert window.unconfirmable is not None and reason.value in window.unconfirmable
        assert reason in h.feed.blockers(MARKET)  # and the batch's own blocker opens
        h.transport.batch(h.topic(), batch(42, 41))
        await settle()
        assert h.ledger.open_windows(MARKET) == (window,)
        await h.stop()

    run(scenario, policy=QUIET)


def test_at34_reconnect_before_any_batch_has_no_fence_and_never_confirms() -> None:
    async def scenario(h: Harness) -> None:
        await h.start()
        h.transport.push(ChannelStatusEvent(h.topic(), ChannelStatus.CHANNEL_ERROR))
        h.transport.subscribed(h.topic())
        await settle()
        (window,) = h.ledger.open_windows(MARKET)
        assert window.fence is None and window.unconfirmable is not None
        for revision in range(1, 4):
            h.transport.batch(h.topic(), batch(revision, revision - 1))
            await settle()
        await h.clock.advance(2.0)
        assert h.ledger.open_windows(MARKET) == (window,)
        assert not h.feed.is_ready(MARKET)
        await h.stop()

    run(scenario, policy=QUIET)


def test_at35_confirmation_never_claims_a_dropped_standalone_settlement() -> None:
    async def scenario(h: Harness) -> None:
        await _fence_40(h)
        # An admin settlement is sent as a standalone market_settled during the window,
        # and the transport drops it: it is never delivered to the feed.
        h.transport.batch(h.topic(), batch(41, 40))
        await h.clock.advance(2.0)
        result = h.feed.readiness(MARKET)
        assert result.status is ReadinessStatus.READY  # D-1 strict: the window confirmed
        assert StaleReason.MARKET_SETTLED not in h.feed.blockers(MARKET)
        assert "standalone market_settled" in result.continuity.note
        assert "(Q10)" in result.continuity.note
        assert "never authorizes trading" in result.does_not_cover
        await h.stop()

    run(scenario, policy=QUIET)


# --- AT-38: a current confirmation is not rejected by its own change ------------------------


def test_at38_confirmation_and_quarantine_raise_the_generation_exactly_once() -> None:
    held = Held(OPEN)

    async def scenario(h: Harness) -> None:
        h.router.routes[DETAIL_ROUTE] = held
        await h.start()
        h.transport.batch(h.topic(), batch(1, 0, market_settled=[settled()]))
        await settle()
        generation = h.ledger.generation(MARKET)
        assert held.release(detail("settled", "YES"))
        await settle()
        assert h.ledger.generation(MARKET) == generation + 1
        settlement = instance(h, StaleReason.MARKET_SETTLED)
        not_open = instance(h, StaleReason.LIFECYCLE_NOT_OPEN)
        assert settlement.state is not_open.state is InstanceState.QUARANTINED
        observation = settlement.evidence[-1].observation
        assert observation.current and observation.generation == generation
        assert not_open.evidence[-1].observation == observation  # one step, one read
        assert held.pending() == 0  # quarantined: no further attempt
        await h.stop()

    run(scenario, policy=QUIET)


# --- closing and the event order ------------------------------------------------------------


def test_withdrawal_precedes_every_callback_and_is_ready_matches_readiness() -> None:
    async def scenario(h: Harness) -> None:
        await h.start()
        mark = len(h.observed)
        h.transport.batch(
            h.topic(), batch(1, 0, trades=[trade("t1", 11)], books=[book(11)],
                             market_settled=[settled()])
        )
        await settle()
        kinds = h.observed[mark:]
        assert kinds[0] == ("MarketStale", False)
        assert all(not ready for _, ready in kinds)  # every callback saw it not ready
        assert_consistent(h)
        await h.stop()

    run(scenario, policy=QUIET)


def test_no_ledger_change_after_closing_begins() -> None:
    """White-box: the receive loop has stopped, so messages are handed to _handle directly."""
    transport = SlowClose()

    async def scenario(h: Harness) -> None:
        await h.start()
        h.transport.batch(h.topic(), batch(1, 0))
        await settle()
        before = state(h)
        closer = asyncio.create_task(h.feed.aclose())
        await settle()
        # Messages that would raise blockers or open windows, delivered while closing.
        h.feed._handle(BroadcastEvent(h.topic(), "market_batch", '{"delivery": "x"}'))
        h.feed._handle(SocketEvent(SocketState.ERROR))
        assert state(h) == before
        assert not any(isinstance(e, MarketStale) and e.reason in BLOCKING_REASONS
                       for e in h.events)
        transport.allow_close.set()
        await closer
        assert h.task is not None
        await h.task
        assert state(h) == before
        await h.rest.aclose()
        await h.tokens.aclose()

    run(scenario, policy=QUIET, transport=transport)
