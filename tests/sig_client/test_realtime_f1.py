"""S1-T3-A-F1 review repairs: readiness ordering, expiry at boundaries, Retry-After floors,
durable blockers, and shutdown during start-up.

Offline: REST through httpx.MockTransport, FakeTransport for the socket, ManualClock for
time. Every await that could hang is bounded by a real-time watchdog. All realtime
payloads are synthetic.
"""

from __future__ import annotations

import asyncio
from datetime import timedelta
from decimal import Decimal
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
    dumps,
    settle,
    settled,
    token_body,
    trade,
    watchdog,
)
from sig_testkit import TOURNAMENT, error_body, fixture, json_response

from engine.sig_client.errors import SigServiceUnavailableError
from engine.sig_client.rate_limiter import RequestKind
from engine.sig_client.realtime import (
    MarketReady,
    MarketStale,
    RealtimeFeedPolicy,
)
from engine.sig_client.realtime_topic import BookSource, MarketTopic, StaleReason
from engine.sig_client.realtime_wire import epoch_seconds
from engine.sig_client.wire import CombinedOrderbookExchange

ORDERBOOK = f"/markets/{MARKET}/orderbook"
TOKEN = "/realtime/token"
EX = "36"
QUIET = RealtimeFeedPolicy(tick_s=1e6)  # the timer never fires within these tests


def run(scenario: Any, **kwargs: Any) -> Harness:
    async def main() -> Harness:
        # Tasks that exist before the scenario (on Python 3.11 this includes the
        # watchdog's own wrapper task): only tasks created after this are the feed's.
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
    return (T0 + timedelta(seconds=delta_s)).strftime("%Y-%m-%dT%H:%M:%SZ")


def new_tasks(h: Harness) -> set[asyncio.Task[Any]]:
    """Tasks still alive that the scenario created (the current one excluded)."""
    return set(asyncio.all_tasks()) - h.baseline_tasks - {asyncio.current_task()}


def stale_reasons(h: Harness) -> list[StaleReason]:
    return [e.reason for e in h.events if isinstance(e, MarketStale)]


def readies(h: Harness) -> int:
    return sum(isinstance(e, MarketReady) for e in h.events)


def callbacks_after(h: Harness, mark: int) -> list[tuple[str, bool]]:
    return h.observed[mark:]


# --- 1. readiness is withdrawn before any callback for a triggering message -------------------


@pytest.mark.parametrize(
    ("payload", "kinds"),
    [
        # Gap after revision 1: books still apply (by version) and are published.
        (batch(9, 7, books=[book(70)]), {"BookUpdated"}),
        # Settlement in an accepted batch, with an ordinary trade and its book.
        (
            batch(2, 1, trades=[trade("t1", 11)], books=[book(11)], market_settled=[settled()]),
            {"BookUpdated", "TradeReceived", "SettlementSignal"},
        ),
        # resyncRequired carrying a book.
        ({"resyncRequired": True, "delivery": {"revision": 2, "previousRevision": 1},
          "books": [book(12)]}, {"BookUpdated"}),
        # A pushed book whose nextExpiryAt has already passed (no other trigger).
        (batch(2, 1, books=[book(13, next_expiry=iso(-1))]), {"BookUpdated"}),
    ],
)
def test_every_callback_of_a_triggering_message_sees_not_ready(
    payload: Any, kinds: set[str]
) -> None:
    async def scenario(h: Harness) -> None:
        await h.start()
        h.transport.batch(h.topic(), batch(1, 0))  # revision 1 accepted, nothing else
        await settle()
        assert h.feed.is_ready(MARKET) and h.feed.last_revision(MARKET) == 1
        mark = len(h.observed)
        h.transport.batch(h.topic(), payload)
        await settle()
        seen = callbacks_after(h, mark)
        assert seen[0] == ("MarketStale", False)  # published first
        assert {kind for kind, _ in seen} >= kinds
        assert all(ready is False for _, ready in seen), seen
        await h.stop()

    run(scenario, policy=QUIET)


def test_standalone_settlement_callback_sees_not_ready() -> None:
    async def scenario(h: Harness) -> None:
        await h.start()
        mark = len(h.observed)
        h.transport.batch(h.topic(), settled(), event="market_settled")
        await settle()
        assert callbacks_after(h, mark) == [("MarketStale", False), ("SettlementSignal", False)]
        await h.stop()

    run(scenario, policy=QUIET)


def test_non_triggering_message_keeps_callbacks_ready() -> None:
    async def scenario(h: Harness) -> None:
        await h.start()
        mark = len(h.observed)
        h.transport.batch(h.topic(), batch(1, 0, trades=[trade("t1", 11)], books=[book(11)]))
        await settle()
        assert callbacks_after(h, mark) == [("BookUpdated", True), ("TradeReceived", True)]
        await h.stop()

    run(scenario, policy=QUIET)


# --- 2. token and book expiry enforced without the timer -------------------------------------


def test_token_expiry_without_tick_withdraws_readiness_and_blocks_recovery() -> None:
    async def scenario(h: Harness) -> None:
        await h.start()
        assert h.feed.is_ready(MARKET)
        h.clock.now += 3 * 3600 + 1  # past expiresAt; no timer runs
        assert not h.feed.is_ready(MARKET)
        reads = h.count(ORDERBOOK)
        h.transport.subscribed(h.topic())  # a re-join: would normally resync
        await settle()
        assert h.count(ORDERBOOK) == reads  # no recovery under an expired token
        assert StaleReason.TOKEN_EXPIRED in stale_reasons(h)
        assert readies(h) == 1 and not h.feed.is_ready(MARKET)
        await h.stop()

    run(scenario, policy=QUIET)


def test_expiry_during_an_inflight_resync_publishes_no_ready() -> None:
    release: dict[str, asyncio.Event] = {}

    async def slow(request: httpx.Request) -> httpx.Response:
        event = release.setdefault("go", asyncio.Event())
        await event.wait()
        return json_response(200, _orderbook())

    async def scenario(h: Harness) -> None:
        h.router.routes[f"/api/v1{ORDERBOOK}"] = [json_response(200, _orderbook()), slow]
        await h.start()
        assert h.feed.is_ready(MARKET) and readies(h) == 1
        h.clock.now += 3 * 3600 - 5  # five seconds of token left; no timer runs
        h.transport.subscribed(h.topic())  # a re-join starts a resync
        await settle()
        assert h.count(ORDERBOOK) == 2
        # The token runs out while the read is in flight (well inside the read deadline).
        h.clock.now += 10
        release["go"].set()
        await settle()
        assert readies(h) == 1 and not h.feed.is_ready(MARKET)
        assert h.count(ORDERBOOK) == 2
        await h.stop()

    run(scenario, policy=QUIET)


def test_book_expiry_passing_without_tick_withdraws_readiness() -> None:
    async def scenario(h: Harness) -> None:
        await h.start()
        h.transport.batch(h.topic(), batch(1, 0, books=[book(9, next_expiry=iso(30))]))
        await settle()
        assert h.feed.is_ready(MARKET)
        h.clock.now += 31  # nextExpiryAt passes; no timer runs
        assert not h.feed.is_ready(MARKET)
        await h.stop()

    run(scenario, policy=QUIET)


def test_already_expired_pushed_book_is_refetched_without_tick() -> None:
    async def scenario(h: Harness) -> None:
        await h.start()
        # A versioned REST read made after the expiry (T0 - 1 s), by SIG's own clock.
        h.router.routes[f"/api/v1{ORDERBOOK}"] = json_response(200, _orderbook(9))
        h.transport.batch(h.topic(), batch(1, 0, books=[book(9, next_expiry=iso(-1))]))
        await settle()
        assert stale_reasons(h) == [StaleReason.BOOK_EXPIRY]
        assert not h.feed.is_ready(MARKET)
        await h.clock.advance(2.0)  # min_resync_interval_s; the timer still never fires
        assert h.count(ORDERBOOK) == 2
        held = h.feed.books(MARKET)[EX]
        assert held.source is BookSource.REST and held.expiry_epoch is None
        assert h.feed.is_ready(MARKET) and readies(h) == 2
        await h.stop()

    run(scenario, policy=QUIET)


def test_older_rest_read_cannot_clear_an_expired_book() -> None:
    async def scenario(h: Harness) -> None:
        h.router.routes[f"/api/v1{ORDERBOOK}"] = [
            json_response(200, _orderbook(5)),
            json_response(200, _orderbook(5)),  # older than the pushed book below
            json_response(200, _orderbook(9)),  # same version: expiry moved it, so it wins
        ]
        await h.start()
        h.transport.batch(h.topic(), batch(1, 0, books=[book(9, next_expiry=iso(-1))]))
        await settle()
        await h.clock.advance(2.0)
        assert h.count(ORDERBOOK) == 2 and not h.feed.is_ready(MARKET)
        await h.clock.advance(2.0)
        assert h.count(ORDERBOOK) == 3 and h.feed.is_ready(MARKET)
        assert h.feed.books(MARKET)[EX].expiry_known is False
        await h.stop()

    run(scenario, policy=QUIET)


# --- 3. Retry-After is a floor for mint and resync retries ----------------------------------


def _unavailable(retry_after: str | None = None) -> httpx.Response:
    headers = {} if retry_after is None else {"Retry-After": retry_after}
    return json_response(503, error_body("SERVICE_UNAVAILABLE"), headers)


def test_startup_mint_honours_a_long_retry_after() -> None:
    async def scenario(h: Harness) -> None:
        h.task = asyncio.create_task(h.feed.run())
        await settle()
        assert h.count(TOKEN) == 1
        await h.clock.advance(7199.0)
        assert h.count(TOKEN) == 1 and h.transport.calls == []
        await h.clock.advance(2.0)
        assert h.count(TOKEN) == 2
        await h.stop()

    run(
        scenario,
        policy=RealtimeFeedPolicy(tick_s=1e6, mint_retry_s=15.0, max_retry_s=60.0),
        token_responses=[_unavailable("7200"), json_response(200, token_body())],
    )


def test_refresh_mint_honours_a_long_retry_after() -> None:
    async def scenario(h: Harness) -> None:
        await h.start()
        await h.clock.advance(3 * 3600 - 540)  # refresh at 11200 fails with Retry-After 7200
        assert h.count(TOKEN) == 2
        await h.clock.advance(18_399 - h.clock.now)
        assert h.count(TOKEN) == 2  # backoff (max 120 s) did not shorten SIG's wait
        await h.clock.advance(2.0)
        assert h.count(TOKEN) == 3
        await h.stop()

    run(
        scenario,
        policy=RealtimeFeedPolicy(tick_s=60.0, mint_retry_s=15.0, max_retry_s=120.0),
        token_responses=[
            json_response(200, token_body()),
            _unavailable("7200"),
            json_response(200, token_body(expires=T0 + timedelta(hours=8))),
        ],
    )


def test_oversized_retry_after_stops_instead_of_retrying() -> None:
    async def scenario(h: Harness) -> None:
        h.task = asyncio.create_task(h.feed.run())
        with pytest.raises(SigServiceUnavailableError):
            await watchdog(h.task)
        assert h.count(TOKEN) == 1 and h.transport.closed

    run(scenario, token_responses=[_unavailable("99999999999")])


def test_rate_limited_mint_waits_for_the_shared_cooldown() -> None:
    async def scenario(h: Harness) -> None:
        h.task = asyncio.create_task(h.feed.run())
        await settle()
        assert h.count(TOKEN) == 1
        await h.clock.advance(299.0)
        assert h.count(TOKEN) == 1
        await h.clock.advance(2.0)
        assert h.count(TOKEN) == 2
        assert h.limiter.used(RequestKind.WRITE) == 1  # one admission in the window
        await h.stop()

    run(
        scenario,
        policy=RealtimeFeedPolicy(tick_s=1e6),
        token_responses=[
            json_response(429, error_body("RATE_LIMITED"), {"Retry-After": "300"}),
            json_response(200, token_body()),
        ],
    )


def test_resync_honours_retry_after_beyond_the_read_budget() -> None:
    async def scenario(h: Harness) -> None:
        h.router.routes[f"/api/v1{ORDERBOOK}"] = [_unavailable("100"), json_response(200, _orderbook())]
        await h.start()
        assert h.count(ORDERBOOK) == 1 and not h.feed.is_ready(MARKET)
        await h.clock.advance(99.0)
        assert h.count(ORDERBOOK) == 1  # the 2 s resync backoff did not shorten it
        await h.clock.advance(2.0)
        assert h.count(ORDERBOOK) == 2 and h.feed.is_ready(MARKET)
        await h.stop()

    run(scenario, policy=QUIET)


# --- 4. settlement and null-sequence recovery are not cleared by an orderbook read ------------


@pytest.mark.parametrize(
    ("payload", "event", "blockers"),
    [
        (batch(1, 0, market_settled=[settled()]), "market_batch", {StaleReason.MARKET_SETTLED}),
        (settled(), "market_settled", {StaleReason.MARKET_SETTLED}),
        (
            batch(1, 0, trades=[trade(None, None)], books=[book(5)]),
            "market_batch",
            {StaleReason.NULL_TRADE_SEQUENCE},
        ),
        (
            batch(1, 0, trades=[trade(None, None)], books=[book(5)], market_settled=[settled()]),
            "market_batch",
            {StaleReason.NULL_TRADE_SEQUENCE, StaleReason.MARKET_SETTLED},
        ),
    ],
)
def test_book_refresh_cannot_clear_blockers(payload: Any, event: str, blockers: set) -> None:
    async def scenario(h: Harness) -> None:
        await h.start()
        h.transport.batch(h.topic(), payload, event=event)
        await settle()
        await h.clock.advance(2.0)
        assert h.count(ORDERBOOK) == 2  # the orderbook resync ran and succeeded ...
        assert not h.feed.is_ready(MARKET) and readies(h) == 1
        h.transport.subscribed(h.topic())  # ... and so does a later, unrelated one
        await h.clock.advance(2.0)
        assert h.count(ORDERBOOK) == 3
        assert not h.feed.is_ready(MARKET) and readies(h) == 1
        assert h.feed.blockers(MARKET) == blockers
        await h.stop()

    run(scenario, policy=QUIET)


def test_settlement_is_still_signalled_with_a_null_sequence_trade() -> None:
    async def scenario(h: Harness) -> None:
        await h.start()
        mark = len(h.observed)
        h.transport.batch(
            h.topic(),
            batch(1, 0, trades=[trade(None, None)], books=[book(5)], market_settled=[settled()]),
        )
        await settle()
        kinds = [kind for kind, _ in callbacks_after(h, mark)]
        assert "SettlementSignal" in kinds and "TradeReceived" not in kinds
        await h.stop()

    run(scenario, policy=QUIET)


# --- 5. aclose() stops run() during start-up --------------------------------------------------


class HangingTransport(FakeTransport):
    def __init__(self, hang: str, after: int = 0) -> None:
        super().__init__()
        self.hang = hang
        self.after = after
        self.cancelled: list[str] = []

    async def _maybe_hang(self, name: str) -> None:
        if name == self.hang and sum(k == name for k, _ in self.calls) > self.after:
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                self.cancelled.append(name)
                raise

    async def connect(self, endpoint: Any, token: Any) -> None:
        await super().connect(endpoint, token)
        await self._maybe_hang("connect")

    async def subscribe(self, topic: str) -> None:
        await super().subscribe(topic)
        await self._maybe_hang("subscribe")


def _hanging_route(state: dict[str, Any]) -> Any:
    async def handler(request: httpx.Request) -> httpx.Response:
        state["started"] = state.get("started", 0) + 1
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            state["cancelled"] = True
            raise
        raise AssertionError

    return handler


async def _close_and_check(h: Harness) -> None:
    await settle()
    await watchdog(h.feed.aclose())
    await watchdog(h.task)  # run() returns: no exception, no hang
    assert h.task.exception() is None
    assert h.transport.closed
    assert new_tasks(h) == set()
    calls, posts = list(h.transport.calls), h.count(TOKEN)
    await h.clock.advance(10_000.0)  # nothing new starts after shutdown
    await settle()
    assert h.transport.calls == calls and h.count(TOKEN) == posts


def test_aclose_during_tournament_verification() -> None:
    state: dict[str, Any] = {}

    async def scenario(h: Harness) -> None:
        h.router.routes["/api/v1/tournaments/alpha-tournament"] = _hanging_route(state)
        h.task = asyncio.create_task(h.feed.run())
        await _close_and_check(h)
        assert state == {"started": 1, "cancelled": True}
        assert h.count(TOKEN) == 0 and h.transport.calls == []

    run(scenario, policy=QUIET)


def test_aclose_during_a_hanging_mint() -> None:
    state: dict[str, Any] = {}

    async def scenario(h: Harness) -> None:
        h.task = asyncio.create_task(h.feed.run())
        await _close_and_check(h)
        assert state == {"started": 1, "cancelled": True}
        assert h.transport.calls == []

    run(scenario, policy=QUIET, token_responses=[_hanging_route(state)])


def test_aclose_during_a_mint_retry_wait() -> None:
    async def scenario(h: Harness) -> None:
        h.task = asyncio.create_task(h.feed.run())
        await settle()
        assert h.count(TOKEN) == 1  # failed; now waiting to retry
        await _close_and_check(h)
        assert h.count(TOKEN) == 1 and h.transport.calls == []

    run(
        scenario,
        policy=QUIET,
        token_responses=[_unavailable("600"), json_response(200, token_body())],
    )


def test_aclose_during_connect() -> None:
    transport = HangingTransport("connect")

    async def scenario(h: Harness) -> None:
        h.task = asyncio.create_task(h.feed.run())
        await _close_and_check(h)
        assert transport.cancelled == ["connect"]
        assert [kind for kind, _ in transport.calls] == ["connect"]  # never subscribed

    run(scenario, policy=QUIET, transport=transport)


def test_aclose_during_subscribe() -> None:
    transport = HangingTransport("subscribe", after=1)  # the second subscribe hangs

    async def scenario(h: Harness) -> None:
        h.task = asyncio.create_task(h.feed.run())
        await _close_and_check(h)
        assert transport.cancelled == ["subscribe"]
        assert [kind for kind, _ in transport.calls] == ["connect", "subscribe", "subscribe"]

    run(scenario, policy=QUIET, market_ids=("26", "27", "28"), transport=transport)


def test_cancelling_run_during_connect_cleans_up() -> None:
    transport = HangingTransport("connect")

    async def scenario(h: Harness) -> None:
        h.task = asyncio.create_task(h.feed.run())
        await settle()
        h.task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await watchdog(h.task)
        assert transport.cancelled == ["connect"] and transport.closed
        assert new_tasks(h) == set()

    run(scenario, policy=QUIET, transport=transport)


def test_aclose_before_run_sends_nothing() -> None:
    async def scenario(h: Harness) -> None:
        await h.feed.aclose()
        await watchdog(h.feed.run())
        assert h.router.requests == [] and h.transport.calls == []

    run(scenario, policy=QUIET)


# --- helpers ---------------------------------------------------------------------------------


def _orderbook(sequence: int | None = None) -> dict[str, Any]:
    body = fixture("market_orderbook")
    exchange = body["contexts"][0]["orderbook"]["exchanges"][0]
    exchange["asOf"] = (
        None if sequence is None else {"sequence": sequence, "at": "2026-10-10T12:00:00.0000001Z"}
    )
    return body



# --- topic-level rules behind 2 and 4 ----------------------------------------------------------


def _topic_with_expired_push(sequence: int) -> Any:
    t = MarketTopic(TOURNAMENT.id, MARKET)
    t.on_message("market_batch", dumps(batch(1, 0, books=[book(sequence, next_expiry=iso(-1))])))
    return t


def _rest(sequence: int | None) -> Any:
    return CombinedOrderbookExchange.model_validate(
        {
            **_orderbook(sequence)["contexts"][0]["orderbook"]["exchanges"][0],
            "impliedProbability": None,
            "bids": [],
            "asks": [],
        }
    )


@pytest.mark.parametrize(("rest_sequence", "wins"), [(8, False), (9, True), (10, True), (None, True)])
def test_expired_held_book_yields_to_equal_or_newer_rest(rest_sequence: Any, wins: bool) -> None:
    t = _topic_with_expired_push(9)
    now = epoch_seconds(T0)
    # Requested after the expiry (T0 - 1 s) plus the margin, so an unversioned read covers it.
    changed = t.apply_snapshot(
        [_rest(rest_sequence)],
        depth=200,
        started_at=t.stamp(),
        now_epoch=now,
        requested_epoch=now,
        skew_margin=Decimal("0.5"),
    )
    assert bool(changed) is wins
    assert t.has_expired_book(now) is (not wins)


def test_unexpired_held_book_keeps_the_strictly_newer_rule() -> None:
    t = _topic_with_expired_push(9)
    before = epoch_seconds(T0 - timedelta(seconds=10))  # expiry not yet reached
    assert t.apply_snapshot([_rest(9)], depth=200, started_at=0, now_epoch=before) == []


def test_null_sequence_with_settlement_reports_both_blockers() -> None:
    t = MarketTopic(TOURNAMENT.id, MARKET)
    out = t.on_message(
        "market_batch",
        dumps(batch(1, 0, trades=[trade(None, None)], books=[book(5)], market_settled=[settled()])),
    )
    assert out.blockers == {StaleReason.NULL_TRADE_SEQUENCE, StaleReason.MARKET_SETTLED}
    assert out.trades == [] and [s.settledWith for s in out.settlements] == ["YES"]


def test_settlement_survives_a_batch_missing_a_named_book() -> None:
    t = MarketTopic(TOURNAMENT.id, MARKET)
    out = t.on_message(
        "market_batch",
        dumps(batch(1, 0, trades=[trade("t", 5, exchange=37)], books=[book(5)],
                    market_settled=[settled()])),
    )
    assert out.resync is StaleReason.MISSING_BOOK and out.trades == []
    assert out.blockers == {StaleReason.MARKET_SETTLED}
    assert [s.settledWith for s in out.settlements] == ["YES"]
