"""Helpers for the realtime tests: a manual clock, a fake transport, synthetic batches.

Every realtime payload here is SYNTHETIC: built from the shapes the OpenAPI prose gives
for `market_batch` (#/info/description, "Realtime"; POST /realtime/token). No realtime
message has been recorded from SIG. REST traffic goes through httpx.MockTransport, using
the synthetic fixtures or the recorded fixtures in tests/fixtures/sig/recorded/.
"""

from __future__ import annotations

import asyncio
import json
from datetime import UTC, datetime, timedelta
from typing import Any

import httpx
from sig_testkit import BASE_URL, SENTINEL_KEY, TOURNAMENT, Router, json_response

from engine.config import ApiKey, SigConfig, TournamentRef
from engine.sig_client.rate_limiter import RateLimiter
from engine.sig_client.realtime import (
    BroadcastEvent,
    ChannelStatus,
    ChannelStatusEvent,
    FeedEvent,
    RealtimeFeedPolicy,
    RealtimeMarketFeed,
    TransportEvent,
)
from engine.sig_client.realtime_token import RealtimeEndpoint, RealtimeSecret, RealtimeTokenClient
from engine.sig_client.rest import SigRestClient

T0 = datetime(2026, 10, 10, 12, 0, 0, tzinfo=UTC)
TOKEN_VALUE = "rt-sentinel-token-1"  # test placeholder, never a credential
ANON_VALUE = "sb_publishable_sentinel"  # test placeholder
SUPABASE_URL = "https://realtime.invalid"
USER_CHANNEL = "user:00000000-0000-0000-0000-000000000001"
MARKET = "26"  # market of the synthetic market_orderbook fixture (exchange 36)
EXCHANGE = 36


async def settle(rounds: int = 30) -> None:
    for _ in range(rounds):
        await asyncio.sleep(0)


class ManualClock:
    """Monotonic and wall time that move only when a test calls `advance`.

    `sleep` blocks until the clock has been advanced past its deadline, so background
    timers never fire on their own.
    """

    def __init__(self, start: float = 1000.0, wall: datetime = T0) -> None:
        self.now = start
        self._start = start
        self._wall0 = wall
        self._waiters: list[tuple[float, asyncio.Future[None]]] = []
        self.sleeps: list[float] = []

    def __call__(self) -> float:
        return self.now

    def wall(self) -> datetime:
        return self._wall0 + timedelta(seconds=self.now - self._start)

    async def sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)
        if seconds <= 0:
            await asyncio.sleep(0)
            return
        future: asyncio.Future[None] = asyncio.get_running_loop().create_future()
        self._waiters.append((self.now + seconds, future))
        await future

    async def advance(self, seconds: float) -> None:
        target = self.now + seconds
        while True:
            self._waiters = [(d, f) for d, f in self._waiters if not f.done()]
            due = [d for d, _ in self._waiters if d <= target]
            if not due:
                break
            self.now = max(self.now, min(due))
            for deadline, future in self._waiters:
                if deadline <= self.now and not future.done():
                    future.set_result(None)
            await settle()
        self.now = target
        await settle()


class FakeTransport:
    """In-memory RealtimeTransport. Tests push events; calls are recorded."""

    def __init__(self) -> None:
        self.queue: asyncio.Queue[TransportEvent | None] = asyncio.Queue()
        self.calls: list[tuple[str, str]] = []
        self.tokens: list[str] = []
        self.endpoints: list[RealtimeEndpoint] = []
        self.closed = False

    async def connect(self, endpoint: RealtimeEndpoint, token: RealtimeSecret) -> None:
        self.endpoints.append(endpoint)
        self.tokens.append(token.reveal())
        self.calls.append(("connect", endpoint.supabase_url))

    async def set_auth(self, token: RealtimeSecret) -> None:
        self.tokens.append(token.reveal())
        self.calls.append(("set_auth", ""))

    async def subscribe(self, topic: str) -> None:
        self.calls.append(("subscribe", topic))

    async def receive(self) -> TransportEvent | None:
        return await self.queue.get()

    async def close(self) -> None:
        if not self.closed:
            self.closed = True
            self.queue.put_nowait(None)

    def push(self, event: TransportEvent) -> None:
        self.queue.put_nowait(event)

    def subscribed(self, topic: str) -> None:
        self.push(ChannelStatusEvent(topic, ChannelStatus.SUBSCRIBED))

    def batch(self, topic: str, payload: Any, *, event: str = "market_batch") -> None:
        self.push(BroadcastEvent(topic, event, dumps(payload)))


# --- synthetic payloads -----------------------------------------------------------------


def delivery(revision: int, previous: int) -> dict[str, Any]:
    return {
        "revision": revision,
        "previousRevision": previous,
        "correlationId": f"corr-{revision}",
        # Provenance only: deliberately unrelated numbers, never a gap counter.
        "sourceSequenceFrom": 900_000 + revision,
        "sourceSequenceThrough": 900_010 + revision,
    }


def book(
    sequence: int,
    at: str = "2026-10-10T12:00:00.0000001+00:00",
    *,
    exchange: int = EXCHANGE,
    bids: list[tuple[str, int]] | None = None,
    asks: list[tuple[str, int]] | None = None,
    next_expiry: str | None = None,
) -> dict[str, Any]:
    def levels(pairs: list[tuple[str, int]]) -> list[dict[str, Any]]:
        return [{"price": RawNumber(p), "quantity": q} for p, q in pairs]

    return {
        "exchangeId": exchange,
        "asOf": {"sequence": sequence, "at": at},
        "nextExpiryAt": next_expiry,
        "bids": levels(bids if bids is not None else [("0.41", 300)]),
        "asks": levels(asks if asks is not None else [("0.55", 600)]),
    }


def trade(
    trade_id: str | None,
    sequence: int | None,
    *,
    exchange: int | str = EXCHANGE,
    market: int | str = MARKET,
    price: str = "0.545",
    tournament: str | None = TOURNAMENT.id,
) -> dict[str, Any]:
    item: dict[str, Any] = {
        "id": trade_id,
        "sequence": sequence,
        "exchangeId": exchange,
        "marketId": market,
        "price": RawNumber(price),
        "quantity": 10,
        "executedAt": "2026-10-10T12:00:00.123Z",
    }
    if tournament is not None:
        item["tournamentId"] = tournament
    return item


def dirty(exchange: int | str = EXCHANGE, market: int | str = MARKET) -> dict[str, Any]:
    return {
        "exchangeId": exchange,
        "marketId": market,
        "tournamentId": TOURNAMENT.id,
        "at": "2026-10-10T12:00:00.123Z",
    }


def settled(market: int | str = MARKET, value: str = "YES") -> dict[str, Any]:
    return {
        "marketId": market,
        "tournamentId": TOURNAMENT.id,
        "settledWith": value,
        "at": "2026-10-10T12:00:00.123Z",
    }


def batch(
    revision: int,
    previous: int,
    *,
    trades: list[dict[str, Any]] | None = None,
    books: list[dict[str, Any]] | None = None,
    book_dirty: list[dict[str, Any]] | None = None,
    market_settled: list[dict[str, Any]] | None = None,
    resync_required: bool | None = None,
) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "trades": trades or [],
        "bookDirty": book_dirty or [],
        "marketSettled": market_settled or [],
        "delivery": delivery(revision, previous),
    }
    if books:
        payload["books"] = books
    if resync_required is not None:
        payload["resyncRequired"] = resync_required
    return payload


class RawNumber(str):
    """A JSON number written exactly as given (encode with `dumps`)."""


_NUMBER_MARK = "\x00num:"  # json.dumps writes it as "\u0000num:"


def dumps(payload: Any) -> str:
    """JSON text in which RawNumber values appear as bare numbers, digit for digit."""

    def mark(value: Any) -> Any:
        if isinstance(value, RawNumber):
            return _NUMBER_MARK + str(value)
        if isinstance(value, dict):
            return {k: mark(v) for k, v in value.items()}
        if isinstance(value, list):
            return [mark(v) for v in value]
        return value

    text = json.dumps(mark(payload))
    token = '"\\u0000num:'
    parts = []
    start = 0
    while (found := text.find(token, start)) >= 0:
        end = text.index('"', found + len(token))
        parts += [text[start:found], text[found + len(token) : end]]
        start = end + 1
    return "".join([*parts, text[start:]])


# --- clients and feed -----------------------------------------------------------------------


def token_body(
    *,
    token: str = TOKEN_VALUE,
    expires: datetime | str = T0 + timedelta(hours=3),
    url: str = SUPABASE_URL,
    anon: str = ANON_VALUE,
) -> dict[str, Any]:
    expires_at = expires if isinstance(expires, str) else expires.strftime("%Y-%m-%dT%H:%M:%S.000Z")
    return {
        "token": token,
        "expiresAt": expires_at,
        "supabaseUrl": url,
        "anonKey": anon,
        "channels": {"user": USER_CHANNEL},
    }


class Harness:
    """A feed wired to a Router (REST), a FakeTransport and a ManualClock."""

    def __init__(
        self,
        router: Router | None = None,
        *,
        market_ids: tuple[str, ...] = (MARKET,),
        policy: RealtimeFeedPolicy | None = None,
        tournament: TournamentRef = TOURNAMENT,
        token_responses: list[Any] | None = None,
    ) -> None:
        self.clock = ManualClock()
        self.router = router or Router()
        self.router.routes.setdefault(
            "/api/v1/realtime/token", token_responses or [json_response(200, token_body())]
        )
        self.transport = FakeTransport()
        self.events: list[FeedEvent] = []
        self.limiter = RateLimiter(clock=self.clock, sleep=self.clock.sleep)
        config = SigConfig(api_key=ApiKey(SENTINEL_KEY), tournament=tournament, base_url=BASE_URL)
        mock = httpx.MockTransport(self.router)
        self.rest = SigRestClient(
            config,
            httpx.AsyncClient(transport=mock),
            owns_http=True,
            clock=self.clock,
            sleep=self.clock.sleep,
            jitter=lambda delay: delay,
            limiter=self.limiter,
        )
        self.tokens = RealtimeTokenClient(
            config,
            httpx.AsyncClient(transport=mock),
            owns_http=True,
            limiter=self.limiter,
            clock=self.clock,
        )
        self.feed = RealtimeMarketFeed(
            self.rest,
            self.tokens,
            self.transport,
            list(market_ids),
            policy=policy or RealtimeFeedPolicy(tick_s=1.0),
            sink=self.events.append,
            clock=self.clock,
            sleep=self.clock.sleep,
            wall_clock=self.clock.wall,
        )
        self.task: asyncio.Task[None] | None = None

    def topic(self, market_id: str = MARKET) -> str:
        return self.feed.topic(market_id)

    async def start(self, *, subscribe: bool = True) -> None:
        self.task = asyncio.create_task(self.feed.run())
        await settle()
        if subscribe:
            for market_id in self.feed_market_ids():
                self.transport.subscribed(self.topic(market_id))
            await settle()

    def feed_market_ids(self) -> list[str]:
        return [topic.rsplit(":", 1)[1] for kind, topic in self.transport.calls if kind == "subscribe"]

    async def stop(self) -> None:
        await self.feed.aclose()
        assert self.task is not None
        await self.task
        await self.rest.aclose()
        await self.tokens.aclose()

    def count(self, path: str) -> int:
        return self.router.count(path)

    def kinds(self) -> list[str]:
        return [type(e).__name__ for e in self.events]

