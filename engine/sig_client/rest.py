"""Read-only SIG REST client (S1-T2-B; market detail and trade tape added in S1-T3-B;
raw tournament account reads added in A2-A).

Scope: thirteen documented GET endpoints, always pinned to the configured tournament. There
are no write methods (orders, cancels, realtime tokens) and no public way to issue an
arbitrary request.

Account reads (A2-A; docs/design/account-state-contracts.md, section 8):
- `read_tournament_balance` always makes a fresh GET /tournaments/{slug} and checks the
  echoed slug and UUID. It never returns the verification cache as an observation.
- `read_tournament_positions` reads GET /tournaments/{slug}/portfolio/positions. Its
  documented 409 for holdings without a valuation price is returned as
  `UnvaluedHoldingsRead` when the body is a valid `MissingTournamentValuation`; any
  other 409 raises SigConflictError as before.
- `read_open_orders` walks GET /orders?status=open and keeps every page's coverage and
  provenance.

Further account reads (A2-B), on the same machinery:
- `read_tournament_fills` walks GET /tournaments/{slug}/portfolio/fills.
- `read_tournament_pnl` reads GET /tournaments/{slug}/portfolio/pnl, sending `period`
  explicitly ("all" by default), with the same typed 409 as positions.
- `read_tournament_settlements` walks GET /tournaments/{slug}/portfolio/settlements and
  is never labelled complete if any page's economic-command coverage reported false.
- `read_order_fills` walks GET /orders/{id}/fills; every page must echo the configured
  tournament and the same lifetime totals.
Each returns a `ReadProvenance` built from that call's own local variables: there is no
shared "last response" state, so concurrent calls and retries cannot mix observations.
Provenance is our own local record, never evidence of server freshness, atomicity or
readiness. No account value is converted into domain units.

Tournament scoping: before any scoped read, the client calls GET /tournaments/{slug}
once and checks that SIG reports the configured slug and UUID. A mismatch is permanent
for the client's lifetime, and the first mismatch observed is the one kept. Once it is
latched, no request or retry starts: every read re-checks the latch before rate
admission, after it, and when a response arrives, so a read that was already waiting
(on verification, admission or a retry wait) sends nothing more and returns nothing.
UUID-scoped reads always send `tournamentId`, and the echoed
context of the market orderbook and the market detail is checked as well. The trade
tape echoes no tournament, so its scope rests on the `tournamentId` sent.

Reporting projection: the trade tape (like /orders) may carry `coverage`, metadata about
SIG's reporting projection. It is kept exactly as sent, per page. A complete cursor walk
shows only that every page of the projection was read; it does not show that the
projection has caught up with the trading engine, and `projectedThroughSequence` is never
compared here with any realtime sequence.

Transport and credentials:
- The caller injects an `httpx.AsyncClient`. The SIG client closes it only when it was
  created with `owns_http=True` (as `SigRestClient.create` does).
- Every request carries the configured per-phase timeouts and `follow_redirects=False`;
  a redirect is returned as SigUnexpectedStatusError and never followed, so the key is
  never forwarded to another host.
- The key is added per request in the Authorization header. It is never put in a URL,
  log line, exception or repr. An injected client with its own auth or credential
  headers is refused. Errors and logs carry no text taken from a response body (see
  errors.py).
- httpx itself logs each request URL at INFO on the "httpx" logger. A cursor walk's URL
  carries the previous page's `nextCursor`, which is response data, so in the line for
  a request this client sends, that one query value is logged as REDACTED. Ownership is
  per request object: requests of any other httpx client (including ones made inside an
  event hook of this client's httpx client, or by tasks created there) are logged
  unchanged. The rest of the line (method, URL, configuration-derived parameters,
  status) is kept, and no logger is disabled or reconfigured.

Deadlines and retries (ReadRetryPolicy):
- One logical read (one public page or object request) has one overall deadline,
  `total_budget_s` from its start on the injected clock. It covers rate admission, every
  attempt (sending, waiting, reading the body) and every retry wait. Per-phase httpx
  timeouts alone would not bound the whole read. A response that completes at or after
  the deadline is not returned. SigDeadlineExceededError names the phase that ran out.
- Separate budgets: the one-time tournament verification is its own logical read, and
  so is every page of a collection. There is no collection-wide deadline: a full
  collection may take up to `max_pages` budgets, plus one for verification.
- Every await in a logical read (rate admission, the request, each retry sleep) runs
  under `asyncio.timeout` for the time remaining on the injected clock, so a wait that
  never returns is still cut off. Clock checks after each await stay as a second line
  of defence. In production the injected clock is `time.monotonic`, the clock the event
  loop's timers use.
- Retried: HTTP 429 and 503 (documented transient), and the transport failures in
  RETRYABLE_TRANSPORT_ERRORS. Everything else is raised at once.
- Each attempt consumes read budget. The delay is the larger of Retry-After and a
  jittered exponential backoff, so a retry never comes earlier than SIG instructed.
  Retry-After is never shortened: it is parsed exactly and rounded up to a float. A wait
  that does not fit in the remaining budget ends the read; it is not trimmed.
- A 429 imposes its full Retry-After as a cooldown on the rate limiter. The cooldown
  outlives the call and applies to every client sharing that limiter. A 429 with no
  Retry-After, or an unparseable one, uses the documented 60 s. A valid number too
  large to hold safely (above MAX_COOLDOWN_SECONDS) blocks automatic read admission
  until RateLimiter.clear_block(); it never becomes infinity or a shorter wait.
- An unparseable or oversized Retry-After is never retried automatically.
These rules are for reads only. They say nothing about retrying orders or cancels.

Response checks are labelled in the code as [documented] when the spec states the
property, or [client policy] when it is an extra consistency check of ours.
"""

from __future__ import annotations

import asyncio
import functools
import itertools
import json
import logging
import math
import random
import re
import time
from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any, Protocol, Self, TypeVar
from urllib.parse import quote

import httpx
from pydantic import BaseModel, ValidationError

from engine.config import SigConfig, TournamentRef
from engine.sig_client.account_reads import (
    CoverageState,
    FillsWalk,
    MissingValuation,
    OrderFillsWalk,
    OrdersWalk,
    PageCoverage,
    PageRecord,
    PnlRead,
    PositionsRead,
    ReadProvenance,
    ScopeEvidence,
    SettlementsWalk,
    TournamentBalanceRead,
    UnvaluedHoldingsRead,
    VerificationUse,
)
from engine.sig_client.errors import (
    SigApiError,
    SigArgumentError,
    SigAuthError,
    SigBadRequestError,
    SigClientClosedError,
    SigConflictError,
    SigContextMismatchError,
    SigDeadlineExceededError,
    SigNotFoundError,
    SigPaginationError,
    SigRateBudgetError,
    SigRateLimitedError,
    SigResponseError,
    SigRetryExhaustedError,
    SigServerError,
    SigServiceUnavailableError,
    SigTransportError,
    SigUnexpectedStatusError,
    known_code,
)
from engine.sig_client.rate_limiter import (
    MAX_COOLDOWN_SECONDS,
    Clock,
    RateLimiter,
    RequestKind,
    Sleep,
    check_positive_int,
    check_positive_seconds,
)
from engine.sig_client.realtime_wire import epoch_seconds, parse_instant
from engine.sig_client.wire import (
    BulkExchangePrices,
    CombinedMarketOrderbook,
    EconomicCommandCoverage,
    ExchangeOrderbook,
    ExchangeTrade,
    ExchangeTradesPage,
    Market,
    MarketOrderbook,
    MarketOrderbookContext,
    MarketPricingContext,
    MissingTournamentValuation,
    Order,
    OrderbookLevel,
    OrderFillsPage,
    OrdersPage,
    Tournament,
    TournamentFillsPage,
    TournamentMarket,
    TournamentMarketsPage,
    TournamentPnl,
    TournamentPositions,
    TournamentSettlementsPage,
    TradeCoverage,
)

logger = logging.getLogger(__name__)

# Documented limits (docs/api/openapi.json).
MARKETS_PAGE_MAX = 100  # #/paths/~1tournaments~1{slug}~1markets/get/parameters/1
ORDERS_PAGE_MAX = 200  # #/paths/~1orders/get/parameters/3
TRADES_PAGE_MAX = 200  # #/paths/~1exchanges~1{id}~1trades/get/parameters/4
BULK_PRICE_IDS_MAX = 100  # #/paths/~1exchanges~1prices/get/parameters/0
ORDERBOOK_DEPTH_MAX = 200  # #/paths/~1exchanges~1{id}~1orderbook/get/parameters/1
DEFAULT_ORDERBOOK_DEPTH = 20
MARKET_STATUSES = frozenset({"open", "closed", "settled", "any"})  # "resolved" is deprecated
ORDER_STATUSES = frozenset({"open", "closed", "expired", "all"})
FILLS_PAGE_MAX = 200  # #/paths/~1tournaments~1{slug}~1portfolio~1fills/get/parameters/3
SETTLEMENTS_PAGE_MAX = 200  # #/paths/~1tournaments~1{slug}~1portfolio~1settlements/get/parameters/1
ORDER_FILLS_PAGE_MAX = 200  # #/paths/~1orders~1{id}~1fills/get/parameters/1
# #/paths/~1tournaments~1{slug}~1portfolio~1pnl/get/parameters/1 (documented default: quarter)
PNL_PERIODS = frozenset({"day", "week", "month", "quarter", "year", "all"})
DEFAULT_MAX_PAGES = 50

# All token patterns are applied with fullmatch(); `$` with match() accepts a final "\n".
_NUMERIC_ID = re.compile(r"[0-9]+")
_RETRY_AFTER = re.compile(r"[0-9]+(\.[0-9]+)?")
_UUID_TEXT = re.compile(
    r"[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}"
)
_LOC_PART = re.compile(r"[A-Za-z_][A-Za-z0-9_]{0,63}")

# Read transport failures that are safe to retry: the request either never reached SIG
# or a GET's response was lost. Proxy, protocol-usage and unsupported-scheme errors are
# configuration bugs and are raised at once.
RETRYABLE_TRANSPORT_ERRORS: tuple[type[httpx.TransportError], ...] = (
    httpx.ConnectError,
    httpx.ConnectTimeout,
    httpx.ReadTimeout,
    httpx.WriteTimeout,
    httpx.PoolTimeout,
    httpx.ReadError,
    httpx.WriteError,
    httpx.RemoteProtocolError,
)

Jitter = Callable[[float], float]


def equal_jitter(delay: float) -> float:
    """A random delay in [delay / 2, delay]."""
    return random.uniform(delay / 2, delay)


@dataclass(frozen=True)
class ReadRetryPolicy:
    """Limits for one logical read. Every value is validated; none may be infinite."""

    max_attempts: int = 4
    total_budget_s: float = 30.0
    base_backoff_s: float = 0.25
    max_backoff_s: float = 4.0
    # Cooldown for a 429 with no Retry-After or an unparseable one; the spec documents
    # `Retry-After: 60`. A valid server value is always used as sent, never this.
    rate_limited_default_wait_s: float = 60.0

    def __post_init__(self) -> None:
        check_positive_int("max_attempts", self.max_attempts)
        for name in (
            "total_budget_s",
            "base_backoff_s",
            "max_backoff_s",
            "rate_limited_default_wait_s",
        ):
            check_positive_seconds(name, getattr(self, name))
        if self.base_backoff_s > self.max_backoff_s:
            raise ValueError("base_backoff_s must not exceed max_backoff_s")
        if self.rate_limited_default_wait_s > MAX_COOLDOWN_SECONDS:
            raise ValueError("rate_limited_default_wait_s must not exceed MAX_COOLDOWN_SECONDS")

    def backoff(self, attempt: int) -> float:
        # The exponent is capped so a large attempt number cannot overflow.
        return min(self.max_backoff_s, self.base_backoff_s * 2 ** min(attempt - 1, 60))


@dataclass(frozen=True)
class ExchangeTradeHistory:
    """A complete trade tape for one exchange and window, as SIG reported it.

    `trades` is newest first, across every page. `window_from`/`window_to` are the
    window SIG echoed on every page (inclusive / exclusive), as sent. `page_coverage`
    holds each page's `coverage` exactly as sent, in page order: None where SIG omitted
    it, and a None `projectedThroughSequence` where SIG reported no checkpoint. Pages
    may report different checkpoints. None of this shows that the reporting projection
    has caught up with the trading engine.
    """

    exchange_id: str
    market_id: str
    window_from: str
    window_to: str
    trades: tuple[ExchangeTrade, ...]
    page_coverage: tuple[TradeCoverage | None, ...]


WallClock = Callable[[], datetime]


def utc_now() -> datetime:
    """The local wall clock in UTC (display only; never used for ordering or deadlines)."""
    return datetime.now(UTC)


# Route templates recorded in ReadProvenance.
TOURNAMENT_ROUTE = "/tournaments/{slug}"
POSITIONS_ROUTE = "/tournaments/{slug}/portfolio/positions"
ORDERS_ROUTE = "/orders"
FILLS_ROUTE = "/tournaments/{slug}/portfolio/fills"
PNL_ROUTE = "/tournaments/{slug}/portfolio/pnl"
SETTLEMENTS_ROUTE = "/tournaments/{slug}/portfolio/settlements"
ORDER_FILLS_ROUTE = "/orders/{id}/fills"

# Query parameters a ReadProvenance may record (design 4.1 R6). A cursor is response
# data: only the fact that one was sent is recorded, never its value.
_RECORDED_PARAMS = frozenset({"tournamentId", "status", "limit", "period", "type"})


@dataclass(frozen=True)
class _Exchange:
    """One completed logical GET, as this call observed it. Private; never logged.

    Built only from the local variables of one `_get_observed` call, so concurrent reads,
    and the attempts of one read, can never share or overwrite it.
    """

    path: str
    params: tuple[tuple[str, str | int], ...]
    status: int
    attempts: int
    started_mono: float
    finished_mono: float
    finished_utc: datetime
    body: object  # the decoded 2xx body; None for an accepted 409
    conflict: tuple[MissingValuation, ...] | None  # set only for an accepted 409


@dataclass(frozen=True)
class _ObservedOrdersPage:
    page: OrdersPage
    exchange: _Exchange
    verification: VerificationUse
    verification_read: ReadProvenance | None


def _recorded_params(
    params: tuple[tuple[str, str | int], ...],
) -> tuple[tuple[tuple[str, str], ...], bool]:
    """The allowlisted parameters as sent (sorted), and whether a cursor was sent."""
    recorded: list[tuple[str, str]] = []
    cursor_sent = False
    for name, value in params:
        if name == "cursor":
            cursor_sent = True
        elif name in _RECORDED_PARAMS:
            recorded.append((name, str(value)))
        else:
            # A programming error: every parameter of an account read is accounted for.
            raise ValueError(f"query parameter {name} is not on the provenance allowlist")
    return tuple(sorted(recorded)), cursor_sent


# Query parameters whose values come from a response (design 4.1 R8: never logged).
_RESPONSE_DERIVED_QUERY_PARAMS = frozenset({"cursor"})
_REDACTED = "REDACTED"
# Every SIG request currently inside httpx's `send`, keyed by id() and holding the request
# object itself, so an id can never match once its request is gone. `_send` adds its own
# request before sending and removes it in `finally` (success, failure or cancellation).
# Ownership is per request object, not per task, context or time window: requests made by
# other clients (in event hooks, in tasks spawned there, or concurrently) are never in it.
_SIG_REQUESTS_IN_FLIGHT: dict[int, httpx.Request] = {}


def _is_sig_request_url(arg: object) -> bool:
    """True if `arg` is the current URL object of a SIG request being sent.

    Identity, not equality: another request to the same URL text is not a SIG request.
    The request's *current* `url` is compared, so a hook that replaces it is still covered.
    """
    if not isinstance(arg, httpx.URL):
        return False
    return any(request.url is arg for request in list(_SIG_REQUESTS_IN_FLIGHT.values()))


def _redacted_url(url: httpx.URL) -> httpx.URL:
    """`url` with every response-derived query value replaced; otherwise unchanged."""
    params = url.params
    if not any(name in params for name in _RESPONSE_DERIVED_QUERY_PARAMS):
        return url
    return url.copy_with(
        params=[
            (name, _REDACTED if name in _RESPONSE_DERIVED_QUERY_PARAMS else value)
            for name, value in params.multi_items()
        ]
    )


class _RequestLogRedaction(logging.Filter):
    """Redacts response-derived query values in httpx's request log lines.

    Attached once to the "httpx" logger. It rewrites only a record whose URL argument is
    the URL of a SIG request being sent (`_is_sig_request_url`); every other record,
    including those of other clients' requests made while a SIG request is in flight, is
    passed on unchanged. It never drops a record. If redacting a SIG URL fails, every URL
    in that record is withheld instead (fail closed).
    """

    def filter(self, record: logging.LogRecord) -> bool:
        args = record.args
        if not isinstance(args, tuple):
            return True
        owned = [_is_sig_request_url(arg) for arg in args]
        if not any(owned):
            return True
        try:
            record.args = tuple(
                _redacted_url(arg) if mine else arg for arg, mine in zip(args, owned, strict=True)
            )
        except Exception:  # noqa: BLE001 - logging must never break the request
            record.args = tuple(
                "<url withheld>" if isinstance(arg, httpx.URL) else arg for arg in args
            )
        return True


_REQUEST_LOG_REDACTION = _RequestLogRedaction()


def _missing_valuations(content: bytes) -> tuple[MissingValuation, ...] | None:
    """The (exchange, market) pairs of a documented 409 MissingTournamentValuation, or None.

    None for anything else: another code, a malformed body, an empty list or a non-digit
    id. The caller then raises SigConflictError exactly as for any other 409. Nothing
    from the body is logged or put in an error, and titles and settlement labels are not
    kept.
    """
    try:
        data = json.loads(content, parse_float=Decimal, parse_constant=_reject_constant)
        body = MissingTournamentValuation.model_validate(data)
    except (ValueError, ValidationError):  # bad JSON or encoding, or not the schema
        return None
    return tuple(
        MissingValuation(exchange_id=item.exchangeId, market_id=item.marketId)
        for item in body.error.details.missingValuations
    )


class _Page(Protocol):
    @property
    def data(self) -> Sequence[Any]: ...

    @property
    def pagination(self) -> Any: ...


ModelT = TypeVar("ModelT", bound=BaseModel)
PageT = TypeVar("PageT", bound=_Page)
T = TypeVar("T")

_STATUS_ERRORS: dict[int, type[SigApiError]] = {
    400: SigBadRequestError,
    401: SigAuthError,
    403: SigAuthError,
    404: SigNotFoundError,
    409: SigConflictError,
    429: SigRateLimitedError,
    503: SigServiceUnavailableError,
}


def _reject_constant(name: str) -> None:
    raise ValueError(f"non-finite JSON constant {name}")


@dataclass(frozen=True)
class _RetryAfter:
    """A parsed Retry-After header. All flags False and `seconds` None means absent."""

    seconds: float | None = None
    malformed: bool = False
    oversized: bool = False


_MAX_COOLDOWN_DIGITS = len(str(int(MAX_COOLDOWN_SECONDS)))


def _not_shorter(seconds: Decimal) -> float:
    """The nearest float that is not below `seconds` (float() alone may round down)."""
    value = float(seconds)
    if Decimal(value) < seconds:
        value = math.nextafter(value, math.inf)
    return value


def _parse_retry_after(value: str | None) -> _RetryAfter:
    """Delta-seconds only (no HTTP-date support). Never shortens the server's wait.

    A valid number above MAX_COOLDOWN_SECONDS is reported as oversized. It is not
    clamped, not converted to infinity, and not replaced with a default.
    """
    if value is None:
        return _RetryAfter()
    text = value.strip()
    if not _RETRY_AFTER.fullmatch(text):
        return _RetryAfter(malformed=True)
    # Cheap length test first, so a huge digit string is never converted at all.
    if len(text.partition(".")[0].lstrip("0")) > _MAX_COOLDOWN_DIGITS:
        return _RetryAfter(oversized=True)
    seconds = Decimal(text)
    if seconds > Decimal(int(MAX_COOLDOWN_SECONDS)):
        return _RetryAfter(oversized=True)
    return _RetryAfter(seconds=_not_shorter(seconds))


def _check_int(name: str, value: object, low: int, high: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or not low <= value <= high:
        raise SigArgumentError(f"{name} must be an integer from {low} to {high}")
    return value


def _check_numeric_id(name: str, value: object) -> str:
    if not isinstance(value, str) or not _NUMERIC_ID.fullmatch(value):
        raise SigArgumentError(f"{name} must be a string of ASCII digits")
    return value


def _check_optional_id(name: str, value: object) -> str | None:
    return None if value is None else _check_numeric_id(name, value)


def _check_choice(name: str, value: object, choices: frozenset[str]) -> str:
    if value not in choices:
        raise SigArgumentError(f"{name} must be one of {sorted(choices)}")
    return value  # type: ignore[return-value]


def _check_cursor(cursor: object) -> str | None:
    if cursor is not None and (not isinstance(cursor, str) or not cursor):
        raise SigArgumentError("cursor must be a non-empty string or None")
    return cursor


def _check_instant(name: str, value: object) -> datetime | None:
    """A timezone-aware datetime in whole milliseconds, returned in UTC, or None."""
    if value is None:
        return None
    if not isinstance(value, datetime) or value.utcoffset() is None:
        raise SigArgumentError(f"{name} must be a timezone-aware datetime or None")
    try:
        moment = value.astimezone(UTC)
    except (OverflowError, ValueError):
        raise SigArgumentError(f"{name} is out of range in UTC") from None
    # [client policy] SIG's documented examples carry milliseconds; a finer value would
    # be truncated or rejected somewhere we cannot see, moving the window's edge.
    if moment.microsecond % 1000:
        raise SigArgumentError(f"{name} must be a whole number of milliseconds")
    return moment


def _format_instant(moment: datetime) -> str:
    """A UTC datetime as the spec's example format, e.g. 2026-01-01T00:00:00.000Z."""
    return (
        f"{moment.year:04d}-{moment.month:02d}-{moment.day:02d}T"
        f"{moment.hour:02d}:{moment.minute:02d}:{moment.second:02d}."
        f"{moment.microsecond // 1000:03d}Z"
    )


def _check_order_id(order_id: object) -> int:
    # Order ids are integers on the wire (#/components/schemas/Order/properties/id).
    if isinstance(order_id, bool) or not isinstance(order_id, int) or order_id < 0:
        raise SigArgumentError("order_id must be a non-negative integer")
    return order_id


def _optional_coverage(page: Any) -> tuple[CoverageState, PageCoverage | None]:
    """A page whose coverage is documented as optional: kept as sent, or omitted."""
    if page.coverage is None:
        return CoverageState.OMITTED, None
    return CoverageState.PRESENT, page.coverage


class _NewestFirst:
    """[documented] newest-first order, checked within each page and across pages.

    Timestamps are compared as exact instants; equal timestamps are allowed. Values are
    response data and are never repeated in an error.
    """

    def __init__(self, path: str, field: str) -> None:
        self._path = path
        self._field = field
        self._oldest: Decimal | None = None  # the previous page's last (oldest) instant

    def check(self, stamps: Sequence[str]) -> None:
        times = [_response_instant(self._path, self._field, stamp) for stamp in stamps]
        if any(newer < older for newer, older in itertools.pairwise(times)):
            raise SigResponseError(
                method="GET",
                path=self._path,
                reason=f"items are not sorted newest first by {self._field}",
            )
        if times:
            if self._oldest is not None and times[0] > self._oldest:
                raise SigPaginationError(
                    method="GET",
                    path=self._path,
                    reason="a page holds items newer than the previous page's",
                )
            self._oldest = times[-1]


def _response_instant(path: str, field: str, text: str) -> Decimal:
    """A response timestamp as exact epoch seconds; the value is never echoed."""
    try:
        return parse_instant(text)
    except ValueError:
        raise SigResponseError(
            method="GET", path=path, reason=f"{field} is not an ISO-8601 timestamp with an offset"
        ) from None


def _segment(value: str) -> str:
    return quote(value, safe="")


def _same_uuid(reported: str, expected: str) -> bool | None:
    """True/False for a canonical UUID string (any case); None if it is not one."""
    if not _UUID_TEXT.fullmatch(reported):
        return None
    return reported.lower() == expected


def _validation_summary(exc: ValidationError) -> str:
    """Field locations and error types only; never input values or response keys."""
    parts = []
    for error in exc.errors(include_input=False, include_url=False)[:5]:
        loc = ".".join(
            str(part)
            if isinstance(part, int) or (isinstance(part, str) and _LOC_PART.fullmatch(part))
            else "<?>"
            for part in error["loc"]
        )
        parts.append(f"{loc or '<root>'}: {error['type']}")
    return "; ".join(parts)


class SigRestClient:
    """Read-only client for one SIG account and one configured tournament.

    Pass the account's shared RateLimiter as `limiter`. Without one, this client gets a
    private limiter that does not coordinate with any other client of the account.
    """

    def __init__(
        self,
        config: SigConfig,
        http: httpx.AsyncClient,
        *,
        owns_http: bool = False,
        limiter: RateLimiter | None = None,
        retry: ReadRetryPolicy | None = None,
        clock: Clock = time.monotonic,
        sleep: Sleep = asyncio.sleep,
        jitter: Jitter = equal_jitter,
        wall_clock: WallClock = utc_now,
    ) -> None:
        if http.auth is not None:
            raise ValueError("the injected httpx client must not carry its own auth")
        if "authorization" in http.headers or "x-api-key" in http.headers:
            raise ValueError("the injected httpx client must not carry credential headers")
        self._config = config
        self._http = http
        self._owns_http = owns_http
        self._clock = clock
        self._sleep = sleep
        self._jitter = jitter
        self._wall_clock = wall_clock
        # A shared limiter must use the same clock as this client (deadlines compare).
        self._limiter = limiter if limiter is not None else RateLimiter(clock=clock, sleep=sleep)
        self._retry = retry if retry is not None else ReadRetryPolicy()
        self._closed = False
        self._verified: Tournament | None = None
        self._mismatch: SigContextMismatchError | None = None
        self._verify_lock = asyncio.Lock()
        # Idempotent: Logger.addFilter ignores a filter that is already attached.
        logging.getLogger("httpx").addFilter(_REQUEST_LOG_REDACTION)

    @classmethod
    def create(
        cls,
        config: SigConfig,
        *,
        transport: httpx.AsyncBaseTransport | None = None,
        **kwargs: Any,
    ) -> SigRestClient:
        """Build a client that owns its httpx client and closes it in `aclose()`."""
        http = httpx.AsyncClient(
            transport=transport,
            timeout=config.timeout,
            follow_redirects=False,
            trust_env=False,
        )
        return cls(config, http, owns_http=True, **kwargs)

    def __repr__(self) -> str:
        return (
            f"SigRestClient(base_url={self._config.base_url!r}, "
            f"tournament={self._config.tournament.slug!r}, closed={self._closed})"
        )

    async def __aenter__(self) -> Self:
        return self

    async def __aexit__(self, *exc_info: object) -> None:
        await self.aclose()

    async def aclose(self) -> None:
        """Stop accepting requests. Closes the httpx client only if this client owns it."""
        if self._closed:
            return
        self._closed = True
        if self._owns_http:
            await self._http.aclose()

    @property
    def closed(self) -> bool:
        return self._closed

    @property
    def limiter(self) -> RateLimiter:
        """The rate limiter this client admits through (share it across the account)."""
        return self._limiter

    @property
    def tournament(self) -> TournamentRef:
        """The configured tournament every scoped read is pinned to."""
        return self._config.tournament

    # --- tournament ---------------------------------------------------------------------

    async def get_tournament(self) -> Tournament:
        """GET /tournaments/{slug}, checking the echoed slug and UUID [client policy]."""
        return (await self._read_tournament()).tournament

    async def read_tournament_balance(self) -> TournamentBalanceRead:
        """A fresh GET /tournaments/{slug} with its provenance (A2-A).

        Always sends a request: it never returns the Tournament cached by
        `verify_tournament`, which may be hours old. The echoed slug and UUID are checked
        on every call, and a mismatch latches for the client's life as for any tournament
        read. A mismatch latched by a concurrent read while this one was in flight also
        fails this call. `myBalance` is kept exactly as sent; it is a candidate, not
        established tournament cash (see account_reads.NUMERIC_FIELD_STATUS).
        """
        return await self._read_tournament()

    async def _read_tournament(self) -> TournamentBalanceRead:
        if self._mismatch is not None:
            raise self._mismatch
        path = f"/tournaments/{_segment(self._config.tournament.slug)}"
        exchange = await self._get_observed(path, None)
        tournament = self._validate(path, exchange.body, Tournament)
        self._check_tournament(path, tournament)
        self._verified = tournament
        return TournamentBalanceRead(
            tournament=tournament,
            provenance=self._provenance(TOURNAMENT_ROUTE, exchange, ScopeEvidence.ECHO),
        )

    async def verify_tournament(self) -> Tournament:
        """Verify the slug/UUID pair once (its own logical read); later calls reuse it."""
        if self._mismatch is not None:
            raise self._mismatch
        async with self._verify_lock:
            self._raise_if_latched()  # latched while this call waited for the lock
            if self._verified is None:
                return await self.get_tournament()
            return self._verified

    async def _verify_observed(self) -> tuple[VerificationUse, ReadProvenance | None]:
        """`verify_tournament`, reporting whether this call made the verification read.

        PERFORMED (with that read's own provenance) when this call sent it; CACHED when
        an earlier read had verified the tournament and this call sent nothing.
        """
        if self._mismatch is not None:
            raise self._mismatch
        async with self._verify_lock:
            self._raise_if_latched()  # latched while this call waited for the lock
            if self._verified is None:
                read = await self._read_tournament()
                return VerificationUse.PERFORMED, read.provenance
            return VerificationUse.CACHED, None

    def _raise_if_latched(self) -> None:
        # [client policy] once a mismatch is latched (by any read) the client no longer
        # vouches for its tournament: no request or retry starts, nothing is returned.
        if self._mismatch is not None:
            raise self._mismatch

    def _check_tournament(self, path: str, tournament: Tournament) -> None:
        expected = self._config.tournament
        same_id = _same_uuid(tournament.id, expected.id)
        if same_id is None:
            raise SigResponseError(
                method="GET", path=path, reason="tournament id is not a canonical UUID"
            )
        # The reported slug and id are response data and are not repeated in the error.
        problems = []
        if tournament.slug != expected.slug:
            problems.append("slug")
        if not same_id:
            problems.append("id")
        if problems:
            if self._mismatch is None:  # the first mismatch observed stays latched
                self._mismatch = SigContextMismatchError(
                    method="GET",
                    path=path,
                    reason=(
                        f"SIG reports a different tournament {' and '.join(problems)} than "
                        f"configured ({expected.slug} / {expected.id})"
                    ),
                )
            raise self._mismatch

    # --- positions (A2-A) ---------------------------------------------------------------

    async def read_tournament_positions(self) -> PositionsRead | UnvaluedHoldingsRead:
        """GET /tournaments/{slug}/portfolio/positions in the configured tournament.

        One request, no pagination. The rows carry no tournament id: scope rests on the
        verified slug in the path. Rows, lots and the summary are kept exactly as sent;
        nothing is rounded, complemented, bounded or converted, and no cross-row check is
        applied (that belongs to the account snapshot, A3).

        [documented] 409 means "holdings exist without valuation prices". When the body
        is a valid MissingTournamentValuation (code CONFLICT, a non-empty list, digit-
        string ids), the result is UnvaluedHoldingsRead with the (exchange, market)
        pairs. Any other 409 raises SigConflictError as before. Every other status is
        handled as for any read (403 raises SigAuthError with its documented code).
        """
        verification, verification_read = await self._verify_observed()
        path = f"/tournaments/{_segment(self._config.tournament.slug)}/portfolio/positions"
        exchange = await self._get_observed(path, None, conflict=_missing_valuations)
        provenance = self._provenance(
            POSITIONS_ROUTE,
            exchange,
            ScopeEvidence.VERIFIED_SLUG_PATH,
            verification=verification,
            verification_read=verification_read,
        )
        if exchange.conflict is not None:
            return UnvaluedHoldingsRead(missing=exchange.conflict, provenance=provenance)
        positions = self._validate(path, exchange.body, TournamentPositions)
        return PositionsRead(
            rows=tuple(positions.positions), summary=positions.summary, provenance=provenance
        )

    # --- markets ------------------------------------------------------------------------

    async def list_tournament_markets_page(
        self,
        *,
        limit: int = MARKETS_PAGE_MAX,
        cursor: str | None = None,
        status: str = "any",
        search: str | None = None,
        before_attempt: Callable[[], Awaitable[None]] | None = None,
    ) -> TournamentMarketsPage:
        """One page of GET /tournaments/{slug}/markets."""
        limit = _check_int("limit", limit, 1, MARKETS_PAGE_MAX)
        cursor = _check_cursor(cursor)
        status = _check_choice("status", status, MARKET_STATUSES)
        if search is not None and (not isinstance(search, str) or not search):
            raise SigArgumentError("search must be a non-empty string or None")
        await self.verify_tournament()
        path = f"/tournaments/{_segment(self._config.tournament.slug)}/markets"
        params: dict[str, str | int] = {"limit": limit, "status": status}
        if cursor is not None:
            params["cursor"] = cursor
        if search is not None:
            params["search"] = search
        page = await self._get_model(
            path, params, TournamentMarketsPage, before_attempt=before_attempt
        )
        self._check_page_size(path, len(page.data), limit)
        return page

    async def list_all_tournament_markets(
        self,
        *,
        status: str = "any",
        search: str | None = None,
        page_limit: int = MARKETS_PAGE_MAX,
        max_pages: int = DEFAULT_MAX_PAGES,
        before_attempt: Callable[[], Awaitable[None]] | None = None,
    ) -> list[TournamentMarket]:
        """Every market in the tournament, or an error. Never a partial list.

        Each page has its own read deadline; there is no deadline for the whole list.
        `before_attempt`, if given, is awaited before each HTTP attempt, including every
        retry, before the shared account limiter. It applies to market-list pages only;
        tournament verification is a separate read. A failure ends the walk without a
        partial result. The hook must be scoped to this call, not shared client state.
        """
        path = f"/tournaments/{_segment(self._config.tournament.slug)}/markets"

        async def fetch(cursor: str | None) -> TournamentMarketsPage:
            return await self.list_tournament_markets_page(
                limit=page_limit, cursor=cursor, status=status, search=search,
                before_attempt=before_attempt,
            )

        return await self._collect(
            path, fetch, max_pages=max_pages, key=lambda market: market.id
        )

    async def get_market(self, market_id: str) -> Market:
        """GET /markets/{id} in the configured tournament (the authoritative market state).

        `contexts` must be exactly the configured tournament, and the top-level scope
        fields must agree with it. Read them from `Market.tournament_context()`.
        """
        market_id = _check_numeric_id("market_id", market_id)
        await self.verify_tournament()
        path = f"/markets/{_segment(market_id)}"
        params = {"tournamentId": self._config.tournament.id}
        market = await self._get_model(path, params, Market)
        # [client policy] the echoed market id must be the one requested.
        if market.id != market_id:
            raise SigResponseError(
                method="GET", path=path, reason="market detail is for a different market"
            )
        self._check_single_context(path, market.contexts)
        self._check_market_scope(path, market)
        return market

    @staticmethod
    def _check_market_scope(path: str, market: Market) -> None:
        # [documented] the top-level status, settlement and exchange prices are "in the
        # request's scope" / "the resolved context", and each context carries its own.
        # [client policy] with one explicit tournament context the two must agree, so
        # no caller can read a value that disagrees with the tournament context.
        def bad(reason: str) -> SigResponseError:
            return SigResponseError(method="GET", path=path, reason=reason)

        context = market.tournament_context()
        if (market.status, market.settledWith, market.settledOn) != (
            context.status,
            context.settledWith,
            context.settledOn,
        ):
            raise bad("top-level status or settlement differs from the tournament context")
        top = {exchange.id: exchange.latestPrice for exchange in market.exchanges}
        scoped = {exchange.id: exchange.latestPrice for exchange in context.exchanges}
        if len(top) != len(market.exchanges) or len(scoped) != len(context.exchanges):
            raise bad("duplicate exchange ids in the market detail")
        if top.keys() != scoped.keys():
            raise bad("market exchanges differ from the tournament context's exchanges")
        if top != scoped:
            raise bad("an exchange latestPrice differs from the tournament context's")

    # --- prices and orderbooks ----------------------------------------------------------

    async def get_exchange_prices(self, exchange_ids: Sequence[str]) -> BulkExchangePrices:
        """GET /exchanges/prices for 1-100 distinct exchange ids in the tournament."""
        if isinstance(exchange_ids, str) or not isinstance(exchange_ids, Sequence):
            raise SigArgumentError("exchange_ids must be a sequence of id strings")
        ids = [_check_numeric_id("exchange id", value) for value in exchange_ids]
        if not 1 <= len(ids) <= BULK_PRICE_IDS_MAX:
            raise SigArgumentError(f"exchange_ids must hold 1 to {BULK_PRICE_IDS_MAX} ids")
        # [client policy] duplicates would make the response ambiguous.
        if len(set(ids)) != len(ids):
            raise SigArgumentError("exchange_ids must not contain duplicates")
        await self.verify_tournament()
        path = "/exchanges/prices"
        params = {"ids": ",".join(ids), "tournamentId": self._config.tournament.id}
        prices = await self._get_model(path, params, BulkExchangePrices)
        self._check_bulk_prices(path, ids, prices)
        return prices

    def _check_bulk_prices(self, path: str, ids: list[str], prices: BulkExchangePrices) -> None:
        # [documented] data preserves request order; ids outside the context are listed
        # in missingIds. [client policy] no duplicates, no unrequested ids, every id
        # accounted for exactly once.
        def bad(reason: str) -> SigResponseError:
            return SigResponseError(method="GET", path=path, reason=reason)

        returned = [price.exchangeId for price in prices.data]
        requested = set(ids)
        if len(set(returned)) != len(returned) or len(set(prices.missingIds)) != len(
            prices.missingIds
        ):
            raise bad("duplicate exchange ids in data or missingIds")
        if not requested.issuperset(returned) or not requested.issuperset(prices.missingIds):
            raise bad("response names exchange ids that were not requested")
        if set(returned) & set(prices.missingIds):
            raise bad("an exchange id is both priced and missing")
        if set(returned) | set(prices.missingIds) != requested:
            raise bad("some requested exchange ids are neither priced nor missing")
        priced = set(returned)
        if returned != [i for i in ids if i in priced]:
            raise bad("data does not preserve request order")

    async def get_exchange_orderbook(
        self, exchange_id: str, *, depth: int = DEFAULT_ORDERBOOK_DEPTH
    ) -> ExchangeOrderbook:
        """GET /exchanges/{id}/orderbook in the configured tournament."""
        exchange_id = _check_numeric_id("exchange_id", exchange_id)
        depth = _check_int("depth", depth, 1, ORDERBOOK_DEPTH_MAX)
        await self.verify_tournament()
        path = f"/exchanges/{_segment(exchange_id)}/orderbook"
        params = {"depth": depth, "tournamentId": self._config.tournament.id}
        book = await self._get_model(path, params, ExchangeOrderbook)
        # [client policy] the echoed exchange id must be the one requested.
        if book.exchangeId != exchange_id:
            raise SigResponseError(
                method="GET", path=path, reason="orderbook is for a different exchange"
            )
        self._check_levels(path, book.bids, book.asks, depth)
        return book

    async def get_market_orderbook(
        self, market_id: str, *, depth: int = DEFAULT_ORDERBOOK_DEPTH
    ) -> MarketOrderbook:
        """GET /markets/{id}/orderbook; `contexts` must be exactly the configured tournament."""
        market_id = _check_numeric_id("market_id", market_id)
        depth = _check_int("depth", depth, 1, ORDERBOOK_DEPTH_MAX)
        await self.verify_tournament()
        path = f"/markets/{_segment(market_id)}/orderbook"
        params = {"depth": depth, "tournamentId": self._config.tournament.id}
        book = await self._get_model(path, params, MarketOrderbook)
        self._check_single_context(path, book.contexts)
        for combined in (book, book.tournament_orderbook()):
            self._check_combined(path, combined, depth)
        return book

    def _check_single_context(
        self, path: str, contexts: Sequence[MarketOrderbookContext | MarketPricingContext]
    ) -> None:
        # [documented] the orderbook: "Pass tournamentId for one explicit context"; the
        # market detail: the same explicit tournament scope as GET /markets.
        # [client policy] exactly one context, naming the configured slug and UUID.
        # Reported ids and slugs are response data and are not repeated in errors.
        expected = self._config.tournament
        if len(contexts) != 1:
            raise SigContextMismatchError(
                method="GET",
                path=path,
                reason=f"expected exactly one context, got {len(contexts)}",
            )
        context = contexts[0]
        tournament = context.tournament
        if context.type != "tournament" or tournament is None:
            raise SigContextMismatchError(
                method="GET", path=path, reason="context is not a tournament context"
            )
        same_id = _same_uuid(tournament.id, expected.id)
        if same_id is None:
            raise SigResponseError(
                method="GET", path=path, reason="context tournament id is not a canonical UUID"
            )
        if not same_id or tournament.slug != expected.slug:
            raise SigContextMismatchError(
                method="GET",
                path=path,
                reason=(
                    "context names a different tournament than configured "
                    f"({expected.slug} / {expected.id})"
                ),
            )

    def _check_combined(self, path: str, book: CombinedMarketOrderbook, depth: int) -> None:
        for exchange in book.exchanges:
            self._check_levels(path, exchange.bids, exchange.asks, depth)

    @staticmethod
    def _check_levels(
        path: str, bids: list[OrderbookLevel], asks: list[OrderbookLevel], depth: int
    ) -> None:
        # [documented] at most `depth` levels per side; bids descending, asks ascending.
        def bad(reason: str) -> SigResponseError:
            return SigResponseError(method="GET", path=path, reason=reason)

        if len(bids) > depth or len(asks) > depth:
            raise bad(f"more than {depth} price levels on one side")
        if any(a.price < b.price for a, b in itertools.pairwise(bids)):
            raise bad("bids are not sorted descending")
        if any(a.price > b.price for a, b in itertools.pairwise(asks)):
            raise bad("asks are not sorted ascending")

    # --- orders -------------------------------------------------------------------------

    async def list_orders_page(
        self,
        *,
        status: str = "open",
        exchange_id: str | None = None,
        market_id: str | None = None,
        limit: int = ORDERS_PAGE_MAX,
        cursor: str | None = None,
    ) -> OrdersPage:
        """One page of GET /orders in the configured tournament.

        The Order schema carries no tournament id, so the scope cannot be echo-checked.
        `coverage` is optional but not nullable: an explicit null is rejected.
        """
        observed = await self._orders_page_observed(
            status=status, exchange_id=exchange_id, market_id=market_id, limit=limit, cursor=cursor
        )
        return observed.page

    async def _orders_page_observed(
        self,
        *,
        status: str,
        exchange_id: str | None,
        market_id: str | None,
        limit: int,
        cursor: str | None,
    ) -> _ObservedOrdersPage:
        # Every argument is validated before verification, so bad input sends nothing.
        status = _check_choice("status", status, ORDER_STATUSES)
        limit = _check_int("limit", limit, 1, ORDERS_PAGE_MAX)
        cursor = _check_cursor(cursor)
        exchange_id = _check_optional_id("exchange_id", exchange_id)
        market_id = _check_optional_id("market_id", market_id)
        verification, verification_read = await self._verify_observed()
        path = "/orders"
        params: dict[str, str | int] = {
            "status": status,
            "limit": limit,
            "tournamentId": self._config.tournament.id,
        }
        if exchange_id is not None:
            params["exchangeId"] = exchange_id
        if market_id is not None:
            params["marketId"] = market_id
        if cursor is not None:
            params["cursor"] = cursor
        exchange = await self._get_observed(path, params)
        page = self._validate(path, exchange.body, OrdersPage)
        self._check_page_size(path, len(page.data), limit)
        # [client policy] an exchange filter must be honoured.
        if exchange_id is not None and any(o.exchangeId != exchange_id for o in page.data):
            raise SigResponseError(
                method="GET", path=path, reason="orders from another exchange were returned"
            )
        return _ObservedOrdersPage(page, exchange, verification, verification_read)

    async def read_open_orders(
        self, *, page_limit: int = ORDERS_PAGE_MAX, max_pages: int = DEFAULT_MAX_PAGES
    ) -> OrdersWalk:
        """Every open order in the configured tournament, with per-page provenance (A2-A).

        Walks GET /orders with `status=open` and the configured `tournamentId` under the
        same rules as `list_all_orders`: a repeated cursor, a duplicate order id, a
        cursor on the last page or the page cap raises SigPaginationError, and no
        partial walk is ever returned. Each page has its own read deadline; there is no
        deadline for the whole walk.

        Every page's coverage is kept exactly as sent, or recorded as omitted; an
        explicit `coverage: null` fails the walk. Pages may report different
        `projectedThroughSequence` values; none is compared with anything. A complete
        walk shows that every page was read, not that the reporting projection had
        caught up with the trading engine.
        """
        # Validated before anything is sent.
        page_limit = _check_int("page_limit", page_limit, 1, ORDERS_PAGE_MAX)
        max_pages = _check_int("max_pages", max_pages, 1, 10_000)
        records: list[PageRecord] = []  # this call's pages only

        async def fetch(cursor: str | None) -> OrdersPage:
            observed = await self._orders_page_observed(
                status="open", exchange_id=None, market_id=None, limit=page_limit, cursor=cursor
            )
            page = observed.page
            provenance = self._provenance(
                ORDERS_ROUTE,
                observed.exchange,
                ScopeEvidence.TOURNAMENT_ID_SENT,
                page_index=len(records),
                coverage_state=(
                    CoverageState.OMITTED if page.coverage is None else CoverageState.PRESENT
                ),
                coverage=page.coverage,
                verification=observed.verification,
                verification_read=observed.verification_read,
            )
            records.append(PageRecord(provenance=provenance, item_count=len(page.data)))
            return page

        orders = await self._collect(
            ORDERS_ROUTE, fetch, max_pages=max_pages, key=lambda order: order.id
        )
        return OrdersWalk(orders=tuple(orders), pages=tuple(records))

    async def list_all_orders(
        self,
        *,
        status: str = "open",
        exchange_id: str | None = None,
        market_id: str | None = None,
        page_limit: int = ORDERS_PAGE_MAX,
        max_pages: int = DEFAULT_MAX_PAGES,
    ) -> list[Order]:
        """Every matching order, or an error. Never a partial list.

        Each page has its own read deadline; there is no deadline for the whole list.
        """
        return await self._collect(
            "/orders",
            lambda cursor: self.list_orders_page(
                status=status,
                exchange_id=exchange_id,
                market_id=market_id,
                limit=page_limit,
                cursor=cursor,
            ),
            max_pages=max_pages,
            key=lambda order: order.id,
        )

    # --- further account reads (A2-B) ---------------------------------------------------

    async def read_tournament_fills(
        self, *, page_limit: int = FILLS_PAGE_MAX, max_pages: int = DEFAULT_MAX_PAGES
    ) -> FillsWalk:
        """Every fill of the caller in the configured tournament, newest first (A2-B).

        Walks GET /tournaments/{slug}/portfolio/fills (verified slug in the path; rows
        carry no tournament id). A repeated cursor, a duplicate fill id, a cursor on the
        last page or the page cap raises SigPaginationError; no partial walk is returned.
        [documented] fills are newest first; [client policy] a page out of that order, or
        a page holding fills newer than the previous page's, is rejected. Each page's
        coverage is kept exactly as sent or recorded as omitted; an explicit
        `coverage: null` fails the walk. Every number is kept exactly as sent.
        """
        page_limit = _check_int("page_limit", page_limit, 1, FILLS_PAGE_MAX)
        max_pages = _check_int("max_pages", max_pages, 1, 10_000)
        path = f"/tournaments/{_segment(self._config.tournament.slug)}/portfolio/fills"
        newest_first = _NewestFirst(path, "filledAt")

        def check(page: TournamentFillsPage) -> None:
            newest_first.check([fill.filledAt for fill in page.data])

        fills, pages = await self._walk_observed(
            FILLS_ROUTE,
            path,
            TournamentFillsPage,
            ScopeEvidence.VERIFIED_SLUG_PATH,
            page_limit=page_limit,
            max_pages=max_pages,
            key=lambda fill: fill.id,
            check=check,
            coverage=_optional_coverage,
        )
        return FillsWalk(fills=tuple(fills), pages=pages)

    async def read_tournament_pnl(self, *, period: str = "all") -> PnlRead | UnvaluedHoldingsRead:
        """GET /tournaments/{slug}/portfolio/pnl for `period` (A2-B), sent explicitly.

        `period` defaults to "all" and is always sent, never left to SIG's documented
        default ("quarter"). [client policy] the echoed `period` must be the one sent.
        [documented] 409 means "holdings exist without valuation prices"; a valid
        MissingTournamentValuation body becomes UnvaluedHoldingsRead, exactly as for
        positions, and any other 409 raises SigConflictError. Fields are kept exactly as
        sent; nothing is derived from them.
        """
        period = _check_choice("period", period, PNL_PERIODS)
        verification, verification_read = await self._verify_observed()
        path = f"/tournaments/{_segment(self._config.tournament.slug)}/portfolio/pnl"
        exchange = await self._get_observed(path, {"period": period}, conflict=_missing_valuations)
        provenance = self._provenance(
            PNL_ROUTE,
            exchange,
            ScopeEvidence.VERIFIED_SLUG_PATH,
            verification=verification,
            verification_read=verification_read,
        )
        if exchange.conflict is not None:
            return UnvaluedHoldingsRead(missing=exchange.conflict, provenance=provenance)
        pnl = self._validate(path, exchange.body, TournamentPnl)
        if pnl.period != period:
            raise SigResponseError(
                method="GET", path=path, reason="P&L is for a different period than requested"
            )
        return PnlRead(pnl=pnl, provenance=provenance)

    async def read_tournament_settlements(
        self, *, page_limit: int = SETTLEMENTS_PAGE_MAX, max_pages: int = DEFAULT_MAX_PAGES
    ) -> SettlementsWalk:
        """Every settlement entry of the caller in the configured tournament (A2-B).

        Walks GET /tournaments/{slug}/portfolio/settlements under the same pagination
        rules as the other walks (duplicate entry ids raise). [documented] each page's
        economic-command `coverage.complete` is false when SIG's own history walk hit its
        20-page budget. Such a page is kept as sent and the walk continues; the result
        lists those page indexes and is `complete` only when no page reported false. It
        is never presented as complete otherwise, and never silently truncated.
        """
        page_limit = _check_int("page_limit", page_limit, 1, SETTLEMENTS_PAGE_MAX)
        max_pages = _check_int("max_pages", max_pages, 1, 10_000)
        path = f"/tournaments/{_segment(self._config.tournament.slug)}/portfolio/settlements"
        entries, pages = await self._walk_observed(
            SETTLEMENTS_ROUTE,
            path,
            TournamentSettlementsPage,
            ScopeEvidence.VERIFIED_SLUG_PATH,
            page_limit=page_limit,
            max_pages=max_pages,
            key=lambda entry: entry.id,
            coverage=lambda page: (CoverageState.PRESENT, page.coverage),
        )
        incomplete = tuple(
            index
            for index, page in enumerate(pages)
            if isinstance(page.provenance.coverage, EconomicCommandCoverage)
            and page.provenance.coverage.complete is not True
        )
        return SettlementsWalk(
            entries=tuple(entries),
            pages=pages,
            complete=not incomplete,
            incomplete_pages=incomplete,
        )

    async def read_order_fills(
        self,
        order_id: int,
        *,
        page_limit: int = ORDER_FILLS_PAGE_MAX,
        max_pages: int = DEFAULT_MAX_PAGES,
    ) -> OrderFillsWalk:
        """Every fill of one order, with its lifetime totals (A2-B).

        Walks GET /orders/{id}/fills. "The order ID determines its trading context" and
        the response echoes `tournamentId`: [client policy] every page must echo the
        configured tournament, and a null (a global order) or any other value raises
        SigContextMismatchError for this order. That is about the order, not about the
        client's tournament identity, so it does not latch the client. Every page must
        also echo the requested order and one exchange, and the same lifetime totals
        (`totalQuantityFilled`, `avgFillPrice`, documented as independent of the page);
        otherwise the walk raises. [documented] fills are newest first, checked as for
        the tournament fills. An order with no fills is one request and an empty walk.
        """
        order_id = _check_order_id(order_id)
        page_limit = _check_int("page_limit", page_limit, 1, ORDER_FILLS_PAGE_MAX)
        max_pages = _check_int("max_pages", max_pages, 1, 10_000)
        path = f"/orders/{order_id}/fills"
        newest_first = _NewestFirst(path, "filledAt")
        firsts: list[OrderFillsPage] = []

        def check(page: OrderFillsPage) -> None:
            if page.orderId != order_id:
                raise SigResponseError(
                    method="GET", path=path, reason="fills are for a different order"
                )
            self._check_order_tournament(path, page.tournamentId)
            if firsts:
                first = firsts[0]
                # Response values are not repeated in the errors.
                if page.exchangeId != first.exchangeId:
                    raise SigPaginationError(
                        method="GET", path=path, reason="a page reports a different exchange"
                    )
                if (page.totalQuantityFilled, page.avgFillPrice) != (
                    first.totalQuantityFilled,
                    first.avgFillPrice,
                ):
                    raise SigPaginationError(
                        method="GET",
                        path=path,
                        reason="the order's lifetime totals differ between pages",
                    )
            else:
                firsts.append(page)
            newest_first.check([fill.filledAt for fill in page.data])

        fills, pages = await self._walk_observed(
            ORDER_FILLS_ROUTE,
            path,
            OrderFillsPage,
            ScopeEvidence.ECHO,
            page_limit=page_limit,
            max_pages=max_pages,
            key=lambda fill: fill.id,
            check=check,
            coverage=lambda page: (CoverageState.PRESENT, page.coverage),
        )
        first = firsts[0]  # _collect reads at least one page or raises
        assert first.tournamentId is not None  # checked by _check_order_tournament
        return OrderFillsWalk(
            order_id=first.orderId,
            exchange_id=first.exchangeId,
            echoed_tournament_id=first.tournamentId,
            fills=tuple(fills),
            total_quantity_filled=first.totalQuantityFilled,
            avg_fill_price=first.avgFillPrice,
            pages=pages,
        )

    def _check_order_tournament(self, path: str, echoed: str | None) -> None:
        # [documented] "Tournament this order belongs to, or null for global orders".
        # The echoed value is response data and is not repeated in the error.
        expected = self._config.tournament
        if echoed is None:
            raise SigContextMismatchError(
                method="GET",
                path=path,
                reason=f"the order is a global order, not in {expected.slug} / {expected.id}",
            )
        if _same_uuid(echoed, expected.id) is not True:
            raise SigContextMismatchError(
                method="GET",
                path=path,
                reason=f"the order belongs to a different tournament than {expected.slug}",
            )

    async def _walk_observed(
        self,
        route: str,
        path: str,
        model: type[PageT],
        scope: ScopeEvidence,
        *,
        page_limit: int,
        max_pages: int,
        key: Callable[[Any], object],
        coverage: Callable[[Any], tuple[CoverageState, PageCoverage | None]],
        check: Callable[[Any], None] | None = None,
    ) -> tuple[list[Any], tuple[PageRecord, ...]]:
        """A complete cursor walk of one account read, with one PageRecord per page.

        Every page re-runs the (cached) tournament verification, has its own read
        deadline and is validated, size-checked and passed to `check` before it is
        recorded. `_collect` enforces the pagination rules. Records live only in this
        call, so concurrent walks never share them.
        """
        records: list[PageRecord] = []

        async def fetch(cursor: str | None) -> Any:
            cursor = _check_cursor(cursor)
            verification, verification_read = await self._verify_observed()
            params: dict[str, str | int] = {"limit": page_limit}
            if cursor is not None:
                params["cursor"] = cursor
            exchange = await self._get_observed(path, params)
            page = self._validate(path, exchange.body, model)
            self._check_page_size(path, len(page.data), page_limit)
            if check is not None:
                check(page)
            state, kept = coverage(page)
            provenance = self._provenance(
                route,
                exchange,
                scope,
                page_index=len(records),
                coverage_state=state,
                coverage=kept,
                verification=verification,
                verification_read=verification_read,
            )
            records.append(PageRecord(provenance=provenance, item_count=len(page.data)))
            return page

        items = await self._collect(path, fetch, max_pages=max_pages, key=key)
        return items, tuple(records)

    # --- trade tape ---------------------------------------------------------------------

    async def list_exchange_trades_page(
        self,
        exchange_id: str,
        *,
        since: datetime | None = None,
        until: datetime | None = None,
        limit: int = TRADES_PAGE_MAX,
        cursor: str | None = None,
    ) -> ExchangeTradesPage:
        """One page of GET /exchanges/{id}/trades in the configured tournament, newest first.

        `since` (inclusive) and `until` (exclusive) are timezone-aware datetimes in whole
        milliseconds; omitted, SIG uses the start of the exchange's history and now. The
        response echoes no tournament, so the scope rests on the `tournamentId` sent.
        `coverage` is kept exactly as sent; see the module docstring.
        """
        # Every argument is validated before verification, so bad input sends nothing.
        exchange_id = _check_numeric_id("exchange_id", exchange_id)
        since_utc = _check_instant("since", since)
        until_utc = _check_instant("until", until)
        if since_utc is not None and until_utc is not None and since_utc >= until_utc:
            raise SigArgumentError("since must be earlier than until")
        limit = _check_int("limit", limit, 1, TRADES_PAGE_MAX)
        cursor = _check_cursor(cursor)
        await self.verify_tournament()
        path = f"/exchanges/{_segment(exchange_id)}/trades"
        params: dict[str, str | int] = {
            "tournamentId": self._config.tournament.id,
            "limit": limit,
        }
        if since_utc is not None:
            params["from"] = _format_instant(since_utc)
        if until_utc is not None:
            params["to"] = _format_instant(until_utc)
        if cursor is not None:
            params["cursor"] = cursor
        page = await self._get_model(path, params, ExchangeTradesPage)
        self._check_page_size(path, len(page.data), limit)
        self._check_trades_page(path, page, exchange_id, since_utc, until_utc)
        return page

    @staticmethod
    def _check_trades_page(
        path: str,
        page: ExchangeTradesPage,
        exchange_id: str,
        since: datetime | None,
        until: datetime | None,
    ) -> None:
        def bad(reason: str) -> SigResponseError:
            return SigResponseError(method="GET", path=path, reason=reason)

        # [client policy] the echoed exchange id must be the one requested.
        if page.exchangeId != exchange_id:
            raise bad("trade tape is for a different exchange")
        # [documented] `complete` is "always true because cursor pagination exposes the
        # full requested trade history without window truncation". [client policy] a
        # page that reports otherwise is not returned.
        if page.coverage is not None and page.coverage.complete is not True:
            raise bad("coverage.complete is false")
        # [documented] `from` is inclusive and `to` exclusive. [client policy] the echoed
        # window must be the one requested, and must not be inverted.
        window_from = _response_instant(path, "from", page.from_)
        window_to = _response_instant(path, "to", page.to)
        if since is not None and window_from != epoch_seconds(since):
            raise bad("echoed from differs from the requested since")
        if until is not None and window_to != epoch_seconds(until):
            raise bad("echoed to differs from the requested until")
        if window_from > window_to:
            raise bad("echoed from is later than echoed to")
        # [documented] newest first. [client policy] every trade inside the window.
        times = [_response_instant(path, "createdAt", trade.createdAt) for trade in page.data]
        if any(newer < older for newer, older in itertools.pairwise(times)):
            raise bad("trades are not sorted newest first")
        if any(not window_from <= moment < window_to for moment in times):
            raise bad("a trade lies outside the echoed from/to window")

    async def list_all_exchange_trades(
        self,
        exchange_id: str,
        *,
        since: datetime | None = None,
        until: datetime | None = None,
        page_limit: int = TRADES_PAGE_MAX,
        max_pages: int = DEFAULT_MAX_PAGES,
    ) -> ExchangeTradeHistory:
        """Every trade in the window, newest first, or an error. Never a partial tape.

        Each page has its own read deadline; there is no deadline for the whole tape.
        Every page must echo the same market and window [client policy]. If SIG moved an
        omitted `until` ("now") between pages the walk fails, so pass `until` for a long
        tape. Coverage is returned per page, exactly as sent: a complete walk does not
        show that SIG's reporting projection has caught up with the trading engine.
        """
        path = f"/exchanges/{_segment(_check_numeric_id('exchange_id', exchange_id))}/trades"
        firsts: list[ExchangeTradesPage] = []  # the first page, once read
        coverage: list[TradeCoverage | None] = []
        last_time: Decimal | None = None

        def on_page(page: ExchangeTradesPage) -> None:
            nonlocal last_time

            def fail(reason: str) -> SigPaginationError:
                return SigPaginationError(method="GET", path=path, reason=reason)

            if not firsts:
                firsts.append(page)
            else:
                first = firsts[0]
                # Market ids are response data and are not repeated in the error.
                if page.marketId != first.marketId:
                    raise fail("a page reports a different market")
                if _response_instant(path, "from", page.from_) != _response_instant(
                    path, "from", first.from_
                ) or _response_instant(path, "to", page.to) != _response_instant(
                    path, "to", first.to
                ):
                    raise fail("a page reports a different from/to window")
            if page.data:
                newest = _response_instant(path, "createdAt", page.data[0].createdAt)
                if last_time is not None and newest > last_time:
                    raise fail("a page holds trades newer than the previous page's")
                last_time = _response_instant(path, "createdAt", page.data[-1].createdAt)
            coverage.append(page.coverage)

        trades = await self._collect(
            path,
            lambda cursor: self.list_exchange_trades_page(
                exchange_id, since=since, until=until, limit=page_limit, cursor=cursor
            ),
            max_pages=max_pages,
            key=lambda trade: trade.id,
            on_page=on_page,
        )
        first = firsts[0]  # _collect reads at least one page or raises
        return ExchangeTradeHistory(
            exchange_id=first.exchangeId,
            market_id=first.marketId,
            window_from=first.from_,
            window_to=first.to,
            trades=tuple(trades),
            page_coverage=tuple(coverage),
        )

    # --- pagination ---------------------------------------------------------------------

    @staticmethod
    def _check_page_size(path: str, count: int, limit: int) -> None:
        # [documented] every response is a bounded page.
        if count > limit:
            raise SigResponseError(
                method="GET", path=path, reason=f"page has {count} items, limit was {limit}"
            )

    async def _collect(
        self,
        path: str,
        fetch: Callable[[str | None], Awaitable[PageT]],
        *,
        max_pages: int,
        key: Callable[[Any], object],
        on_page: Callable[[Any], None] | None = None,
    ) -> list[Any]:
        # [documented] follow nextCursor while hasMore. [client policy] fail on a
        # repeated cursor, a cursor on the last page, a duplicate item or the page cap.
        # `on_page` sees every page, in order, before its items are taken; it may raise.
        max_pages = _check_int("max_pages", max_pages, 1, 10_000)

        def fail(reason: str) -> SigPaginationError:
            return SigPaginationError(method="GET", path=path, reason=reason)

        items: list[Any] = []
        keys: set[object] = set()
        seen_cursors: set[str] = set()
        cursor: str | None = None
        for _ in range(max_pages):
            page = await fetch(cursor)
            if on_page is not None:
                on_page(page)
            for item in page.data:
                item_key = key(item)
                if item_key in keys:
                    # The item id is response data and is not repeated in the error.
                    raise fail("an item appeared on more than one page")
                keys.add(item_key)
                items.append(item)
            has_more = page.pagination.hasMore
            next_cursor = page.pagination.nextCursor
            if not has_more:
                if next_cursor is not None:
                    raise fail("hasMore is false but nextCursor is set")
                return items
            if next_cursor is None:
                raise fail("hasMore is true but nextCursor is null")
            if next_cursor in seen_cursors:
                raise fail("SIG repeated a pagination cursor")
            seen_cursors.add(next_cursor)
            cursor = next_cursor
        raise fail(f"more than {max_pages} pages; no partial result returned")

    # --- transport ----------------------------------------------------------------------

    async def _get_model(
        self,
        path: str,
        params: Mapping[str, str | int] | None,
        model: type[ModelT],
        *,
        before_attempt: Callable[[], Awaitable[None]] | None = None,
    ) -> ModelT:
        return self._validate(
            path, await self._get_json(path, params, before_attempt=before_attempt), model
        )

    @staticmethod
    def _validate(path: str, data: object, model: type[ModelT]) -> ModelT:
        try:
            return model.model_validate(data)
        except ValidationError as exc:
            raise SigResponseError(
                method="GET", path=path, reason=_validation_summary(exc)
            ) from None

    @staticmethod
    def _provenance(
        route: str,
        exchange: _Exchange,
        scope: ScopeEvidence,
        *,
        page_index: int | None = None,
        coverage_state: CoverageState = CoverageState.NOT_APPLICABLE,
        coverage: PageCoverage | None = None,
        verification: VerificationUse = VerificationUse.NOT_APPLICABLE,
        verification_read: ReadProvenance | None = None,
    ) -> ReadProvenance:
        params, cursor_sent = _recorded_params(exchange.params)
        return ReadProvenance(
            route=route,
            path=exchange.path,
            params=params,
            cursor_sent=cursor_sent,
            scope=scope,
            attempts=exchange.attempts,
            http_status=exchange.status,
            started_mono=exchange.started_mono,
            finished_mono=exchange.finished_mono,
            finished_utc=exchange.finished_utc,
            page_index=page_index,
            coverage_state=coverage_state,
            coverage=coverage,
            verification=verification,
            verification_read=verification_read,
        )

    async def _get_json(
        self,
        path: str,
        params: Mapping[str, str | int] | None,
        *,
        before_attempt: Callable[[], Awaitable[None]] | None = None,
    ) -> object:
        """One logical GET under one overall deadline. Private by design."""
        return (await self._get_observed(path, params, before_attempt=before_attempt)).body

    async def _get_observed(
        self,
        path: str,
        params: Mapping[str, str | int] | None,
        *,
        conflict: Callable[[bytes], tuple[MissingValuation, ...] | None] | None = None,
        before_attempt: Callable[[], Awaitable[None]] | None = None,
    ) -> _Exchange:
        """One logical GET under one overall deadline, with this call's own observations.

        The attempt count and local times come only from this call's local variables.
        `conflict`, if given, sees the body of a 409 that arrived within the deadline and
        returns the validated missing-valuation pairs, or None to raise SigConflictError
        exactly as for any other 409.
        """
        method = "GET"
        started = self._clock()
        deadline = started + self._retry.total_budget_s
        attempts = 0
        last_error: SigApiError | SigTransportError | None = None

        def expired(phase: str) -> SigDeadlineExceededError:
            return SigDeadlineExceededError(
                method=method, path=path, phase=phase, attempts=attempts, last_error=last_error
            )

        async def within_deadline(phase: str, start: Callable[[], Awaitable[T]]) -> T:
            # Actively bounds one await by the time left on the injected clock, so an
            # await that never returns is still cut off. Only this scope's own timeout
            # becomes a deadline error; external cancellation propagates unchanged, and
            # a TimeoutError raised by the awaited code itself is re-raised as is.
            remaining = deadline - self._clock()
            if remaining <= 0:
                raise expired(phase)
            scope = asyncio.timeout(remaining)
            try:
                async with scope:
                    return await start()
            except TimeoutError:
                if scope.expired():
                    raise expired(phase) from None
                raise

        while True:
            if self._closed:
                raise SigClientClosedError("the SIG client is closed")
            # A mismatch latched before this attempt (or during a retry wait) stops it.
            self._raise_if_latched()
            if before_attempt is not None:
                # The caller's own gate, before any shared token is taken. Its budget
                # failure is named for it, not for the shared limiter.
                try:
                    await within_deadline("caller admission", before_attempt)
                except SigRateBudgetError:
                    raise expired("caller admission") from None
                self._raise_if_latched()
            try:
                # A cancelled admission consumes no token: the limiter admits only in a
                # synchronous step after its wait, and keeps no queue of waiters.
                await within_deadline(
                    "rate admission",
                    functools.partial(self._limiter.acquire, RequestKind.READ, deadline=deadline),
                )
            except SigRateBudgetError:
                raise expired("rate admission") from None
            self._raise_if_latched()  # latched while this attempt waited for admission
            attempts += 1
            error: SigApiError | SigTransportError
            wait_floor = 0.0
            try:
                # Covers sending, waiting for and reading the whole body.
                response = await within_deadline(
                    "request", functools.partial(self._send, method, path, params)
                )
            except RETRYABLE_TRANSPORT_ERRORS as exc:
                error = SigTransportError(method=method, path=path, kind=type(exc).__name__)
            except httpx.TransportError as exc:
                raise SigTransportError(method=method, path=path, kind=type(exc).__name__) from None
            else:
                finished = self._clock()
                if finished >= deadline:
                    raise expired("request")  # a late response is not returned
                self._raise_if_latched()  # latched while the request was in flight
                missing = None
                if response.status_code == 409 and conflict is not None:
                    missing = conflict(response.content)  # None: handled as any 409 below
                if 200 <= response.status_code < 300 or missing is not None:
                    return _Exchange(
                        path=path,
                        params=tuple((params or {}).items()),
                        status=response.status_code,
                        attempts=attempts,
                        started_mono=started,
                        finished_mono=finished,
                        finished_utc=self._wall_clock(),
                        body=None if missing is not None else self._decode(method, path, response),
                        conflict=missing,
                    )
                retry_after = _parse_retry_after(response.headers.get("Retry-After"))
                error = self._api_error(method, path, response, retry_after)
                if not isinstance(error, (SigRateLimitedError, SigServiceUnavailableError)):
                    raise error
                if isinstance(error, SigRateLimitedError):
                    if retry_after.oversized:
                        # Too long to hold as a deadline: stop, never resume early.
                        self._limiter.block(RequestKind.READ)
                    else:
                        # The server's value as sent; the documented 60 s only when the
                        # header is missing or unparseable.
                        wait_floor = (
                            retry_after.seconds
                            if retry_after.seconds is not None
                            else self._retry.rate_limited_default_wait_s
                        )
                        self._limiter.impose_cooldown(RequestKind.READ, wait_floor)
                elif retry_after.seconds is not None:
                    wait_floor = retry_after.seconds
                if retry_after.malformed or retry_after.oversized:
                    kind = "oversized" if retry_after.oversized else "unparseable"
                    logger.warning("SIG read not retried: %s Retry-After; %s", kind, error)
                    raise error

            last_error = error
            if attempts >= self._retry.max_attempts:
                raise SigRetryExhaustedError(attempts=attempts, last_error=error)
            jittered = self._jitter(self._retry.backoff(attempts))
            if (
                isinstance(jittered, bool)
                or not isinstance(jittered, (int, float))
                or not math.isfinite(jittered)
                or jittered < 0
            ):
                raise ValueError("jitter must return a finite, non-negative delay")
            delay = max(wait_floor, float(jittered))
            if self._clock() + delay >= deadline:
                raise expired("retry wait")
            logger.warning(
                "SIG read retry %d/%d in %.2fs: %s",
                attempts,
                self._retry.max_attempts,
                delay,
                error,
            )
            await within_deadline("retry wait", functools.partial(self._sleep, delay))
            if self._clock() >= deadline:
                raise expired("retry wait")  # the sleep overran the deadline

    async def _send(
        self, method: str, path: str, params: Mapping[str, str | int] | None
    ) -> httpx.Response:
        request = self._http.build_request(
            method,
            self._config.base_url + path,
            params=params,
            headers={
                "Authorization": f"Bearer {self._config.api_key.reveal()}",
                "Accept": "application/json",
            },
            timeout=self._config.timeout,
        )
        key = id(request)
        _SIG_REQUESTS_IN_FLIGHT[key] = request  # owned by _RequestLogRedaction until sent
        try:
            return await self._http.send(request, follow_redirects=False)
        finally:
            _SIG_REQUESTS_IN_FLIGHT.pop(key, None)

    @staticmethod
    def _api_error(
        method: str, path: str, response: httpx.Response, retry_after: _RetryAfter
    ) -> SigApiError:
        # Only the status, a documented code and a fixed description are kept. SIG's
        # free-form message and any unknown code are response text and are dropped.
        status = response.status_code
        code: str | None = None
        unrecognized = False
        detail = "non-JSON error body"
        try:
            body = json.loads(response.content)
        except ValueError:
            body = None
        if isinstance(body, dict) and isinstance(body.get("error"), dict):
            raw_code = body["error"].get("code")
            code = known_code(raw_code)
            unrecognized = code is None and raw_code is not None
            detail = "error envelope"
        elif body is not None:
            detail = "error body without an error object"
        cls = _STATUS_ERRORS.get(status)
        if cls is None:
            cls = SigServerError if 500 <= status < 600 else SigUnexpectedStatusError
        return cls(
            method=method,
            path=path,
            status=status,
            code=code,
            unrecognized_code=unrecognized,
            detail=detail,
            retry_after=retry_after.seconds,
            retry_after_oversized=retry_after.oversized,
        )

    @staticmethod
    def _decode(method: str, path: str, response: httpx.Response) -> object:
        try:
            return json.loads(
                response.content, parse_float=Decimal, parse_constant=_reject_constant
            )
        except ValueError:
            raise SigResponseError(
                method=method, path=path, reason="body is not valid JSON"
            ) from None
