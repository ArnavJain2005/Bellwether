"""Read-only SIG REST client (S1-T2-B).

Scope: six documented GET endpoints, always pinned to the configured tournament. There
are no write methods (orders, cancels, realtime tokens) and no public way to issue an
arbitrary request.

Tournament scoping: before any scoped read, the client calls GET /tournaments/{slug}
once and checks that SIG reports the configured slug and UUID. A mismatch is permanent
for the client's lifetime. UUID-scoped reads always send `tournamentId`, and the market
orderbook's echoed context is checked as well.

Transport and credentials:
- The caller injects an `httpx.AsyncClient`. The SIG client closes it only when it was
  created with `owns_http=True` (as `SigRestClient.create` does).
- Every request carries the configured per-attempt timeout and `follow_redirects=False`;
  a redirect is returned as SigUnexpectedStatusError and never followed, so the key is
  never forwarded to another host.
- The key is added per request in the Authorization header. It is never put in a URL,
  log line, exception message or repr. An injected client with its own auth or
  credential headers is refused.

Read retries (ReadRetryPolicy):
- Retried: HTTP 429 and 503 (documented transient), and the transport failures in
  RETRYABLE_TRANSPORT_ERRORS. Everything else is raised at once.
- A finite attempt limit and an overall time budget apply. Each attempt consumes read
  budget. The delay is the larger of Retry-After and a jittered exponential backoff, so
  a retry never comes earlier than SIG instructed. If that delay would exceed the time
  budget, SigRetryExhaustedError is raised. An unparseable Retry-After is not retried.
- A 429 also imposes a shared read cooldown on the rate limiter.
These rules are for reads only. They say nothing about retrying orders or cancels.
"""

from __future__ import annotations

import asyncio
import itertools
import json
import logging
import random
import re
import time
import uuid
from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass
from decimal import Decimal
from typing import Any, Protocol, Self, TypeVar
from urllib.parse import quote

import httpx
from pydantic import BaseModel, ValidationError

from engine.config import SigConfig
from engine.sig_client.errors import (
    SigApiError,
    SigArgumentError,
    SigAuthError,
    SigBadRequestError,
    SigClientClosedError,
    SigConflictError,
    SigContextMismatchError,
    SigNotFoundError,
    SigPaginationError,
    SigRateLimitedError,
    SigResponseError,
    SigRetryExhaustedError,
    SigServerError,
    SigServiceUnavailableError,
    SigTransportError,
    SigUnexpectedStatusError,
    sanitize_code,
    sanitize_text,
)
from engine.sig_client.rate_limiter import Clock, RateLimiter, RequestKind, Sleep
from engine.sig_client.wire import (
    BulkExchangePrices,
    CombinedMarketOrderbook,
    ExchangeOrderbook,
    MarketOrderbook,
    Order,
    OrderbookLevel,
    OrdersPage,
    Tournament,
    TournamentMarket,
    TournamentMarketsPage,
)

logger = logging.getLogger(__name__)

# Documented limits (docs/api/openapi.json).
MARKETS_PAGE_MAX = 100  # #/paths/~1tournaments~1{slug}~1markets/get/parameters/1
ORDERS_PAGE_MAX = 200  # #/paths/~1orders/get/parameters/3
BULK_PRICE_IDS_MAX = 100  # #/paths/~1exchanges~1prices/get/parameters/0
ORDERBOOK_DEPTH_MAX = 200  # #/paths/~1exchanges~1{id}~1orderbook/get/parameters/1
DEFAULT_ORDERBOOK_DEPTH = 20
MARKET_STATUSES = frozenset({"open", "closed", "settled", "any"})  # "resolved" is deprecated
ORDER_STATUSES = frozenset({"open", "closed", "expired", "all"})
DEFAULT_MAX_PAGES = 50

_NUMERIC_ID = re.compile(r"^[0-9]+$")
_RETRY_AFTER = re.compile(r"^[0-9]+(\.[0-9]+)?$")

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
    max_attempts: int = 4
    total_budget_s: float = 30.0
    base_backoff_s: float = 0.25
    max_backoff_s: float = 4.0
    # Used when a 429 carries no Retry-After; the spec documents `Retry-After: 60`.
    rate_limited_default_wait_s: float = 60.0

    def __post_init__(self) -> None:
        if self.max_attempts < 1:
            raise ValueError("max_attempts must be at least 1")
        if min(self.total_budget_s, self.base_backoff_s, self.max_backoff_s) <= 0:
            raise ValueError("time values must be positive")

    def backoff(self, attempt: int) -> float:
        return min(self.max_backoff_s, self.base_backoff_s * 2 ** (attempt - 1))


class _Page(Protocol):
    @property
    def data(self) -> Sequence[Any]: ...

    @property
    def pagination(self) -> Any: ...


ModelT = TypeVar("ModelT", bound=BaseModel)
PageT = TypeVar("PageT", bound=_Page)

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


def _parse_retry_after(value: str | None) -> tuple[bool, float | None]:
    """(valid, seconds). Only delta-seconds is accepted; an HTTP-date is not."""
    if value is None:
        return True, None
    value = value.strip()
    if not _RETRY_AFTER.match(value):
        return False, None
    return True, float(value)


def _check_int(name: str, value: object, low: int, high: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or not low <= value <= high:
        raise SigArgumentError(f"{name} must be an integer from {low} to {high}")
    return value


def _check_numeric_id(name: str, value: object) -> str:
    if not isinstance(value, str) or not _NUMERIC_ID.match(value):
        raise SigArgumentError(f"{name} must be a string of ASCII digits")
    return value


def _check_choice(name: str, value: object, choices: frozenset[str]) -> str:
    if value not in choices:
        raise SigArgumentError(f"{name} must be one of {sorted(choices)}")
    return value  # type: ignore[return-value]


def _check_cursor(cursor: object) -> str | None:
    if cursor is not None and (not isinstance(cursor, str) or not cursor):
        raise SigArgumentError("cursor must be a non-empty string or None")
    return cursor


def _segment(value: str) -> str:
    return quote(value, safe="")


class SigRestClient:
    """Read-only client for one SIG account and one configured tournament."""

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
        # A shared limiter must use the same clock as this client (deadlines compare).
        self._limiter = limiter if limiter is not None else RateLimiter(clock=clock, sleep=sleep)
        self._retry = retry if retry is not None else ReadRetryPolicy()
        self._closed = False
        self._verified: Tournament | None = None
        self._mismatch: SigContextMismatchError | None = None
        self._verify_lock = asyncio.Lock()

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

    # --- tournament ---------------------------------------------------------------------

    async def get_tournament(self) -> Tournament:
        """GET /tournaments/{slug}, checking the echoed slug and UUID."""
        if self._mismatch is not None:
            raise self._mismatch
        path = f"/tournaments/{_segment(self._config.tournament.slug)}"
        tournament = await self._get_model(path, None, Tournament)
        self._check_tournament(path, tournament)
        self._verified = tournament
        return tournament

    async def verify_tournament(self) -> Tournament:
        """Verify the slug/UUID pair once; later calls reuse the result."""
        if self._mismatch is not None:
            raise self._mismatch
        async with self._verify_lock:
            if self._verified is None:
                return await self.get_tournament()
            return self._verified

    def _check_tournament(self, path: str, tournament: Tournament) -> None:
        expected = self._config.tournament
        try:
            reported_id = uuid.UUID(tournament.id)
        except ValueError:
            raise SigResponseError(
                method="GET", path=path, reason="tournament id is not a UUID"
            ) from None
        if tournament.slug != expected.slug or reported_id != uuid.UUID(expected.id):
            self._mismatch = SigContextMismatchError(
                method="GET",
                path=path,
                reason=(
                    f"SIG reports tournament {tournament.slug!r} / {tournament.id!r}; "
                    f"configured {expected.slug!r} / {expected.id!r}"
                ),
            )
            raise self._mismatch

    # --- markets ------------------------------------------------------------------------

    async def list_tournament_markets_page(
        self,
        *,
        limit: int = MARKETS_PAGE_MAX,
        cursor: str | None = None,
        status: str = "any",
        search: str | None = None,
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
        page = await self._get_model(path, params, TournamentMarketsPage)
        self._check_page_size(path, len(page.data), limit)
        return page

    async def list_all_tournament_markets(
        self,
        *,
        status: str = "any",
        search: str | None = None,
        page_limit: int = MARKETS_PAGE_MAX,
        max_pages: int = DEFAULT_MAX_PAGES,
    ) -> list[TournamentMarket]:
        """Every market in the tournament, or SigPaginationError. Never a partial list."""
        path = f"/tournaments/{_segment(self._config.tournament.slug)}/markets"
        return await self._collect(
            path,
            lambda cursor: self.list_tournament_markets_page(
                limit=page_limit, cursor=cursor, status=status, search=search
            ),
            max_pages=max_pages,
            key=lambda market: market.id,
        )

    # --- prices and orderbooks ----------------------------------------------------------

    async def get_exchange_prices(self, exchange_ids: Sequence[str]) -> BulkExchangePrices:
        """GET /exchanges/prices for 1-100 distinct exchange ids in the tournament."""
        if isinstance(exchange_ids, str) or not isinstance(exchange_ids, Sequence):
            raise SigArgumentError("exchange_ids must be a sequence of id strings")
        ids = [_check_numeric_id("exchange id", value) for value in exchange_ids]
        if not 1 <= len(ids) <= BULK_PRICE_IDS_MAX:
            raise SigArgumentError(f"exchange_ids must hold 1 to {BULK_PRICE_IDS_MAX} ids")
        if len(set(ids)) != len(ids):
            raise SigArgumentError("exchange_ids must not contain duplicates")
        await self.verify_tournament()
        path = "/exchanges/prices"
        params = {"ids": ",".join(ids), "tournamentId": self._config.tournament.id}
        prices = await self._get_model(path, params, BulkExchangePrices)
        self._check_bulk_prices(path, ids, prices)
        return prices

    def _check_bulk_prices(self, path: str, ids: list[str], prices: BulkExchangePrices) -> None:
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
        self._check_market_context(path, book)
        for combined in (book, book.tournament_orderbook()):
            self._check_combined(path, combined, depth)
        return book

    def _check_market_context(self, path: str, book: MarketOrderbook) -> None:
        expected = self._config.tournament
        if len(book.contexts) != 1:
            raise SigContextMismatchError(
                method="GET",
                path=path,
                reason=f"expected exactly one context, got {len(book.contexts)}",
            )
        context = book.contexts[0]
        tournament = context.tournament
        if context.type != "tournament" or tournament is None:
            raise SigContextMismatchError(
                method="GET", path=path, reason="context is not a tournament context"
            )
        try:
            reported_id = uuid.UUID(tournament.id)
        except ValueError:
            raise SigResponseError(
                method="GET", path=path, reason="context tournament id is not a UUID"
            ) from None
        if reported_id != uuid.UUID(expected.id) or tournament.slug != expected.slug:
            raise SigContextMismatchError(
                method="GET",
                path=path,
                reason=(
                    f"context is tournament {tournament.slug!r} / {tournament.id!r}; "
                    f"configured {expected.slug!r} / {expected.id!r}"
                ),
            )

    def _check_combined(self, path: str, book: CombinedMarketOrderbook, depth: int) -> None:
        for exchange in book.exchanges:
            self._check_levels(path, exchange.bids, exchange.asks, depth)

    @staticmethod
    def _check_levels(
        path: str, bids: list[OrderbookLevel], asks: list[OrderbookLevel], depth: int
    ) -> None:
        # Documented: at most `depth` levels per side; bids descending, asks ascending.
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
        """
        status = _check_choice("status", status, ORDER_STATUSES)
        limit = _check_int("limit", limit, 1, ORDERS_PAGE_MAX)
        cursor = _check_cursor(cursor)
        await self.verify_tournament()
        path = "/orders"
        params: dict[str, str | int] = {
            "status": status,
            "limit": limit,
            "tournamentId": self._config.tournament.id,
        }
        if exchange_id is not None:
            params["exchangeId"] = _check_numeric_id("exchange_id", exchange_id)
        if market_id is not None:
            params["marketId"] = _check_numeric_id("market_id", market_id)
        if cursor is not None:
            params["cursor"] = cursor
        page = await self._get_model(path, params, OrdersPage)
        self._check_page_size(path, len(page.data), limit)
        if exchange_id is not None and any(o.exchangeId != exchange_id for o in page.data):
            raise SigResponseError(
                method="GET", path=path, reason="orders from another exchange were returned"
            )
        return page

    async def list_all_orders(
        self,
        *,
        status: str = "open",
        exchange_id: str | None = None,
        market_id: str | None = None,
        page_limit: int = ORDERS_PAGE_MAX,
        max_pages: int = DEFAULT_MAX_PAGES,
    ) -> list[Order]:
        """Every matching order, or SigPaginationError. Never a partial list."""
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

    # --- pagination ---------------------------------------------------------------------

    @staticmethod
    def _check_page_size(path: str, count: int, limit: int) -> None:
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
    ) -> list[Any]:
        max_pages = _check_int("max_pages", max_pages, 1, 10_000)

        def fail(reason: str) -> SigPaginationError:
            return SigPaginationError(method="GET", path=path, reason=reason)

        items: list[Any] = []
        keys: set[object] = set()
        seen_cursors: set[str] = set()
        cursor: str | None = None
        for _ in range(max_pages):
            page = await fetch(cursor)
            for item in page.data:
                item_key = key(item)
                if item_key in keys:
                    raise fail(f"item {item_key!r} appeared on more than one page")
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
        self, path: str, params: Mapping[str, str | int] | None, model: type[ModelT]
    ) -> ModelT:
        data = await self._get_json(path, params)
        try:
            return model.model_validate(data)
        except ValidationError as exc:
            problems = "; ".join(
                f"{'.'.join(str(part) for part in error['loc']) or '<root>'}: {error['type']}"
                for error in exc.errors(include_input=False, include_url=False)[:5]
            )
            raise SigResponseError(method="GET", path=path, reason=problems) from None

    async def _get_json(self, path: str, params: Mapping[str, str | int] | None) -> object:
        """GET with read budgeting and the read retry policy. Private by design."""
        method = "GET"
        deadline = self._clock() + self._retry.total_budget_s
        attempt = 0
        while True:
            if self._closed:
                raise SigClientClosedError("the SIG client is closed")
            attempt += 1
            await self._limiter.acquire(RequestKind.READ, deadline=deadline)
            error: SigApiError | SigTransportError
            wait_floor = 0.0
            try:
                response = await self._send(method, path, params)
            except RETRYABLE_TRANSPORT_ERRORS as exc:
                error = SigTransportError(method=method, path=path, kind=type(exc).__name__)
            except httpx.TransportError as exc:
                raise SigTransportError(method=method, path=path, kind=type(exc).__name__) from None
            else:
                if 200 <= response.status_code < 300:
                    return self._decode(method, path, response)
                valid, retry_after = _parse_retry_after(response.headers.get("Retry-After"))
                error = self._api_error(method, path, response, retry_after)
                if not isinstance(error, (SigRateLimitedError, SigServiceUnavailableError)):
                    raise error
                if isinstance(error, SigRateLimitedError):
                    # A missing or unreadable Retry-After falls back to the documented 60 s.
                    wait_floor = (
                        retry_after
                        if retry_after is not None
                        else self._retry.rate_limited_default_wait_s
                    )
                    self._limiter.impose_cooldown(RequestKind.READ, wait_floor)
                elif retry_after is not None:
                    wait_floor = retry_after
                if not valid:
                    logger.warning("SIG read not retried: unparseable Retry-After; %s", error)
                    raise error

            if attempt >= self._retry.max_attempts:
                raise SigRetryExhaustedError(
                    reason="attempt limit reached", attempts=attempt, last_error=error
                )
            delay = max(wait_floor, self._jitter(self._retry.backoff(attempt)))
            if self._clock() + delay > deadline:
                raise SigRetryExhaustedError(
                    reason=f"next retry in {delay:.2f}s would exceed the time budget",
                    attempts=attempt,
                    last_error=error,
                )
            logger.warning(
                "SIG read retry %d/%d in %.2fs: %s",
                attempt,
                self._retry.max_attempts,
                delay,
                error,
            )
            await self._sleep(delay)

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
        return await self._http.send(request, follow_redirects=False)

    def _api_error(
        self, method: str, path: str, response: httpx.Response, retry_after: float | None
    ) -> SigApiError:
        status = response.status_code
        code: str | None = None
        message = "non-JSON error body"
        try:
            body = json.loads(response.content)
        except ValueError:
            body = None
        if isinstance(body, dict) and isinstance(body.get("error"), dict):
            code = sanitize_code(body["error"].get("code"))
            raw_message = body["error"].get("message")
            message = raw_message if isinstance(raw_message, str) else "no message"
        elif body is not None:
            message = "error body without an error object"
        cls = _STATUS_ERRORS.get(status)
        if cls is None:
            cls = SigServerError if 500 <= status < 600 else SigUnexpectedStatusError
        return cls(
            method=method,
            path=path,
            status=status,
            code=code,
            message=sanitize_text(message, [self._config.api_key.reveal()]),
            retry_after=retry_after,
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
