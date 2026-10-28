"""M2-B2 acceptance tests: the periodic lifecycle sweep, offline.

Design: docs/design/market-data-recovery.md, section 9.3 ("a periodic lifecycle sweep with
the market list [TMKTS] (*configurable* interval)"), 4.6 (restriction by a superseded
read), section 5 (bounds) and AT-26. The sweep is opt-in: a feed runs one only when it is
given a SweepPolicy, whose values have no defaults. The values used here are test values,
not an operational choice.

REST goes through httpx.MockTransport (synthetic market-list pages built in code from the
synthetic `tournament_markets_page1` row), the socket is FakeTransport and time is
ManualClock. Unless a test says otherwise the feed has a TEST-ONLY bootstrap ledger
(realtime_kit.test_bootstrap).
"""

from __future__ import annotations

import asyncio
import copy
import json
import logging
import math
import re
from decimal import Decimal
from pathlib import Path
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
from sig_testkit import TOURNAMENT, fixture, json_response

import engine
from engine.sig_client.errors import SigRateLimitedError
from engine.sig_client.readiness import ReadinessStatus
from engine.sig_client.realtime import (
    MarketReady,
    MarketStale,
    RealtimeFeedPolicy,
    RealtimeMarketFeed,
)
from engine.sig_client.realtime_topic import StaleReason
from engine.sig_client.recovery import (
    SWEEP_NOTE,
    InstanceState,
    RecoveryLedger,
    SweepObservation,
    SweepPolicy,
)
from engine.sig_client.wire import TournamentMarket

QUIET = RealtimeFeedPolicy(tick_s=1e6)  # the timer never fires within these tests
INTERVAL = 60.0  # test value only
SWEEP = SweepPolicy(  # test values only
    interval_s=INTERVAL, deadline_s=10.0, max_pages=3, pages_per_window=10
)
MARKETS_ROUTE = f"/api/v1/tournaments/{TOURNAMENT.slug}/markets"
MARKETS = f"/tournaments/{TOURNAMENT.slug}/markets"
DETAIL_ROUTE = f"/api/v1/markets/{MARKET}"
SETTLED_ON = "2026-10-10T12:00:01.000Z"
LOGGER = "engine.sig_client.realtime"


def row(market_id: str, status: str = "open", settled_with: str | None = None) -> dict[str, Any]:
    """A synthetic TournamentMarket row (the spec-derived fixture, re-labelled)."""
    body = copy.deepcopy(fixture("tournament_markets_page1")["data"][0])
    body.update(
        id=market_id,
        status=status,
        settledWith=settled_with,
        settledOn=SETTLED_ON if status == "settled" else None,
    )
    return body


class Listing:
    """The market-list route: page i of `pages` is served for cursor None, "c1", "c2", ...

    `held` page indexes wait, on every request, until the test releases them (or the
    request is cancelled). `responses` replaces a page with a fixed response.
    """

    def __init__(
        self,
        *pages: list[dict[str, Any]],
        held: frozenset[int] = frozenset(),
        responses: dict[int, httpx.Response] | None = None,
    ) -> None:
        self.pages = [list(p) for p in pages]
        self.held = held
        self.responses = responses or {}
        self.requests: list[httpx.Request] = []
        self.waiting: list[asyncio.Future[None]] = []
        self.cancelled = 0
        self.clock: Any = None  # set to a ManualClock to record request times
        self.times: list[float] = []

    def body(self, index: int) -> dict[str, Any]:
        last = index == len(self.pages) - 1
        return {
            "data": self.pages[index],
            "pagination": {
                "total": sum(len(p) for p in self.pages),
                "limit": 100,
                "hasMore": not last,
                "nextCursor": None if last else f"c{index + 1}",
            },
        }

    async def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        if self.clock is not None:
            self.times.append(self.clock())
        cursor = request.url.params.get("cursor")
        index = 0 if cursor is None else int(cursor[1:])
        if index in self.held:
            future: asyncio.Future[None] = asyncio.get_running_loop().create_future()
            self.waiting.append(future)
            try:
                await future
            except asyncio.CancelledError:
                self.cancelled += 1
                raise
        if index in self.responses:
            return self.responses[index]
        return json_response(200, self.body(index))

    def pending(self) -> int:
        return sum(not f.done() for f in self.waiting)

    def release(self) -> None:
        for future in self.waiting:
            if not future.done():
                future.set_result(None)


def harness(listing: Listing, **kwargs: Any) -> Harness:
    h = Harness(policy=QUIET, sweep=kwargs.pop("sweep", SWEEP), **kwargs)
    h.router.routes[MARKETS_ROUTE] = listing
    return h


def run(scenario: Any, h: Harness) -> Harness:
    async def main() -> Harness:
        try:
            await scenario(h)
        finally:
            if h.task is not None and not h.task.done():
                await h.feed.aclose()
                await asyncio.gather(h.task, return_exceptions=True)
        return h

    return asyncio.run(main())


def walks(h: Harness) -> int:
    """Market-list walks begun: requests for the first page."""
    return sum(
        1 for r in h.router.requests
        if r.url.path == MARKETS_ROUTE and "cursor" not in r.url.params
    )


def found(h: Harness, reason: StaleReason, market_id: str = MARKET) -> list[Any]:
    return [i for i in h.ledger.instances(market_id) if i.reason is reason]


def readies(h: Harness) -> int:
    return sum(isinstance(e, MarketReady) for e in h.events)


def ledger_state(h: Harness, market_id: str = MARKET) -> tuple[Any, ...]:
    ledger = h.ledger
    return (
        ledger.instances(market_id),
        ledger.generation(market_id),
        ledger.windows(market_id),
        ledger.lifecycle(market_id),
        ledger.sweep(market_id),
        ledger.initial_evidence(market_id),
    )


async def ready_market(h: Harness) -> None:
    await h.start()
    assert h.feed.is_ready(MARKET) and readies(h) == 1


# --- disabled by default; the policy is explicit and validated -------------------------------


def test_sweep_is_disabled_by_default() -> None:
    listing = Listing([row(MARKET, "closed")])

    async def scenario(h: Harness) -> None:
        await ready_market(h)
        for _ in range(5):
            await h.clock.advance(3600.0 / 5)
        assert h.count(MARKETS) == 0 and walks(h) == 0
        assert h.feed._sweep_gate is None  # no sweep budget is taken either
        assert h.feed.readiness(MARKET).sweep is None
        assert h.feed.is_ready(MARKET)
        await h.stop()

    h = Harness(policy=QUIET)
    h.router.routes[MARKETS_ROUTE] = listing
    run(scenario, h)


def test_sweep_policy_has_no_defaults() -> None:
    with pytest.raises(TypeError):
        SweepPolicy()  # type: ignore[call-arg]
    with pytest.raises(TypeError):
        SweepPolicy(interval_s=60.0)  # type: ignore[call-arg]
    with pytest.raises(TypeError):  # the page budget is explicit too
        SweepPolicy(interval_s=60.0, deadline_s=10.0, max_pages=3)  # type: ignore[call-arg]


@pytest.mark.parametrize(
    "kwargs",
    [
        {"interval_s": 0},
        {"interval_s": -1.0},
        {"interval_s": math.nan},
        {"interval_s": math.inf},
        {"interval_s": True},
        {"interval_s": "60"},
        {"deadline_s": 0},
        {"deadline_s": math.inf},
        {"max_pages": 0},
        {"max_pages": 51},
        {"max_pages": 2.0},
        {"max_pages": True},
        {"pages_per_window": 0},
        {"pages_per_window": 1.5},
        {"pages_per_window": True},
    ],
)
def test_sweep_policy_rejects_invalid_values(kwargs: dict[str, Any]) -> None:
    values: dict[str, Any] = {
        "interval_s": 60.0, "deadline_s": 10.0, "max_pages": 3, "pages_per_window": 10
    }
    with pytest.raises(ValueError):
        SweepPolicy(**{**values, **kwargs})


def test_feed_rejects_a_sweep_that_is_not_a_policy() -> None:
    h = Harness(policy=QUIET)
    with pytest.raises(TypeError, match="SweepPolicy"):
        RealtimeMarketFeed(
            h.rest, h.tokens, FakeTransport(), ["27"], ledger=RecoveryLedger(),
            sweep={"interval_s": 60.0},  # type: ignore[arg-type]
        )


def test_no_module_in_engine_creates_a_sweep_policy() -> None:
    """Enabling the sweep in production is Arnav's decision (an interval is required)."""
    root = Path(engine.__file__).parent
    creations = [
        path.relative_to(root).as_posix()
        for path in root.rglob("*.py")
        if re.search(r"(?<!class )\bSweepPolicy\(", path.read_text(encoding="utf-8"))
    ]
    assert creations == []


# --- AT-26 ---------------------------------------------------------------------------------


@pytest.mark.parametrize("status", ["closed", "settled"])
def test_at26_sweep_finds_a_market_not_open_and_quarantines_it(status: str) -> None:
    label = "YES" if status == "settled" else None
    listing = Listing([row(MARKET, status, label)])

    async def scenario(h: Harness) -> None:
        await ready_market(h)
        generation = h.ledger.generation(MARKET)
        mark = len(h.observed)
        await h.clock.advance(INTERVAL)
        assert walks(h) == 1
        (blocker,) = found(h, StaleReason.LIFECYCLE_NOT_OPEN)
        assert blocker.state is InstanceState.QUARANTINED
        observation = blocker.evidence[-1].observation
        assert isinstance(observation, SweepObservation)
        assert blocker.evidence[-1].finding.startswith(f"market list shows {status}")
        assert (observation.status, observation.settled_with) == (status, label)
        assert observation.listed and observation.current and observation.note == SWEEP_NOTE
        assert h.ledger.generation(MARKET) == generation + 1
        # Readiness withdrawn, with the withdrawal the first callback, seen not ready.
        assert h.observed[mark] == ("MarketStale", False)
        assert h.events[-1] == MarketStale(MARKET, StaleReason.LIFECYCLE_NOT_OPEN)
        result = h.feed.readiness(MARKET)
        assert result.status is ReadinessStatus.QUARANTINED and not h.feed.is_ready(MARKET)
        assert result.sweep == observation
        assert result.lifecycle is not None and result.lifecycle.status == "open"  # untouched
        # Quarantined: not retried, and later walks change nothing more.
        await h.clock.advance(INTERVAL)
        assert walks(h) == 2 and len(found(h, StaleReason.LIFECYCLE_NOT_OPEN)) == 1
        assert h.ledger.generation(MARKET) == generation + 1 and readies(h) == 1
        await h.stop()

    run(scenario, harness(listing))


def test_complete_multi_page_walk_reads_every_page_with_status_any() -> None:
    listing = Listing(
        [row("101"), row("102")], [row("103"), row("104", "closed")], [row(MARKET, "closed")]
    )

    async def scenario(h: Harness) -> None:
        await ready_market(h)
        await h.clock.advance(INTERVAL)
        assert [r.url.params.get("cursor") for r in listing.requests] == [None, "c1", "c2"]
        assert {r.url.params["status"] for r in listing.requests} == {"any"}
        assert {r.url.params["limit"] for r in listing.requests} == {"100"}
        assert found(h, StaleReason.LIFECYCLE_NOT_OPEN)[0].state is InstanceState.QUARANTINED
        await h.stop()

    run(scenario, harness(listing))


def test_sweep_never_confirms_a_settlement_even_when_current() -> None:
    """A current listing showing `settled` with the signal's label restricts only: the
    MARKET_SETTLED instance stays open with no sweep evidence."""
    listing = Listing([row(MARKET, "settled", "YES")])

    async def scenario(h: Harness) -> None:
        await ready_market(h)
        h.transport.batch(h.topic(), batch(1, 0, market_settled=[settled()]))
        await settle()
        await h.clock.advance(INTERVAL)
        (settlement,) = found(h, StaleReason.MARKET_SETTLED)
        assert settlement.state is InstanceState.OPEN
        assert not any(isinstance(e.observation, SweepObservation) for e in settlement.evidence)
        assert found(h, StaleReason.LIFECYCLE_NOT_OPEN)[0].state is InstanceState.QUARANTINED
        assert h.feed.readiness(MARKET).sweep.current
        await h.stop()

    run(scenario, harness(listing))


def test_every_withdrawal_is_in_effect_before_the_first_callback() -> None:
    markets = (MARKET, "27")
    listing = Listing([row(MARKET, "closed"), row("27", "settled", "NO")])
    seen: list[dict[str, bool]] = []

    async def scenario(h: Harness) -> None:
        h.router.routes["/api/v1/markets/27/orderbook"] = json_response(
            200, fixture("market_orderbook")
        )
        await h.start()
        assert all(h.feed.is_ready(m) for m in markets)

        def sink(event: Any) -> None:
            if isinstance(event, MarketStale):
                seen.append({m: h.feed.is_ready(m) for m in markets})

        h.feed._sink = sink  # white-box: observe every market during each callback
        await h.clock.advance(INTERVAL)
        assert seen == [{MARKET: False, "27": False}] * 2
        await h.stop()

    run(scenario, harness(listing, market_ids=markets))


# --- an open listing never makes a market more ready ----------------------------------------


def test_open_listing_changes_nothing_and_publishes_nothing() -> None:
    listing = Listing([row(MARKET, "open")])

    async def scenario(h: Harness) -> None:
        await ready_market(h)
        h.transport.batch(h.topic(), batch(1, 0))
        h.transport.batch(h.topic(), batch(5, 4))  # a gap: REVISION_GAP
        await settle()
        await h.clock.advance(2.0)
        before = h.ledger.instances(MARKET), h.ledger.generation(MARKET)
        events = len(h.events)
        await h.clock.advance(INTERVAL)
        assert walks(h) == 1
        assert (h.ledger.instances(MARKET), h.ledger.generation(MARKET)) == before
        assert found(h, StaleReason.REVISION_GAP)[0].state is InstanceState.OPEN
        assert not any(isinstance(e, (MarketReady, MarketStale)) for e in h.events[events:])
        observation = h.feed.readiness(MARKET).sweep
        assert observation is not None and observation.status == "open"
        assert not h.feed.is_ready(MARKET)
        await h.stop()

    run(scenario, harness(listing))


def test_open_listing_is_never_bootstrap_evidence() -> None:
    """A declared market whose market-detail read never succeeds stays without
    initial-state evidence, whatever the sweep shows."""
    listing = Listing([row(MARKET, "open")])
    unavailable = json_response(400, {"error": {"code": "BAD_REQUEST", "message": "x"}})

    async def scenario(h: Harness) -> None:
        h.router.routes[DETAIL_ROUTE] = unavailable
        await h.start()
        await h.clock.advance(INTERVAL)
        await h.clock.advance(INTERVAL)
        assert walks(h) == 2
        assert h.ledger.initial_evidence(MARKET) is None
        assert h.ledger.lifecycle(MARKET) is None
        assert not h.feed.is_ready(MARKET) and readies(h) == 0
        await h.stop()

    run(scenario, harness(listing))


def test_open_listing_never_resolves_unknown_history() -> None:
    listing = Listing([row(MARKET, "open")])

    async def scenario(h: Harness) -> None:
        await h.start()
        await h.clock.advance(INTERVAL)
        assert walks(h) == 1
        assert h.feed.blockers(MARKET) == {StaleReason.HISTORY_UNKNOWN}
        assert not h.feed.is_ready(MARKET) and readies(h) == 0
        await h.stop()

    run(scenario, harness(listing, ledger=RecoveryLedger()))


# --- a market absent from a complete list --------------------------------------------------


def test_absent_ready_market_gets_an_open_lifecycle_unknown_blocker(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """M2-B2-F1: no documented rule says what absence from a complete status=any list
    means (Q16). Fail closed without inferring a status: an open LIFECYCLE_UNKNOWN blocks
    the market, which is NOT_READY, not QUARANTINED (nothing says it closed)."""
    listing = Listing([row("101", "closed"), row("102")])

    async def scenario(h: Harness) -> None:
        await ready_market(h)
        generation = h.ledger.generation(MARKET)
        mark = len(h.observed)
        with caplog.at_level(logging.WARNING, logger=LOGGER):
            await h.clock.advance(INTERVAL)
        (blocker,) = found(h, StaleReason.LIFECYCLE_UNKNOWN)
        assert blocker.state is InstanceState.OPEN
        assert found(h, StaleReason.LIFECYCLE_NOT_OPEN) == []  # not read as closed
        observation = blocker.evidence[-1].observation
        assert isinstance(observation, SweepObservation) and not observation.listed
        assert observation.status is None and observation.settled_with is None
        assert blocker.missing == ("Q16", "D-7")
        assert h.ledger.generation(MARKET) == generation + 1
        assert h.observed[mark] == ("MarketStale", False)
        assert h.events[-1] == MarketStale(MARKET, StaleReason.LIFECYCLE_UNKNOWN)
        result = h.feed.readiness(MARKET)
        assert result.status is ReadinessStatus.NOT_READY and not h.feed.is_ready(MARKET)
        assert any(line.startswith("lifecycle unknown: absent") for line in result.reasons)
        assert any("absent from the complete market list" in r.getMessage()
                   for r in caplog.records)
        await h.stop()

    run(scenario, harness(listing))


def test_lifecycle_unknown_is_never_cleared_by_a_later_open_row() -> None:
    """A later walk listing the market `open`, more absences, book resyncs and an `open`
    market-detail read leave the one instance open: no clearing, no MarketReady."""
    listing = Listing([row("101")])

    async def scenario(h: Harness) -> None:
        await ready_market(h)
        await h.clock.advance(INTERVAL)  # absent
        (blocker,) = found(h, StaleReason.LIFECYCLE_UNKNOWN)
        generation = h.ledger.generation(MARKET)
        await h.clock.advance(INTERVAL)  # absent again: no second instance
        assert len(found(h, StaleReason.LIFECYCLE_UNKNOWN)) == 1
        assert h.ledger.generation(MARKET) == generation
        listing.pages = [[row("101"), row(MARKET, "open")]]
        await h.clock.advance(INTERVAL)  # listed open now
        h.transport.subscribed(h.topic())  # a re-join, a resync, a detail read
        await h.clock.advance(5.0)
        assert walks(h) == 3
        assert found(h, StaleReason.LIFECYCLE_UNKNOWN) == [blocker]
        assert h.feed.readiness(MARKET).sweep.status == "open"
        assert not h.feed.is_ready(MARKET) and readies(h) == 1
        await h.stop()

    run(scenario, harness(listing))


def test_absent_and_closed_markets_are_withdrawn_after_every_ledger_change() -> None:
    markets = (MARKET, "27")
    listing = Listing([row("27", "closed")])
    seen: list[dict[str, bool]] = []

    async def scenario(h: Harness) -> None:
        h.router.routes["/api/v1/markets/27/orderbook"] = json_response(
            200, fixture("market_orderbook")
        )
        await h.start()
        assert all(h.feed.is_ready(m) for m in markets)

        def sink(event: Any) -> None:
            if isinstance(event, MarketStale):
                seen.append({m: h.feed.is_ready(m) for m in markets})

        h.feed._sink = sink  # white-box: observe every market during each callback
        await h.clock.advance(INTERVAL)
        assert seen == [{MARKET: False, "27": False}] * 2
        assert h.feed.readiness(MARKET).status is ReadinessStatus.NOT_READY
        assert h.feed.readiness("27").status is ReadinessStatus.QUARANTINED
        await h.stop()

    run(scenario, harness(listing, market_ids=markets))


def test_absence_in_a_superseded_walk_still_blocks() -> None:
    listing = Listing([row("101")], held=frozenset({0}))

    async def scenario(h: Harness) -> None:
        await ready_market(h)
        await h.clock.advance(INTERVAL)
        assert listing.pending() == 1
        h.transport.batch(h.topic(), batch(1, 0))
        h.transport.batch(h.topic(), batch(5, 4))  # a gap raises the generation
        await settle()
        listing.release()
        await settle()
        assert found(h, StaleReason.LIFECYCLE_UNKNOWN)[0].state is InstanceState.OPEN
        assert not h.feed.readiness(MARKET).sweep.current
        await h.stop()

    run(scenario, harness(listing))


def test_control_partial_walk_omitting_the_market_applies_nothing() -> None:
    """Absence counts only in a complete walk: page 1 omits the market, page 2 fails."""
    listing = Listing([row("101")], [row(MARKET)], responses={1: _not_found()})

    async def scenario(h: Harness) -> None:
        await ready_market(h)
        before = ledger_state(h)
        await h.clock.advance(INTERVAL)
        assert len(listing.requests) == 2
        assert ledger_state(h) == before and h.feed.is_ready(MARKET)
        await h.stop()

    run(scenario, harness(listing))


def test_control_listed_open_market_gets_no_absence_blocker() -> None:
    listing = Listing([row("101")], [row(MARKET, "open")])

    async def scenario(h: Harness) -> None:
        await ready_market(h)
        before = h.ledger.instances(MARKET), h.ledger.generation(MARKET)
        await h.clock.advance(INTERVAL)
        assert (h.ledger.instances(MARKET), h.ledger.generation(MARKET)) == before
        assert h.feed.readiness(MARKET).sweep.listed and h.feed.is_ready(MARKET)
        await h.stop()

    run(scenario, harness(listing))


# --- failed, partial, capped, stale and superseded walks -----------------------------------


def _not_found() -> httpx.Response:
    return json_response(404, {"error": {"code": "NOT_FOUND", "message": "x"}})


@pytest.mark.parametrize(
    "case",
    ["later page error", "item on two pages", "page cap", "malformed later page"],
)
def test_failed_or_partial_walk_applies_nothing(case: str) -> None:
    """Page 1 lists the market `closed`, but the walk does not complete: nothing applies."""
    first = [row(MARKET, "closed")]
    if case == "later page error":
        listing = Listing(first, [row("102")], responses={1: _not_found()})
    elif case == "item on two pages":
        listing = Listing(first, [row(MARKET, "closed")])
    elif case == "page cap":
        listing = Listing(first, [row("102")], [row("103")], [row("104")])  # 4 pages, cap 3
    else:
        listing = Listing(first, [row("102")], responses={1: json_response(200, {"data": 1})})

    async def scenario(h: Harness) -> None:
        await ready_market(h)
        before = ledger_state(h)
        events = len(h.events)
        await h.clock.advance(INTERVAL)
        assert walks(h) == 1
        if case == "page cap":
            assert len(listing.requests) == SWEEP.max_pages  # the cap bounds the reads
        assert ledger_state(h) == before
        assert h.events[events:] == [] and h.feed.is_ready(MARKET)
        assert h.task is not None and not h.task.done()  # the feed carries on
        await h.stop()

    run(scenario, harness(listing))


def test_walk_past_its_deadline_applies_nothing_and_the_next_walk_follows() -> None:
    listing = Listing([row(MARKET, "closed")], held=frozenset({0}))

    async def scenario(h: Harness) -> None:
        await ready_market(h)
        before = ledger_state(h)
        await h.clock.advance(INTERVAL)
        assert listing.pending() == 1
        await h.clock.advance(SWEEP.deadline_s)  # the walk's deadline passes
        assert listing.cancelled == 1 and ledger_state(h) == before
        listing.release()
        await settle()
        assert ledger_state(h) == before and h.feed.is_ready(MARKET)
        listing.held = frozenset()
        await h.clock.advance(INTERVAL - 1)
        assert walks(h) == 1  # fixed delay from the end of the failed walk
        await h.clock.advance(1.0)
        assert walks(h) == 2
        assert found(h, StaleReason.LIFECYCLE_NOT_OPEN)[0].state is InstanceState.QUARANTINED
        await h.stop()

    run(scenario, harness(listing))


@pytest.mark.parametrize("status", ["open", "settled"])
def test_push_during_a_walk_supersedes_it_and_a_settled_row_only_restricts(status: str) -> None:
    """A blocker raised while the walk is in flight raises the generation: the walk is
    superseded. `settled` still quarantines (design 4.6); `open` changes nothing."""
    listing = Listing([row(MARKET, status, "YES" if status == "settled" else None)],
                      held=frozenset({0}))

    async def scenario(h: Harness) -> None:
        await ready_market(h)
        await h.clock.advance(INTERVAL)
        assert listing.pending() == 1
        h.transport.batch(h.topic(), batch(1, 0, market_settled=[settled()]))
        await settle()
        generation = h.ledger.generation(MARKET)
        before = h.ledger.instances(MARKET)
        listing.release()
        await settle()
        observation = h.feed.readiness(MARKET).sweep
        assert observation is not None and not observation.current
        (settlement,) = found(h, StaleReason.MARKET_SETTLED)
        assert settlement.state is InstanceState.OPEN
        if status == "open":
            assert h.ledger.instances(MARKET) == before
            assert h.ledger.generation(MARKET) == generation
        else:
            (not_open,) = found(h, StaleReason.LIFECYCLE_NOT_OPEN)
            assert not_open.state is InstanceState.QUARANTINED
            assert h.ledger.generation(MARKET) == generation + 1
        assert not h.feed.is_ready(MARKET) and readies(h) == 1
        await h.stop()

    run(scenario, harness(listing))


# --- closing and cancellation --------------------------------------------------------------


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


def test_closing_during_a_walk_cancels_it_and_applies_nothing() -> None:
    transport = SlowClose()
    listing = Listing([row(MARKET, "closed")], held=frozenset({0}))

    async def scenario(h: Harness) -> None:
        try:
            await ready_market(h)
            await h.clock.advance(INTERVAL)
            assert listing.pending() == 1
            before = ledger_state(h)
            events = len(h.events)
            closer = asyncio.create_task(h.feed.aclose())
            await settle()
            assert listing.cancelled == 1  # the walk was cancelled while close() is pending
            listing.release()
            await settle()
            assert ledger_state(h) == before
            assert all(e.reason is StaleReason.STOPPED
                       for e in h.events[events:] if isinstance(e, MarketStale))
            assert h.feed.readiness(MARKET).status is ReadinessStatus.NOT_READY
        finally:
            transport.allow_close.set()
        await closer
        assert h.task is not None
        await h.task
        await h.clock.advance(INTERVAL * 2)
        assert walks(h) == 1  # nothing new starts after closing
        await h.rest.aclose()
        await h.tokens.aclose()

    run(scenario, harness(listing, transport=transport))


def test_complete_walk_reaching_the_step_after_closing_began_is_dropped() -> None:
    """White-box: the walk completed, but the step runs after aclose() began."""
    transport = SlowClose()
    listing = Listing([row(MARKET, "closed")])

    async def scenario(h: Harness) -> None:
        try:
            await ready_market(h)
            exact = json.loads(json.dumps(row(MARKET, "closed")), parse_float=Decimal)
            rows = [TournamentMarket.model_validate(exact)]
            generations = {MARKET: h.ledger.generation(MARKET)}
            before = ledger_state(h)
            closer = asyncio.create_task(h.feed.aclose())
            await settle()
            now = h.clock()
            h.feed._commit_sweep("S99", generations, rows, now, now, now + 10.0)
            assert ledger_state(h) == before
        finally:
            transport.allow_close.set()
        await closer
        assert h.task is not None
        await h.task
        await h.rest.aclose()
        await h.tokens.aclose()

    run(scenario, harness(listing, transport=transport))


# --- scheduling and read cost --------------------------------------------------------------


def test_repeated_schedule_is_a_fixed_delay_and_walks_never_overlap() -> None:
    long_deadline = SweepPolicy(
        interval_s=INTERVAL, deadline_s=500.0, max_pages=3, pages_per_window=10
    )
    listing = Listing([row(MARKET)], held=frozenset({0}))

    async def scenario(h: Harness) -> None:
        await ready_market(h)
        await h.clock.advance(INTERVAL - 1)
        assert walks(h) == 0
        await h.clock.advance(1.0)
        assert walks(h) == 1 and listing.pending() == 1
        for _ in range(4):  # a slow walk: no second walk starts while it runs
            await h.clock.advance(INTERVAL)
        assert walks(h) == 1
        listing.held = frozenset()
        listing.release()
        await settle()
        await h.clock.advance(INTERVAL - 1)
        assert walks(h) == 1  # the next one waits a full interval after the end
        await h.clock.advance(1.0)
        assert walks(h) == 2
        for expected in (3, 4, 5):
            await h.clock.advance(INTERVAL)
            assert walks(h) == expected
        await h.stop()

    run(scenario, harness(listing, sweep=long_deadline))


def test_retry_after_is_a_floor_on_the_next_walk() -> None:
    listing = Listing([row(MARKET)])
    calls: list[float] = []

    async def scenario(h: Harness) -> None:
        async def limited(**kwargs: Any) -> list[TournamentMarket]:
            calls.append(h.clock())
            raise SigRateLimitedError(
                method="GET", path=MARKETS, status=429, code="RATE_LIMITED",
                unrecognized_code=False, detail="test", retry_after=300.0,
            )

        await ready_market(h)
        h.rest.list_all_tournament_markets = limited  # type: ignore[method-assign]
        await h.clock.advance(INTERVAL)
        assert len(calls) == 1
        await h.clock.advance(299.0)
        assert len(calls) == 1  # the interval (60 s) never shortens SIG's Retry-After
        await h.clock.advance(1.0)
        assert len(calls) == 2 and calls[1] - calls[0] == 300.0
        assert h.feed.is_ready(MARKET)
        await h.stop()

    run(scenario, harness(listing))


def test_pages_use_the_sweep_gate_and_shared_limiter_not_recovery_or_resync() -> None:
    listing = Listing([row("101")], [row("102")], [row(MARKET)])
    counts = {"shared": 0, "sweep": 0, "recovery": 0, "resync": 0}

    async def scenario(h: Harness) -> None:
        await ready_market(h)

        def counting(name: str, acquire: Any) -> Any:
            async def wrapped(*args: Any, **kwargs: Any) -> None:
                counts[name] += 1
                await acquire(*args, **kwargs)

            return wrapped

        h.limiter.acquire = counting("shared", h.limiter.acquire)  # type: ignore[method-assign]
        feed = h.feed
        feed._recovery_gate.acquire = counting(  # type: ignore[method-assign]
            "recovery", feed._recovery_gate.acquire
        )
        feed._resync_gate.acquire = counting(  # type: ignore[method-assign]
            "resync", feed._resync_gate.acquire
        )
        assert feed._sweep_gate is not None
        feed._sweep_gate.acquire = counting(  # type: ignore[method-assign]
            "sweep", feed._sweep_gate.acquire
        )
        await h.clock.advance(INTERVAL)
        assert len(listing.requests) == 3
        assert counts == {"shared": 3, "sweep": 3, "recovery": 0, "resync": 0}
        await h.stop()

    run(scenario, harness(listing))


def test_detail_route_is_untouched_by_the_sweep() -> None:
    """The sweep reads only the market list: no market-detail or orderbook read."""
    listing = Listing([row(MARKET, "closed")])

    async def scenario(h: Harness) -> None:
        await ready_market(h)
        details, books = h.count(f"/markets/{MARKET}"), h.count(f"/markets/{MARKET}/orderbook")
        await h.clock.advance(INTERVAL)
        assert h.count(f"/markets/{MARKET}") == details
        assert h.count(f"/markets/{MARKET}/orderbook") == books
        await h.stop()

    h = harness(listing)
    h.router.routes[DETAIL_ROUTE] = json_response(200, market_detail(MARKET))
    run(scenario, h)


# --- the sweep's own page budget (M2-B2-F1) ------------------------------------------------


def test_page_budget_bounds_a_fast_sweep_and_leaves_shared_capacity() -> None:
    """A short interval and many pages: the sweep starts at most `pages_per_window` page
    reads in any rolling 60 s, so a recovery read is admitted at once (at 674091a the same
    sweep took 77 of the 80 shared reads in 2 s and the recovery read waited 60 s)."""
    fast = SweepPolicy(interval_s=1.0, deadline_s=600.0, max_pages=50, pages_per_window=10)
    pages = [[row(str(1000 + i))] for i in range(49)] + [[row(MARKET)]]
    listing = Listing(*pages)

    async def scenario(h: Harness) -> None:
        listing.clock = h.clock
        await ready_market(h)
        await h.clock.advance(2.0)
        assert 0 < len(listing.requests) <= 10
        t0 = h.clock()
        details = h.count(f"/markets/{MARKET}")
        h.transport.batch(h.topic(), batch(1, 0))
        h.transport.batch(h.topic(), batch(5, 4))  # a gap: a recovery read is wanted now
        await settle()
        assert h.count(f"/markets/{MARKET}") == details + 1 and h.clock() == t0
        for _ in range(6):
            await h.clock.advance(60.0)
        times = listing.times
        assert len(times) > 20  # the sweep kept walking ...
        assert max(sum(1 for t in times if start <= t < start + 60.0) for start in times) <= 10
        assert h.task is not None and not h.task.done()
        await h.stop()

    run(scenario, harness(listing, sweep=fast))


@pytest.mark.parametrize(("pages", "accepted"), [(29, True), (30, False), (500, False)])
def test_feed_refuses_a_page_budget_that_leaves_no_shared_capacity(
    pages: int, accepted: bool
) -> None:
    """With the default gates (40 resyncs and 10 recovery starts a minute) and the shared
    80-read limit, the sweep may take at most 29: the gated starts stay below the limit."""
    policy = SweepPolicy(interval_s=60.0, deadline_s=10.0, max_pages=3, pages_per_window=pages)
    if accepted:
        Harness(policy=QUIET, sweep=policy)
    else:
        with pytest.raises(ValueError, match="no shared read capacity"):
            Harness(policy=QUIET, sweep=policy)


def test_page_admission_past_the_walk_deadline_fails_the_walk() -> None:
    """One page per window and a 10 s deadline: page 2 cannot be admitted in time, so the
    walk fails at once (the gate knows the wait passes the deadline) and page 1's `closed`
    row is never applied; the next walk starts one interval after that failure."""
    tight = SweepPolicy(interval_s=INTERVAL, deadline_s=10.0, max_pages=3, pages_per_window=1)
    listing = Listing([row(MARKET, "closed")], [row("102")])

    async def scenario(h: Harness) -> None:
        await ready_market(h)
        before = ledger_state(h)
        await h.clock.advance(INTERVAL)
        assert len(listing.requests) == 1
        assert ledger_state(h) == before and h.feed.is_ready(MARKET)
        await h.clock.advance(INTERVAL)  # failed at once, so the next walk is due now
        assert walks(h) == 2 and len(listing.requests) == 2
        assert ledger_state(h) == before and h.feed.is_ready(MARKET)
        await h.stop()

    run(scenario, harness(listing, sweep=tight))
