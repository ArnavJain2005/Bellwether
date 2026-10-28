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
- no blocker instance is open or quarantined, no unobserved window is open and the
  market's history is known (below).
The same token and expiry checks gate starting a resync and publishing MarketReady.
A message that needs recovery marks the market not ready (and publishes MarketStale)
before any BookUpdated, TradeReceived or SettlementSignal from that message is
published, so a sink never sees a ready market for data that has invalidated it.
Pushed books and trades keep flowing while a market is not ready, but downstream code
must not act on a market until it is ready again. Each HeldBook also says whether it is
`complete` (a REST side may be cut off at the requested depth) and whether its order
expiry time is known (REST books carry no `nextExpiryAt`).

Blockers and recovery (M2-A, docs/design/market-data-recovery.md sections 4-9): a
settlement requires the authoritative market state, and a trade without a sequence
requires the exchange's price and trade tape over REST. A revision gap, a batch flagged
`resyncRequired`, a malformed message or a batch missing a named book may have hidden
either. A duplicate batch's arrays are never emitted, but a settlement, a null-sequence
trade or an unremembered trade id in one raises its blocker too (M2-B, realtime_topic.py
step 4). Each such event is a blocker instance in the RecoveryLedger (recovery.py), which
the engine owns and which outlives this feed object: a replacement feed given the same
ledger inherits every instance, its last accepted revisions and its windows. Nothing
clears an instance: `blockers(market_id)` lists every open and every quarantined one,
and the market is not ready while any exists. An orderbook resync refreshes the books
but never clears a blocker. A feed constructed without a ledger gets a new one with
unknown history: every market then carries HISTORY_UNKNOWN and is never ready, because a
new process cannot show what happened before it. Restarting is therefore never recovery.

Unobserved windows: a socket or channel break, a re-join, a token refresh or expiry, and
a replacement feed each open a window whose fence is the topic's last accepted revision
at that instant. While D-1 is undecided an open window keeps the market not ready until
clean batches alone advance the revision past the fence (recovery.py). Confirmation
covers the revisioned stream only, never standalone events.

Recovery attempts: one per market at a time, admitted through a feed-wide gate
(RecoveryPolicy: 10 starts per rolling 60 s by default), each with one market-detail
read through `SigRestClient.get_market`. An attempt begins when it asks the gate for
admission; its one overall deadline (30 s) covers that wait, shared account admission,
the request and every retry wait, and is enforced actively. An admission that does not
fit the deadline, or is granted after it, fails the attempt without starting a read.
The gate reserves no account budget: each HTTP attempt of the read still goes through
the shared account RateLimiter, which has no priorities. Failed attempts back off
exponentially with bounded jitter, never shorter than SIG's Retry-After; after
`escalate_after` failures in a row the market is reported escalated and is still retried
at the capped interval. Escalation is scheduling metadata: it is dropped as soon as the
market no longer wants recovery, and it never clears a blocker. A quarantined market is
not retried. A response is applied only by the synchronous observe/commit step: if the
feed has begun closing, the attempt was cancelled (including an external cancellation
that the reader swallowed) or its deadline has passed, it is dropped with no effect but
a log line, and a cancellation is propagated. Otherwise it may only
restrict (a market detail showing `settled` or `closed` quarantines the market with
LIFECYCLE_NOT_OPEN) or, when current, confirm a settlement (a MARKET_SETTLED instance
whose label matches is quarantined). It never clears anything and never publishes
MarketReady; that comes only from the orderbook-resync path, which re-checks every
condition synchronously first.

Lifecycle sweep (M2-B2, design 9.3, AT-26): opt-in, and off unless the feed is given a
SweepPolicy, whose interval, walk deadline and page cap have no defaults (Arnav sets them;
no module in engine/ creates one). One walk at a time reads the complete tournament market
list with `status=any` through `list_all_tournament_markets`, which returns every page or
raises; its pages go through the shared account RateLimiter, and the sweep uses neither
the recovery gate nor the resync gate. A walk that fails, hits its page cap or deadline,
is cancelled, or completes after closing began applies nothing. A complete walk is applied
by one synchronous step: a market listed `settled` or `closed` is quarantined with
LIFECYCLE_NOT_OPEN (current or superseded, as for a lifecycle read) and its readiness is
withdrawn; a market listed `open` changes nothing; a market absent from the list gets an
open LIFECYCLE_UNKNOWN blocker and its readiness is withdrawn, because no documented rule
says what absence means (it is read as neither open nor closed). The sweep never confirms
or clears a blocker, never provides bootstrap evidence and never publishes MarketReady.
Its own page gate (SweepPolicy.pages_per_window) admits each page before the shared
limiter, and the feed refuses a policy whose gated reads leave no shared capacity.

Readiness: `readiness(market_id)` returns a MarketReadiness (readiness.py) explaining
every failing condition; `is_ready(market_id)` is exactly `readiness(...).status is
READY`. Publication uses the same predicate, so MarketReady is never published while
readiness says not ready. Feed readiness never authorizes trading on its own, and protective cancellation
is never gated on it.

Order expiry: a REST read clears a known `nextExpiryAt` only when it is shown to have
been made at or after it (see realtime_topic.py); otherwise the expiry stays outstanding,
the market stays not ready and is read again on the normal resync schedule. A versioned
read is shown by SIG's own `asOf.at`. An unversioned read can be shown only through
`expiry_skew_margin_s`, an operator-configured bound on how far the local clock may run
ahead of SIG's; it is unset (None) by default, and then an unversioned read never clears
a known expiry. No clock synchronization is attempted.

Closing: once `aclose()` begins, `is_ready()` is False for every market, `readiness()`
reports "feed closing", every recovery attempt is cancelled, and a resync or recovery
response that arrives afterwards applies nothing and publishes nothing. No ledger entry
is created or changed after closing begins.

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
import math
import time
from collections.abc import Awaitable, Callable, Coroutine, Sequence
from dataclasses import dataclass
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
from engine.sig_client.readiness import (
    BookState,
    ContinuityState,
    HistoryState,
    MarketReadiness,
    ReadinessStatus,
    TransportState,
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
    TopicOutcome,
)
from engine.sig_client.realtime_wire import BatchTrade, MarketSettled, epoch_seconds
from engine.sig_client.recovery import (
    MISSING,
    WINDOW_MISSING,
    BlockerInstance,
    InstanceSpec,
    InstanceState,
    LedgerOrigin,
    RecoveryAttempt,
    RecoveryLedger,
    RecoveryPolicy,
    SweepPolicy,
    UnobservedWindow,
    WindowCause,
    bounded_jitter,
)
from engine.sig_client.rest import (
    MARKETS_PAGE_MAX,
    ORDERBOOK_DEPTH_MAX,
    Jitter,
    SigRestClient,
    equal_jitter,
)
from engine.sig_client.wire import MarketPricingContext, TournamentMarket

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
    # [client policy] A bound, in seconds, on how far the local wall clock may run ahead of
    # SIG's. None (the default) means unknown: an unversioned REST book then never clears
    # a known order expiry. Set it only with evidence that the bound holds.
    expiry_skew_margin_s: float | None = None

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
        bound = self.expiry_skew_margin_s
        if bound is not None and (
            isinstance(bound, bool)
            or not isinstance(bound, (int, float))
            or not math.isfinite(bound)
            or bound < 0
        ):
            raise ValueError("expiry_skew_margin_s must be None or a finite number >= 0")
        check_positive_int("resync_reads_per_minute", self.resync_reads_per_minute)
        check_positive_int("trade_id_memory", self.trade_id_memory)
        if self.refresh_margin_s >= DOCUMENTED_TOKEN_LIFETIME_S:
            raise ValueError("refresh_margin_s must be shorter than the token lifetime")


@dataclass
class _Market:
    topic: MarketTopic
    subscribed: bool = False
    ready: bool = False  # MarketReady published and not yet withdrawn
    wanted: int = 0  # resync requests so far
    done: int = 0  # the request number the last completed resync covered
    last_start: float | None = None
    task: asyncio.Task[None] | None = None
    # Recovery scheduling (this feed's runtime state; evidence lives in the ledger).
    recovery_task: asyncio.Task[None] | None = None
    attempt: RecoveryAttempt | None = None
    covered: int | None = None  # generation the last applied attempt started at
    failures: int = 0  # consecutive failed attempts
    next_attempt: float | None = None  # monotonic time before which none starts
    escalated: bool = False

    @property
    def market_id(self) -> str:
        return self.topic.market_id


class RealtimeMarketFeed:
    """Market-data feed for a set of markets in the REST client's tournament.

    `rest` and `tokens` must share one RateLimiter: the account's budget. Call `run()`
    once; it returns after `aclose()` and raises if the feed must stop.

    `sink` is called synchronously with every FeedEvent, in order. It must be quick and
    must not raise: an exception from it stops the feed with that exception.

    `ledger` is the engine's RecoveryLedger. Pass the same one to a replacement feed so
    that it inherits every blocker; it is refused while an earlier feed using it has not
    begun closing. Without one, the feed creates a ledger with unknown history, and no
    market is ever ready (module docstring). `recovery` sets the recovery bounds and
    `recovery_jitter` the bounded jitter of their backoff. `sweep` enables the periodic
    lifecycle sweep with its bounds; without one (the default) no sweep runs.
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
        ledger: RecoveryLedger | None = None,
        recovery: RecoveryPolicy | None = None,
        recovery_jitter: Jitter = equal_jitter,
        sweep: SweepPolicy | None = None,
    ) -> None:
        if sweep is not None and not isinstance(sweep, SweepPolicy):
            raise TypeError("sweep must be a SweepPolicy or None")
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
        self._ledger = ledger if ledger is not None else RecoveryLedger()
        self._recovery = recovery if recovery is not None else RecoveryPolicy()
        self._recovery_jitter = recovery_jitter
        self._sweep = sweep
        self._sweep_ids = 0
        self._closing = asyncio.Event()
        # Registering inherits the ledger's state: a replacement feed opens a window on
        # every market an earlier feed registered (fence = the retained revision).
        self._ledger.attach(self, rest.tournament, ids, clock())
        if self._ledger.origin is not LedgerOrigin.UNKNOWN:
            # Only tests create bootstrap ledgers in M2-A (D-3 undecided); make any other
            # use visible.
            logger.warning("realtime feed given a %s recovery ledger", self._ledger.origin)
        tournament_id = rest.tournament.id
        self._markets = {
            i: _Market(
                MarketTopic(
                    tournament_id,
                    i,
                    trade_id_memory=self._policy.trade_id_memory,
                    last_revision=self._ledger.last_revision(i),
                )
            )
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
        # The recovery gate counts attempt starts; it reserves no account budget.
        self._recovery_gate = RateLimiter(
            read_limit=self._recovery.attempts_per_window,
            write_limit=1,
            window=self._recovery.attempt_window_s,
            clock=clock,
            sleep=sleep,
        )
        self._sweep_gate: RateLimiter | None = None
        if sweep is not None:
            self._sweep_gate = self._sweep_budget(sweep, rest.limiter)
        self._attempt_ids = 0
        self._endpoint: RealtimeEndpoint | None = None
        self._expires_mono: float | None = None
        self._refresh_mono: float | None = None
        self._token_expired = True
        self._last_mint: float | None = None
        self._refresh_task: asyncio.Task[None] | None = None
        self._group: asyncio.TaskGroup | None = None
        self._tasks: set[asyncio.Task[None]] = set()
        self._started = False
        bound = self._policy.expiry_skew_margin_s
        self._skew_margin = None if bound is None else Decimal(str(bound))

    # --- public state -------------------------------------------------------------------

    def topic(self, market_id: str) -> str:
        return self._markets[market_id].topic.topic

    @property
    def closing(self) -> bool:
        """True from the moment `aclose()` begins."""
        return self._closing.is_set()

    @property
    def ledger(self) -> RecoveryLedger:
        return self._ledger

    def is_ready(self, market_id: str) -> bool:
        """Exactly `readiness(market_id).status is READY` (see the module docstring).

        Necessary for acting on the market, never sufficient: feed readiness says nothing
        about account state, orders, risk or configuration.
        """
        return self.readiness(market_id).status is ReadinessStatus.READY

    def blockers(self, market_id: str) -> frozenset[StaleReason]:
        """The reason of every open and every quarantined blocker instance.

        None is ever removed in M2-A. A ledger with unknown history reports
        HISTORY_UNKNOWN for every market; a quarantined settlement stays MARKET_SETTLED.
        """
        if market_id not in self._markets:
            raise KeyError(market_id)
        return self._ledger.reasons(market_id)

    def readiness(self, market_id: str) -> MarketReadiness:
        """Why the market is or is not ready now, built from this feed and the ledger."""
        market = self._markets[market_id]
        closing = self._closing.is_set()
        reasons = ["feed closing"] if closing else []
        # The same predicate gates publication in the orderbook-resync path.
        reasons += self._unmet(market)
        if not market.ready:
            reasons.append(
                "not published ready: no orderbook resync has answered the latest trigger "
                "with every other condition met"
            )
        if closing:
            status = ReadinessStatus.NOT_READY
        elif self._ledger.quarantined(market_id):
            status = ReadinessStatus.QUARANTINED
        elif reasons:
            status = ReadinessStatus.NOT_READY
        else:
            status = ReadinessStatus.READY
        wall = self._wall_epoch()
        topic = market.topic
        return MarketReadiness(
            market_id=market_id,
            status=status,
            generation=self._ledger.generation(market_id),
            evaluated_mono=self._clock(),
            evaluated_utc=self._wall_clock(),
            reasons=tuple(reasons),
            closing=closing,
            history=HistoryState(
                origin=self._ledger.origin,
                history_unknown_open=StaleReason.HISTORY_UNKNOWN in self.blockers(market_id),
                bootstrap_declared=self._ledger.declared(market_id),
                history_began=self._ledger.history_began(market_id),
                initial_evidence=self._ledger.initial_evidence(market_id),
            ),
            transport=TransportState(subscribed=market.subscribed, token_valid=self._token_valid()),
            books=tuple(
                BookState(
                    exchange_id=book.exchange_id,
                    version=book.as_of,
                    source=book.source,
                    complete=book.complete,
                    expiry_known=book.expiry_known,
                    next_expiry_at=book.next_expiry_at,
                    expired=book.expiry_epoch is not None and book.expiry_epoch <= wall,
                )
                for book in topic.books.values()
            ),
            continuity=ContinuityState(
                last_accepted_revision=topic.last_revision,
                windows=self._ledger.windows(market_id),
            ),
            lifecycle=self._ledger.lifecycle(market_id),
            blockers=self._ledger.instances(market_id),
            escalated=market.escalated,
            consecutive_failures=market.failures,
            sweep=self._ledger.sweep(market_id),
        )

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
                    if self._sweep is not None:
                        self._spawn(self._sweep_loop(self._sweep))
                    for market in self._markets.values():
                        self._start_recovery(market)  # e.g. HISTORY_UNKNOWN from the start
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
        """Stop the feed. `run()` returns promptly, wherever it is waiting.

        Readiness ends and every recovery attempt is cancelled before anything is awaited.
        """
        self._closing.set()
        for market in self._markets.values():
            if market.attempt is not None:
                market.attempt.cancelled = True
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
        if self._closing.is_set():
            return  # nothing is recorded or published once closing has begun
        self._note_token_expiry()
        if isinstance(event, SocketEvent):
            reason = (
                StaleReason.SOCKET_ERROR
                if event.state is SocketState.ERROR
                else StaleReason.SOCKET_CLOSED
            )
            for market in self._markets.values():
                market.subscribed = False
                self._open_window(market, _WINDOW_CAUSES[reason])
                self._want_resync(market, reason)
            return
        market = self._by_topic.get(event.topic)
        if market is None:
            return  # not a topic this feed joined
        if isinstance(event, ChannelStatusEvent):
            if event.status is ChannelStatus.SUBSCRIBED:
                market.subscribed = True
                # A declared market's first subscription begins its history (bootstrap,
                # tests only); every other SUBSCRIBED is a re-join after a window.
                if not self._ledger.begin_subscription(market.market_id):
                    self._open_window(market, WindowCause.REJOIN)
                self._want_resync(market, StaleReason.SUBSCRIBED)
            else:
                reason = _CHANNEL_REASONS[event.status]
                market.subscribed = False
                self._open_window(market, _WINDOW_CAUSES[reason])
                self._want_resync(market, reason)
            return
        prior = market.topic.last_revision
        outcome = market.topic.on_message(event.event, event.payload)
        # A pushed book may arrive with its nextExpiryAt already passed.
        expired = market.topic.take_due_expiry(self._wall_epoch())
        reason = outcome.resync or (StaleReason.BOOK_EXPIRY if expired else None)
        # Invalidate first: every callback below must already see the market not ready.
        confirmed = self._record_outcome(market, prior, outcome)
        if reason is not None:
            self._want_resync(market, reason)
        for book in outcome.books:
            self._emit(BookUpdated(market.market_id, book))
        for trade in outcome.trades:
            self._emit(TradeReceived(market.market_id, trade))
        for settlement in outcome.settlements:
            self._emit(SettlementSignal(market.market_id, settlement))
        if confirmed:
            self._drop_obsolete_escalation(market)
            self._request_publication(market)

    def _record_outcome(self, market: _Market, prior: int | None, outcome: TopicOutcome) -> bool:
        """Record one message in the ledger, synchronously. True if it confirmed a window."""
        market_id = market.market_id
        now = self._clock()
        window = outcome.window
        specs: list[InstanceSpec] = []
        if StaleReason.MARKET_SETTLED in outcome.blockers:
            # Two settlements are two instances, each with its own signal. A duplicate's
            # settlements raise instances too, but are never emitted (design 4.7).
            specs += [
                InstanceSpec(StaleReason.MARKET_SETTLED, window, item)
                for item in [*outcome.settlements, *outcome.duplicate_settlements]
            ]
        if StaleReason.NULL_TRADE_SEQUENCE in outcome.blockers:
            specs.append(InstanceSpec(StaleReason.NULL_TRADE_SEQUENCE, window))
        if StaleReason.REVISION_ANOMALY in outcome.blockers:
            specs.append(InstanceSpec(StaleReason.REVISION_ANOMALY, window))
        resync = outcome.resync
        if resync in BLOCKING_REASONS and resync not in outcome.blockers:
            revisions = window
            if resync is StaleReason.REVISION_GAP and window is not None and prior is not None:
                revisions = (prior, window[1])  # the missed revisions and the gap batch's own
            specs.append(InstanceSpec(resync, revisions))
        if specs:
            self._ledger.raise_instances(market_id, specs, now)
            logger.warning(
                "realtime market %s blocked%s: %s",
                market_id,
                " by a duplicate batch" if outcome.duplicate else "",
                ", ".join(sorted({spec.reason.value for spec in specs})),
            )
        confirmed = self._ledger.note_revision(
            market_id,
            prior,
            market.topic.last_revision,
            clean=outcome.clean,
            why=f"a batch that was not clean ({resync or 'not accepted cleanly'})",
        )
        for window_id in confirmed:
            logger.info("realtime market %s window %s confirmed", market_id, window_id)
        return bool(confirmed)

    # --- readiness and resync -----------------------------------------------------------

    def _set_stale(self, market: _Market, reason: StaleReason) -> None:
        if market.ready:
            market.ready = False
            logger.info("realtime market %s not ready: %s", market.market_id, reason)
            self._emit(MarketStale(market.market_id, reason))

    def _want_resync(self, market: _Market, reason: StaleReason) -> None:
        market.wanted += 1
        self._set_stale(market, reason)
        # Recovery first: a lifecycle read started now is never later than this resync.
        self._start_recovery(market)
        self._start_resync(market)

    def _open_window(self, market: _Market, cause: WindowCause) -> None:
        """Capture the window's fence in the same step as its event (design 4.3)."""
        if self._closing.is_set():
            return
        self._ledger.open_window(market.market_id, cause, self._clock())

    def _request_publication(self, market: _Market) -> None:
        """A non-book condition was just met (a window confirmed, initial evidence).

        MarketReady comes only from the orderbook-resync path, so when the books are
        already synced and nothing else is unmet, ask for one more resync; it re-checks
        every condition before it publishes.
        """
        if market.ready or market.task is not None or market.done < market.wanted:
            return
        if self._closing.is_set() or self._unmet(market):
            return
        market.wanted += 1
        self._start_resync(market)

    def _unmet(self, market: _Market) -> list[str]:
        """Every unmet readiness condition except closing and the published flag.

        The one eligibility predicate: `readiness()` and every MarketReady publication
        use it, so a market is never published ready while readiness says otherwise.
        """
        market_id = market.market_id
        unmet = []
        if not market.subscribed:
            unmet.append("transport: topic not subscribed")
        if not self._token_valid():
            unmet.append("transport: realtime token not valid")
        if market.topic.has_expired_book(self._wall_epoch()):
            unmet.append(
                "books: a held book's known order expiry has passed; waiting for a read "
                "shown to follow it"
            )
        unmet += [_instance_reason(i) for i in self._ledger.instances(market_id)]
        unmet += [_window_reason(w) for w in self._ledger.open_windows(market_id)]
        if market.escalated:
            unmet.append(
                f"recovery escalated after {market.failures} consecutive failed attempts; "
                "still retried at the capped interval"
            )
        if self._ledger.declared(market_id):
            if not self._ledger.history_began(market_id):
                unmet.append("history: bootstrap declared; first subscription pending")
            elif self._ledger.initial_evidence(market_id) is None:
                unmet.append(
                    "history: bootstrap initial-state evidence pending: a current lifecycle "
                    "read showing open, started after the first subscription (D-3, tests only)"
                )
        return unmet

    def _wall_epoch(self) -> Decimal:
        return epoch_seconds(self._wall_clock())

    def _token_valid(self) -> bool:
        """The installed token has not expired on the clock (timer or not)."""
        return (
            not self._token_expired
            and self._expires_mono is not None
            and self._clock() < self._expires_mono
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
                requested_epoch = self._wall_epoch()
                try:
                    snapshot = await self._rest.get_market_orderbook(
                        market.market_id, depth=self._policy.snapshot_depth
                    )
                    if self._closing.is_set():
                        break  # closing began while the read was in flight
                    changed = market.topic.apply_snapshot(
                        snapshot.tournament_orderbook().exchanges,
                        depth=self._policy.snapshot_depth,
                        started_at=started_at,
                        now_epoch=self._wall_epoch(),
                        requested_epoch=requested_epoch,
                        skew_margin=self._skew_margin,
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
                if self._closing.is_set():
                    break
                # Every other condition, re-checked synchronously before publishing.
                unmet = self._unmet(market)
                if unmet:
                    logger.warning(
                        "realtime market %s stays not ready: %s",
                        market.market_id,
                        "; ".join(unmet),
                    )
                    continue
                market.ready = True
                logger.info("realtime market %s ready", market.market_id)
                self._emit(MarketReady(market.market_id))
        finally:
            market.task = None

    # --- recovery attempts (design 4.5, 4.6, 5) -----------------------------------------

    def _wants_attempt(self, market: _Market) -> bool:
        market_id = market.market_id
        return (
            not self._closing.is_set()
            and not self._ledger.quarantined(market_id)  # a quarantined market is not retried
            and self._ledger.needs_recovery(market_id)
            and market.covered != self._ledger.generation(market_id)
        )

    def _start_recovery(self, market: _Market) -> None:
        if market.recovery_task is None and self._group is not None and self._wants_attempt(market):
            market.recovery_task = self._spawn(self._recover(market))

    async def _recover(self, market: _Market) -> None:
        """One attempt at a time; triggers meanwhile coalesce into one follow-up.

        Only the scheduling backoff waits outside an attempt. An attempt begins when it
        asks the recovery gate for admission, and its overall deadline covers that wait.
        """
        try:
            while self._wants_attempt(market):
                if market.next_attempt is not None:
                    wait = market.next_attempt - self._clock()
                    if wait > 0:
                        await self._sleep(wait)
                        continue
                await self._attempt(market)
        finally:
            market.recovery_task = None
            market.attempt = None
            if not self._closing.is_set():
                self._drop_obsolete_escalation(market)

    def _drop_obsolete_escalation(self, market: _Market) -> None:
        """Clear failure counts and escalation once no recovery is wanted any more.

        Scheduling metadata only: no ledger instance, window or quarantine changes. It
        keeps escalation from outliving its demand and stranding a market not ready with
        no attempt scheduled.
        """
        market_id = market.market_id
        if self._ledger.needs_recovery(market_id) and not self._ledger.quarantined(market_id):
            return
        market.failures = 0
        market.escalated = False
        market.next_attempt = None

    @staticmethod
    def _cancel_requested() -> bool:
        """True if this task was asked to cancel, even if an awaited reader swallowed it."""
        task = asyncio.current_task()
        return task is not None and task.cancelling() > 0

    def _raise_if_cancel_requested(self) -> None:
        if self._cancel_requested():
            raise asyncio.CancelledError

    def _attempt_live(self, attempt: RecoveryAttempt) -> bool:
        return (
            not self._closing.is_set()
            and not attempt.cancelled
            and not self._cancel_requested()
            and self._clock() < attempt.deadline_mono
        )

    async def _attempt(self, market: _Market) -> None:
        """One attempt: admission through the recovery gate, the read, then observe/commit.

        The overall deadline runs from the request for admission and covers the gate
        wait, shared account admission, the request and every retry wait. It is enforced
        actively, by the event loop's clock and by a timer on the injected clock, so a
        hanging wait is cut off; an admission granted after it never starts a read. An
        external cancellation always propagates, even if an awaited reader swallowed it
        and returned a response: nothing is then recorded or published.
        """
        market_id = market.market_id
        now = self._clock()
        self._attempt_ids += 1
        attempt = RecoveryAttempt(
            attempt_id=f"A{self._attempt_ids}",
            market_id=market_id,
            generation=self._ledger.generation(market_id),
            targets=self._ledger.open_instance_ids(market_id),
            started_mono=now,
            deadline_mono=now + self._recovery.attempt_deadline_s,
            started_seq=self._ledger.next_seq(),
        )
        market.attempt = attempt
        phase = "recovery gate admission"
        try:
            try:
                async with asyncio.timeout(self._recovery.attempt_deadline_s) as scope:
                    timer = self._spawn(self._deadline(attempt.deadline_mono, scope))
                    try:
                        await self._recovery_gate.acquire(
                            RequestKind.READ, deadline=attempt.deadline_mono
                        )
                        self._raise_if_cancel_requested()
                        if not self._attempt_live(attempt):
                            # Admitted after the deadline (or closing began): no read.
                            self._attempt_failed(market, attempt, "admitted too late", None)
                            return
                        phase = "market-detail read"
                        read_seq = self._ledger.next_seq()  # this attempt's own read
                        read_started = self._clock()
                        detail = await self._rest.get_market(market_id)
                    finally:
                        timer.cancel()
            except TimeoutError:
                self._raise_if_cancel_requested()
                if not scope.expired():
                    raise
                self._attempt_failed(market, attempt, f"deadline passed during {phase}", None)
                return
            except SigClientError as exc:
                self._raise_if_cancel_requested()
                floor = _server_wait(exc)  # an oversized Retry-After stops the feed
                self._attempt_failed(market, attempt, f"{phase}: {type(exc).__name__}", floor)
                return
            # A reader may have swallowed a cancellation (an external one, or the
            # deadline's) and returned anyway: neither may lead to a commit.
            self._raise_if_cancel_requested()
            if scope.expired():
                self._attempt_failed(market, attempt, f"deadline passed during {phase}", None)
                return
            # No await between the response and the step.
            self._observe_commit(
                market, attempt, detail.tournament_context(), read_seq, read_started, self._clock()
            )
        finally:
            market.attempt = None

    async def _deadline(self, deadline_mono: float, scope: asyncio.Timeout) -> None:
        """Expire an attempt's or a walk's scope when the injected clock reaches its deadline."""
        wait = deadline_mono - self._clock()
        if wait > 0:
            await self._sleep(wait)
        if not scope.expired():  # still admitting or reading (cancelled once it ends)
            scope.reschedule(asyncio.get_running_loop().time())

    def _observe_commit(
        self,
        market: _Market,
        attempt: RecoveryAttempt,
        context: MarketPricingContext,
        read_seq: int,
        read_started: float,
        read_finished: float,
    ) -> None:
        """Design 4.6. Synchronous: there is no await anywhere in this step."""
        market_id = market.market_id
        # 1. Guard: closing and cancellation drop the response with no effect at all.
        if self._closing.is_set() or attempt.cancelled:
            logger.info("realtime market %s recovery %s dropped: feed closing", market_id,
                        attempt.attempt_id)
            return
        if self._cancel_requested():
            logger.info("realtime market %s recovery %s dropped: cancelled", market_id,
                        attempt.attempt_id)
            return
        if self._clock() >= attempt.deadline_mono:
            logger.info("realtime market %s recovery %s dropped: past its deadline", market_id,
                        attempt.attempt_id)
            self._attempt_failed(market, attempt, "response reached the step after the deadline",
                                 None)
            return
        # 2-5. Classify, decide, apply together, raise the generation once.
        result = self._ledger.apply_lifecycle(
            attempt,
            context,
            read_seq=read_seq,
            read_started_mono=read_started,
            read_finished_mono=read_finished,
        )
        logger.info(
            "realtime market %s recovery %s applied (%s): market detail shows %s; "
            "restricted=%s confirmed=%d",
            market_id,
            attempt.attempt_id,
            "current" if result.current else "superseded",
            context.status,
            result.restricted,
            len(result.confirmed),
        )
        # 6. At most a withdrawal; MarketReady comes only from the orderbook-resync path.
        if result.changed:
            reason = (
                StaleReason.LIFECYCLE_NOT_OPEN if result.restricted else StaleReason.MARKET_SETTLED
            )
            self._set_stale(market, reason)
        market.covered = attempt.generation
        market.failures = 0
        market.escalated = False
        market.next_attempt = None
        if result.initial_evidence:
            self._request_publication(market)

    def _attempt_failed(
        self, market: _Market, attempt: RecoveryAttempt, why: str, floor: float | None
    ) -> None:
        """Back off; SIG's Retry-After is a floor that the cap never shortens."""
        if self._closing.is_set() or attempt.cancelled or self._cancel_requested():
            return
        market.failures += 1
        delay = self._recovery.backoff(market.failures)
        wait = max(bounded_jitter(delay, self._recovery_jitter(delay)), floor or 0.0)
        market.next_attempt = self._clock() + wait
        if market.failures >= self._recovery.escalate_after and not market.escalated:
            market.escalated = True
            logger.warning(
                "realtime market %s recovery escalated after %d failed attempts",
                market.market_id,
                market.failures,
            )
        logger.warning(
            "realtime market %s recovery %s failed (%s); next attempt in %.1fs",
            market.market_id,
            attempt.attempt_id,
            why,
            wait,
        )

    # --- lifecycle sweep (M2-B2, design 9.3, AT-26; opt-in) ---------------------------

    def _sweep_budget(self, sweep: SweepPolicy, shared: RateLimiter) -> RateLimiter:
        """The sweep's page gate, after checking that the feed's gated read starts leave
        capacity in the shared account limiter for every other reader.

        Per window of the shared limiter, the resync gate, the recovery gate and the sweep
        gate together may start at most `demand` reads; it must stay strictly below the
        shared read limit. This bounds logical starts only (see SweepPolicy).
        """
        window = shared.window
        demand = (
            sweep.pages_per_window
            + math.ceil(self._policy.resync_reads_per_minute * window / 60.0)
            + math.ceil(
                self._recovery.attempts_per_window * window / self._recovery.attempt_window_s
            )
        )
        limit = shared.limit(RequestKind.READ)
        if demand >= limit:
            raise ValueError(
                f"the sweep page budget leaves no shared read capacity: resyncs, recovery "
                f"attempts and sweep pages may start {demand} reads per {window:g} s, and "
                f"the shared limiter admits {limit}"
            )
        return RateLimiter(
            read_limit=sweep.pages_per_window,
            write_limit=1,
            window=window,
            clock=self._clock,
            sleep=self._sleep,
        )

    async def _sweep_loop(self, policy: SweepPolicy) -> None:
        """One walk at a time: each starts `interval_s` after the feed started or the
        previous walk ended (fixed delay), never earlier than SIG's Retry-After."""
        wait = policy.interval_s
        while not self._closing.is_set():
            await self._sleep(wait)
            if self._closing.is_set():
                return
            floor = await self._sweep_once(policy)
            wait = max(policy.interval_s, floor or 0.0)

    async def _sweep_once(self, policy: SweepPolicy) -> float | None:
        """One complete walk of the market list, then the synchronous commit step.

        Returns SIG's Retry-After floor after a failed walk. A failed, capped or timed-out
        walk applies nothing; an external cancellation propagates even if a reader
        swallowed it, and nothing is then applied.
        """
        self._sweep_ids += 1
        sweep_id = f"S{self._sweep_ids}"
        started = self._clock()
        deadline = started + policy.deadline_s
        # Each market's generation at the start: a change during the walk supersedes it.
        generations = {market_id: self._ledger.generation(market_id) for market_id in self._markets}
        gate = self._sweep_gate
        assert gate is not None

        async def admit_page() -> None:
            # The sweep's own page budget, before the shared limiter (SweepPolicy).
            await gate.acquire(RequestKind.READ, deadline=deadline)

        try:
            async with asyncio.timeout(policy.deadline_s) as scope:
                timer = self._spawn(self._deadline(deadline, scope))
                try:
                    rows = await self._rest.list_all_tournament_markets(
                        status="any",
                        page_limit=MARKETS_PAGE_MAX,
                        max_pages=policy.max_pages,
                        before_page=admit_page,
                    )
                finally:
                    timer.cancel()
        except TimeoutError:
            self._raise_if_cancel_requested()
            if not scope.expired():
                raise
            logger.warning("realtime lifecycle sweep %s failed (deadline passed); nothing applied",
                           sweep_id)
            return None
        except SigClientError as exc:
            self._raise_if_cancel_requested()
            floor = _server_wait(exc)  # an oversized Retry-After stops the feed
            logger.warning("realtime lifecycle sweep %s failed (%s); nothing applied", sweep_id,
                           type(exc).__name__)
            return floor
        self._raise_if_cancel_requested()
        if scope.expired():
            logger.warning("realtime lifecycle sweep %s failed (deadline passed); nothing applied",
                           sweep_id)
            return None
        # No await between the complete list and the step.
        self._commit_sweep(sweep_id, generations, rows, started, self._clock(), deadline)
        return None

    def _commit_sweep(
        self,
        sweep_id: str,
        generations: dict[str, int],
        rows: Sequence[TournamentMarket],
        started: float,
        finished: float,
        deadline: float,
    ) -> None:
        """Design 4.6 applied to a complete walk, restriction only. Synchronous."""
        # 1. Guard: a walk is applied only while the feed is live and before its deadline.
        if self._closing.is_set() or self._cancel_requested():
            logger.info("realtime lifecycle sweep %s dropped: feed closing or cancelled",
                        sweep_id)
            return
        if self._clock() >= deadline:
            logger.info("realtime lifecycle sweep %s dropped: past its deadline", sweep_id)
            return
        by_id = {row.id: row for row in rows}
        # 2-5 for every market first, so that every withdrawal below is already in effect
        # when the first callback runs.
        restricted: list[tuple[_Market, StaleReason]] = []
        absent = []
        for market_id, market in self._markets.items():
            row = by_id.get(market_id)
            if row is None:
                absent.append(market_id)
            added = self._ledger.apply_listing(
                market_id,
                sweep_id=sweep_id,
                generation=generations[market_id],
                row=row,
                walk_started_mono=started,
                walk_finished_mono=finished,
            )
            if added is not None:
                restricted.append((market, added))
        if absent:
            # No documented rule says what absence from a complete status=any list means
            # (Q16): the market is blocked with LIFECYCLE_UNKNOWN, not read as open or closed.
            logger.warning(
                "realtime lifecycle sweep %s: %d market(s) absent from the complete market "
                "list (%s); lifecycle unknown, blocked",
                sweep_id,
                len(absent),
                ", ".join(absent),
            )
        logger.info("realtime lifecycle sweep %s applied: %d market(s) restricted", sweep_id,
                    len(restricted))
        # 6. At most a withdrawal; MarketReady comes only from the orderbook-resync path.
        for market, reason in restricted:
            self._set_stale(market, reason)

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
                self._open_window(market, WindowCause.TOKEN_REFRESH)
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
                self._open_window(market, WindowCause.TOKEN_EXPIRED)
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

_WINDOW_CAUSES = {
    StaleReason.SOCKET_ERROR: WindowCause.SOCKET_ERROR,
    StaleReason.SOCKET_CLOSED: WindowCause.SOCKET_CLOSED,
    StaleReason.CHANNEL_ERROR: WindowCause.CHANNEL_ERROR,
    StaleReason.CHANNEL_TIMED_OUT: WindowCause.CHANNEL_TIMED_OUT,
    StaleReason.CHANNEL_CLOSED: WindowCause.CHANNEL_CLOSED,
}


def _revisions_text(revisions: tuple[int, int] | None) -> str:
    if revisions is None:
        return "revisions unknown"
    after, through = revisions
    return f"revisions {after + 1}-{through}"


def _instance_reason(instance: BlockerInstance) -> str:
    """One readiness line for a blocker instance, with the ids it is missing."""
    reason = instance.reason
    missing = ", ".join(MISSING[reason])
    if reason is StaleReason.HISTORY_UNKNOWN:
        return (
            f"history unknown: no retained blocker history ({instance.instance_id}); "
            f"missing {missing}"
        )
    if reason is StaleReason.LIFECYCLE_UNKNOWN:
        return (
            f"lifecycle unknown: absent from a complete market list ({instance.instance_id}); "
            f"read as neither open nor closed, and nothing clears it; missing {missing}"
        )
    if instance.state is InstanceState.QUARANTINED:
        return (
            f"blocker: {reason.value} ({instance.instance_id}) quarantined under local "
            f"policy, not proof SIG cannot reopen or correct it; missing {missing}"
        )
    where = _revisions_text(instance.revisions)
    finding = f"; {instance.evidence[-1].finding}" if instance.evidence else ""
    if reason is StaleReason.REVISION_ANOMALY:
        finding += (
            "; the trade may have been accepted already, its id evicted from the topic's "
            "bounded trade-id memory or never seen by this feed's topic (a replacement "
            "feed's memory starts empty): a possible false positive that still blocks"
        )
    return (
        f"blocker: {reason.value} ({instance.instance_id}, {where}) open{finding}; "
        f"missing {missing}"
    )


def _window_reason(window: UnobservedWindow) -> str:
    causes = ", ".join(cause.value for cause in window.causes)
    missing = ", ".join(WINDOW_MISSING)
    if window.unconfirmable is not None:
        return (
            f"continuity: unobserved window {window.window_id} ({causes}) open and cannot "
            f"be confirmed: {window.unconfirmable}; missing {missing}"
        )
    return (
        f"continuity: unobserved window {window.window_id} ({causes}) open since fence "
        f"revision {window.fence}; waiting for a clean batch past it; missing {missing}"
    )
