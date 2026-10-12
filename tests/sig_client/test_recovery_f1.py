"""M2-A-F1: repairs for four independent-review findings, offline.

1. The recovery ledger is bound to one tournament; a mismatching feed is refused before
   it changes ownership or records.
2. Readiness and publication use one predicate, and escalation that outlived its
   recovery demand is dropped (scheduling metadata only), so it cannot strand a market.
3. An attempt's overall deadline covers recovery-gate admission; a hanging or late
   admission never starts a read.
4. An external cancellation reaching the reader is propagated even when the reader
   swallows it and returns a response; nothing is committed.

A few tests replace one collaborator of the feed (its recovery gate, or the REST
client's `get_market`) to create a hanging, late or cancellation-swallowing reader
deterministically; they say so where they do.
"""

from __future__ import annotations

import asyncio
import copy
import json
from decimal import Decimal
from typing import Any

import httpx
import pytest
from realtime_kit import (
    MARKET,
    FakeTransport,
    Harness,
    batch,
    market_detail,
    settle,
    settled,
)
from realtime_kit import test_bootstrap as bootstrap_ledger
from sig_testkit import error_body, fixture, json_response

from engine.config import TournamentRef
from engine.sig_client.errors import SigAuthError
from engine.sig_client.readiness import ReadinessStatus
from engine.sig_client.realtime import (
    MarketReady,
    RealtimeFeedPolicy,
    RealtimeMarketFeed,
    SocketEvent,
    SocketState,
)
from engine.sig_client.realtime_topic import StaleReason
from engine.sig_client.recovery import (
    InstanceState,
    RecoveryLedger,
    RecoveryPolicy,
)
from engine.sig_client.wire import Market

QUIET = RealtimeFeedPolicy(tick_s=1e6)
DETAIL = f"/markets/{MARKET}"
DETAIL_ROUTE = f"/api/v1{DETAIL}"
ORDERBOOK_ROUTE = f"/api/v1/markets/{MARKET}/orderbook"
OTHER = TournamentRef(slug="beta-tournament", id="660e8400-e29b-41d4-a716-446655440000")
BUSY = json_response(503, error_body("SERVICE_UNAVAILABLE"))


def detail(status: str = "open", settled_with: str | None = None) -> httpx.Response:
    settled_on = "2026-10-10T12:00:01.000Z" if status == "settled" else None
    body = market_detail(MARKET, status=status, settled_with=settled_with, settled_on=settled_on)
    return json_response(200, body)


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


def ledger_state(ledger: RecoveryLedger, market_id: str = MARKET) -> tuple[Any, ...]:
    return (
        ledger.instances(market_id),
        ledger.generation(market_id),
        ledger.windows(market_id),
        ledger.lifecycle(market_id),
        ledger.last_revision(market_id),
        ledger.initial_evidence(market_id),
    )


def times_route(h: Harness, responses: list[httpx.Response]) -> list[float]:
    times: list[float] = []

    def route(request: httpx.Request) -> httpx.Response:
        times.append(h.clock.now)
        return responses.pop(0) if len(responses) > 1 else responses[0]

    h.router.routes[DETAIL_ROUTE] = route
    return times


def deadline_timers() -> list[asyncio.Task[Any]]:
    return [
        t for t in asyncio.all_tasks()
        if not t.done() and getattr(t.get_coro(), "__qualname__", "").endswith("._deadline")
    ]


# --- 1. the ledger is bound to one tournament -----------------------------------------------


def test_f1_ledger_refuses_a_feed_for_another_tournament() -> None:
    async def main() -> None:
        first = Harness(policy=QUIET)
        ledger = first.ledger
        await first.start()
        first.transport.batch(first.topic(), batch(5, 4))
        await settle()
        await first.stop()
        before = ledger_state(ledger)
        markets = ledger.market_ids()
        other = Harness(policy=QUIET, tournament=OTHER)
        try:
            with pytest.raises(ValueError, match="tournament"):
                RealtimeMarketFeed(
                    other.rest, other.tokens, FakeTransport(), [MARKET, "27"], ledger=ledger
                )
            # Nothing was registered, opened or carried across.
            assert ledger_state(ledger) == before
            assert ledger.market_ids() == markets
            assert ledger.tournament == first.rest.tournament
        finally:
            await other.feed.aclose()
            await other.rest.aclose()
            await other.tokens.aclose()

    asyncio.run(main())


def test_f1_mismatching_feed_is_refused_before_ownership_changes() -> None:
    async def main() -> None:
        first = Harness(policy=QUIET)
        ledger = first.ledger
        await first.start()
        other = Harness(policy=QUIET, tournament=OTHER)
        try:
            with pytest.raises(ValueError, match="tournament"):
                RealtimeMarketFeed(other.rest, other.tokens, FakeTransport(), [MARKET], ledger=ledger)
            # The first feed still owns the ledger: a same-tournament feed is still refused
            # while it is live, as before.
            with pytest.raises(RuntimeError, match="not closing"):
                RealtimeMarketFeed(first.rest, first.tokens, FakeTransport(), [MARKET], ledger=ledger)
            assert first.feed.is_ready(MARKET)
        finally:
            await other.feed.aclose()
            await other.rest.aclose()
            await other.tokens.aclose()
            await first.stop()

    asyncio.run(main())


def test_f1_control_same_tournament_replacement_inherits_the_ledger() -> None:
    async def main() -> None:
        first = Harness(policy=QUIET)
        ledger = first.ledger
        await first.start()
        first.transport.batch(first.topic(), batch(5, 4))
        first.transport.batch(first.topic(), batch(8, 7))  # a gap
        await settle()
        await first.stop()
        h = Harness(policy=QUIET, ledger=ledger, clock=first.clock)
        try:
            assert h.feed.blockers(MARKET) == {StaleReason.REVISION_GAP}
            assert h.feed.last_revision(MARKET) == 8
            assert ledger.open_windows(MARKET)[0].fence == 8
        finally:
            await h.feed.aclose()
            await h.rest.aclose()
            await h.tokens.aclose()

    asyncio.run(main())


def test_f1_unit_attach_binds_on_first_use_and_checks_every_later_one() -> None:
    class Owner:
        closing = True

    from realtime_kit import TOURNAMENT

    ledger = bootstrap_ledger()
    assert ledger.tournament is None
    ledger.attach(Owner(), TOURNAMENT, [MARKET], 0.0)
    assert ledger.tournament == TOURNAMENT
    before = ledger_state(ledger)
    with pytest.raises(ValueError, match="tournament"):
        ledger.attach(Owner(), OTHER, [MARKET, "27"], 1.0)
    assert ledger_state(ledger) == before and ledger.market_ids() == {MARKET}
    ledger.attach(Owner(), TournamentRef(TOURNAMENT.slug, TOURNAMENT.id), [MARKET], 2.0)


# --- 2. readiness and publication agree; escalation cannot strand a market -------------------


async def _escalate_on_a_window(h: Harness) -> list[float]:
    """Bootstrap ready at revision 40; a reconnect opens a window (fence 40); every
    later lifecycle read fails, so the window's recovery escalates."""
    times = times_route(h, [detail(), BUSY])
    await h.start()
    h.transport.batch(h.topic(), batch(40, 39))
    await settle()
    assert h.feed.is_ready(MARKET)
    h.transport.push(SocketEvent(SocketState.CLOSED))
    await settle()
    h.transport.subscribed(h.topic())
    await settle()
    await h.clock.advance(90.0)  # five failed attempts: 1.75 s each, backoff 5..40 s
    result = h.feed.readiness(MARKET)
    assert result.escalated and result.consecutive_failures == 5
    assert result.status is ReadinessStatus.NOT_READY
    return times


def test_f1_escalation_does_not_outlive_its_recovery_demand() -> None:
    async def scenario(h: Harness) -> None:
        times = await _escalate_on_a_window(h)
        reads = len(times)
        h.transport.batch(h.topic(), batch(41, 40))  # a clean batch confirms the window
        await settle()
        await h.clock.advance(2.0)  # the publication resync
        # Every MarketReady was published while readiness agreed.
        assert all(ready for kind, ready in h.observed if kind == "MarketReady")
        result = h.feed.readiness(MARKET)
        assert result.status is ReadinessStatus.READY and result.reasons == ()
        assert not result.escalated and result.consecutive_failures == 0
        await h.clock.advance(600.0)
        assert h.feed.is_ready(MARKET)
        assert len(times) == reads  # no demand left: no further lifecycle reads
        assert sum(isinstance(e, MarketReady) for e in h.events) == 2
        await h.stop()

    run(scenario, policy=QUIET)


def test_f1_control_escalation_stays_while_demand_remains_and_never_clears_blockers() -> None:
    async def scenario(h: Harness) -> None:
        times = await _escalate_on_a_window(h)
        # A settlement arrives in the confirming batch: the window is confirmed but an
        # open MARKET_SETTLED instance still wants recovery.
        h.transport.batch(h.topic(), batch(41, 40, market_settled=[settled()]))
        await settle()
        await h.clock.advance(2.0)
        result = h.feed.readiness(MARKET)
        assert result.escalated and result.status is ReadinessStatus.NOT_READY
        assert all(ready for kind, ready in h.observed if kind == "MarketReady")
        reads = len(times)
        await h.clock.advance(130.0)
        assert len(times) > reads  # still retried at the capped interval
        assert StaleReason.MARKET_SETTLED in h.feed.blockers(MARKET)
        assert not h.feed.is_ready(MARKET)
        await h.stop()

    run(scenario, policy=QUIET)


def test_f1_published_ready_always_matches_readiness() -> None:
    """Across reconnects, escalation and recovery: no MarketReady while not READY."""

    async def scenario(h: Harness) -> None:
        await _escalate_on_a_window(h)
        for revision in range(41, 45):
            h.transport.batch(h.topic(), batch(revision, revision - 1))
            await h.clock.advance(2.0)
            h.transport.push(SocketEvent(SocketState.ERROR))
            await settle()
            h.transport.subscribed(h.topic())
            await h.clock.advance(3.0)
        assert all(ready for kind, ready in h.observed if kind == "MarketReady")
        assert h.feed.is_ready(MARKET) is (
            h.feed.readiness(MARKET).status is ReadinessStatus.READY
        )
        await h.stop()

    run(scenario, policy=QUIET)


# --- 3. the overall deadline covers recovery-gate admission ----------------------------------


MANY = tuple(str(26 + i) for i in range(12))


def test_f1_gate_saturation_keeps_ten_starts_per_minute_within_the_deadline() -> None:
    async def main() -> None:
        h = Harness(policy=QUIET, market_ids=MANY, ledger=RecoveryLedger())
        starts: list[float] = []
        for market_id in MANY:
            body = copy.deepcopy(fixture("market_orderbook"))
            h.router.routes[f"/api/v1/markets/{market_id}/orderbook"] = json_response(200, body)

            def route(request: httpx.Request, market_id: str = market_id) -> httpx.Response:
                starts.append(h.clock.now)
                return json_response(200, market_detail(market_id))

            h.router.routes[f"/api/v1/markets/{market_id}"] = route
        try:
            await h.start()
            await settle(200)
            assert len(starts) == 10
            await h.clock.advance(600.0)
            # Never more than 10 admitted starts in any rolling minute ...
            assert all(
                sum(1 for t in starts if s <= t < s + 60.0) <= 10 for s in starts
            )
            # ... and every market was read at its current generation eventually.
            for market_id in MANY:
                lifecycle = h.feed.readiness(market_id).lifecycle
                assert lifecycle is not None and lifecycle.current
        finally:
            await h.stop()

    asyncio.run(main())


class HangingGate:
    """A recovery gate whose admission never completes (its sleeper hangs)."""

    def __init__(self) -> None:
        self.waiting = 0
        self.cancelled = 0

    async def acquire(self, kind: Any, *, deadline: float | None = None) -> None:
        self.waiting += 1
        try:
            await asyncio.get_running_loop().create_future()
        except asyncio.CancelledError:
            self.cancelled += 1
            raise


def test_f1_hanging_admission_is_cut_off_at_the_attempt_deadline() -> None:
    async def scenario(h: Harness) -> None:
        await h.start()
        gate = HangingGate()
        h.feed._recovery_gate = gate  # white-box: the admission sleeper hangs
        h.transport.batch(h.topic(), batch(1, 0, market_settled=[settled()]))
        await settle()
        before = ledger_state(h.ledger)
        assert gate.waiting == 1
        await h.clock.advance(29.9)
        assert gate.cancelled == 0
        await h.clock.advance(0.1)
        assert gate.cancelled == 1  # cut off by the 30 s overall deadline
        assert h.feed.readiness(MARKET).consecutive_failures == 1
        assert h.count(DETAIL) == 1  # only the bootstrap read: none started
        assert ledger_state(h.ledger) == before
        assert deadline_timers() == []  # cleaned up
        await h.stop()

    run(scenario, policy=QUIET)


class LateGate:
    """A recovery gate that admits only after the attempt's deadline has passed."""

    def __init__(self, h: Harness) -> None:
        self.h = h
        self.admitted = 0

    async def acquire(self, kind: Any, *, deadline: float | None = None) -> None:
        await asyncio.sleep(0)
        self.h.clock.now = (deadline if deadline is not None else self.h.clock.now) + 1.0
        self.admitted += 1


def test_f1_late_admission_never_starts_a_read() -> None:
    async def scenario(h: Harness) -> None:
        await h.start()
        gate = LateGate(h)
        h.feed._recovery_gate = gate  # white-box: admission returns after the deadline
        h.transport.batch(h.topic(), batch(1, 0, market_settled=[settled()]))
        await settle()
        assert gate.admitted >= 1
        assert h.count(DETAIL) == 1  # only the bootstrap read
        assert h.feed.readiness(MARKET).consecutive_failures >= 1
        assert instance_state(h, StaleReason.MARKET_SETTLED) is InstanceState.OPEN
        await h.stop()

    run(scenario, policy=QUIET)


def instance_state(h: Harness, reason: StaleReason) -> InstanceState:
    (found,) = [i for i in h.ledger.instances(MARKET) if i.reason is reason]
    return found.state


def test_f1_close_during_admission_cancels_and_cleans_up() -> None:
    async def scenario(h: Harness) -> None:
        await h.start()
        gate = HangingGate()
        h.feed._recovery_gate = gate
        h.transport.batch(h.topic(), batch(1, 0, market_settled=[settled()]))
        await settle()
        before = ledger_state(h.ledger)
        assert gate.waiting == 1
        await h.stop()
        assert gate.cancelled == 1
        assert ledger_state(h.ledger) == before
        assert h.feed.readiness(MARKET).consecutive_failures == 0  # not a failure
        assert deadline_timers() == []

    run(scenario, policy=QUIET)


def test_f1_control_admission_wait_within_the_deadline_is_admitted() -> None:
    async def scenario(h: Harness) -> None:
        await h.start()
        times = times_route(h, [detail()])
        # The bootstrap read used the only start of this 20 s window.
        h.transport.batch(h.topic(), batch(1, 0, market_settled=[settled()]))
        await settle()
        assert times == []
        await h.clock.advance(20.1)
        assert len(times) == 1  # admitted after ~20 s, inside the 30 s deadline
        assert h.feed.readiness(MARKET).consecutive_failures == 0
        await h.stop()

    run(scenario, policy=QUIET, recovery=RecoveryPolicy(attempts_per_window=1, attempt_window_s=20.0))


# --- 4. an external cancellation is never committed ------------------------------------------


def settled_market() -> Market:
    body = market_detail(MARKET, status="settled", settled_with="YES",
                         settled_on="2026-10-10T12:00:01.000Z")
    return Market.model_validate(json.loads(json.dumps(body), parse_float=Decimal))


def test_f1_swallowed_external_cancellation_commits_nothing() -> None:
    async def scenario(h: Harness) -> None:
        await h.start()
        swallowed: list[bool] = []

        async def swallowing_reader(market_id: str) -> Market:
            try:
                await asyncio.get_running_loop().create_future()  # never answered
            except asyncio.CancelledError:
                swallowed.append(True)
            return settled_market()  # ... and returns a response anyway

        h.rest.get_market = swallowing_reader  # type: ignore[method-assign]
        # The next orderbook read fails permanently, which stops the feed and cancels
        # every task in it, the attempt included (an external cancellation).
        h.router.routes[ORDERBOOK_ROUTE] = json_response(401, error_body("INVALID_API_KEY"))
        h.transport.batch(h.topic(), batch(1, 0, market_settled=[settled()]))
        await settle()
        assert swallowed == []  # the attempt is reading
        await h.clock.advance(2.0)  # the resync runs (min_resync_interval_s) and fails
        assert h.task is not None and h.task.done()
        with pytest.raises(SigAuthError):
            h.task.result()
        assert swallowed == [True]
        assert not h.feed.closing  # not a closing: only the cancellation can stop it
        assert instance_state(h, StaleReason.MARKET_SETTLED) is InstanceState.OPEN
        assert StaleReason.LIFECYCLE_NOT_OPEN not in h.feed.blockers(MARKET)
        lifecycle = h.ledger.lifecycle(MARKET)
        assert lifecycle is not None and lifecycle.status == "open"  # the bootstrap read
        await h.rest.aclose()
        await h.tokens.aclose()

    run(scenario, policy=QUIET)


def test_f1_control_the_same_reader_commits_without_a_cancellation() -> None:
    async def scenario(h: Harness) -> None:
        await h.start()

        async def reader(market_id: str) -> Market:
            await asyncio.sleep(0)
            return settled_market()

        h.rest.get_market = reader  # type: ignore[method-assign]
        h.transport.batch(h.topic(), batch(1, 0, market_settled=[settled()]))
        await settle()
        assert instance_state(h, StaleReason.MARKET_SETTLED) is InstanceState.QUARANTINED
        assert instance_state(h, StaleReason.LIFECYCLE_NOT_OPEN) is InstanceState.QUARANTINED
        await h.stop()

    run(scenario, policy=QUIET)


def test_f1_control_superseded_live_response_still_restricts() -> None:
    async def scenario(h: Harness) -> None:
        await h.start()
        answer: asyncio.Future[Market] = asyncio.get_running_loop().create_future()

        async def reader(market_id: str) -> Market:
            return await answer

        h.rest.get_market = reader  # type: ignore[method-assign]
        h.transport.batch(h.topic(), batch(1, 0, market_settled=[settled()]))
        await settle()
        h.transport.batch(h.topic(), batch(5, 4))  # a gap supersedes the attempt
        await settle()
        answer.set_result(settled_market())
        await settle()
        assert instance_state(h, StaleReason.MARKET_SETTLED) is InstanceState.OPEN
        assert instance_state(h, StaleReason.LIFECYCLE_NOT_OPEN) is InstanceState.QUARANTINED
        await h.stop()

    run(scenario, policy=QUIET)


def test_f1_control_deadline_still_fails_the_attempt() -> None:
    async def scenario(h: Harness) -> None:
        await h.start()

        async def reader(market_id: str) -> Market:
            await asyncio.get_running_loop().create_future()
            raise AssertionError("unreachable")

        h.rest.get_market = reader  # type: ignore[method-assign]
        h.transport.batch(h.topic(), batch(1, 0, market_settled=[settled()]))
        await settle()
        await h.clock.advance(30.0)
        assert h.feed.readiness(MARKET).consecutive_failures == 1
        assert instance_state(h, StaleReason.MARKET_SETTLED) is InstanceState.OPEN
        assert h.task is not None and not h.task.done()
        await h.stop()

    run(scenario, policy=QUIET)

