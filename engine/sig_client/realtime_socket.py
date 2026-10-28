"""The concrete RealtimeTransport: Supabase Realtime over a raw WebSocket (M3).

Market data only. This module joins `tournament:{tournament_id}:market:{market_id}`
topics as private broadcast channels and hands every frame to RealtimeMarketFeed as a
TransportEvent. It mints no token, decides nothing about markets, readiness or recovery,
and never touches `user:{profile_id}` or any other channel, orders or account state.
A SUBSCRIBED status means only that the server acknowledged a join: it does not clear a
recovery blocker, confirm an unobserved window or make a market ready (the feed and its
ledger decide that), and it never authorizes trading.

Sources
- [documented] docs/api/openapi.json, #/info/description ("Realtime (WebSocket) feeds")
  and POST /realtime/token: `supabaseUrl` + `anonKey` are the connection
  (`createClient(supabaseUrl, anonKey)`; the anon key is "the connection apikey"), the
  token is the access token, set with `setAuth(token)` before subscribing to any private
  channel, every channel is joined with `{ config: { private: true } }`, a refresh is a
  new token passed to `setAuth`, and broadcasts are `.on("broadcast", { event })`.
- [library] The SIG prose names supabase-js and the asynchronous supabase-py client. The
  wire protocol below is the one those clients speak, read from pinned sources:
  supabase 2.33.0 (`supabase/_async/client.py`), realtime 2.33.0 (`realtime/types.py`,
  `realtime/message.py`, `realtime/_async/client.py`, `channel.py`, `push.py`) and
  @supabase/realtime-js 2.11.2 (`dist/main/RealtimeClient.js`). Neither is a dependency:
  realtime-py validates every frame with pydantic (`ServerMessageAdapter.validate_json`)
  before a broadcast callback runs, which turns JSON numbers into binary floats.
- [client policy] Anything those sources leave open, marked where it is applied.

Protocol (Phoenix channels, serializer vsn 1.0.0, JSON text frames)
- URL: `wss://{host}{path}/realtime/v1/websocket?apikey={anonKey}&vsn=1.0.0`. supabase-py
  joins `realtime/v1` to the project URL with scheme wss for https; realtime-py appends
  `/websocket`, puts the key in `apikey` and adds `vsn` (VSN = "1.0.0").
- Frames: `{"topic", "event", "payload", "ref", "join_ref"}`. Channel topics are
  `realtime:{topic}`; the socket's own topic is `phoenix`.
- Join: `phx_join` with payload `{"config": {"broadcast": {"ack": false, "self": false},
  "presence": {"key": "", "enabled": false}, "private": true, "postgres_changes": []},
  "access_token": token}` and `join_ref` = `ref`. The token rides in every join, so a
  channel is authorized before it is joined and a rejoin always carries the current
  token (realtime-py `set_auth` updates the stored join payload for the same reason).
- Join acknowledgment: `phx_reply` with the join's `ref` and `{"status": "ok"}`; any
  other status refuses the join. Server text in a reply is never read, logged or kept.
- Refresh: `access_token` with `{"access_token": token}` to every joined channel
  (realtime-py and realtime-js `setAuth`). The server sends no positive acknowledgment.
- Heartbeat: `heartbeat` on `phoenix` every 25 s (realtime-py default), answered by a
  `phx_reply` with the same ref. As in realtime-js, a heartbeat still unanswered when the
  next one is due fails the socket.
- Channel failure: `phx_error`, `phx_close` and a `system` message whose status is not
  "ok" end the channel (realtime-py `on_error` / `on_close`).
- Broadcast: `event` "broadcast", payload `{"type": "broadcast", "event": name,
  "payload": body}`.

Precision: from WebSocket frame to BroadcastEvent.payload
1. websockets 15.0.1 `ClientConnection.recv()` (asyncio/connection.py, `recv`) returns a
   message from the frame assembler (asyncio/messages.py, `Assembler.get`): the frame
   bytes joined and, for a text frame, `bytes.decode()` (UTF-8). No JSON parsing, no
   numbers. A binary frame comes back as bytes and is refused here (vsn 1.0.0 is text).
2. `decode_frame()` parses that text with `json.loads(text, parse_float=Decimal,
   parse_constant=_reject_constant, object_pairs_hook=_unique_object)`. The decoder
   hands every number lexeme with a fraction or exponent to `Decimal(lexeme)` (exact)
   and every integral lexeme to `int(lexeme)` (exact). NaN, Infinity and -Infinity, a
   duplicate key, invalid JSON, or an integer past Python's digit limit fail the frame.
3. The broadcast body is taken from that decoded object as is and becomes
   `BroadcastEvent.payload`: no copy, no re-encoding, no model in between.
4. The feed's `MarketTopic` keeps an already-decoded payload unchanged
   (`decode_payload`), and its strict wire models accept only `int` and `Decimal` as JSON
   numbers (wire.py `_json_number`), so a price or quantity reaches `OrderbookLevel` and
   `BatchTrade` digit for digit. No step converts a lexeme to a binary float.

Failing closed
- A frame that cannot be decoded exactly or does not have the frame shape above, a binary
  frame, a frame for a topic outside `realtime:` or `phoenix`, an unexpected `phoenix`
  event, a send or receive failure, a socket close and an overdue heartbeat all end the
  socket: SocketEvent (ERROR, or CLOSED for a clean close) is queued at once, before the
  old connection is cleaned up, so the feed withdraws readiness without waiting for that
  cleanup. Then the old connection is closed (bounded, below), and the socket
  reconnects after a bounded backoff and every topic is joined again. A frame that
  cannot be attributed to a topic always takes this path. A SocketEvent only reports
  the loss: it clears no recovery blocker and confirms nothing.
- On a joined topic, a broadcast without the documented envelope, a refused, timed-out
  or ended join and any event this channel does not carry end that channel:
  ChannelStatusEvent (CHANNEL_ERROR, TIMED_OUT or CLOSED) is emitted, then the topic is
  joined again on its own after a bounded backoff. `presence_state` and `presence_diff`
  carry no market data (presence is disabled in the join) and are ignored.
- ChannelStatusEvent(SUBSCRIBED) is emitted only for a `phx_reply` "ok" whose ref is the
  topic's current join ref, received on the current socket, for every join and rejoin.
  Replies, errors and closes for an earlier join ref are ignored, so nothing from a
  previous join or socket can report a topic subscribed during an outage.
- [client policy] A channel the server ends (`phx_close`) is joined again: the feed asked
  for it and nothing here leaves a topic.
- [owner decision] An explicitly refused join stays fail closed for its market only. The
  topic keeps being retried, the feed keeps running, and no refusal is treated as
  permanent. After `refusal_escalate_after` refusals in a row (reset by an acknowledged
  join) a sanitized ERROR line escalates it, at most once per `escalation_interval_s`.
  Engine health/alert wiring does not exist yet; it will read `refused_joins()`.
- Broadcast event names are passed through; the feed decides which it carries.

Token rotation: `set_auth` stores the new token before anything else, and every join is
built from the stored token when it is sent. A channel joined with an older token gets an
`access_token` push with the current one, and a join still in flight when the token
changes gets that push as soon as it is acknowledged, before its SUBSCRIBED is emitted.

Ordering: one task owns the socket. It reads frames, sends joins, heartbeats and token
pushes, runs the timers, and emits every event into one bounded queue, so the feed sees
events in the order they happened: a SocketEvent always precedes every SUBSCRIBED of the
rejoins that follow it.

Secrets: the anon key, the token, the connection URL (it carries the key) and raw frames
never appear in a log line, repr or raised error. The websockets library gets a disabled
logger (its debug log would print frames). Server-supplied text (reply reasons, system
messages, close reasons) is never read into a log line or error. Only fixed descriptions
and exception class names are logged. Topics are market topics and carry no profile id.

Shutdown: after `close()` begins, `receive()` returns None, the socket task is cancelled
(with every timer, backoff and reconnect attempt), the connection is closed, and
`connect`, `subscribe` and `set_auth` start nothing. `close()` is idempotent.

Cleanup bounds: closing a connection waits at most `close_timeout_s` for a graceful
close, then aborts it; cancelled work (a send, a receive, a close) then gets at most
`cancel_grace_s` to stop. Work that suppresses cancellation is no longer waited for: it
stays referenced, with a done callback that retrieves its result, until it ends (and a
connection an abandoned open attempt yields is aborted). So `close()` returns within
`close_timeout_s + 2 * cancel_grace_s` on the injected clock, and losing a socket
delays the reconnect by no more than that.

Proxy: [owner decision] environment proxies are disabled, as in the REST clients
(`trust_env=False`). Hosting can ask for an explicit, reviewed configuration later.
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
import time
from collections import deque
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from decimal import Decimal
from enum import Enum
from typing import Any, Protocol
from urllib.parse import urlencode, urlsplit, urlunsplit

import websockets
from websockets.asyncio.client import connect as websockets_connect

from engine.sig_client.errors import SigArgumentError, SigClientClosedError, SigClientError
from engine.sig_client.rate_limiter import (
    Clock,
    Sleep,
    check_positive_int,
    check_positive_seconds,
)
from engine.sig_client.realtime import (
    BroadcastEvent,
    ChannelStatus,
    ChannelStatusEvent,
    SocketEvent,
    SocketState,
    TransportEvent,
)
from engine.sig_client.realtime_token import RealtimeEndpoint, RealtimeSecret

logger = logging.getLogger(__name__)

VSN = "1.0.0"  # realtime 2.33.0 realtime/types.py: VSN
REALTIME_PATH = "/realtime/v1/websocket"  # supabase 2.33.0 + realtime 2.33.0
PHOENIX_TOPIC = "phoenix"
TOPIC_PREFIX = "realtime:"

# [client policy] The only topics this adapter joins: tournament market topics.
_MARKET_TOPIC = re.compile(r"tournament:[0-9A-Za-z-]+:market:[0-9]+")

# The private join config: realtime-py's default broadcast and presence settings with
# `private: true`, as the SIG prose requires.
_JOIN_CONFIG: dict[str, Any] = {
    "broadcast": {"ack": False, "self": False},
    "presence": {"key": "", "enabled": False},
    "private": True,
    "postgres_changes": [],
}


class RealtimeSocketError(SigClientError):
    """The realtime socket cannot be used. Carries a fixed description only."""


class RawSocketClosed(Exception):
    """The raw connection ended. `clean` is True for a normal close."""

    def __init__(self, *, clean: bool) -> None:
        self.clean = clean
        super().__init__("clean close" if clean else "connection lost")


class RawConnection(Protocol):
    """One open WebSocket. `recv` returns a text frame as str and a binary one as bytes,
    and raises RawSocketClosed (or any exception) once the connection is unusable."""

    async def send(self, text: str) -> None: ...

    async def recv(self) -> str | bytes: ...

    async def close(self) -> None: ...

    def abort(self) -> None:
        """Tear the connection down at once, without a closing handshake."""
        ...


Connector = Callable[[str], Awaitable[RawConnection]]


# --- frame decoding -------------------------------------------------------------------------


class FrameError(Exception):
    """A frame that cannot be used. `reason` is a fixed description, never frame text."""

    def __init__(self, reason: str) -> None:
        self.reason = reason
        super().__init__(reason)


_INVALID = object()


def _reject_constant(name: str) -> None:
    raise ValueError("non-finite JSON constant")


def _unique_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    obj = dict(pairs)
    if len(obj) != len(pairs):
        raise ValueError("duplicate JSON object key")
    return obj


@dataclass(frozen=True)
class PhoenixMessage:
    topic: str
    event: str
    payload: dict[str, Any] = field(repr=False)
    ref: str | None
    join_ref: str | None


def decode_frame(data: object) -> PhoenixMessage:
    """One received frame as a Phoenix message, every JSON number exact.

    Raises FrameError for a binary frame, text that is not exact JSON, or anything
    without the vsn 1.0.0 frame shape.
    """
    if not isinstance(data, str):
        raise FrameError("binary frame (vsn 1.0.0 frames are text)")
    try:
        obj = json.loads(
            data,
            parse_float=Decimal,
            parse_constant=_reject_constant,
            object_pairs_hook=_unique_object,
        )
    except (ValueError, RecursionError):
        obj = _INVALID  # raised below, so no decoder text is chained to the error
    if obj is _INVALID:
        raise FrameError("not exact JSON")
    if not isinstance(obj, dict):
        raise FrameError("frame is not a JSON object")
    topic, event, payload = obj.get("topic"), obj.get("event"), obj.get("payload")
    ref, join_ref = obj.get("ref"), obj.get("join_ref")
    if not isinstance(topic, str) or not isinstance(event, str):
        raise FrameError("frame topic or event is not a string")
    if not isinstance(payload, dict):
        raise FrameError("frame payload is not an object")
    if not (ref is None or isinstance(ref, str)) or not (
        join_ref is None or isinstance(join_ref, str)
    ):
        raise FrameError("frame ref or join_ref is not a string or null")
    return PhoenixMessage(topic, event, payload, ref, join_ref)


def broadcast_body(payload: dict[str, Any]) -> tuple[str, object]:
    """(event name, body) of a broadcast message's payload; raises FrameError."""
    kind = payload.get("type", "broadcast")
    name = payload.get("event")
    if kind != "broadcast" or not isinstance(name, str) or "payload" not in payload:
        raise FrameError("broadcast without the documented envelope")
    return name, payload["payload"]


def realtime_url(endpoint: RealtimeEndpoint) -> str:
    """`wss://{host}{path}/realtime/v1/websocket?apikey={anonKey}&vsn=1.0.0`.

    The result holds the anon key: callers keep it in a RealtimeSecret.
    """
    parts = urlsplit(endpoint.supabase_url)
    # The token client already admits only plain https URLs; check again here.
    if parts.scheme != "https" or not parts.hostname or parts.query or parts.fragment:
        raise SigArgumentError("supabase_url must be a plain https URL")
    if "@" in parts.netloc:
        raise SigArgumentError("supabase_url must not carry credentials")
    path = parts.path.rstrip("/") + REALTIME_PATH
    query = urlencode({"apikey": endpoint.anon_key.reveal(), "vsn": VSN})
    return urlunsplit(("wss", parts.netloc, path, query, ""))


# --- the websockets connection ----------------------------------------------------------------


def _silent_logger() -> logging.Logger:
    """A logger outside the logging tree that drops everything (websockets logs frames)."""
    # Deliberately outside the logging tree, so no logging configuration can enable it.
    silent = logging.Logger("bellwether.realtime_socket.silenced")  # noqa: LOG001
    silent.disabled = True
    silent.propagate = False
    silent.addHandler(logging.NullHandler())
    return silent


class _WebSocketsConnection:
    """RawConnection over websockets 15.0.1. Errors keep no library text."""

    __slots__ = ("_ws",)

    def __init__(self, ws: Any) -> None:
        self._ws = ws

    def __repr__(self) -> str:
        return "_WebSocketsConnection(<redacted>)"

    async def send(self, text: str) -> None:
        clean: bool
        try:
            await self._ws.send(text)
            return
        except websockets.exceptions.ConnectionClosedOK:
            clean = True
        except Exception:  # noqa: BLE001 - any other failure ends the socket; text dropped
            clean = False
        raise RawSocketClosed(clean=clean)

    async def recv(self) -> str | bytes:
        clean: bool
        try:
            return await self._ws.recv()
        except websockets.exceptions.ConnectionClosedOK:
            clean = True
        except Exception:  # noqa: BLE001 - any other failure ends the socket; text dropped
            clean = False
        raise RawSocketClosed(clean=clean)

    async def close(self) -> None:
        await self._ws.close()

    def abort(self) -> None:
        transport = self._ws.transport
        if transport is not None:
            transport.abort()


async def connect_websocket(
    url: str,
    *,
    open_timeout_s: float,
    max_frame_bytes: int,
    close_timeout_s: float = 5.0,
    **options: Any,
) -> RawConnection:
    """Open `url` with websockets 15.0.1 (asyncio implementation).

    [owner decision] No proxy from the environment, by default and with no option to
    change it here, consistent with the REST clients' `trust_env=False`. If hosting
    needs a proxy, an explicit, reviewed configuration can be added later (H1 evidence).
    [client policy] A silenced library logger, `max_frame_bytes` as the message size
    limit (a larger message closes the socket), and `close_timeout_s` for the library's
    own closing handshake. Other settings are the library defaults realtime-py also uses
    (permessage-deflate offered, keepalive pings every 20 s).
    `options` go to `websockets.asyncio.client.connect` (tests pass a local `sock`).
    """
    ws = await websockets_connect(
        url,
        proxy=None,
        open_timeout=open_timeout_s,
        close_timeout=close_timeout_s,
        max_size=max_frame_bytes,
        logger=_silent_logger(),
        **options,
    )
    return _WebSocketsConnection(ws)


# --- the transport ------------------------------------------------------------------------------


@dataclass(frozen=True)
class RealtimeSocketPolicy:
    """Timeouts and bounds. Library defaults are cited; the rest is client policy."""

    connect_timeout_s: float = 10.0  # websockets 15.0.1 open_timeout default
    join_timeout_s: float = 10.0  # realtime 2.33.0 DEFAULT_TIMEOUT
    heartbeat_interval_s: float = 25.0  # realtime 2.33.0 DEFAULT_HEARTBEAT_INTERVAL
    send_timeout_s: float = 10.0  # [client policy]
    close_timeout_s: float = 5.0  # [client policy] graceful close, then abort
    cancel_grace_s: float = 1.0  # [client policy] for cancelled work to stop
    reconnect_initial_s: float = 1.0  # realtime-js 2.11.2 reconnectAfterMs starts at 1 s
    reconnect_max_s: float = 10.0  # ... and is capped at 10 s
    rejoin_initial_s: float = 1.0  # [client policy] same bounds per topic
    rejoin_max_s: float = 10.0
    max_frame_bytes: int = 8 * 2**20  # [client policy]
    max_pending_events: int = 4096  # [client policy] receive() queue bound
    # [owner decision] Explicit join refusals in a row before an escalation, and the
    # minimum time between escalation log lines.
    refusal_escalate_after: int = 5
    escalation_interval_s: float = 300.0

    def __post_init__(self) -> None:
        for name in (
            "connect_timeout_s",
            "join_timeout_s",
            "heartbeat_interval_s",
            "send_timeout_s",
            "close_timeout_s",
            "cancel_grace_s",
            "escalation_interval_s",
            "reconnect_initial_s",
            "reconnect_max_s",
            "rejoin_initial_s",
            "rejoin_max_s",
        ):
            check_positive_seconds(name, getattr(self, name))
        check_positive_int("max_frame_bytes", self.max_frame_bytes)
        check_positive_int("max_pending_events", self.max_pending_events)
        check_positive_int("refusal_escalate_after", self.refusal_escalate_after)
        if self.reconnect_max_s < self.reconnect_initial_s:
            raise ValueError("reconnect_max_s must be at least reconnect_initial_s")
        if self.rejoin_max_s < self.rejoin_initial_s:
            raise ValueError("rejoin_max_s must be at least rejoin_initial_s")


def _backoff(failures: int, initial: float, cap: float) -> float:
    """initial, 2x, 4x, ... capped: the wait after `failures` consecutive failures."""
    return min(initial * 2 ** max(failures - 1, 0), cap)


class _Join(Enum):
    PENDING = "pending"  # join as soon as a socket is open
    JOINING = "joining"  # join sent, no reply yet
    JOINED = "joined"  # acknowledged on the current socket
    WAITING = "waiting"  # ended; join again at retry_at


@dataclass
class _Channel:
    topic: str
    state: _Join = _Join.PENDING
    join_ref: str | None = None
    deadline: float | None = None  # join timeout while JOINING
    retry_at: float | None = None  # next join while WAITING
    failures: int = 0
    refusals: int = 0  # explicit join refusals in a row; reset only by an acknowledged join
    token: RealtimeSecret | None = field(default=None, repr=False)  # the server's copy


class _SocketLost(Exception):
    def __init__(self, state: SocketState, why: str) -> None:
        self.state = state
        self.why = why
        super().__init__(why)


class RealtimeSocketTransport:
    """RealtimeTransport over a Supabase Realtime WebSocket (see the module docstring).

    `connect()` opens the first socket and raises RealtimeSocketError if it cannot; after
    that the transport reconnects and rejoins on its own until `close()`. `connector`
    opens one raw connection for a URL (default: websockets); `clock` and `sleep` drive
    every timer, so tests can run on a manual clock.
    """

    def __init__(
        self,
        *,
        policy: RealtimeSocketPolicy | None = None,
        connector: Connector | None = None,
        clock: Clock = time.monotonic,
        sleep: Sleep = asyncio.sleep,
    ) -> None:
        self._policy = policy if policy is not None else RealtimeSocketPolicy()
        self._connector: Connector = connector if connector is not None else self._websocket
        self._clock = clock
        self._sleep = sleep
        self._url: RealtimeSecret | None = None
        self._token: RealtimeSecret | None = None
        self._channels: dict[str, _Channel] = {}
        self._conn: RawConnection | None = None
        self._recv_task: asyncio.Task[str | bytes] | None = None
        self._task: asyncio.Task[None] | None = None
        self._started = False
        self._closing = False
        self._closed = asyncio.Event()  # set when close() begins
        self._wake = asyncio.Event()
        self._ref = 0
        self._heartbeat_due = 0.0
        self._heartbeat_ref: str | None = None
        self._reconnect_at = 0.0
        self._reconnect_failures = 0
        self._requested = 0  # commands (subscribe, set_auth) posted
        self._applied = 0  # commands the socket task has acted on
        self._appliers: list[tuple[int, asyncio.Future[None]]] = []
        self._events: deque[TransportEvent] = deque()
        self._readable = asyncio.Event()
        self._writable = asyncio.Event()
        self._failure: str | None = None  # the socket task died unexpectedly
        self._stragglers: set[asyncio.Future[Any]] = set()  # cancelled, not yet ended
        self._escalated_at: float | None = None  # last refused-join escalation

    def __repr__(self) -> str:
        if self._closing:
            state = "closed"
        elif self._conn is not None:
            state = "connected"
        else:
            state = "disconnected"
        return f"RealtimeSocketTransport(state={state}, topics={len(self._channels)})"

    async def _websocket(self, url: str) -> RawConnection:
        return await connect_websocket(
            url,
            open_timeout_s=self._policy.connect_timeout_s,
            max_frame_bytes=self._policy.max_frame_bytes,
            close_timeout_s=self._policy.close_timeout_s,
        )

    # --- RealtimeTransport ------------------------------------------------------------------

    async def connect(self, endpoint: RealtimeEndpoint, token: RealtimeSecret) -> None:
        if self._closing:
            raise SigClientClosedError("the realtime socket is closed")
        if self._started:
            raise RuntimeError("connect() may be called only once")
        if not isinstance(endpoint, RealtimeEndpoint) or not isinstance(token, RealtimeSecret):
            raise TypeError("connect() takes a RealtimeEndpoint and a RealtimeSecret")
        self._started = True
        self._url = RealtimeSecret(realtime_url(endpoint))
        self._token = token  # every join carries it: authorized before any join
        conn = await self._open()
        if conn is None:
            if self._closing:
                raise SigClientClosedError("the realtime socket is closed")
            raise RealtimeSocketError("the realtime socket could not be opened")
        self._install(conn)
        self._task = asyncio.create_task(self._supervise())

    async def set_auth(self, token: RealtimeSecret) -> None:
        """Use `token` for every later join and push it to every joined channel.

        Returns once the socket task has acted on it (or the socket is down, or closing).
        """
        if not isinstance(token, RealtimeSecret):
            raise TypeError("set_auth() takes a RealtimeSecret")
        if self._closing:
            return
        self._token = token
        await self._post()

    async def subscribe(self, topic: str) -> None:
        """Join `topic` as a private channel (now, or as soon as a socket is open)."""
        if not isinstance(topic, str) or not _MARKET_TOPIC.fullmatch(topic):
            raise SigArgumentError("only tournament market topics can be subscribed")
        if self._closing or topic in self._channels:
            return
        self._channels[topic] = _Channel(topic)
        await self._post()

    async def receive(self) -> TransportEvent | None:
        while True:
            if self._closing:
                return None
            if self._events:
                event = self._events.popleft()
                self._writable.set()
                return event
            if self._failure is not None:
                raise RealtimeSocketError(f"the realtime socket task failed ({self._failure})")
            self._readable.clear()
            await self._readable.wait()

    async def close(self) -> None:
        """Idempotent. Everything stops synchronously; then waits for the socket task.

        Cancelling a waiting `close()` does not interrupt the socket task's own shutdown.
        """
        task = self._task
        if not self._closing:
            self._closing = True
            self._closed.set()
            self._wake.set()
            self._readable.set()
            self._writable.set()
            self._resolve_appliers()
            if task is not None:
                task.cancel()
        if task is None:
            await self._drop_connection()
            return
        await asyncio.wait({task})  # unlike gather, never cancels the task it waits for
        if not task.cancelled():
            task.exception()  # retrieved: the task records its own failures

    # --- commands -----------------------------------------------------------------------

    async def _post(self) -> None:
        """Wake the socket task and wait until it has acted on everything posted so far."""
        self._requested += 1
        task = self._task
        if task is None or task.done():
            self._applied = self._requested  # no socket task: state alone is enough
            return
        future: asyncio.Future[None] = asyncio.get_running_loop().create_future()
        self._appliers.append((self._requested, future))
        self._wake.set()
        await future

    def _mark_applied(self, upto: int) -> None:
        self._applied = max(self._applied, upto)
        waiting = []
        for wanted, future in self._appliers:
            if wanted <= self._applied:
                if not future.done():
                    future.set_result(None)
            else:
                waiting.append((wanted, future))
        self._appliers = waiting

    def _resolve_appliers(self) -> None:
        for _, future in self._appliers:
            if not future.done():
                future.set_result(None)
        self._appliers = []

    # --- events -------------------------------------------------------------------------

    def _emit_now(self, event: TransportEvent) -> None:
        """Queue past the bound: only a SocketEvent, at most one per lost socket."""
        if self._closing:
            return
        self._events.append(event)
        self._readable.set()

    async def _emit(self, event: TransportEvent) -> None:
        while len(self._events) >= self._policy.max_pending_events and not self._closing:
            self._writable.clear()
            await self._writable.wait()
        if self._closing:
            return
        self._events.append(event)
        self._readable.set()

    # --- the socket task --------------------------------------------------------------------

    async def _supervise(self) -> None:
        try:
            while not self._closing:
                if self._conn is None:
                    await self._disconnected_step()
                else:
                    try:
                        await self._connected_step()
                    except _SocketLost as lost:
                        await self._lose(lost)
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 - a bug must not leave receive() waiting
            self._failure = type(exc).__name__
            logger.error("realtime socket task failed (%s)", self._failure)
            self._events.append(SocketEvent(SocketState.ERROR))
            self._readable.set()
        finally:
            self._resolve_appliers()
            await self._drop_connection()

    async def _connected_step(self) -> None:
        recv = self._recv_task
        if recv is not None and recv.done():
            self._recv_task = None
            await self._on_frame(self._received(recv))
            return
        upto = self._requested
        self._wake.clear()
        await self._service()
        self._mark_applied(upto)
        await self._wait(self._next_deadline(), receive=True)

    async def _disconnected_step(self) -> None:
        self._wake.clear()
        self._mark_applied(self._requested)  # commands change only state while down
        if self._clock() < self._reconnect_at:
            await self._wait(self._reconnect_at, receive=False)
            return
        conn = await self._open()
        if self._closing:
            if conn is not None:
                await self._close_raw(conn)
            return
        if conn is None:
            self._reconnect_failures += 1
            delay = self._reconnect_delay()
            self._reconnect_at = self._clock() + delay
            logger.warning("realtime socket reconnect failed; next attempt in %.1fs", delay)
            return
        logger.info("realtime socket reconnected")
        self._install(conn)

    def _install(self, conn: RawConnection) -> None:
        self._conn = conn
        self._heartbeat_due = self._clock() + self._policy.heartbeat_interval_s
        self._heartbeat_ref = None
        for channel in self._channels.values():
            channel.state = _Join.PENDING
            channel.join_ref = channel.deadline = channel.retry_at = None
            channel.failures = 0
            channel.token = None

    def _reconnect_delay(self) -> float:
        policy = self._policy
        return _backoff(self._reconnect_failures, policy.reconnect_initial_s, policy.reconnect_max_s)

    async def _service(self) -> None:
        """Heartbeat, join timeouts, joins and token pushes that are due now."""
        now = self._clock()
        if now >= self._heartbeat_due:
            if self._heartbeat_ref is not None:
                raise _SocketLost(SocketState.ERROR, "heartbeat reply overdue")
            ref = self._next_ref()
            await self._send({"topic": PHOENIX_TOPIC, "event": "heartbeat", "payload": {},
                              "ref": ref, "join_ref": None})
            self._heartbeat_ref = ref
            self._heartbeat_due = now + self._policy.heartbeat_interval_s
        for channel in list(self._channels.values()):
            joining = channel.state is _Join.JOINING and channel.deadline is not None
            if joining and now >= channel.deadline:
                await self._channel_ended(channel, ChannelStatus.TIMED_OUT, "join timed out")
        for channel in list(self._channels.values()):
            due = channel.state is _Join.WAITING and (
                channel.retry_at is None or now >= channel.retry_at
            )
            if channel.state is _Join.PENDING or due:
                await self._join(channel)
        for channel in list(self._channels.values()):
            if channel.state is _Join.JOINED and channel.token != self._token:
                await self._push_token(channel)

    def _next_deadline(self) -> float:
        deadline = self._heartbeat_due
        for channel in self._channels.values():
            if channel.state is _Join.JOINING and channel.deadline is not None:
                deadline = min(deadline, channel.deadline)
            elif channel.state is _Join.WAITING and channel.retry_at is not None:
                deadline = min(deadline, channel.retry_at)
        return deadline

    async def _wait(self, deadline: float, *, receive: bool) -> None:
        """Until a frame arrives (`receive`), a command is posted, or `deadline` passes."""
        delay = deadline - self._clock()
        if delay <= 0:
            return
        waiters: set[asyncio.Future[Any]] = set()
        if receive and self._conn is not None:
            if self._recv_task is None:
                self._recv_task = asyncio.ensure_future(self._conn.recv())
            waiters.add(self._recv_task)
        wake = asyncio.ensure_future(self._wake.wait())
        timer = asyncio.ensure_future(self._sleep(delay))
        try:
            await asyncio.wait({*waiters, wake, timer}, return_when=asyncio.FIRST_COMPLETED)
        finally:
            wake.cancel()
            timer.cancel()
            await asyncio.gather(wake, timer, return_exceptions=True)

    @staticmethod
    def _received(task: asyncio.Task[str | bytes]) -> str | bytes:
        clean: bool | None = None
        try:
            return task.result()
        except RawSocketClosed as closed:
            clean = closed.clean
        except Exception:  # noqa: BLE001 - any receive failure ends the socket
            clean = False
        state = SocketState.CLOSED if clean else SocketState.ERROR
        raise _SocketLost(state, "socket closed" if clean else "receive failed")

    # --- frames -----------------------------------------------------------------------------

    async def _on_frame(self, data: str | bytes) -> None:
        message: PhoenixMessage | None = None
        try:
            message = decode_frame(data)
        except FrameError as error:
            reason = error.reason
        if message is None:
            raise _SocketLost(SocketState.ERROR, f"unusable frame: {reason}")
        if message.topic == PHOENIX_TOPIC:
            self._on_phoenix(message)
            return
        if not message.topic.startswith(TOPIC_PREFIX):
            raise _SocketLost(SocketState.ERROR, "unusable frame: topic outside realtime:")
        channel = self._channels.get(message.topic[len(TOPIC_PREFIX):])
        if channel is None:
            logger.warning("realtime socket: frame for a topic not joined here ignored")
            return
        await self._on_channel(channel, message)

    def _on_phoenix(self, message: PhoenixMessage) -> None:
        if message.event != "phx_reply":
            raise _SocketLost(SocketState.ERROR, "unusable frame: unexpected phoenix event")
        if message.ref is None or message.ref != self._heartbeat_ref:
            return  # not the heartbeat outstanding now
        if message.payload.get("status") != "ok":
            raise _SocketLost(SocketState.ERROR, "heartbeat refused")
        self._heartbeat_ref = None
        self._reconnect_failures = 0  # the socket is answering

    async def _on_channel(self, channel: _Channel, message: PhoenixMessage) -> None:
        event = message.event
        live = channel.state in (_Join.JOINING, _Join.JOINED)
        if event == "broadcast":
            try:
                name, body = broadcast_body(message.payload)
            except FrameError:
                if live:
                    await self._channel_ended(
                        channel, ChannelStatus.CHANNEL_ERROR, "broadcast without its envelope"
                    )
                return
            await self._emit(BroadcastEvent(channel.topic, name, body))
        elif event == "phx_reply":
            if channel.state is not _Join.JOINING or message.ref != channel.join_ref:
                return  # a reply to an earlier join or to a push: nothing to report
            if message.join_ref not in (None, channel.join_ref):
                return
            if message.payload.get("status") == "ok":
                await self._joined(channel)
            else:
                channel.refusals += 1
                await self._channel_ended(channel, ChannelStatus.CHANNEL_ERROR, "join refused")
                self._escalate_refusals()
        elif event in ("phx_error", "phx_close"):
            if live and self._current(channel, message):
                status = ChannelStatus.CHANNEL_ERROR if event == "phx_error" else ChannelStatus.CLOSED
                await self._channel_ended(channel, status, f"server sent {event}")
        elif event == "system":
            if message.payload.get("status") != "ok" and live:
                await self._channel_ended(channel, ChannelStatus.CHANNEL_ERROR, "system error")
        elif event in ("presence_state", "presence_diff"):
            return  # presence is disabled in the join; no market data
        elif live:
            await self._channel_ended(channel, ChannelStatus.CHANNEL_ERROR, "unexpected event")

    @staticmethod
    def _current(channel: _Channel, message: PhoenixMessage) -> bool:
        """The message is for the channel's current join (or names no join at all)."""
        ref = message.join_ref if message.join_ref is not None else message.ref
        return ref is None or ref == channel.join_ref

    async def _joined(self, channel: _Channel) -> None:
        if channel.token != self._token:
            # The token changed while this join was in flight.
            await self._push_token(channel)
        channel.state = _Join.JOINED
        channel.deadline = channel.retry_at = None
        channel.failures = 0
        channel.refusals = 0
        self._reconnect_failures = 0
        logger.info("realtime socket: %s joined", channel.topic)
        await self._emit(ChannelStatusEvent(channel.topic, ChannelStatus.SUBSCRIBED))

    async def _channel_ended(self, channel: _Channel, status: ChannelStatus, why: str) -> None:
        channel.failures += 1
        delay = _backoff(channel.failures, self._policy.rejoin_initial_s, self._policy.rejoin_max_s)
        channel.state = _Join.WAITING
        channel.deadline = None
        channel.retry_at = self._clock() + delay
        logger.warning(
            "realtime socket: %s %s (%s); joining again in %.1fs", channel.topic, status, why, delay
        )
        await self._emit(ChannelStatusEvent(channel.topic, status))

    def refused_joins(self) -> dict[str, int]:
        """Topics whose explicit join refusals in a row reached the escalation threshold.

        For the engine health signal that does not exist yet (see `_escalate_refusals`).
        """
        threshold = self._policy.refusal_escalate_after
        return {c.topic: c.refusals for c in self._channels.values() if c.refusals >= threshold}

    def _escalate_refusals(self) -> None:
        """A bounded, rate-limited operational escalation of repeated join refusals.

        [owner decision] A refused join stays fail closed for its market only: the topic
        keeps being retried with backoff, the feed keeps running, and no refusal is taken
        as permanent. Once a topic reaches `refusal_escalate_after` refusals in a row, one
        ERROR line is logged, at most once per `escalation_interval_s` for the whole
        socket, naming counts and one market topic only (never server text, tokens or
        the URL). Handoff: when engine health/alert wiring exists, it reads
        `refused_joins()` (or replaces this log line) instead.
        """
        escalated = self.refused_joins()
        if not escalated:
            return
        now = self._clock()
        interval = self._policy.escalation_interval_s
        if self._escalated_at is not None and now - self._escalated_at < interval:
            return
        self._escalated_at = now
        topic, worst = max(escalated.items(), key=lambda item: item[1])
        logger.error(
            "realtime socket ESCALATION: %d topic(s) had at least %d joins refused in a row "
            "(most: %d, %s); those markets stay not ready and are still retried; a refusal "
            "is not treated as permanent",
            len(escalated),
            self._policy.refusal_escalate_after,
            worst,
            topic,
        )

    # --- sending --------------------------------------------------------------------------

    def _next_ref(self) -> str:
        self._ref += 1
        return str(self._ref)

    async def _join(self, channel: _Channel) -> None:
        token = self._token
        assert token is not None
        ref = self._next_ref()
        channel.join_ref = ref
        channel.token = token
        channel.state = _Join.JOINING
        channel.retry_at = None
        channel.deadline = self._clock() + self._policy.join_timeout_s
        await self._send({
            "topic": TOPIC_PREFIX + channel.topic,
            "event": "phx_join",
            "payload": {"config": _JOIN_CONFIG, "access_token": token.reveal()},
            "ref": ref,
            "join_ref": ref,
        })

    async def _push_token(self, channel: _Channel) -> None:
        token = self._token
        assert token is not None
        channel.token = token
        await self._send({
            "topic": TOPIC_PREFIX + channel.topic,
            "event": "access_token",
            "payload": {"access_token": token.reveal()},
            "ref": self._next_ref(),
            "join_ref": channel.join_ref,
        })

    async def _send(self, message: dict[str, Any]) -> None:
        conn = self._conn
        if conn is None:
            raise _SocketLost(SocketState.ERROR, "no socket")
        text = json.dumps(message, separators=(",", ":"))
        outcome = await self._bounded(conn.send(text), self._policy.send_timeout_s)
        if outcome is not None:
            clean = isinstance(outcome, RawSocketClosed) and outcome.clean
            raise _SocketLost(
                SocketState.CLOSED if clean else SocketState.ERROR,
                "socket closed" if clean else "send failed",
            )

    # --- bounded waits ----------------------------------------------------------------------
    # Every wait on raw-connection work is bounded on the injected clock. Work that does not
    # finish in time is cancelled and given `cancel_grace_s`; work that ignores even that is
    # tracked (a strong reference and a done callback) and no longer waited for, so neither
    # a stuck connection nor a coroutine that suppresses cancellation can hold up the socket
    # task or close().

    async def _bounded(self, step: Awaitable[Any], seconds: float) -> BaseException | None:
        """Run `step` for at most `seconds`. None on success, else its exception
        (TimeoutError on timeout). External cancellation propagates at once."""
        work = asyncio.ensure_future(step)
        timer = asyncio.ensure_future(self._sleep(seconds))
        try:
            await asyncio.wait({work, timer}, return_when=asyncio.FIRST_COMPLETED)
        except BaseException:
            timer.cancel()
            self._track(work)
            raise
        timer.cancel()
        if not work.done():
            await self._reap(work)
            return TimeoutError()
        if work.cancelled():
            return TimeoutError()
        return work.exception()

    async def _reap(self, work: asyncio.Future[Any]) -> None:
        """Cancel `work` and wait at most `cancel_grace_s` for it to stop."""
        work.cancel()
        if work.done():
            return
        grace = asyncio.ensure_future(self._sleep(self._policy.cancel_grace_s))
        try:
            await asyncio.wait({work, grace}, return_when=asyncio.FIRST_COMPLETED)
        finally:
            elapsed = grace.done() and not grace.cancelled()
            grace.cancel()
            if not work.done():
                if elapsed:
                    logger.warning(
                        "realtime socket cleanup ignored cancellation; no longer waited for"
                    )
                self._track(work)

    def _track(self, work: asyncio.Future[Any]) -> None:
        """Cancel `work` without waiting; keep it referenced until it ends."""
        work.cancel()
        if work.done():
            self._settled(work)
            return
        self._stragglers.add(work)
        work.add_done_callback(self._settled)

    def _settled(self, work: asyncio.Future[Any]) -> None:
        self._stragglers.discard(work)
        if not work.cancelled():
            work.exception()  # retrieved, never re-raised or logged (it may hold text)

    @staticmethod
    def _abort(conn: RawConnection) -> None:
        try:
            conn.abort()
        except Exception:  # noqa: BLE001 - tearing down must not raise; text dropped
            logger.info("realtime socket abort failed")

    # --- losing and opening sockets ------------------------------------------------------------

    async def _lose(self, lost: _SocketLost) -> None:
        """End the socket: report SocketEvent first, then clean up the old connection.

        The event is queued before any cleanup is awaited, so the feed withdraws
        readiness while the old connection is still closing. It also precedes every
        rejoin: rejoins need a new socket, which this task opens only after cleanup.
        """
        conn, self._conn = self._conn, None
        recv, self._recv_task = self._recv_task, None
        for channel in self._channels.values():
            channel.state = _Join.PENDING
            channel.join_ref = channel.deadline = channel.retry_at = None
            channel.token = None
        self._reconnect_failures += 1
        delay = self._reconnect_delay()
        self._reconnect_at = self._clock() + delay
        logger.warning("realtime socket lost (%s); reconnecting in %.1fs", lost.why, delay)
        self._emit_now(SocketEvent(lost.state))
        await self._discard_connection(conn, recv)

    async def _drop_connection(self) -> None:
        conn, self._conn = self._conn, None
        recv, self._recv_task = self._recv_task, None
        await self._discard_connection(conn, recv)

    async def _discard_connection(
        self, conn: RawConnection | None, recv: asyncio.Future[Any] | None
    ) -> None:
        """Close `conn` (which ends its receive), then stop `recv`. At most
        close_timeout_s + 2 * cancel_grace_s on the injected clock."""
        try:
            if conn is not None:
                await self._close_raw(conn)
        finally:
            if recv is not None:
                await self._reap(recv)

    async def _close_raw(self, conn: RawConnection) -> None:
        """Close gracefully within close_timeout_s, else abort the connection."""
        work = asyncio.ensure_future(conn.close())
        timer = asyncio.ensure_future(self._sleep(self._policy.close_timeout_s))
        try:
            await asyncio.wait({work, timer}, return_when=asyncio.FIRST_COMPLETED)
        except BaseException:
            timer.cancel()
            self._abort(conn)
            self._track(work)
            raise
        timer.cancel()
        if not work.done():
            self._abort(conn)
            logger.info("realtime socket close timed out; connection aborted")
            await self._reap(work)
        elif work.cancelled() or work.exception() is not None:
            self._abort(conn)
            logger.info("realtime socket close did not finish cleanly; connection aborted")

    async def _open(self) -> RawConnection | None:
        """One connection attempt, bounded by the connect timeout and by close()."""
        assert self._url is not None
        work = asyncio.ensure_future(self._connector(self._url.reveal()))
        timer = asyncio.ensure_future(self._sleep(self._policy.connect_timeout_s))
        closer = asyncio.ensure_future(self._closed.wait())
        try:
            await asyncio.wait({work, timer, closer}, return_when=asyncio.FIRST_COMPLETED)
        except BaseException:
            self._abandon_open(work)
            raise
        finally:
            timer.cancel()
            closer.cancel()
        if not work.done() or self._closing:
            self._abandon_open(work)
            if not self._closing:
                logger.warning("realtime socket open timed out")
            return None
        if work.cancelled():
            return None
        failure = work.exception()
        if failure is not None:
            logger.warning("realtime socket open failed (%s)", type(failure).__name__)
            return None
        return work.result()

    def _abandon_open(self, work: asyncio.Future[RawConnection]) -> None:
        """Cancel an open attempt without waiting; abort a connection it yields anyway."""

        def opened_anyway(done: asyncio.Future[RawConnection]) -> None:
            if not done.cancelled() and done.exception() is None:
                self._abort(done.result())

        work.add_done_callback(opened_anyway)
        self._track(work)
