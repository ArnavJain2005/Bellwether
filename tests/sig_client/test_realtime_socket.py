"""RealtimeSocketTransport: the Phoenix/Supabase socket contract, offline (M3).

The connector is a scripted raw-frame server (socket_kit.FakeServer): every frame the
adapter parses is literal JSON text, so number lexemes reach the decoder exactly as
written. Time is a ManualClock. Every frame is SYNTHETIC (socket_kit docstring).
"""

from __future__ import annotations

import asyncio
import json
import logging
from decimal import Decimal
from typing import Any

import pytest
from realtime_kit import ANON_VALUE, TOKEN_VALUE, USER_CHANNEL
from sig_testkit import secret_leaks
from socket_kit import (
    ENDPOINT,
    NEW_TOKEN,
    NEW_TOKEN_VALUE,
    OTHER,
    TOKEN,
    TOPIC,
    FakeConnection,
    Rig,
    own_tasks,
    settle,
)

from engine.sig_client.errors import SigArgumentError, SigClientClosedError
from engine.sig_client.realtime import (
    BroadcastEvent,
    ChannelStatus,
    ChannelStatusEvent,
    SocketEvent,
    SocketState,
)
from engine.sig_client.realtime_socket import (
    RealtimeSocketError,
    RealtimeSocketPolicy,
    realtime_url,
)
from engine.sig_client.realtime_token import RealtimeEndpoint, RealtimeSecret

# Heartbeats are tested on their own; elsewhere they would only add frames.
QUIET = RealtimeSocketPolicy(heartbeat_interval_s=1e6)
SUBSCRIBED = ChannelStatusEvent(TOPIC, ChannelStatus.SUBSCRIBED)
PROFILE_ID = USER_CHANNEL.split(":", 1)[1]

PRIVATE_JOIN_CONFIG = {
    "broadcast": {"ack": False, "self": False},
    "presence": {"key": "", "enabled": False},
    "private": True,
    "postgres_changes": [],
}


def run(scenario: Any, policy: RealtimeSocketPolicy | None = QUIET) -> Rig:
    """Run `scenario(rig)`, close the transport, and check that no task is left."""

    async def main() -> Rig:
        rig = Rig(policy)
        baseline = set(asyncio.all_tasks())
        try:
            await scenario(rig)
        finally:
            await rig.stop()
        await settle()
        assert own_tasks(baseline) == set()
        assert all(c.closed for c in rig.server.connections)
        return rig

    return asyncio.run(asyncio.wait_for(main(), 20.0))


def no_float(value: Any) -> bool:
    if isinstance(value, float):
        return False
    if isinstance(value, dict):
        return all(no_float(v) for v in value.values())
    if isinstance(value, list):
        return all(no_float(v) for v in value)
    return True


# --- connection URL -----------------------------------------------------------------------------


def test_url_carries_the_anon_key_and_protocol_version() -> None:
    assert realtime_url(ENDPOINT) == (
        "wss://realtime.invalid/realtime/v1/websocket?apikey=sb_publishable_sentinel&vsn=1.0.0"
    )
    based = RealtimeEndpoint("https://example.invalid/base/", RealtimeSecret("a+b/c="))
    assert realtime_url(based) == (
        "wss://example.invalid/base/realtime/v1/websocket?apikey=a%2Bb%2Fc%3D&vsn=1.0.0"
    )


@pytest.mark.parametrize(
    "url",
    ["http://realtime.invalid", "https://realtime.invalid?x=1", "https://u:p@realtime.invalid"],
)
def test_url_refuses_anything_but_plain_https(url: str) -> None:
    with pytest.raises(SigArgumentError) as caught:
        realtime_url(RealtimeEndpoint(url, RealtimeSecret(ANON_VALUE)))
    assert "realtime.invalid" not in str(caught.value)


def test_connect_opens_the_built_url() -> None:
    async def scenario(rig: Rig) -> None:
        await rig.start()
        assert rig.server.conn.url == realtime_url(ENDPOINT)

    run(scenario)


# --- join and acknowledgment ----------------------------------------------------------------------


def test_private_join_carries_the_token_and_reports_subscribed_only_on_ack() -> None:
    async def scenario(rig: Rig) -> None:
        await rig.start()
        conn = rig.server.conn
        (join,) = conn.joins()
        assert join == {
            "topic": "realtime:" + TOPIC,
            "event": "phx_join",
            "payload": {"config": PRIVATE_JOIN_CONFIG, "access_token": TOKEN_VALUE},
            "ref": join["ref"],
            "join_ref": join["ref"],
        }
        assert rig.events == []  # sent is not subscribed
        conn.ack()
        await settle()
        assert rig.take() == [SUBSCRIBED]

    run(scenario)


def test_subscribe_before_connect_joins_once_the_socket_is_open() -> None:
    async def scenario(rig: Rig) -> None:
        await rig.transport.subscribe(TOPIC)
        assert rig.server.connections == []
        await rig.start(topics=(TOPIC,))  # subscribing again is a no-op
        assert len(rig.server.conn.joins()) == 1

    run(scenario)


def test_only_tournament_market_topics_can_be_subscribed() -> None:
    async def scenario(rig: Rig) -> None:
        await rig.start(topics=())
        for topic in (USER_CHANNEL, "market:36", "leaderboard:all", TOPIC + "\n", 7):
            with pytest.raises(SigArgumentError) as caught:
                await rig.transport.subscribe(topic)  # type: ignore[arg-type]
            assert PROFILE_ID not in str(caught.value)
        assert rig.server.conn.sent == []

    run(scenario)


def test_refused_join_is_a_channel_error_and_rejoins_with_bounded_backoff() -> None:
    async def scenario(rig: Rig) -> None:
        await rig.start()
        conn = rig.server.conn
        waits = []
        for _ in range(6):
            conn.ack(status="error", response={"reason": "Unauthorized"})
            await settle()
            assert rig.take() == [ChannelStatusEvent(TOPIC, ChannelStatus.CHANNEL_ERROR)]
            joins = len(conn.joins())
            started = rig.clock.now
            while len(conn.joins()) == joins:
                await rig.clock.advance(0.5)
            waits.append(rig.clock.now - started)
        assert waits == [1.0, 2.0, 4.0, 8.0, 10.0, 10.0]
        refs = [join["ref"] for join in conn.joins()]
        assert len(set(refs)) == len(refs)  # every rejoin is a new join ref
        conn.ack()
        await settle()
        assert rig.take() == [SUBSCRIBED]

    run(scenario)


def test_join_timeout_and_late_acks_never_report_subscribed() -> None:
    async def scenario(rig: Rig) -> None:
        await rig.start()
        conn = rig.server.conn
        first = conn.joins()[-1]["ref"]
        await rig.clock.advance(9.9)
        assert rig.events == []
        await rig.clock.advance(0.1)
        assert rig.take() == [ChannelStatusEvent(TOPIC, ChannelStatus.TIMED_OUT)]
        conn.reply(TOPIC, first)  # the timed-out join answers late
        await settle()
        assert rig.events == []
        await rig.clock.advance(1.0)
        second = conn.joins()[-1]["ref"]
        assert second != first
        conn.reply(TOPIC, first)  # still the old join
        await settle()
        assert rig.events == []
        conn.reply(TOPIC, second, join_ref=first)  # a ref and join_ref that disagree
        await settle()
        assert rig.events == []
        conn.reply(TOPIC, second)
        await settle()
        assert rig.take() == [SUBSCRIBED]

    run(scenario)


@pytest.mark.parametrize(
    ("event", "payload", "status"),
    [
        ("phx_error", {}, ChannelStatus.CHANNEL_ERROR),
        ("phx_close", {}, ChannelStatus.CLOSED),
        ("system", {"status": "error", "message": "x", "extension": "y", "channel": "z"},
         ChannelStatus.CHANNEL_ERROR),
        ("system", {"message": "no status"}, ChannelStatus.CHANNEL_ERROR),
        ("postgres_changes", {"ids": []}, ChannelStatus.CHANNEL_ERROR),
        ("custom_event", {}, ChannelStatus.CHANNEL_ERROR),
    ],
)
def test_channel_endings_report_status_then_rejoin(
    event: str, payload: dict[str, Any], status: ChannelStatus
) -> None:
    async def scenario(rig: Rig) -> None:
        await rig.start()
        conn = rig.server.conn
        ref = conn.ack()
        await settle()
        assert rig.take() == [SUBSCRIBED]
        conn.channel_event(TOPIC, event, payload, ref, ref)
        await settle()
        assert rig.take() == [ChannelStatusEvent(TOPIC, status)]
        conn.channel_event(TOPIC, event, payload, ref, ref)  # repeated while waiting
        await settle()
        assert rig.events == []
        await rig.clock.advance(1.0)
        assert len(conn.joins()) == 2
        conn.ack()
        await settle()
        assert rig.take() == [SUBSCRIBED]

    run(scenario)


def test_endings_and_replies_for_an_earlier_join_are_ignored() -> None:
    async def scenario(rig: Rig) -> None:
        await rig.start()
        conn = rig.server.conn
        old = conn.ack(status="error")
        await settle()
        await rig.clock.advance(1.0)
        current = conn.ack()
        await settle()
        assert rig.take() == [ChannelStatusEvent(TOPIC, ChannelStatus.CHANNEL_ERROR), SUBSCRIBED]
        for event in ("phx_error", "phx_close"):
            conn.channel_event(TOPIC, event, {}, old, old)
        conn.reply(TOPIC, old)
        conn.reply(TOPIC, current)  # a second ok for a join already acknowledged
        await settle()
        assert rig.events == []
        conn.channel_event(TOPIC, "system", {"status": "ok", "message": "ok"}, None, None)
        conn.channel_event(TOPIC, "presence_state", {}, None, None)
        conn.channel_event(TOPIC, "presence_diff", {"joins": {}, "leaves": {}}, None, None)
        await settle()
        assert rig.events == []

    run(scenario)


# --- broadcasts: exact numbers and demux -------------------------------------------------------

LONG = "0.123456789012345678901234567890123"


def test_long_fractional_lexemes_survive_exactly() -> None:
    # The premise: a binary float changes this value.
    assert Decimal(float(LONG)) != Decimal(LONG)
    body = (
        '{"delivery":{"revision":1,"previousRevision":0},'
        '"trades":[{"price":' + LONG + ',"quantity":12.5000}],'
        '"books":[{"exchangeId":36,"asOf":{"sequence":7,"at":"x"},'
        '"bids":[{"price":0.415,"quantity":1e3}],'
        '"asks":[{"price":' + LONG + ',"quantity":3.14159265358979323846264338327950288}]}],'
        '"big":123456789012345678901234567890,"negzero":-0.0}'
    )

    async def scenario(rig: Rig) -> None:
        await rig.start()
        conn = rig.server.conn
        conn.ack()
        conn.broadcast(TOPIC, "market_batch", body)
        await settle()
        subscribed, event = rig.take()
        assert subscribed == SUBSCRIBED
        assert isinstance(event, BroadcastEvent)
        assert (event.topic, event.event) == (TOPIC, "market_batch")
        payload = event.payload
        assert isinstance(payload, dict) and no_float(payload)
        trade = payload["trades"][0]
        assert type(trade["price"]) is Decimal and str(trade["price"]) == LONG
        assert str(trade["quantity"]) == "12.5000"
        (book,) = payload["books"]
        assert str(book["bids"][0]["price"]) == "0.415"
        assert book["bids"][0]["quantity"] == Decimal("1E+3")
        assert str(book["asks"][0]["price"]) == LONG
        assert str(book["asks"][0]["quantity"]) == "3.14159265358979323846264338327950288"
        assert payload["big"] == 123456789012345678901234567890 and type(payload["big"]) is int
        assert str(payload["negzero"]) == "-0.0"
        assert book["asOf"]["sequence"] == 7 and type(book["asOf"]["sequence"]) is int

    run(scenario)


def test_broadcasts_are_demultiplexed_by_topic_and_event() -> None:
    async def scenario(rig: Rig) -> None:
        await rig.start(topics=(TOPIC, OTHER))
        conn = rig.server.conn
        conn.ack(TOPIC)
        conn.ack(OTHER)
        conn.broadcast(OTHER, "market_batch", '{"n":1}')
        conn.broadcast(TOPIC, "book_dirty", '{"n":2}')
        conn.broadcast(TOPIC, "market_settled", '{"n":3}')
        conn.broadcast(TOPIC, "future_event", '[1,2.50]')  # the feed decides what it carries
        # A market topic this socket never joined: dropped, the socket stays up.
        conn.broadcast(TOPIC.replace(":26", ":99"), "market_batch", '{"n":4}')
        await settle()
        assert rig.take() == [
            SUBSCRIBED,
            ChannelStatusEvent(OTHER, ChannelStatus.SUBSCRIBED),
            BroadcastEvent(OTHER, "market_batch", {"n": 1}),
            BroadcastEvent(TOPIC, "book_dirty", {"n": 2}),
            BroadcastEvent(TOPIC, "market_settled", {"n": 3}),
            BroadcastEvent(TOPIC, "future_event", [1, Decimal("2.50")]),
        ]
        assert len(rig.server.connections) == 1

    run(scenario)


@pytest.mark.parametrize(
    "payload",
    [
        {"type": "broadcast", "payload": {}},  # no event name
        {"type": "broadcast", "event": "market_batch"},  # no body
        {"type": "broadcast", "event": 5, "payload": {}},
        {"type": "presence", "event": "market_batch", "payload": {}},
    ],
)
def test_broadcast_without_its_envelope_fails_only_that_channel(payload: dict[str, Any]) -> None:
    async def scenario(rig: Rig) -> None:
        await rig.start(topics=(TOPIC, OTHER))
        conn = rig.server.conn
        conn.ack(TOPIC)
        conn.ack(OTHER)
        await settle()
        rig.take()
        conn.channel_event(TOPIC, "broadcast", payload, None, None)
        await settle()
        assert rig.take() == [ChannelStatusEvent(TOPIC, ChannelStatus.CHANNEL_ERROR)]
        await rig.clock.advance(1.0)
        assert len(conn.joins(TOPIC)) == 2 and len(conn.joins(OTHER)) == 1
        assert len(rig.server.connections) == 1

    run(scenario)


# --- unusable frames fail the socket ----------------------------------------------------------

BROADCAST_WITH = (
    '{"topic":"realtime:' + TOPIC + '","event":"broadcast","payload":{"type":"broadcast",'
    '"event":"market_batch","payload":{"price":%s}},"ref":null}'
)


@pytest.mark.parametrize(
    "frame",
    [
        BROADCAST_WITH % "NaN",
        BROADCAST_WITH % "Infinity",
        BROADCAST_WITH % "-Infinity",
        BROADCAST_WITH % ("1" * 5000),  # past Python's integer digit limit
        BROADCAST_WITH % "0.5,\"price\":0.6",  # duplicate key
        BROADCAST_WITH % "0.5",  # ... the same frame sent as a binary frame (below)
        "{",
        "[]",
        '"text"',
        '{"topic":"realtime:' + TOPIC + '","event":"broadcast","payload":[],"ref":null}',
        '{"topic":"realtime:' + TOPIC + '","event":"broadcast","payload":{},"ref":5}',
        '{"topic":"realtime:' + TOPIC + '","event":7,"payload":{},"ref":null}',
        '{"event":"broadcast","payload":{},"ref":null}',
        '{"topic":"' + USER_CHANNEL + '","event":"broadcast","payload":{},"ref":null}',
        '{"topic":"phoenix","event":"phx_error","payload":{},"ref":null}',
    ],
)
def test_unusable_frames_fail_the_socket_then_it_rejoins(frame: str) -> None:
    binary = frame == BROADCAST_WITH % "0.5"

    async def scenario(rig: Rig) -> None:
        await rig.start()
        conn = rig.server.conn
        conn.ack()
        await settle()
        assert rig.take() == [SUBSCRIBED]
        conn.push(frame.encode() if binary else frame)
        await settle()
        assert rig.take() == [SocketEvent(SocketState.ERROR)]  # nothing usable came out
        assert conn.closed
        await rig.clock.advance(1.0)
        fresh = rig.server.conn
        assert fresh is not conn and len(fresh.joins()) == 1
        assert rig.events == []  # no SUBSCRIBED before the new join's ack
        fresh.ack()
        await settle()
        assert rig.take() == [SUBSCRIBED]

    run(scenario)


# --- socket loss and reconnection ------------------------------------------------------------


@pytest.mark.parametrize(("clean", "state"), [(False, SocketState.ERROR), (True, SocketState.CLOSED)])
def test_socket_loss_reports_first_then_rejoins_every_topic(clean: bool, state: SocketState) -> None:
    async def scenario(rig: Rig) -> None:
        await rig.start(topics=(TOPIC, OTHER))
        conn = rig.server.conn
        conn.ack(TOPIC)
        conn.ack(OTHER)
        await settle()
        rig.take()
        conn.lose(clean=clean)
        await settle()
        assert rig.take() == [SocketEvent(state)]
        assert conn.closed and len(rig.server.connections) == 1
        conn.ack(TOPIC)  # anything left on the dead socket is never read
        await rig.clock.advance(0.9)
        assert len(rig.server.connections) == 1 and rig.events == []
        await rig.clock.advance(0.1)
        fresh = rig.server.conn
        assert [j["payload"]["access_token"] for j in fresh.joins(TOPIC) + fresh.joins(OTHER)] == [
            TOKEN_VALUE,
            TOKEN_VALUE,
        ]
        assert rig.events == []  # no SUBSCRIBED during the outage or before an ack
        fresh.ack(OTHER)
        await settle()
        assert rig.take() == [ChannelStatusEvent(OTHER, ChannelStatus.SUBSCRIBED)]
        fresh.ack(TOPIC)
        await settle()
        assert rig.take() == [SUBSCRIBED]

    run(scenario)


def test_reconnect_backs_off_within_bounds_and_resets_once_healthy() -> None:
    async def scenario(rig: Rig) -> None:
        await rig.start()
        rig.server.conn.ack()
        await settle()
        rig.take()
        rig.server.refuse = 7
        lost_at = rig.clock.now
        rig.server.conn.lose()
        await settle()
        await rig.clock.advance(55.0)  # the eighth attempt opens; its join is in flight
        attempts = [round(t - lost_at, 6) for t in rig.server.attempts[1:]]
        assert attempts == [1.0, 3.0, 7.0, 15.0, 25.0, 35.0, 45.0, 55.0]
        assert rig.take() == [SocketEvent(SocketState.ERROR)]  # one event for the outage
        rig.server.conn.ack()
        await settle()
        assert rig.take() == [SUBSCRIBED]
        lost_at = rig.clock.now
        rig.server.conn.lose()
        await settle()
        await rig.clock.advance(1.0)
        assert rig.server.attempts[-1] - lost_at == 1.0  # the backoff started over

    run(scenario)


def test_send_failure_fails_the_socket() -> None:
    async def scenario(rig: Rig) -> None:
        await rig.start(topics=())
        rig.server.conn.closed = True  # every send now fails
        await rig.transport.subscribe(TOPIC)
        await settle()
        assert rig.take() == [SocketEvent(SocketState.ERROR)]

    run(scenario)


# --- heartbeat ---------------------------------------------------------------------------------


def test_heartbeat_is_sent_and_an_overdue_reply_fails_the_socket() -> None:
    async def scenario(rig: Rig) -> None:
        await rig.start(topics=())
        conn = rig.server.conn
        await rig.clock.advance(24.9)
        assert conn.heartbeats() == []
        await rig.clock.advance(0.1)
        (beat,) = conn.heartbeats()
        assert beat == {
            "topic": "phoenix", "event": "heartbeat", "payload": {}, "ref": beat["ref"],
            "join_ref": None,
        }
        conn.push_json({"topic": "phoenix", "event": "phx_reply",
                        "payload": {"status": "ok", "response": {}}, "ref": beat["ref"]})
        await rig.clock.advance(25.0)
        assert len(conn.heartbeats()) == 2 and rig.events == []
        await rig.clock.advance(25.0)  # the second one was never answered
        assert rig.take() == [SocketEvent(SocketState.ERROR)]
        assert conn.closed

    run(scenario, policy=RealtimeSocketPolicy())


def test_refused_heartbeat_fails_the_socket() -> None:
    async def scenario(rig: Rig) -> None:
        await rig.start(topics=())
        conn = rig.server.conn
        await rig.clock.advance(25.0)
        (beat,) = conn.heartbeats()
        conn.push_json({"topic": "phoenix", "event": "phx_reply",
                        "payload": {"status": "error", "response": {}}, "ref": beat["ref"]})
        await settle()
        assert rig.take() == [SocketEvent(SocketState.ERROR)]

    run(scenario, policy=RealtimeSocketPolicy())


# --- token rotation --------------------------------------------------------------------------


def test_set_auth_pushes_the_new_token_to_joined_channels_and_later_joins() -> None:
    async def scenario(rig: Rig) -> None:
        await rig.start(topics=(TOPIC, OTHER))
        conn = rig.server.conn
        ref = conn.ack(TOPIC)
        conn.ack(OTHER)
        await settle()
        before = len(conn.sent)
        await rig.transport.set_auth(NEW_TOKEN)
        pushes = conn.frames()[before:]  # sent by the time set_auth returned
        assert {p["topic"] for p in pushes} == {"realtime:" + TOPIC, "realtime:" + OTHER}
        mine = next(p for p in pushes if p["topic"] == "realtime:" + TOPIC)
        assert mine["event"] == "access_token" and mine["join_ref"] == ref
        assert mine["payload"] == {"access_token": NEW_TOKEN_VALUE}
        conn.lose()
        await settle()
        await rig.clock.advance(1.0)
        fresh = rig.server.conn
        assert fresh.joins(TOPIC)[-1]["payload"]["access_token"] == NEW_TOKEN_VALUE
        assert all(f["event"] != "access_token" for f in fresh.frames())

    run(scenario)


def test_a_join_in_flight_during_rotation_gets_the_new_token_before_subscribed() -> None:
    async def scenario(rig: Rig) -> None:
        sent_at_event: list[int] = []
        await rig.transport.connect(ENDPOINT, TOKEN)
        await rig.transport.subscribe(TOPIC)
        conn = rig.server.conn
        assert conn.joins()[-1]["payload"]["access_token"] == TOKEN_VALUE
        await rig.transport.set_auth(NEW_TOKEN)
        assert [f["event"] for f in conn.frames()] == ["phx_join"]  # not joined yet
        reader = asyncio.create_task(rig.transport.receive())
        conn.ack()
        event = await asyncio.wait_for(reader, 5.0)
        sent_at_event.append(len(conn.sent))
        assert event == SUBSCRIBED
        # The push went out before SUBSCRIBED was handed over.
        assert conn.frames()[: sent_at_event[0]][-1] == {
            "topic": "realtime:" + TOPIC,
            "event": "access_token",
            "payload": {"access_token": NEW_TOKEN_VALUE},
            "ref": conn.frames()[1]["ref"],
            "join_ref": conn.joins()[-1]["ref"],
        }

    rig = Rig(QUIET)

    async def main() -> None:
        try:
            await scenario(rig)
        finally:
            await rig.transport.close()

    asyncio.run(asyncio.wait_for(main(), 20.0))


def test_rotation_while_waiting_or_down_is_used_by_the_next_join() -> None:
    async def scenario(rig: Rig) -> None:
        await rig.start()
        conn = rig.server.conn
        conn.ack(status="error")
        await settle()
        await rig.transport.set_auth(NEW_TOKEN)  # waiting to rejoin
        await settle()  # let the socket task re-arm its timer before the clock moves
        await rig.clock.advance(1.0)
        assert conn.joins()[-1]["payload"]["access_token"] == NEW_TOKEN_VALUE
        conn.lose()
        await settle()
        await rig.transport.set_auth(RealtimeSecret("rt-sentinel-token-3"))  # socket down
        await settle()
        await rig.clock.advance(1.0)
        assert rig.server.conn.joins()[-1]["payload"]["access_token"] == "rt-sentinel-token-3"
        assert all(f["event"] != "access_token" for f in conn.frames() + rig.server.conn.frames())

    run(scenario)


# --- close and cancellation --------------------------------------------------------------------


def test_close_unblocks_receive_is_idempotent_and_starts_nothing_after() -> None:
    async def scenario(rig: Rig) -> None:
        await rig.start(topics=())
        conn = rig.server.conn
        waiting = asyncio.create_task(rig.transport.receive())
        await settle()
        await asyncio.gather(rig.transport.close(), rig.transport.close())
        assert await asyncio.wait_for(waiting, 5.0) is None
        assert await rig.transport.receive() is None
        await rig.transport.close()
        await rig.transport.subscribe(TOPIC)
        await rig.transport.set_auth(NEW_TOKEN)
        with pytest.raises(SigClientClosedError):
            await rig.transport.connect(ENDPOINT, TOKEN)
        await rig.clock.advance(100.0)
        assert conn.closed and conn.sent == [] and len(rig.server.connections) == 1
        assert "closed" in repr(rig.transport)

    run(scenario)


def test_close_before_connect_and_connect_twice() -> None:
    async def scenario(rig: Rig) -> None:
        await rig.start(topics=())
        with pytest.raises(RuntimeError):
            await rig.transport.connect(ENDPOINT, TOKEN)

    run(scenario)

    async def never_connected() -> None:
        rig = Rig(QUIET)
        await rig.transport.close()
        assert await rig.transport.receive() is None

    asyncio.run(never_connected())


def test_close_drops_pending_events_and_late_acks() -> None:
    async def scenario(rig: Rig) -> None:
        await rig.transport.connect(ENDPOINT, TOKEN)
        await rig.transport.subscribe(TOPIC)
        rig.server.conn.ack()
        rig.server.conn.broadcast(TOPIC, "market_batch", "{}")
        await settle()
        await rig.transport.close()
        assert await rig.transport.receive() is None  # queued events are not handed out

    run(scenario)


def test_close_during_reconnect_backoff_stops_reconnecting() -> None:
    async def scenario(rig: Rig) -> None:
        await rig.start()
        rig.server.refuse = 100
        rig.server.conn.lose()
        await settle()
        await rig.clock.advance(3.5)
        attempts = len(rig.server.attempts)
        await rig.transport.close()
        await rig.clock.advance(1000.0)
        assert len(rig.server.attempts) == attempts

    run(scenario)


def test_initial_open_failure_and_timeout_raise_without_detail() -> None:
    async def refused() -> None:
        rig = Rig(QUIET)
        rig.server.refuse = 1
        rig.server.refusal = OSError(f"refused {rig.server} apikey={ANON_VALUE}")
        with pytest.raises(RealtimeSocketError) as caught:
            await rig.transport.connect(ENDPOINT, TOKEN)
        for secret in (ANON_VALUE, TOKEN_VALUE, "apikey"):
            assert secret_leaks(caught.value, secret) == []
        await rig.transport.close()

    async def timed_out() -> None:
        rig = Rig(QUIET)
        rig.server.gate = asyncio.Event()
        connecting = asyncio.create_task(rig.transport.connect(ENDPOINT, TOKEN))
        await settle()
        await rig.clock.advance(10.0)
        with pytest.raises(RealtimeSocketError):
            await connecting
        await rig.transport.close()

    asyncio.run(refused())
    asyncio.run(timed_out())


def test_cancelled_connect_leaves_nothing_open() -> None:
    async def main() -> None:
        rig = Rig(QUIET)
        baseline = set(asyncio.all_tasks())
        opened: list[FakeConnection] = []
        gate = asyncio.Event()

        async def stubborn(url: str) -> FakeConnection:
            try:
                await gate.wait()
            except asyncio.CancelledError:
                pass  # opens anyway, ignoring the cancellation
            conn = FakeConnection(url)
            opened.append(conn)
            return conn

        rig.transport._connector = stubborn  # type: ignore[assignment]
        connecting = asyncio.create_task(rig.transport.connect(ENDPOINT, TOKEN))
        await settle()
        connecting.cancel()
        with pytest.raises(asyncio.CancelledError):
            await connecting
        await settle()  # the abandoned attempt ends on its own; its connection is aborted
        assert [(c.closed, c.aborted) for c in opened] == [(True, True)]
        await rig.transport.close()
        await settle()
        assert own_tasks(baseline) == set()

    asyncio.run(asyncio.wait_for(main(), 20.0))


def test_cancellation_at_each_wait_propagates_and_the_transport_keeps_working() -> None:
    async def scenario(rig: Rig) -> None:
        await rig.start(topics=())
        conn = rig.server.conn
        conn.send_gate = asyncio.Event()
        # subscribe: waiting for the socket task to send the join
        subscribing = asyncio.create_task(rig.transport.subscribe(TOPIC))
        await settle()
        subscribing.cancel()
        with pytest.raises(asyncio.CancelledError):
            await subscribing
        # set_auth: waiting while the socket task is still blocked in a send
        rotating = asyncio.create_task(rig.transport.set_auth(NEW_TOKEN))
        await settle()
        rotating.cancel()
        with pytest.raises(asyncio.CancelledError):
            await rotating
        conn.send_gate.set()
        await settle()
        # The join was built before set_auth: it carries the old token ...
        assert conn.joins()[-1]["payload"]["access_token"] == TOKEN_VALUE
        # receive: a cancelled wait loses nothing
        receiving = asyncio.create_task(rig.transport.receive())
        await settle()
        receiving.cancel()
        with pytest.raises(asyncio.CancelledError):
            await receiving
        conn.ack()
        await settle()
        assert rig.take() == [SUBSCRIBED]
        # ... and the token stored before the cancellation follows on acknowledgment.
        assert conn.frames()[-1]["event"] == "access_token"
        assert conn.frames()[-1]["payload"] == {"access_token": NEW_TOKEN_VALUE}

    run(scenario)


def test_cancelled_close_still_finishes_the_shutdown() -> None:
    async def main() -> None:
        rig = Rig(QUIET)
        baseline = set(asyncio.all_tasks())
        await rig.transport.connect(ENDPOINT, TOKEN)
        conn = rig.server.conn
        conn.close_gate = asyncio.Event()  # the server never finishes the close
        closing = asyncio.create_task(rig.transport.close())
        await settle()
        closing.cancel()
        with pytest.raises(asyncio.CancelledError):
            await closing
        assert await rig.transport.receive() is None
        await rig.clock.advance(5.0)  # close_timeout_s ends the wait
        await rig.transport.close()  # idempotent; waits for the socket task
        await settle()
        assert own_tasks(baseline) == set()

    asyncio.run(asyncio.wait_for(main(), 20.0))


def test_loss_is_reported_before_cleanup_and_before_any_rejoin() -> None:
    async def scenario(rig: Rig) -> None:
        await rig.start()
        conn = rig.server.conn
        conn.ack()
        await settle()
        rig.take()
        conn.close_gate = asyncio.Event()
        conn.lose()
        await settle()
        assert rig.take() == [SocketEvent(SocketState.ERROR)]  # cleanup still held
        assert not conn.closed and len(rig.server.connections) == 1
        conn.close_gate.set()
        await rig.clock.advance(1.0)
        rig.server.conn.ack()
        await settle()
        assert rig.take() == [SUBSCRIBED]

    run(scenario)


def test_close_is_bounded_even_when_cleanup_ignores_cancellation() -> None:
    async def main() -> None:
        rig = Rig(QUIET)
        baseline = set(asyncio.all_tasks())
        await rig.transport.connect(ENDPOINT, TOKEN)
        conn = rig.server.conn
        conn.close_gate = asyncio.Event()
        conn.close_ignores_cancel = True
        closing = asyncio.create_task(rig.transport.close())
        await settle()
        assert await rig.transport.receive() is None  # readiness of the stream ends at once
        await rig.clock.advance(4.9)
        assert not closing.done()
        await rig.clock.advance(0.1)  # close_timeout_s: aborted, then cancel_grace_s
        assert conn.aborted and not closing.done()
        await rig.clock.advance(1.0)
        await settle()
        assert closing.done() and closing.exception() is None  # 5 s + 1 s grace
        # The close coroutine that ignored cancellation is still referenced, not lost ...
        assert len(rig.transport._stragglers) == 1
        conn.close_gate.set()
        await settle()
        # ... and once it ends, nothing is left.
        assert rig.transport._stragglers == set() and own_tasks(baseline) == set()

    asyncio.run(asyncio.wait_for(main(), 20.0))


# --- refused joins: fail closed per market, escalate (owner decision) --------------------------

ESCALATE = RealtimeSocketPolicy(
    heartbeat_interval_s=1e6, refusal_escalate_after=3, escalation_interval_s=60.0
)


def escalations(caplog: pytest.LogCaptureFixture) -> list[logging.LogRecord]:
    return [r for r in caplog.records if "ESCALATION" in r.getMessage()]


def test_repeated_refusals_escalate_at_the_threshold_rate_limited_and_reset(
    caplog: pytest.LogCaptureFixture,
) -> None:
    server_text = f"server-secret-reason {PROFILE_ID}"

    async def refuse(rig: Rig, conn: FakeConnection) -> None:
        joins = len(conn.joins())
        conn.ack(status="error", response={"reason": server_text})
        await settle()
        while len(conn.joins()) == joins:
            await rig.clock.advance(0.5)

    async def scenario(rig: Rig) -> None:
        await rig.start(topics=(TOPIC, OTHER))
        conn = rig.server.conn
        conn.ack(OTHER)  # another market stays subscribed throughout
        for _ in range(2):
            await refuse(rig, conn)
        assert escalations(caplog) == [] and rig.transport.refused_joins() == {}
        await refuse(rig, conn)  # the third in a row
        (first,) = escalations(caplog)
        first_at = rig.clock.now
        assert first.levelno == logging.ERROR
        assert rig.transport.refused_joins() == {TOPIC: 3}
        # Rate limited: no second line until escalation_interval_s has passed.
        while len(escalations(caplog)) == 1:
            last_quiet = rig.clock.now
            await refuse(rig, conn)
        assert last_quiet - first_at < 60.0 <= rig.clock.now - first_at
        refusals = rig.transport.refused_joins()[TOPIC]
        assert refusals > 3
        # A timeout is not an explicit refusal.
        joins = len(conn.joins())
        await rig.clock.advance(10.0)  # join_timeout_s for the join now in flight
        assert rig.events[-1] == ChannelStatusEvent(TOPIC, ChannelStatus.TIMED_OUT)
        assert rig.transport.refused_joins() == {TOPIC: refusals}
        # An acknowledged join resets the count.
        while len(conn.joins()) == joins:
            await rig.clock.advance(0.5)
        conn.ack()
        await settle()
        assert rig.events[-1] == SUBSCRIBED
        assert rig.transport.refused_joins() == {}
        events = rig.take()
        # Fail closed per market only: no socket event, the other market untouched.
        assert all(isinstance(e, ChannelStatusEvent) for e in events)
        assert {e.topic for e in events} == {TOPIC, OTHER}
        assert [e for e in events if e.topic == OTHER] == [
            ChannelStatusEvent(OTHER, ChannelStatus.SUBSCRIBED)
        ]
        assert len(rig.server.connections) == 1
        await rig.clock.advance(100.0)  # past the rate limit
        conn.ack(status="error")
        await settle()
        assert len(escalations(caplog)) == 2  # one refusal after a reset: no escalation

    with caplog.at_level(logging.DEBUG):
        run(scenario, policy=ESCALATE)
    for secret in (server_text, PROFILE_ID, TOKEN_VALUE, ANON_VALUE, "apikey", "reason"):
        assert secret not in caplog.text, secret
    for record in escalations(caplog):
        assert TOPIC in record.getMessage() and "not treated as permanent" in record.getMessage()


@pytest.mark.parametrize(
    "fields",
    [{"refusal_escalate_after": 0}, {"refusal_escalate_after": 2.5},
     {"escalation_interval_s": 0}, {"cancel_grace_s": float("nan")}],
)
def test_policy_rejects_invalid_escalation_and_grace_settings(fields: dict[str, Any]) -> None:
    with pytest.raises(ValueError):
        RealtimeSocketPolicy(**fields)


def test_a_failed_socket_task_makes_receive_raise() -> None:
    async def main() -> None:
        rig = Rig(QUIET)
        await rig.transport.connect(ENDPOINT, TOKEN)

        async def broken() -> None:
            raise RuntimeError("bug")

        rig.transport._service = broken  # type: ignore[method-assign]
        await rig.transport.subscribe(TOPIC)
        assert await rig.transport.receive() == SocketEvent(SocketState.ERROR)
        with pytest.raises(RealtimeSocketError):
            await rig.transport.receive()
        await rig.transport.close()

    asyncio.run(asyncio.wait_for(main(), 20.0))


# --- secrets -------------------------------------------------------------------------------------


def test_no_secret_or_server_text_reaches_logs_reprs_or_errors(
    caplog: pytest.LogCaptureFixture,
) -> None:
    server_text = f"server-secret-reason {PROFILE_ID}"
    frame_text = '{"topic":"realtime:' + TOPIC + '","event":"broadcast","payload":"frame-secret"'

    async def scenario(rig: Rig) -> None:
        await rig.start(topics=(TOPIC, OTHER))
        conn = rig.server.conn
        conn.ack(TOPIC, status="error", response={"reason": server_text})
        ref = conn.ack(OTHER)
        conn.channel_event(OTHER, "system", {"status": "error", "message": server_text}, None, None)
        await rig.clock.advance(1.0)
        conn.channel_event(TOPIC, "phx_close", {"reason": server_text}, None, None)
        conn.channel_event(OTHER, "phx_error", {"reason": server_text}, ref, ref)
        await rig.transport.set_auth(NEW_TOKEN)
        conn.broadcast(TOPIC.replace(":26", ":99"), "market_batch", '{"x":"' + server_text + '"}')
        conn.push(frame_text)  # malformed: fails the socket
        rig.server.refuse = 2
        rig.server.refusal = OSError(server_text)
        await rig.clock.advance(5.0)
        rig.server.conn.lose()
        await rig.clock.advance(5.0)
        assert "apikey" not in repr(rig.transport)

    with caplog.at_level(logging.DEBUG):
        rig = run(scenario)
    text = caplog.text + repr(rig.transport) + "".join(repr(e) for e in rig.events)
    for secret in (
        TOKEN_VALUE,
        NEW_TOKEN_VALUE,
        ANON_VALUE,
        "apikey",
        "vsn=",
        "server-secret-reason",
        "frame-secret",
        PROFILE_ID,
        frame_text[:40],
    ):
        assert secret not in text, secret
    assert "realtime socket lost" in caplog.text  # the failures were logged, as text only
    assert json.dumps(TOKEN_VALUE) not in caplog.text
