"""RealtimeMarketFeed: lifecycle, recovery paths and readiness, offline (S1-T3-A).

REST goes through httpx.MockTransport (synthetic fixtures, plus one recorded capture);
the socket is FakeTransport; time is ManualClock. All realtime payloads are synthetic.
"""

from __future__ import annotations

import asyncio
import copy
from datetime import timedelta
from decimal import Decimal
from typing import Any

import httpx
import pytest
from realtime_kit import (
    MARKET,
    T0,
    TOKEN_VALUE,
    Harness,
    batch,
    book,
    settle,
    token_body,
    trade,
)
from recorded_kit import RECORDED, Replay
from recorded_kit import TOURNAMENT as RECORDED_TOURNAMENT
from sig_testkit import TOURNAMENT, error_body, fixture, json_response, secret_leaks

from engine.sig_client.errors import SigAuthError
from engine.sig_client.rate_limiter import RateLimiter, RequestKind
from engine.sig_client.realtime import (
    BookUpdated,
    BroadcastEvent,
    ChannelStatus,
    ChannelStatusEvent,
    MarketReady,
    MarketStale,
    RealtimeFeedError,
    RealtimeFeedPolicy,
    RealtimeMarketFeed,
    SettlementSignal,
    SocketEvent,
    SocketState,
    TradeReceived,
)
from engine.sig_client.realtime_topic import BookSource, StaleReason

ORDERBOOK = f"/markets/{MARKET}/orderbook"
EX = "36"
DIRTY = {"exchangeId": 36, "marketId": MARKET, "tournamentId": TOURNAMENT.id, "at": "x"}


def orderbook(sequence: int | None, at: str = "2026-10-10T12:00:00.5+00:00", bid: str = "0.41"):
    body = copy.deepcopy(fixture("market_orderbook"))
    exchange = body["contexts"][0]["orderbook"]["exchanges"][0]
    exchange["asOf"] = None if sequence is None else {"sequence": sequence, "at": at}
    exchange["bids"] = [{"price": float(bid), "quantity": 300}]
    return json_response(200, body)


def ready_events(h: Harness) -> list[Any]:
    return [e for e in h.events if isinstance(e, (MarketReady, MarketStale))]


def stale_reasons(h: Harness) -> list[StaleReason]:
    return [e.reason for e in h.events if isinstance(e, MarketStale)]


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


# --- start-up and initial subscription ------------------------------------------------------


def test_startup_mints_connects_and_subscribes_the_tournament_topic() -> None:
    async def scenario(h: Harness) -> None:
        await h.start(subscribe=False)
        topic = f"tournament:{TOURNAMENT.id}:market:{MARKET}"
        assert h.transport.calls == [
            ("connect", "https://realtime.invalid"),
            ("subscribe", topic),
        ]
        assert h.transport.tokens == [TOKEN_VALUE]
        assert h.router.paths()[:2] == ["/tournaments/alpha-tournament", "/realtime/token"]
        # No resync before the channel is confirmed; not ready.
        assert h.count(ORDERBOOK) == 0 and not h.feed.is_ready(MARKET)
        assert h.limiter.used(RequestKind.WRITE) == 1  # one mint, on the shared budget
        await h.stop()

    run(scenario)


def test_initial_subscription_resyncs_at_full_depth_then_ready() -> None:
    async def scenario(h: Harness) -> None:
        await h.start()
        (request,) = [r for r in h.router.requests if r.url.path.endswith(ORDERBOOK)]
        assert dict(request.url.params) == {"depth": "200", "tournamentId": TOURNAMENT.id}
        assert h.feed.is_ready(MARKET)
        held = h.feed.books(MARKET)[EX]
        assert held.source is BookSource.REST and held.as_of is None
        assert [str(level.price) for level in held.bids] == ["0.41"]
        assert held.complete and not held.expiry_known
        assert [type(e).__name__ for e in h.events] == ["BookUpdated", "MarketReady"]
        await h.stop()

    run(scenario)


# --- the stage's done-when: a missed batch triggers a resync ----------------------------------


def test_missed_batch_triggers_a_rest_resync() -> None:
    async def scenario(h: Harness) -> None:
        await h.start()
        topic = h.topic()
        h.transport.batch(topic, batch(1, 0, trades=[trade("t1", 11)], books=[book(11)]))
        h.transport.batch(topic, batch(2, 1, trades=[trade("t2", 12)], books=[book(12)]))
        await settle()
        assert h.count(ORDERBOOK) == 1 and h.feed.is_ready(MARKET)
        # Revision 3 is never delivered.
        h.transport.batch(topic, batch(4, 3, trades=[trade("t4", 14)], books=[book(14)]))
        await settle()
        assert not h.feed.is_ready(MARKET)
        assert stale_reasons(h) == [StaleReason.REVISION_GAP]
        await h.clock.advance(2.0)  # min_resync_interval_s
        assert h.count(ORDERBOOK) == 2  # the resync ran and refreshed the books ...
        # ... but a book read cannot restore market-state or trade continuity (S1-T3-A-F2).
        assert not h.feed.is_ready(MARKET)
        assert h.feed.blockers(MARKET) == {StaleReason.REVISION_GAP}
        assert h.feed.last_revision(MARKET) == 4
        trades = [e.trade.id for e in h.events if isinstance(e, TradeReceived)]
        assert trades == ["t1", "t2"]  # nothing from the gap batch
        await h.stop()

    run(scenario)


def test_duplicates_and_overlaps_cost_no_rest_reads() -> None:
    async def scenario(h: Harness) -> None:
        await h.start()
        topic = h.topic()
        h.transport.batch(topic, batch(2, 0, trades=[trade("a", 1), trade("b", 2)], books=[book(2)]))
        h.transport.batch(topic, batch(2, 0, trades=[trade("a", 1), trade("b", 2)], books=[book(2)]))
        h.transport.batch(topic, batch(3, 1, trades=[trade("b", 2), trade("c", 3)], books=[book(3)]))
        await settle()
        assert h.count(ORDERBOOK) == 1 and h.feed.is_ready(MARKET)
        assert [e.trade.id for e in h.events if isinstance(e, TradeReceived)] == ["a", "b", "c"]
        await h.stop()

    run(scenario)


@pytest.mark.parametrize(
    ("payload", "reason"),
    [
        ({"resyncRequired": True, "delivery": {"revision": 9, "previousRevision": 8}},
         StaleReason.RESYNC_REQUIRED),
        # A null-sequence trade also resyncs, but it blocks readiness: test_realtime_f1.py.
        ({"delivery": "broken"}, StaleReason.MALFORMED),
        (batch(9, 8, book_dirty=[{"exchangeId": 36, "marketId": "26",
                                  "tournamentId": TOURNAMENT.id, "at": "x"}]),
         StaleReason.MISSING_BOOK),
    ],
)
def test_batch_level_triggers_resync(payload: Any, reason: StaleReason) -> None:
    async def scenario(h: Harness) -> None:
        await h.start()
        h.transport.batch(h.topic(), payload)
        await settle()
        assert stale_reasons(h) == [reason] and not h.feed.is_ready(MARKET)
        await h.clock.advance(2.0)
        # The books are refreshed, but these messages may have hidden events (F2).
        assert h.count(ORDERBOOK) == 2 and not h.feed.is_ready(MARKET)
        assert h.feed.blockers(MARKET) == {reason}
        await h.stop()

    run(scenario)


def test_settlement_is_signalled_and_resynced() -> None:
    async def scenario(h: Harness) -> None:
        await h.start()
        h.transport.batch(
            h.topic(),
            {"marketId": MARKET, "tournamentId": TOURNAMENT.id, "settledWith": "YES", "at": "x"},
            event="market_settled",
        )
        await settle()
        assert [e.settlement.settledWith for e in h.events if isinstance(e, SettlementSignal)] == [
            "YES"
        ]
        assert stale_reasons(h) == [StaleReason.MARKET_SETTLED]
        await h.stop()

    run(scenario)


# --- reconnect, channel errors, socket errors --------------------------------------------------


def test_socket_error_waits_for_resubscription_then_resyncs() -> None:
    async def scenario(h: Harness) -> None:
        await h.start()
        h.transport.batch(h.topic(), batch(1, 0))  # revision 1 accepted: the fence
        await settle()
        h.transport.push(SocketEvent(SocketState.ERROR))
        await settle()
        assert stale_reasons(h) == [StaleReason.SOCKET_ERROR]
        await h.clock.advance(30.0)
        assert h.count(ORDERBOOK) == 1  # nothing to resync onto while disconnected
        h.transport.subscribed(h.topic())  # the transport re-joined after reconnecting
        await settle()
        assert h.count(ORDERBOOK) == 2
        # M2-A (D-1 undecided): the books are back, but the unobserved window stays open
        # until a clean batch advances past its fence.
        assert not h.feed.is_ready(MARKET)
        h.transport.batch(h.topic(), batch(2, 1))
        await h.clock.advance(2.0)
        assert h.count(ORDERBOOK) == 3 and h.feed.is_ready(MARKET)
        await h.stop()

    run(scenario)


@pytest.mark.parametrize(
    ("status", "reason"),
    [
        (ChannelStatus.CHANNEL_ERROR, StaleReason.CHANNEL_ERROR),
        (ChannelStatus.TIMED_OUT, StaleReason.CHANNEL_TIMED_OUT),
        (ChannelStatus.CLOSED, StaleReason.CHANNEL_CLOSED),
    ],
)
def test_channel_errors_mark_stale_until_rejoined(status: ChannelStatus, reason: StaleReason) -> None:
    async def scenario(h: Harness) -> None:
        await h.start()
        h.transport.batch(h.topic(), batch(1, 0))  # revision 1 accepted: the fence
        await settle()
        h.transport.push(ChannelStatusEvent(h.topic(), status))
        await settle()
        assert stale_reasons(h) == [reason] and not h.feed.is_ready(MARKET)
        h.transport.subscribed(h.topic())
        await h.clock.advance(2.0)
        assert h.count(ORDERBOOK) == 2
        assert not h.feed.is_ready(MARKET)  # the window is open until a clean batch
        h.transport.batch(h.topic(), batch(2, 1))
        await h.clock.advance(2.0)
        assert h.feed.is_ready(MARKET) and h.count(ORDERBOOK) == 3
        await h.stop()

    run(scenario)


def test_events_for_unknown_topics_are_ignored() -> None:
    async def scenario(h: Harness) -> None:
        await h.start()
        other = f"tournament:{TOURNAMENT.id}:market:999"
        h.transport.push(ChannelStatusEvent(other, ChannelStatus.CHANNEL_ERROR))
        h.transport.push(BroadcastEvent(f"market:{MARKET}", "market_batch", "{}"))
        await settle()
        assert h.feed.is_ready(MARKET) and stale_reasons(h) == []
        await h.stop()

    run(scenario)


# --- token refresh and expiry ----------------------------------------------------------------


def second_token(expires_hours: float = 6.0, **kwargs: Any) -> httpx.Response:
    return json_response(
        200,
        token_body(token="rt-sentinel-token-2", expires=T0 + timedelta(hours=expires_hours), **kwargs),
    )


def test_token_refresh_sets_auth_and_resyncs() -> None:
    async def scenario(h: Harness) -> None:
        await h.start()
        h.transport.batch(h.topic(), batch(1, 0))  # revision 1 accepted: the fence
        await settle()
        await h.clock.advance(3 * 3600 - 601)
        assert h.count("/realtime/token") == 1 and h.feed.is_ready(MARKET)
        await h.clock.advance(60)  # past the refresh point: 10 minutes before expiry
        assert h.count("/realtime/token") == 2
        assert h.transport.calls[-1] == ("set_auth", "")
        assert h.transport.tokens == [TOKEN_VALUE, "rt-sentinel-token-2"]
        assert StaleReason.TOKEN_REFRESH in stale_reasons(h)
        assert h.count(ORDERBOOK) == 2
        assert h.limiter.used(RequestKind.WRITE) == 1  # the first mint left the window
        assert not h.feed.is_ready(MARKET)  # the refresh opened an unobserved window
        h.transport.batch(h.topic(), batch(2, 1))
        await h.clock.advance(2.0)
        assert h.count(ORDERBOOK) == 3 and h.feed.is_ready(MARKET)
        await h.stop()

    run(
        scenario,
        policy=RealtimeFeedPolicy(tick_s=60.0),
        token_responses=[json_response(200, token_body()), second_token()],
    )


def test_failed_refresh_lets_the_token_expire_then_recovers() -> None:
    unavailable = json_response(503, error_body("SERVICE_UNAVAILABLE"))

    async def scenario(h: Harness) -> None:
        # Minted at t=1000 for 3 h: refresh due at 11200, expiry at 11800. Mints fail at
        # 11200, 11320 and 11560 (backoff 120, 240, 480) and succeed at 12040.
        await h.start()
        h.transport.batch(h.topic(), batch(1, 0))  # revision 1 accepted: the fence
        await settle()
        await h.clock.advance(3 * 3600 - 540)  # now 11260
        assert h.count("/realtime/token") == 2 and h.feed.is_ready(MARKET)
        reads = h.count(ORDERBOOK)
        await h.clock.advance(600)  # now 11860, past expiresAt
        assert h.count("/realtime/token") == 4
        assert StaleReason.TOKEN_EXPIRED in stale_reasons(h)
        assert not h.feed.is_ready(MARKET)
        assert h.count(ORDERBOOK) == reads  # no resync is spent without a valid token
        await h.clock.advance(300)  # now 12160
        assert h.count("/realtime/token") == 5
        assert h.transport.tokens[-1] == "rt-sentinel-token-2"
        assert h.count(ORDERBOOK) == reads + 1
        assert not h.feed.is_ready(MARKET)  # expiry and refresh left a window open
        h.transport.batch(h.topic(), batch(2, 1))
        await h.clock.advance(2.0)
        assert h.feed.is_ready(MARKET) and h.count(ORDERBOOK) == reads + 2
        await h.stop()

    run(
        scenario,
        policy=RealtimeFeedPolicy(tick_s=30.0, mint_retry_s=120.0, max_retry_s=600.0),
        token_responses=[
            json_response(200, token_body()),
            unavailable,
            unavailable,
            unavailable,
            second_token(expires_hours=8),
        ],
    )


def test_endpoint_change_on_refresh_stops_the_feed() -> None:
    async def scenario(h: Harness) -> None:
        await h.start()
        await h.clock.advance(3 * 3600 - 540)
        with pytest.raises(RealtimeFeedError):
            await h.task
        assert h.transport.closed and not h.feed.is_ready(MARKET)

    run(
        scenario,
        policy=RealtimeFeedPolicy(tick_s=60.0),
        token_responses=[
            json_response(200, token_body()),
            second_token(url="https://elsewhere.invalid"),
        ],
    )


def test_expired_token_at_mint_is_retried_not_used() -> None:
    async def scenario(h: Harness) -> None:
        h.task = asyncio.create_task(h.feed.run())
        await settle()
        assert h.transport.calls == []  # never connected with an expired token
        await h.clock.advance(60)
        assert h.transport.calls[0][0] == "connect"
        await h.stop()

    run(
        scenario,
        token_responses=[
            json_response(200, token_body(expires=T0 - timedelta(seconds=1))),
            json_response(200, token_body()),
        ],
    )


def test_permanent_mint_failure_stops_the_feed() -> None:
    async def scenario(h: Harness) -> None:
        h.task = asyncio.create_task(h.feed.run())
        with pytest.raises(SigAuthError) as caught:
            await h.task
        assert h.transport.closed
        assert secret_leaks(caught.value, "dummy-sentinel-not-a-real-key") == []

    run(scenario, token_responses=[json_response(401, error_body("INVALID_API_KEY"))])


# --- resync scheduling ---------------------------------------------------------------------------


def test_trigger_during_resync_discards_readiness_and_reruns() -> None:
    gate = {"release": None}

    async def slow_orderbook(request: httpx.Request) -> httpx.Response:
        event = asyncio.Event()
        gate["release"] = event
        await event.wait()
        return orderbook(None)

    async def scenario(h: Harness) -> None:
        h.router.routes[f"/api/v1{ORDERBOOK}"] = slow_orderbook
        await h.start()
        assert h.count(ORDERBOOK) == 1 and not h.feed.is_ready(MARKET)
        topic = h.topic()
        h.transport.batch(topic, batch(1, 0, books=[book(50)]))
        h.transport.batch(topic, batch(2, 1, books=[book(60)]))
        # Two non-blocking triggers while the read is in flight.
        h.transport.batch(topic, DIRTY, event="book_dirty")
        h.transport.subscribed(topic)
        # The re-join opened a window (fence 2); a clean batch past it confirms it.
        h.transport.batch(topic, batch(3, 2, books=[book(60)]))
        await settle()
        gate["release"].set()
        await settle()
        # The snapshot was requested before the triggers, so it cannot make the market
        # ready, and the null-asOf REST book does not replace the book pushed meanwhile.
        assert not h.feed.is_ready(MARKET)
        assert h.feed.books(MARKET)[EX].as_of.sequence == 60
        await h.clock.advance(2.0)
        assert h.count(ORDERBOOK) == 2  # two triggers, one follow-up
        gate["release"].set()
        await settle()
        assert h.feed.is_ready(MARKET)
        assert [type(e).__name__ for e in ready_events(h)] == ["MarketReady"]
        await h.stop()

    run(scenario)


def test_stale_versioned_snapshot_does_not_replace_a_newer_push() -> None:
    async def scenario(h: Harness) -> None:
        h.router.routes[f"/api/v1{ORDERBOOK}"] = [orderbook(50), orderbook(80, bid="0.30")]
        await h.start()
        assert h.feed.books(MARKET)[EX].as_of.sequence == 50
        h.transport.batch(h.topic(), batch(2, 0, books=[book(90)]))
        h.transport.batch(h.topic(), batch(3, 2, books=[book(95)]))
        h.transport.batch(h.topic(), DIRTY, event="book_dirty")  # a non-blocking trigger
        await h.clock.advance(2.0)
        assert h.count(ORDERBOOK) == 2 and h.feed.is_ready(MARKET)
        held = h.feed.books(MARKET)[EX]
        assert (held.source, held.as_of.sequence) == (BookSource.PUSH, 95)
        await h.stop()

    run(scenario)


def test_transient_rest_failure_backs_off_then_recovers() -> None:
    async def scenario(h: Harness) -> None:
        h.router.routes[f"/api/v1{ORDERBOOK}"] = [
            json_response(500, error_body("INTERNAL_ERROR")),
            json_response(500, error_body("INTERNAL_ERROR")),
            orderbook(7),
        ]
        await h.start()
        assert h.count(ORDERBOOK) == 1 and not h.feed.is_ready(MARKET)
        await h.clock.advance(2.0)  # first backoff
        assert h.count(ORDERBOOK) == 2
        await h.clock.advance(3.9)  # second backoff is 4 s
        assert h.count(ORDERBOOK) == 2
        await h.clock.advance(0.1)
        assert h.count(ORDERBOOK) == 3 and h.feed.is_ready(MARKET)
        await h.stop()

    run(scenario)


def test_permanent_rest_failure_stops_the_feed() -> None:
    async def scenario(h: Harness) -> None:
        h.router.routes[f"/api/v1{ORDERBOOK}"] = json_response(403, error_body("INSUFFICIENT_SCOPES"))
        await h.start()
        with pytest.raises(SigAuthError):
            await h.task
        assert h.transport.closed

    run(scenario)


def test_resync_budget_caps_reads_across_markets() -> None:
    markets = ("26", "27", "28")

    async def scenario(h: Harness) -> None:
        for market in markets[1:]:
            body = copy.deepcopy(fixture("market_orderbook"))
            h.router.routes[f"/api/v1/markets/{market}/orderbook"] = json_response(200, body)
        await h.start()
        assert sum(h.count(f"/markets/{m}/orderbook") for m in markets) == 2
        await h.clock.advance(59.0)
        assert sum(h.count(f"/markets/{m}/orderbook") for m in markets) == 2
        await h.clock.advance(1.01)  # the rolling window frees a slot after 60 s
        assert sum(h.count(f"/markets/{m}/orderbook") for m in markets) == 3
        assert all(h.feed.is_ready(m) for m in markets)
        await h.stop()

    run(scenario, market_ids=markets, policy=RealtimeFeedPolicy(resync_reads_per_minute=2))


def test_book_expiry_triggers_a_refetch() -> None:
    async def scenario(h: Harness) -> None:
        await h.start()
        expiry = (T0 + timedelta(seconds=30)).strftime("%Y-%m-%dT%H:%M:%SZ")
        # SIG reads the book half a second after the expiry, by its own clock.
        h.router.routes[f"/api/v1{ORDERBOOK}"] = orderbook(9, at="2026-10-10T12:00:30.5+00:00")
        h.transport.batch(h.topic(), batch(1, 0, books=[book(9, next_expiry=expiry)]))
        await h.clock.advance(29.0)
        assert h.count(ORDERBOOK) == 1
        await h.clock.advance(2.0)
        assert StaleReason.BOOK_EXPIRY in stale_reasons(h)
        assert h.count(ORDERBOOK) == 2 and h.feed.is_ready(MARKET)
        await h.stop()

    run(scenario)


# --- shutdown and cancellation -----------------------------------------------------------------


def test_aclose_stops_cleanly_and_marks_markets_stale() -> None:
    async def scenario(h: Harness) -> None:
        await h.start()
        await h.stop()
        assert h.task.done() and h.task.exception() is None
        assert stale_reasons(h) == [StaleReason.STOPPED]
        assert h.transport.closed and not h.feed.is_ready(MARKET)

    run(scenario)


def test_cancellation_during_resync_applies_nothing_and_closes() -> None:
    started = asyncio.Event()

    async def hanging_orderbook(request: httpx.Request) -> httpx.Response:
        started.set()
        await asyncio.Event().wait()
        raise AssertionError

    async def scenario(h: Harness) -> None:
        h.router.routes[f"/api/v1{ORDERBOOK}"] = hanging_orderbook
        await h.start()
        await started.wait()
        h.task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await h.task
        assert h.transport.closed
        assert not h.feed.is_ready(MARKET)
        assert not any(isinstance(e, BookUpdated) for e in h.events)

    run(scenario)


def test_run_is_single_use() -> None:
    async def scenario(h: Harness) -> None:
        await h.start()
        with pytest.raises(RuntimeError):
            await h.feed.run()
        await h.stop()

    run(scenario)


# --- construction ------------------------------------------------------------------------------


def test_feed_requires_one_shared_limiter_and_valid_market_ids() -> None:
    h = Harness()
    other = Harness()
    with pytest.raises(ValueError):
        RealtimeMarketFeed(h.rest, other.tokens, h.transport, [MARKET])
    for bad in ([], ["26", "26"], ["2a"], "26", [26]):
        with pytest.raises(ValueError):
            RealtimeMarketFeed(h.rest, h.tokens, h.transport, bad)  # type: ignore[arg-type]
    with pytest.raises(ValueError):
        RealtimeFeedPolicy(snapshot_depth=201)
    with pytest.raises(ValueError):
        RealtimeFeedPolicy(refresh_margin_s=3 * 3600)
    assert isinstance(h.limiter, RateLimiter)


# --- recorded REST fixtures --------------------------------------------------------------------


def test_resync_from_the_recorded_capture_keeps_values_exact() -> None:
    """Capture fb88c9 recorded market 258 at depth 5, so the feed asks at depth 5 here.

    The REST bodies are the recorded ones, served only for their exact recorded query.
    Five levels on each side at depth 5 means more may exist: the book is not complete.
    """
    replay = Replay(RECORDED / "20261009T190027Z-fb88c9")
    tournament_id = RECORDED_TOURNAMENT.id

    # The lifecycle read (M2-A) is served the market detail recorded for 258 in capture
    # 12ab20, byte for byte, and only for its exact tournament-scoped query.
    detail = (RECORDED / "20261011T211201Z-12ab20/bodies/06-market-detail-258-a1.json")

    def route(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/realtime/token"):
            return json_response(200, token_body())
        if request.url.path == "/api/v1/markets/258":
            assert dict(request.url.params) == {"tournamentId": tournament_id}
            return httpx.Response(
                200, content=detail.read_bytes(), headers={"content-type": "application/json"}
            )
        return replay(request)

    async def scenario(h: Harness) -> None:
        await h.start()
        assert replay.unexpected == []
        assert replay.sent() == [
            ("/tournaments/midterm-elections", {}),
            ("/markets/258/orderbook", {"depth": "5", "tournamentId": tournament_id}),
        ]
        lifecycle = h.feed.readiness("258").lifecycle
        assert lifecycle is not None and lifecycle.current
        assert (lifecycle.status, lifecycle.settled_with) == ("open", None)
        assert h.feed.topic("258") == f"tournament:{tournament_id}:market:258"
        assert h.feed.is_ready("258")
        held = h.feed.books("258")["947"]
        assert held.as_of.sequence == 112608401
        assert held.as_of.at == "2026-10-09T19:00:51.1493955+00:00"
        assert [(str(lv.price), str(lv.quantity)) for lv in held.bids][:2] == [
            ("0.94", "16314"),
            ("0.935", "6348"),
        ]
        assert all(type(lv.price) is Decimal for lv in held.bids + held.asks)
        assert held.complete is False
        # A later push for exchange 947 with a newer engine version replaces it.
        newer = book(
            112608402,
            "2026-10-09T19:00:52Z",
            exchange=947,
            bids=[("0.94", 1)],
            asks=[("0.945", 1)],
        )
        named = {"exchangeId": 947, "marketId": "258", "tournamentId": tournament_id, "at": "x"}
        h.transport.batch(h.topic("258"), batch(1, 0, books=[newer], book_dirty=[named]))
        await settle()
        held = h.feed.books("258")["947"]
        assert (held.source, held.complete, held.as_of.sequence) == (
            BookSource.PUSH,
            True,
            112608402,
        )
        await h.stop()

    run(
        scenario,
        router=_CallableRouter(route),
        market_ids=("258",),
        tournament=RECORDED_TOURNAMENT,
        policy=RealtimeFeedPolicy(snapshot_depth=5),
    )


class _CallableRouter:
    """A Router stand-in that forwards every request to one function."""

    def __init__(self, handler: Any) -> None:
        self.handler = handler
        self.requests: list[httpx.Request] = []
        self.routes: dict[str, Any] = {}

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        return self.handler(request)

    def count(self, path: str) -> int:
        return sum(1 for r in self.requests if r.url.path.endswith(path))
