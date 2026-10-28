"""Helpers for the socket adapter tests: a scripted raw-frame server and a rig (M3).

Every frame here is SYNTHETIC, written from the Phoenix vsn 1.0.0 shapes realtime-py
2.33.0 sends and expects (see engine/sig_client/realtime_socket.py) and the payload shapes
the OpenAPI prose gives. No frame has been recorded from SIG or from Supabase.
"""

from __future__ import annotations

import asyncio
import json
from typing import Any

from realtime_kit import ANON_VALUE, MARKET, SUPABASE_URL, TOKEN_VALUE, ManualClock
from sig_testkit import TOURNAMENT

from engine.sig_client.realtime import TransportEvent
from engine.sig_client.realtime_socket import (
    RawSocketClosed,
    RealtimeSocketPolicy,
    RealtimeSocketTransport,
)
from engine.sig_client.realtime_token import RealtimeEndpoint, RealtimeSecret

TOPIC = f"tournament:{TOURNAMENT.id}:market:{MARKET}"
OTHER = f"tournament:{TOURNAMENT.id}:market:27"
ENDPOINT = RealtimeEndpoint(SUPABASE_URL, RealtimeSecret(ANON_VALUE))
TOKEN = RealtimeSecret(TOKEN_VALUE)
NEW_TOKEN_VALUE = "rt-sentinel-token-2"  # test placeholder, never a credential
NEW_TOKEN = RealtimeSecret(NEW_TOKEN_VALUE)


async def settle(rounds: int = 60) -> None:
    for _ in range(rounds):
        await asyncio.sleep(0)


class FakeConnection:
    """One scripted connection: records every text frame sent, serves queued frames."""

    def __init__(self, url: str) -> None:
        self.url = url
        self.sent: list[str] = []
        self.inbox: asyncio.Queue[str | bytes | BaseException] = asyncio.Queue()
        self.closed = False
        self.send_gate: asyncio.Event | None = None  # set: sends wait for it
        self.close_gate: asyncio.Event | None = None

    async def send(self, text: str) -> None:
        if self.send_gate is not None:
            await self.send_gate.wait()
        if self.closed:
            raise RawSocketClosed(clean=False)
        self.sent.append(text)

    async def recv(self) -> str | bytes:
        item = await self.inbox.get()
        if isinstance(item, BaseException):
            raise item
        return item

    async def close(self) -> None:
        if self.close_gate is not None:
            await self.close_gate.wait()
        self.closed = True

    # --- what the client sent ---------------------------------------------------------------

    def frames(self) -> list[dict[str, Any]]:
        return [json.loads(text) for text in self.sent]

    def events(self) -> list[tuple[str, str]]:
        return [(f["event"], f["topic"]) for f in self.frames()]

    def joins(self, topic: str = TOPIC) -> list[dict[str, Any]]:
        return [
            f for f in self.frames() if f["event"] == "phx_join" and f["topic"] == "realtime:" + topic
        ]

    def heartbeats(self) -> list[dict[str, Any]]:
        return [f for f in self.frames() if f["event"] == "heartbeat"]

    # --- what the server sends -------------------------------------------------------------

    def push(self, item: str | bytes | BaseException) -> None:
        self.inbox.put_nowait(item)

    def push_json(self, frame: dict[str, Any]) -> None:
        self.push(json.dumps(frame))

    def reply(
        self, topic: str, ref: str, status: str = "ok", response: Any = None, *, join_ref: Any = ...
    ) -> None:
        self.push_json({
            "topic": "realtime:" + topic,
            "event": "phx_reply",
            "payload": {"status": status, "response": {} if response is None else response},
            "ref": ref,
            "join_ref": ref if join_ref is ... else join_ref,
        })

    def ack(self, topic: str = TOPIC, status: str = "ok", response: Any = None) -> str:
        """Answer the latest join for `topic`; returns its ref."""
        ref = self.joins(topic)[-1]["ref"]
        self.reply(topic, ref, status, response)
        return ref

    def channel_event(self, topic: str, event: str, payload: Any, ref: Any, join_ref: Any) -> None:
        self.push_json({
            "topic": "realtime:" + topic,
            "event": event,
            "payload": payload,
            "ref": ref,
            "join_ref": join_ref,
        })

    def broadcast(self, topic: str, event: str, body_text: str) -> None:
        """A broadcast whose body is `body_text`, written into the frame verbatim."""
        self.push(
            '{"topic":"realtime:' + topic + '","event":"broadcast","payload":'
            '{"type":"broadcast","event":"' + event + '","payload":' + body_text + '},'
            '"ref":null}'
        )

    def lose(self, *, clean: bool = False) -> None:
        self.push(RawSocketClosed(clean=clean))


class FakeServer:
    """The connector: opens FakeConnections, or refuses / hangs as a test asks."""

    def __init__(self, clock: ManualClock) -> None:
        self.clock = clock
        self.connections: list[FakeConnection] = []
        self.attempts: list[float] = []
        self.refuse = 0
        self.refusal = OSError("connection refused")
        self.gate: asyncio.Event | None = None  # set: opens wait for it

    async def connect(self, url: str) -> FakeConnection:
        self.attempts.append(self.clock.now)
        if self.gate is not None:
            await self.gate.wait()
        if self.refuse:
            self.refuse -= 1
            raise self.refusal
        connection = FakeConnection(url)
        self.connections.append(connection)
        return connection

    @property
    def conn(self) -> FakeConnection:
        return self.connections[-1]


class Rig:
    """A RealtimeSocketTransport on a FakeServer and a ManualClock, read by a task."""

    def __init__(self, policy: RealtimeSocketPolicy | None = None) -> None:
        self.clock = ManualClock()
        self.server = FakeServer(self.clock)
        self.transport = RealtimeSocketTransport(
            policy=policy,
            connector=self.server.connect,
            clock=self.clock,
            sleep=self.clock.sleep,
        )
        self.events: list[TransportEvent] = []
        self.reader: asyncio.Task[None] | None = None
        self.ended = False

    async def _read(self) -> None:
        while (event := await self.transport.receive()) is not None:
            self.events.append(event)
        self.ended = True

    async def start(self, topics: tuple[str, ...] = (TOPIC,)) -> None:
        await self.transport.connect(ENDPOINT, TOKEN)
        for topic in topics:
            await self.transport.subscribe(topic)
        self.reader = asyncio.create_task(self._read())
        await settle()

    async def stop(self) -> None:
        await self.transport.close()
        if self.reader is not None:
            await asyncio.wait_for(self.reader, 5.0)

    def take(self) -> list[TransportEvent]:
        events, self.events = self.events, []
        return events


def own_tasks(baseline: set[asyncio.Task[Any]]) -> set[asyncio.Task[Any]]:
    """Tasks still pending that were not there at `baseline` (the current task excluded)."""
    current = asyncio.current_task()
    return {t for t in asyncio.all_tasks() if t not in baseline and t is not current and not t.done()}
