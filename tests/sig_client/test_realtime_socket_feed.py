"""RealtimeSocketTransport under the real feed, and over a real local WebSocket (M3).

The feed tests drive RealtimeMarketFeed through the adapter and a scripted raw-frame
server (socket_kit), with REST on httpx.MockTransport and one ManualClock for both. The
last tests run websockets 15.0.1 end to end against a server on 127.0.0.1 (pre-bound
sockets, no name resolution, no external network). Every frame is SYNTHETIC.
"""

from __future__ import annotations

import asyncio
import json
import logging
import socket
from datetime import timedelta
from decimal import Decimal
from typing import Any
from urllib.parse import parse_qs, urlsplit

import pytest
from realtime_kit import (
    ANON_VALUE,
    MARKET,
    T0,
    TOKEN_VALUE,
    Harness,
    ManualClock,
    batch,
    book,
    dumps,
    token_body,
)
from sig_testkit import json_response
from socket_kit import (
    ENDPOINT,
    NEW_TOKEN_VALUE,
    TOKEN,
    TOPIC,
    FakeConnection,
    FakeServer,
    own_tasks,
    settle,
)
from websockets.asyncio.server import ServerConnection, serve

from engine.sig_client.realtime import (
    BookUpdated,
    BroadcastEvent,
    ChannelStatus,
    ChannelStatusEvent,
    MarketReady,
    MarketStale,
    RealtimeFeedPolicy,
    SocketEvent,
    SocketState,
)
from engine.sig_client.realtime_socket import (
    RealtimeSocketPolicy,
    RealtimeSocketTransport,
    connect_websocket,
)
from engine.sig_client.realtime_topic import StaleReason

ORDERBOOK = f"/markets/{MARKET}/orderbook"
LONG = "0.123456789012345678901234567890123"  # changed by a binary float
QUIET = RealtimeSocketPolicy(heartbeat_interval_s=1e6)


async def until(condition: Any, rounds: int = 40) -> None:
    for _ in range(rounds):
        if condition():
            return
        await settle()
    assert condition()


class FeedRig:
    """Harness whose transport is the adapter on a FakeServer, sharing its ManualClock."""

    def __init__(self, **kwargs: Any) -> None:
        clock = ManualClock()
        self.server = FakeServer(clock)
        self.transport = RealtimeSocketTransport(
            policy=QUIET, connector=self.server.connect, clock=clock, sleep=clock.sleep
        )
        self.h = Harness(clock=clock, transport=self.transport, **kwargs)  # type: ignore[arg-type]

    async def start(self) -> None:
        await self.h.start(subscribe=False)
        await until(lambda: self.server.connections and self.server.conn.joins())

    def stale(self) -> list[StaleReason]:
        return [e.reason for e in self.h.events if isinstance(e, MarketStale)]


def feed_run(scenario: Any, **kwargs: Any) -> FeedRig:
    async def main() -> FeedRig:
        rig = FeedRig(**kwargs)
        baseline = set(asyncio.all_tasks())
        try:
            await scenario(rig)
        finally:
            if rig.h.task is not None and not rig.h.task.done():
                await rig.h.stop()
        await settle()
        assert own_tasks(baseline) == set()
        assert all(c.closed for c in rig.server.connections)
        return rig

    return asyncio.run(asyncio.wait_for(main(), 30.0))


def market_batch(revision: int, previous: int, ask: str) -> str:
    """A clean batch whose pushed book has one ask at `ask`, written digit for digit."""
    pushed = book(revision + 10, asks=[(ask, 600)], bids=[("0.11", 300)])
    return dumps(batch(revision, previous, books=[pushed]))


def test_feed_waits_for_the_ack_then_resyncs_and_keeps_exact_prices() -> None:
    async def scenario(rig: FeedRig) -> None:
        h = rig.h
        await rig.start()
        conn = rig.server.conn
        (join,) = conn.joins()
        assert join["payload"]["access_token"] == TOKEN_VALUE  # the minted token
        await settle()
        assert h.count(ORDERBOOK) == 0 and not h.feed.is_ready(MARKET)  # sent, not acked
        conn.ack()
        await until(lambda: h.feed.is_ready(MARKET))
        assert h.count(ORDERBOOK) == 1
        conn.broadcast(TOPIC, "market_batch", market_batch(1, 0, LONG))
        await until(lambda: any(isinstance(e, BookUpdated) for e in h.events[2:]))
        held = h.feed.books(MARKET)["36"]
        (ask,) = held.asks
        assert type(ask.price) is Decimal and str(ask.price) == LONG
        assert h.feed.last_revision(MARKET) == 1 and h.feed.is_ready(MARKET)

    feed_run(scenario)


def test_socket_loss_invalidates_first_then_rejoin_and_resync() -> None:
    async def scenario(rig: FeedRig) -> None:
        h = rig.h
        await rig.start()
        conn = rig.server.conn
        conn.ack()
        await until(lambda: h.feed.is_ready(MARKET))
        conn.broadcast(TOPIC, "market_batch", market_batch(1, 0, "0.55"))  # the fence
        await until(lambda: h.feed.last_revision(MARKET) == 1)
        conn.lose()
        await until(lambda: not h.feed.is_ready(MARKET))
        assert rig.stale() == [StaleReason.SOCKET_ERROR]
        reads = h.count(ORDERBOOK)
        await h.clock.advance(1.0)  # reconnect backoff
        fresh = rig.server.conn
        assert fresh is not conn and len(fresh.joins()) == 1
        await h.clock.advance(5.0)
        assert h.count(ORDERBOOK) == reads  # no resync before the rejoin is acknowledged
        assert not h.feed.is_ready(MARKET)
        fresh.ack()
        await until(lambda: h.count(ORDERBOOK) == reads + 1)
        # The socket break opened an unobserved window: the resync alone is not enough.
        assert not h.feed.is_ready(MARKET)
        assert any("unobserved window" in r for r in h.feed.readiness(MARKET).reasons)
        fresh.broadcast(TOPIC, "market_batch", market_batch(2, 1, "0.56"))
        await h.clock.advance(2.0)
        await until(lambda: h.feed.is_ready(MARKET))
        assert [type(e).__name__ for e in h.events].count("MarketReady") == 2

    feed_run(scenario)


@pytest.mark.parametrize("cleanup", ["released", "timed_out", "ignores_cancellation"])
def test_socket_loss_withdraws_readiness_while_cleanup_is_held(cleanup: str) -> None:
    """Review of 67aac28: the loss must reach the feed before raw cleanup finishes."""

    async def scenario(rig: FeedRig) -> None:
        h = rig.h
        await rig.start()
        conn = rig.server.conn
        conn.ack()
        await until(lambda: h.feed.is_ready(MARKET))
        conn.broadcast(TOPIC, "market_batch", market_batch(1, 0, "0.55"))  # the fence
        await until(lambda: h.feed.last_revision(MARKET) == 1)
        assert h.feed.is_ready(MARKET)
        conn.close_gate = asyncio.Event()  # the old connection's cleanup is held
        conn.close_ignores_cancel = cleanup == "ignores_cancellation"
        conn.lose()
        await until(lambda: not h.feed.is_ready(MARKET))
        # Readiness is withdrawn while cleanup is still held, at the same instant.
        assert not conn.closed and h.clock.now == 1000.0
        assert rig.stale() == [StaleReason.SOCKET_ERROR]
        assert not h.feed.readiness(MARKET).transport.subscribed
        await h.clock.advance(1.0)  # the reconnect backoff has passed; cleanup has not
        assert len(rig.server.connections) == 1 and not h.feed.is_ready(MARKET)
        if cleanup == "released":
            conn.close_gate.set()
            await settle()
            assert conn.closed and not conn.aborted
        else:
            await h.clock.advance(4.0)  # close_timeout_s: the connection is aborted
            assert conn.aborted
            if cleanup == "ignores_cancellation":
                assert len(rig.server.connections) == 1
                await h.clock.advance(1.0)  # cancel_grace_s, then no longer waited for
        await until(lambda: len(rig.server.connections) == 2)
        fresh = rig.server.conn
        assert len(fresh.joins()) == 1
        reads = h.count(ORDERBOOK)
        await h.clock.advance(3.0)
        assert h.count(ORDERBOOK) == reads and not h.feed.is_ready(MARKET)  # needs the ack
        fresh.ack()
        await until(lambda: h.count(ORDERBOOK) == reads + 1)  # the required resync
        assert not h.feed.is_ready(MARKET)  # the window still needs a clean batch
        fresh.broadcast(TOPIC, "market_batch", market_batch(2, 1, "0.56"))
        await h.clock.advance(2.0)
        await until(lambda: h.feed.is_ready(MARKET))
        conn.close_gate.set()  # lets an abandoned cleanup end before the task check

    feed_run(scenario)


def hold_consumer(rig: FeedRig) -> asyncio.Event:
    """Gate the feed's next receive() calls (clear the event to hold the feed)."""
    gate = asyncio.Event()
    gate.set()
    receive = rig.transport.receive

    async def held() -> Any:
        await gate.wait()
        return await receive()

    rig.transport.receive = held  # type: ignore[method-assign]
    return gate


async def ready_with_backlog(rig: FeedRig) -> tuple[FakeConnection, asyncio.Event]:
    """A ready market (revision 1 accepted), then a held feed with batches 2 and 3 queued."""
    h = rig.h
    await rig.start()
    conn = rig.server.conn
    conn.ack()
    await until(lambda: h.feed.is_ready(MARKET))
    conn.broadcast(TOPIC, "market_batch", market_batch(1, 0, "0.55"))  # the fence
    await until(lambda: h.feed.last_revision(MARKET) == 1)
    gate = hold_consumer(rig)
    gate.clear()
    conn.broadcast(TOPIC, "noop", "{}")  # answers the receive() already in flight
    await until(lambda: not rig.transport._events)
    conn.broadcast(TOPIC, "market_batch", market_batch(2, 1, "0.57"))
    conn.broadcast(TOPIC, "market_batch", market_batch(3, 2, "0.58"))
    await until(lambda: len(rig.transport._events) == 2)
    assert h.feed.is_ready(MARKET)  # nothing detected yet
    return conn, gate


@pytest.mark.parametrize("evidence", ["contiguous", "gap"])
def test_socket_loss_jumps_a_backlog_for_a_delayed_consumer(evidence: str) -> None:
    """Review of 54032ec: the loss must not wait behind the old socket's queued events."""

    async def scenario(rig: FeedRig) -> None:
        h = rig.h
        conn, gate = await ready_with_backlog(rig)
        conn.lose()
        await until(lambda: list(rig.transport._events) == [SocketEvent(SocketState.ERROR)])
        seen, observed = len(h.events), len(h.observed)
        gate.set()  # the consumer resumes
        await until(lambda: not h.feed.is_ready(MARKET))
        assert h.events[seen] == MarketStale(MARKET, StaleReason.SOCKET_ERROR)  # first
        # The old socket's batches were never applied or published.
        assert h.feed.last_revision(MARKET) == 1
        assert not any(isinstance(e, BookUpdated) for e in h.events[seen:])
        assert all(not ready for _, ready in h.observed[observed:])
        # The next join still needs its ack and a REST resync.
        await h.clock.advance(1.0)
        fresh = rig.server.conn
        assert fresh is not conn and len(fresh.joins()) == 1
        reads = h.count(ORDERBOOK)
        await h.clock.advance(3.0)
        assert h.count(ORDERBOOK) == reads and not h.feed.is_ready(MARKET)
        fresh.ack()
        await until(lambda: h.count(ORDERBOOK) == reads + 1)
        assert not h.feed.is_ready(MARKET)  # the window (fence 1) still blocks
        if evidence == "contiguous":
            # Its own evidence: a clean batch past the fence on the new socket.
            fresh.broadcast(TOPIC, "market_batch", market_batch(2, 1, "0.57"))
            await h.clock.advance(2.0)
            await until(lambda: h.feed.is_ready(MARKET))
        else:
            # The discarded revisions show up as a gap: blocked, not ready.
            fresh.broadcast(TOPIC, "market_batch", market_batch(4, 3, "0.59"))
            await h.clock.advance(2.0)
            await until(lambda: StaleReason.REVISION_GAP in h.feed.blockers(MARKET))
            await h.clock.advance(5.0)
            assert not h.feed.is_ready(MARKET)

    feed_run(scenario)


def test_channel_end_jumps_its_topic_backlog_for_a_delayed_consumer() -> None:
    async def scenario(rig: FeedRig) -> None:
        h = rig.h
        conn, gate = await ready_with_backlog(rig)
        ref = conn.joins()[-1]["ref"]
        conn.channel_event(TOPIC, "phx_error", {}, ref, ref)
        await until(
            lambda: list(rig.transport._events)
            == [ChannelStatusEvent(TOPIC, ChannelStatus.CHANNEL_ERROR)]
        )
        seen = len(h.events)
        gate.set()
        await until(lambda: not h.feed.is_ready(MARKET))
        assert h.events[seen] == MarketStale(MARKET, StaleReason.CHANNEL_ERROR)
        assert h.feed.last_revision(MARKET) == 1
        assert not any(isinstance(e, BookUpdated) for e in h.events[seen:])
        reads = h.count(ORDERBOOK)
        await h.clock.advance(1.0)  # rejoin backoff
        assert len(conn.joins()) == 2 and h.count(ORDERBOOK) == reads
        conn.ack()
        await h.clock.advance(1.0)  # the feed's min_resync_interval_s since its last read
        await until(lambda: h.count(ORDERBOOK) == reads + 1)
        assert not h.feed.is_ready(MARKET)  # the channel break opened a window
        conn.broadcast(TOPIC, "market_batch", market_batch(2, 1, "0.57"))
        await h.clock.advance(2.0)
        await until(lambda: h.feed.is_ready(MARKET))

    feed_run(scenario)


def test_repeated_joins_each_trigger_a_resync_through_the_feed() -> None:
    async def scenario(rig: FeedRig) -> None:
        h = rig.h
        await rig.start()
        conn = rig.server.conn
        ref = conn.ack()
        await until(lambda: h.count(ORDERBOOK) == 1)
        conn.channel_event(TOPIC, "phx_error", {}, ref, ref)
        await until(lambda: StaleReason.CHANNEL_ERROR in rig.stale())
        await h.clock.advance(1.0)
        conn.ack()
        await h.clock.advance(2.0)  # min_resync_interval_s
        await until(lambda: h.count(ORDERBOOK) == 2)

    feed_run(scenario)


def test_token_refresh_reaches_joined_channels_and_later_joins() -> None:
    second = json_response(
        200, token_body(token=NEW_TOKEN_VALUE, expires=T0 + timedelta(hours=6))
    )

    async def scenario(rig: FeedRig) -> None:
        h = rig.h
        await rig.start()
        conn = rig.server.conn
        conn.ack()
        await until(lambda: h.feed.is_ready(MARKET))
        await h.clock.advance(3 * 3600 - 540)  # past the refresh point
        assert h.count("/realtime/token") == 2
        await until(lambda: any(f["event"] == "access_token" for f in conn.frames()))
        (push,) = [f for f in conn.frames() if f["event"] == "access_token"]
        assert push["payload"] == {"access_token": NEW_TOKEN_VALUE}
        assert StaleReason.TOKEN_REFRESH in rig.stale()
        conn.lose()
        await settle()
        await h.clock.advance(1.0)
        assert rig.server.conn.joins()[-1]["payload"]["access_token"] == NEW_TOKEN_VALUE

    feed_run(
        scenario,
        policy=RealtimeFeedPolicy(tick_s=60.0),
        token_responses=[json_response(200, token_body()), second],
    )


def test_feed_close_closes_the_socket_and_ends_receive() -> None:
    async def scenario(rig: FeedRig) -> None:
        await rig.start()
        rig.server.conn.ack()
        await settle()
        await rig.h.stop()
        assert rig.server.conn.closed
        assert await rig.transport.receive() is None
        assert MarketReady(MARKET) in rig.h.events

    feed_run(scenario)


# --- a real WebSocket on 127.0.0.1 ---------------------------------------------------------------


def _silent(name: str) -> logging.Logger:
    log = logging.Logger(name)  # noqa: LOG001 - the test server's frames hold the token
    log.disabled = True
    return log


def _broadcast_frame(event: str, body: str) -> str:
    return (
        '{"topic":"realtime:' + TOPIC + '","event":"broadcast","payload":{"type":"broadcast",'
        '"event":"' + event + '","payload":' + body + '},"ref":null}'
    )


@pytest.mark.parametrize("compression", [True, False])
def test_real_websocket_frames_reach_the_payload_exactly(
    compression: bool, caplog: pytest.LogCaptureFixture
) -> None:
    body = (
        '{"delivery":{"revision":1,"previousRevision":0},'
        '"books":[{"exchangeId":36,"asks":[{"price":' + LONG + ',"quantity":1.50}]}]}'
    )
    seen: dict[str, Any] = {}

    async def handler(ws: ServerConnection) -> None:
        seen["path"] = ws.request.path
        seen["join"] = json.loads(await ws.recv())
        join = seen["join"]
        await ws.send(json.dumps({"topic": join["topic"], "event": "phx_reply",
                                  "payload": {"status": "ok", "response": {}},
                                  "ref": join["ref"], "join_ref": join["ref"]}))
        await ws.send(_broadcast_frame("market_batch", body))
        await ws.send(_broadcast_frame("market_batch", body).encode())  # binary: refused
        await ws.wait_closed()

    async def main() -> None:
        listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        listener.bind(("127.0.0.1", 0))
        listener.listen()
        port = listener.getsockname()[1]
        urls: list[str] = []

        async def local(url: str) -> Any:
            urls.append(url)
            parts = urlsplit(url)
            sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            sock.connect(("127.0.0.1", port))  # numeric: no name resolution
            sock.setblocking(False)
            options: dict[str, Any] = {} if compression else {"compression": None}
            return await connect_websocket(
                f"ws://127.0.0.1:{port}{parts.path}?{parts.query}",
                open_timeout_s=5.0,
                max_frame_bytes=2**20,
                sock=sock,
                **options,
            )

        baseline = set(asyncio.all_tasks())
        async with serve(handler, sock=listener, logger=_silent("test-server")):
            transport = RealtimeSocketTransport(policy=QUIET, connector=local)
            await transport.connect(ENDPOINT, TOKEN)
            await transport.subscribe(TOPIC)
            events = [await asyncio.wait_for(transport.receive(), 5.0) for _ in range(3)]
            await transport.close()
        await settle()
        assert own_tasks(baseline) == set()

        assert events[0] == ChannelStatusEvent(TOPIC, ChannelStatus.SUBSCRIBED)
        broadcast = events[1]
        assert isinstance(broadcast, BroadcastEvent) and broadcast.event == "market_batch"
        (ask,) = broadcast.payload["books"][0]["asks"]  # type: ignore[index]
        assert type(ask["price"]) is Decimal and str(ask["price"]) == LONG
        assert str(ask["quantity"]) == "1.50"
        assert events[2] == SocketEvent(SocketState.ERROR)  # the binary frame

        path, _, query = seen["path"].partition("?")
        assert path == "/realtime/v1/websocket"
        assert parse_qs(query) == {"apikey": [ANON_VALUE], "vsn": ["1.0.0"]}
        assert seen["join"]["payload"]["access_token"] == TOKEN_VALUE
        assert seen["join"]["payload"]["config"]["private"] is True
        assert urls[0].startswith("wss://realtime.invalid/realtime/v1/websocket?")

    with caplog.at_level(logging.DEBUG):
        asyncio.run(asyncio.wait_for(main(), 30.0))
    for secret in (TOKEN_VALUE, ANON_VALUE, "apikey", LONG):
        assert secret not in caplog.text
