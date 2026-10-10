"""SIG Realtime market-data feed for tournament market topics (S1-T3-A).

Scope of this slice: market data only. The feed subscribes to
`tournament:{tournament_id}:market:{market_id}` for the configured tournament, keeps
versioned books and the trade stream for each market, and decides when that data is
ready to use. It does not touch `user:{profile_id}` (fills, order updates), the public
`market:*` topics, leaderboards or relationships, and it places no orders.

The socket itself is behind RealtimeTransport (below). Every decision about the
documented contract is made here and in realtime_topic.py, so it is tested offline with
a fake transport.

Lifecycle
1. Verify the tournament over REST (the REST client's one-time check).
2. Mint a token (one WRITE on the shared account limiter), connect, subscribe every
   market topic as a private channel.
3. [documented] Resynchronize each market over REST "on initial subscription": every
   SUBSCRIBED status from the transport (first join or re-join after a reconnect)
   triggers a REST market orderbook read at `snapshot_depth` (default 200, the depth
   the spec gives for "the full depth the stream pushes").
4. Apply pushed batches through MarketTopic. Any trigger listed in StaleReason marks the
   market not ready and queues a REST resync.
5. [documented] Refresh the token before it expires (mint again, setAuth), then resync
   every market: a token refresh requires authoritative REST resynchronization.

Ready (`is_ready(market_id)`) means all of:
- the token is still valid on the clock (checked when asked, not only by the timer);
- the transport reports the topic SUBSCRIBED, with no channel or socket error since;
- a REST resync was requested after the most recent trigger and has completed;
- no further trigger arrived while it ran;
- no held book's known `nextExpiryAt` has passed (checked when asked);
- no blocker is outstanding (below).
The same token and expiry checks gate starting a resync and publishing MarketReady.
A message that needs recovery marks the market not ready (and publishes MarketStale)
before any BookUpdated, TradeReceived or SettlementSignal from that message is
published, so a sink never sees a ready market for data that has invalidated it.
Pushed books and trades keep flowing while a market is not ready, but downstream code
must not act on a market until it is ready again. Each HeldBook also says whether it is
`complete` (a REST side may be cut off at the requested depth) and whether its order
expiry time is known (REST books carry no `nextExpiryAt`).

Blockers: a settlement requires the authoritative market state, and a trade without a
sequence requires the exchange's price and trade tape over REST. This slice reads
neither, so those reasons stay in `blockers(market_id)` and the market is not ready
again for the life of the feed. An orderbook resync never clears them.

Retry waits: a valid Retry-After on a failed mint or resync is a minimum wait; backoff
limits (`max_retry_s`) never shorten it. A Retry-After too large to hold safely stops
the feed with that error instead of retrying early.

Resync budget [client policy]: `resync_reads_per_minute` caps logical resync *starts*
across the feed. One start is one `SigRestClient.get_market_orderbook` call, which may
make several HTTP attempts under its own ReadRetryPolicy (up to `max_attempts`, each on
the shared account limiter). The cap therefore bounds, but does not fix, the reads the
feed spends, and it reserves nothing for other readers: the shared limiter has no
priorities. At most one resync runs per market at a time; triggers that arrive meanwhile
coalesce into one follow-up, and starts per market are at least `min_resync_interval_s`
apart. Transient REST failures back off; permanent ones (auth, scope, tournament
mismatch, a closed client) stop the feed with that error.

Shutdown: `aclose()` stops `run()` at any point, including tournament verification,
minting and its retry waits, connecting and subscribing. Work in flight is cancelled and
nothing new (no token request, no subscription, no resync) starts afterwards.
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Awaitable, Callable, Coroutine, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from decimal import Decimal
from enum import StrEnum
from typing import Any, Protocol, TypeVar

from engine.sig_client.errors import (
    SigApiError,
    SigArgumentError,
    SigAuthError,
    SigBadRequestError,
    SigClientClosedError,
    SigClientError,
    SigContextMismatchError,
    SigNotFoundError,
    SigRateLimitedError,
    SigResponseError,
)
from engine.sig_client.rate_limiter import (
    Clock,
    RateLimiter,
    RequestKind,
    Sleep,
    check_positive_int,
    check_positive_seconds,
)
from engine.sig_client.realtime_token import (
    DEFAULT_RATE_LIMITED_WAIT_S,
    DOCUMENTED_TOKEN_LIFETIME_S,
    TOKEN_PATH,
    RealtimeCredentials,
    RealtimeEndpoint,
    RealtimeSecret,
    RealtimeTokenClient,
)
from engine.sig_client.realtime_topic import (
    BLOCKING_REASONS,
    DEFAULT_TRADE_ID_MEMORY,
    HeldBook,
    MarketTopic,
    SnapshotError,
    StaleReason,
)
from engine.sig_client.realtime_wire import BatchTrade, MarketSettled, epoch_seconds
from engine.sig_client.rest import ORDERBOOK_DEPTH_MAX, SigRestClient

logger = logging.getLogger(__name__)

T = TypeVar("T")
WallClock = Callable[[], datetime]


def utc_now() -> datetime:
    return datetime.now(UTC)


# --- transport ----------------------------------------------------------------------------


class ChannelStatus(StrEnum):
    """Channel states as Supabase Realtime clients report them."""

    SUBSCRIBED = "SUBSCRIBED"
    CHANNEL_ERROR = "CHANNEL_ERROR"
    TIMED_OUT = "TIMED_OUT"
    CLOSED = "CLOSED"


class SocketState(StrEnum):
    ERROR = "error"
    CLOSED = "closed"


@dataclass(frozen=True)
class ChannelStatusEvent:
    topic: str
    status: ChannelStatus


@dataclass(frozen=True)
class BroadcastEvent:
    """A broadcast on `topic`. `payload` is raw JSON text, or an object decoded with
    `parse_float=Decimal`; a binary float anywhere in it makes the message malformed."""

    topic: str
    event: str
    payload: object


@dataclass(frozen=True)
class SocketEvent:
    state: SocketState


TransportEvent = ChannelStatusEvent | BroadcastEvent | SocketEvent


class RealtimeTransport(Protocol):
    """The socket layer. An adapter (for example over a Supabase Realtime client) must:

    - connect to `endpoint.supabase_url` with `endpoint.anon_key` as the apikey, and
      authorize with the token (setAuth) before joining any channel;
    - join each topic as a private channel (`{ config: { private: true } }`);
    - report SUBSCRIBED for every join and re-join, and any other channel state as it
      happens; report socket errors and closes as SocketEvent; rejoin on its own;
    - pass broadcast payloads without lossy float decoding (see BroadcastEvent);
    - never log or expose the token or anon key;
    - make `receive()` return None once `close()` has been called.
    """

    async def connect(self, endpoint: RealtimeEndpoint, token: RealtimeSecret) -> None: ...

    async def set_auth(self, token: RealtimeSecret) -> None: ...

    async def subscribe(self, topic: str) -> None: ...

    async def receive(self) -> TransportEvent | None: ...

    async def close(self) -> None: ...


# --- feed events ----------------------------------------------------------------------------


@dataclass(frozen=True)
class MarketReady:
    market_id: str


@dataclass(frozen=True)
class MarketStale:
    market_id: str
    reason: StaleReason


@dataclass(frozen=True)
class BookUpdated:
    market_id: str
    book: HeldBook


@dataclass(frozen=True)
class TradeReceived:
    market_id: str
    trade: BatchTrade


@dataclass(frozen=True)
class SettlementSignal:
    market_id: str
    settlement: MarketSettled


FeedEvent = MarketReady | MarketStale | BookUpdated | TradeReceived | SettlementSignal
Sink = Callable[[FeedEvent], None]


class RealtimeFeedError(SigClientError):
    """The feed cannot continue (for example, the realtime endpoint changed)."""


class _Closed(Exception):
    """aclose() was called while run() was waiting."""


def _server_wait(error: SigClientError) -> float | None:
    """The minimum wait SIG asked for before the next attempt, or None.

    Looks at the error and, for a retry or deadline error, at the attempt it wraps. A
    Retry-After too large to hold re-raises the error: it is never retried early.
    """
    api = error if isinstance(error, SigApiError) else getattr(error, "last_error", None)
    if not isinstance(api, SigApiError):
        return None
    if api.retry_after_oversized:
        raise error
    if api.retry_after is not None:
        return api.retry_after
    if isinstance(api, SigRateLimitedError):
        return DEFAULT_RATE_LIMITED_WAIT_S  # the documented default for a 429
    return None


# Errors a retry cannot fix: they stop the feed.
_PERMANENT: tuple[type[BaseException], ...] = (
    SigAuthError,
    SigBadRequestError,
    SigNotFoundError,
    SigContextMismatchError,
    SigClientClosedError,
    SigArgumentError,
    RealtimeFeedError,
)


@dataclass(frozen=True)
class RealtimeFeedPolicy:
    snapshot_depth: int = ORDERBOOK_DEPTH_MAX
    refresh_margin_s: float = 600.0
    min_mint_interval_s: float = 60.0
    mint_retry_s: float = 15.0
    resync_reads_per_minute: int = 40
    min_resync_interval_s: float = 2.0
    resync_retry_s: float = 2.0
    max_retry_s: float = 120.0
    tick_s: float = 1.0
    trade_id_memory: int = DEFAULT_TRADE_ID_MEMORY

    def __post_init__(self) -> None:
        if check_positive_int("snapshot_depth", self.snapshot_depth) > ORDERBOOK_DEPTH_MAX:
            raise ValueError(f"snapshot_depth must be at most {ORDERBOOK_DEPTH_MAX}")
        for name in (
            "refresh_margin_s",
            "min_mint_interval_s",
            "mint_retry_s",
            "min_resync_interval_s",
            "resync_retry_s",
            "max_retry_s",
            "tick_s",
        ):
            check_positive_seconds(name, getattr(self, name))
        check_positive_int("resync_reads_per_minute", self.resync_reads_per_minute)
        check_positive_int("trade_id_memory", self.trade_id_memory)
        if self.refresh_margin_s >= DOCUMENTED_TOKEN_LIFETIME_S:
            raise ValueError("refresh_margin_s must be shorter than the token lifetime")


@dataclass
class _Market:
    topic: MarketTopic
    subscribed: bool = False
    ready: bool = False  # MarketReady published and not yet withdrawn
    blockers: set[StaleReason] = field(default_factory=set)
    wanted: int = 0  # resync requests so far
    done: int = 0  # the request number the last completed resync covered
    last_start: float | None = None
    task: asyncio.Task[None] | None = None

    @property
    def market_id(self) -> str:
        return self.topic.market_id


class RealtimeMarketFeed:
    """Market-data feed for a set of markets in the REST client's tournament.

    `rest` and `tokens` must share one RateLimiter: the account's budget. Call `run()`
    once; it returns after `aclose()` and raises if the feed must stop.

    `sink` is called synchronously with every FeedEvent, in order. It must be quick and
    must not raise: an exception from it stops the feed with that exception.
    """

    def __init__(
        self,
        rest: SigRestClient,
        tokens: RealtimeTokenClient,
        transport: RealtimeTransport,
        market_ids: Sequence[str],
        *,
        policy: RealtimeFeedPolicy | None = None,
        sink: Sink | None = None,
        clock: Clock = time.monotonic,
        sleep: Sleep = asyncio.sleep,
        wall_clock: WallClock = utc_now,
    ) -> None:
        if tokens.limiter is not rest.limiter:
            raise ValueError("the REST and token clients must share the account RateLimiter")
        if isinstance(market_ids, str) or not market_ids:
            raise SigArgumentError("market_ids must be a non-empty sequence of id strings")
        ids = list(market_ids)
        if any(not isinstance(i, str) or not i.isascii() or not i.isdigit() for i in ids):
            raise SigArgumentError("every market id must be a string of ASCII digits")
        if len(set(ids)) != len(ids):
            raise SigArgumentError("market_ids must not contain duplicates")
        self._rest = rest
        self._tokens = tokens
        self._transport = transport
        self._policy = policy if policy is not None else RealtimeFeedPolicy()
        self._sink = sink
        self._clock = clock
        self._sleep = sleep
        self._wall_clock = wall_clock
        tournament_id = rest.tournament.id
        self._markets = {
            i: _Market(MarketTopic(tournament_id, i, trade_id_memory=self._policy.trade_id_memory))
            for i in ids
        }
        self._by_topic = {m.topic.topic: m for m in self._markets.values()}
        # A feed-level gate in front of the shared account limiter (see module docstring).
        self._resync_gate = RateLimiter(
            read_limit=self._policy.resync_reads_per_minute,
            write_limit=1,
            clock=clock,
            sleep=sleep,
        )
        self._endpoint: RealtimeEndpoint | None = None
        self._expires_mono: float | None = None
        self._refresh_mono: float | None = None
        self._token_expired = True
        self._last_mint: float | None = None
        self._refresh_task: asyncio.Task[None] | None = None
        self._group: asyncio.TaskGroup | None = None
        self._tasks: set[asyncio.Task[None]] = set()
        self._started = False
        self._closing = asyncio.Event()

    # --- public state -------------------------------------------------------------------

    def topic(self, market_id: str) -> str:
        return self._markets[market_id].topic.topic

    def is_ready(self, market_id: str) -> bool:
        """Whether downstream code may act on the market now (see the module docstring)."""
        return self._ready_now(self._markets[market_id])

    def blockers(self, market_id: str) -> frozenset[StaleReason]:
        """Recovery an orderbook resync cannot provide; the market stays not ready."""
        return frozenset(self._markets[market_id].blockers)

    def books(self, market_id: str) -> dict[str, HeldBook]:
        """Every book held for the market (check `is_ready` before acting on them)."""
        return dict(self._markets[market_id].topic.books)

    def last_revision(self, market_id: str) -> int | None:
        return self._markets[market_id].topic.last_revision

    # --- run ----------------------------------------------------------------------------

    async def run(self) -> None:
        if self._started:
            raise RuntimeError("run() may be called only once")
        self._started = True
        try:
            await self._until_closed(self._rest.verify_tournament())
            credentials = await self._until_closed(self._mint())
            await self._until_closed(
                self._transport.connect(credentials.endpoint, credentials.token)
            )
            self._install(credentials)
            for market in self._markets.values():
                await self._until_closed(self._transport.subscribe(market.topic.topic))
            try:
                async with asyncio.TaskGroup() as group:
                    self._group = group
                    self._spawn(self._timer_loop())
                    await self._receive_loop()
                    for task in list(self._tasks):
                        task.cancel()
            except BaseExceptionGroup as failure:
                if len(failure.exceptions) == 1:
                    raise failure.exceptions[0] from None
                raise
        except _Closed:
            return  # aclose() during start-up
        finally:
            self._group = None
            for market in self._markets.values():
                market.subscribed = False
                self._set_stale(market, StaleReason.STOPPED)
            await self._transport.close()

    async def aclose(self) -> None:
        """Stop the feed. `run()` returns promptly, wherever it is waiting."""
        self._closing.set()
        await self._transport.close()

    async def _until_closed(self, step: Awaitable[T]) -> T:
        """Await `step`, or cancel it and raise _Closed as soon as aclose() is called."""
        if self._closing.is_set():
            if asyncio.iscoroutine(step):
                step.close()
            raise _Closed
        work = asyncio.ensure_future(step)
        closer = asyncio.ensure_future(self._closing.wait())
        try:
            await asyncio.wait({work, closer}, return_when=asyncio.FIRST_COMPLETED)
        finally:
            # Also runs when run() itself is cancelled: nothing is left behind.
            closer.cancel()
            if not work.done():
                work.cancel()
            await asyncio.gather(work, closer, return_exceptions=True)
        if work.cancelled():
            raise _Closed
        return work.result()

    def _spawn(self, coro: Coroutine[Any, Any, None]) -> asyncio.Task[None]:
        assert self._group is not None
        task = self._group.create_task(coro)
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)
        return task

    def _emit(self, event: FeedEvent) -> None:
        if self._sink is not None:
            self._sink(event)

    async def _receive_loop(self) -> None:
        while True:
            try:
                event = await self._until_closed(self._transport.receive())
            except _Closed:
                return
            if event is None:
                return
            self._handle(event)

    def _handle(self, event: TransportEvent) -> None:
        self._note_token_expiry()
        if isinstance(event, SocketEvent):
            reason = (
                StaleReason.SOCKET_ERROR
                if event.state is SocketState.ERROR
                else StaleReason.SOCKET_CLOSED
            )
            for market in self._markets.values():
                market.subscribed = False
                self._want_resync(market, reason)
            return
        market = self._by_topic.get(event.topic)
        if market is None:
            return  # not a topic this feed joined
        if isinstance(event, ChannelStatusEvent):
            if event.status is ChannelStatus.SUBSCRIBED:
                market.subscribed = True
                self._want_resync(market, StaleReason.SUBSCRIBED)
            else:
                market.subscribed = False
                self._want_resync(market, _CHANNEL_REASONS[event.status])
            return
        outcome = market.topic.on_message(event.event, event.payload)
        # A pushed book may arrive with its nextExpiryAt already passed.
        expired = market.topic.take_due_expiry(self._wall_epoch())
        reason = outcome.resync or (StaleReason.BOOK_EXPIRY if expired else None)
        # Invalidate first: every callback below must already see the market not ready.
        market.blockers |= outcome.blockers
        if reason is not None:
            self._want_resync(market, reason)
        for book in outcome.books:
            self._emit(BookUpdated(market.market_id, book))
        for trade in outcome.trades:
            self._emit(TradeReceived(market.market_id, trade))
        for settlement in outcome.settlements:
            self._emit(SettlementSignal(market.market_id, settlement))

    # --- readiness and resync -----------------------------------------------------------

    def _set_stale(self, market: _Market, reason: StaleReason) -> None:
        if reason in BLOCKING_REASONS:
            market.blockers.add(reason)
        if market.ready:
            market.ready = False
            logger.info("realtime market %s not ready: %s", market.market_id, reason)
            self._emit(MarketStale(market.market_id, reason))

    def _want_resync(self, market: _Market, reason: StaleReason) -> None:
        market.wanted += 1
        self._set_stale(market, reason)
        self._start_resync(market)

    def _wall_epoch(self) -> Decimal:
        return epoch_seconds(self._wall_clock())

    def _token_valid(self) -> bool:
        """The installed token has not expired on the clock (timer or not)."""
        return (
            not self._token_expired
            and self._expires_mono is not None
            and self._clock() < self._expires_mono
        )

    def _ready_now(self, market: _Market) -> bool:
        return (
            market.ready
            and not market.blockers
            and self._token_valid()
            and not market.topic.has_expired_book(self._wall_epoch())
        )

    def _can_resync(self, market: _Market) -> bool:
        return (
            market.subscribed
            and self._token_valid()
            and market.done < market.wanted
            and not self._closing.is_set()
        )

    def _start_resync(self, market: _Market) -> None:
        if market.task is None and self._group is not None and self._can_resync(market):
            market.task = self._spawn(self._resync(market))

    async def _resync(self, market: _Market) -> None:
        delay = self._policy.resync_retry_s
        try:
            while self._can_resync(market):
                if market.last_start is not None:
                    wait = market.last_start + self._policy.min_resync_interval_s - self._clock()
                    if wait > 0:
                        await self._sleep(wait)
                        continue
                await self._resync_gate.acquire(RequestKind.READ)
                if not self._can_resync(market):
                    break
                request = market.wanted
                started_at = market.topic.stamp()
                market.last_start = self._clock()
                try:
                    snapshot = await self._rest.get_market_orderbook(
                        market.market_id, depth=self._policy.snapshot_depth
                    )
                    changed = market.topic.apply_snapshot(
                        snapshot.tournament_orderbook().exchanges,
                        depth=self._policy.snapshot_depth,
                        started_at=started_at,
                        now_epoch=self._wall_epoch(),
                    )
                except _PERMANENT:
                    raise
                except (SigClientError, SnapshotError) as exc:
                    floor = _server_wait(exc) if isinstance(exc, SigClientError) else None
                    wait = max(delay, floor or 0.0)
                    logger.warning(
                        "realtime market %s resync failed (%s); retrying in %.1fs",
                        market.market_id,
                        type(exc).__name__,
                        wait,
                    )
                    await self._sleep(wait)
                    delay = min(delay * 2, self._policy.max_retry_s)
                    continue
                delay = self._policy.resync_retry_s
                market.done = request
                self._note_token_expiry()
                if market.topic.has_expired_book(self._wall_epoch()):
                    # The read was older than the expired book it had to replace.
                    self._want_resync(market, StaleReason.BOOK_EXPIRY)
                for book in changed:
                    self._emit(BookUpdated(market.market_id, book))
                if market.wanted != request or not market.subscribed or not self._token_valid():
                    continue
                if market.blockers:
                    logger.warning(
                        "realtime market %s stays not ready: needs %s",
                        market.market_id,
                        ", ".join(sorted(market.blockers)),
                    )
                    continue
                market.ready = True
                logger.info("realtime market %s ready", market.market_id)
                self._emit(MarketReady(market.market_id))
        finally:
            market.task = None

    # --- token lifecycle ----------------------------------------------------------------

    async def _mint(self) -> RealtimeCredentials:
        delay = self._policy.mint_retry_s
        while True:
            if self._last_mint is not None:
                wait = self._last_mint + self._policy.min_mint_interval_s - self._clock()
                if wait > 0:
                    await self._sleep(wait)
            self._last_mint = self._clock()
            try:
                credentials = await self._tokens.mint()
                self._lifetime(credentials)
                return credentials
            except _PERMANENT:
                raise
            except SigClientError as exc:
                # SIG's Retry-After is a floor; the backoff cap never shortens it.
                wait = max(delay, _server_wait(exc) or 0.0)
                logger.warning("realtime token mint failed: %s; retrying in %.0fs", exc, wait)
                await self._sleep(wait)
                delay = min(delay * 2, self._policy.max_retry_s)

    def _lifetime(self, credentials: RealtimeCredentials) -> float:
        """Seconds the token is usable from now: until `expiresAt`, and never past the
        documented 3 hours. A token that is already expired is a response error."""
        remaining = credentials.expires_epoch - epoch_seconds(self._wall_clock())
        lifetime = min(float(remaining), DOCUMENTED_TOKEN_LIFETIME_S)
        if remaining <= Decimal(0) or lifetime <= 0:
            raise SigResponseError(
                method="POST", path=TOKEN_PATH, reason="expiresAt is not in the future"
            )
        return lifetime

    def _install(self, credentials: RealtimeCredentials) -> None:
        now = self._clock()
        lifetime = self._lifetime(credentials)
        self._endpoint = credentials.endpoint
        self._expires_mono = now + lifetime
        # Refresh `refresh_margin_s` early, but never mint more often than allowed.
        self._refresh_mono = max(
            now + lifetime - self._policy.refresh_margin_s,
            now + self._policy.min_mint_interval_s,
        )
        self._token_expired = False

    async def _refresh(self) -> None:
        try:
            credentials = await self._mint()
            if credentials.endpoint != self._endpoint:
                raise RealtimeFeedError(
                    "the realtime endpoint changed on token refresh; restart the feed"
                )
            await self._transport.set_auth(credentials.token)
            self._install(credentials)
            logger.info("realtime token refreshed")
            # [documented] a token refresh requires REST resynchronization.
            for market in self._markets.values():
                self._want_resync(market, StaleReason.TOKEN_REFRESH)
        finally:
            self._refresh_task = None

    async def _timer_loop(self) -> None:
        while True:
            await self._sleep(self._policy.tick_s)
            self._tick()

    def _note_token_expiry(self) -> None:
        """Withdraw readiness everywhere once the token's lifetime has run out."""
        if (
            self._expires_mono is not None
            and self._clock() >= self._expires_mono
            and not self._token_expired
        ):
            self._token_expired = True
            logger.warning("realtime token expired before it was refreshed")
            for market in self._markets.values():
                self._want_resync(market, StaleReason.TOKEN_EXPIRED)

    def _tick(self) -> None:
        if self._closing.is_set():
            return
        self._note_token_expiry()
        if (
            self._refresh_mono is not None
            and self._clock() >= self._refresh_mono
            and self._refresh_task is None
        ):
            self._refresh_task = self._spawn(self._refresh())
        wall = self._wall_epoch()
        for market in self._markets.values():
            if market.topic.take_due_expiry(wall):
                self._want_resync(market, StaleReason.BOOK_EXPIRY)


_CHANNEL_REASONS = {
    ChannelStatus.CHANNEL_ERROR: StaleReason.CHANNEL_ERROR,
    ChannelStatus.TIMED_OUT: StaleReason.CHANNEL_TIMED_OUT,
    ChannelStatus.CLOSED: StaleReason.CHANNEL_CLOSED,
}
