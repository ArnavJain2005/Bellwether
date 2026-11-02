"""Reference prices from Kalshi and Polymarket public endpoints (no keys, never SIG).

Kalshi: GET /trade-api/v2/markets?tickers=A,B,... (comma-separated, <= 1000 per page).
Prices come from the documented `yes_bid_dollars` / `yes_ask_dollars` strings; the
undocumented cent fields `yes_bid` / `yes_ask` are only a fallback.
Polymarket CLOB: POST /books with [{"token_id": ...}]; GET /midpoint?token_id= fallback.
"""

from __future__ import annotations

import asyncio
import json
import time
from collections.abc import Awaitable, Callable, Iterable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation
from typing import Any, Self

import httpx

KALSHI_HOSTS = ("api.elections.kalshi.com", "external-api.kalshi.com")
KALSHI_PATH = "/trade-api/v2/markets"
POLY_BASE = "https://clob.polymarket.com"
KALSHI_CHUNK = 50
POLY_CHUNK = 50
KALSHI_RPS = 8
POLY_RPS = 10
TIMEOUT = 5.0
MAX_ATTEMPTS = 4
BACKOFF_BASE_S = 0.5
BACKOFF_MAX_S = 8.0

ACTIVE = "active"
_KALSHI_ACTIVE = {"open", "active"}
# Non-active statuses besides the raw venue status: no two-sided quote, no data, bad data.
NO_QUOTE, MISSING, MID_ONLY, CROSSED, ERROR = "no_quote", "missing", "mid_only", "crossed", "error"


@dataclass(frozen=True)
class RefQuote:
    venue: str  # "kalshi" | "polymarket"
    id: str  # Kalshi ticker or Polymarket token id
    bid: Decimal | None
    ask: Decimal | None
    mid: Decimal | None
    spread: Decimal | None
    fetched_utc: datetime
    status: str  # ACTIVE or why not


@dataclass(frozen=True)
class RefSettings:
    max_ref_spread: Decimal
    max_ref_age_s: Decimal
    max_venue_disagreement: Decimal

    @classmethod
    def from_mapping(cls, reference: dict[str, Any]) -> RefSettings:
        """Build from the `strategies.reference` section of config/risk.yaml."""
        return cls(*(Decimal(str(reference[k])) for k in (
            "max_ref_spread", "max_ref_age_s", "max_venue_disagreement")))


@dataclass
class RefSnapshot:
    kalshi: dict[str, RefQuote] = field(default_factory=dict)
    polymarket: dict[str, RefQuote] = field(default_factory=dict)
    errors: dict[str, str] = field(default_factory=dict)  # venue -> last error


class VenueError(Exception):
    pass


def _dec(value: Any) -> Decimal | None:
    if value is None or value == "":
        return None
    try:
        d = Decimal(str(value))
    except InvalidOperation:
        return None
    return d if d.is_finite() else None


def _loads(content: bytes) -> Any:
    return json.loads(content, parse_float=Decimal)


def make_quote(venue: str, id_: str, bid: Decimal | None, ask: Decimal | None,
               fetched: datetime, status: str = ACTIVE) -> RefQuote:
    """Derive mid and spread; a non-active or one-sided/crossed quote has neither."""
    if status == ACTIVE and (bid is None or ask is None):
        status = NO_QUOTE
    if status == ACTIVE and bid > ask:  # type: ignore[operator]
        status = CROSSED
    if status == ACTIVE:
        return RefQuote(venue, id_, bid, ask, (bid + ask) / 2, ask - bid, fetched, status)  # type: ignore[operator]
    return RefQuote(venue, id_, bid, ask, None, None, fetched, status)


def _kalshi_price(market: dict[str, Any], dollars_key: str, cents_key: str) -> Decimal | None:
    d = _dec(market.get(dollars_key))
    if d is None:
        cents = _dec(market.get(cents_key))
        d = None if cents is None else cents / 100
    return d


def parse_kalshi_market(market: dict[str, Any], fetched: datetime) -> RefQuote:
    ticker = str(market["ticker"])
    raw_status = str(market.get("status", "")).lower()
    status = ACTIVE if raw_status in _KALSHI_ACTIVE else (raw_status or "unknown")
    bid = _kalshi_price(market, "yes_bid_dollars", "yes_bid")
    ask = _kalshi_price(market, "yes_ask_dollars", "yes_ask")
    # Kalshi reports an empty side as 0 (bid) or 1 (ask).
    if bid is not None and bid <= 0:
        bid = None
    if ask is not None and ask >= 1:
        ask = None
    return make_quote("kalshi", ticker, bid, ask, fetched, status)


def parse_polymarket_book(book: dict[str, Any], fetched: datetime) -> RefQuote:
    def prices(side: str) -> list[Decimal]:
        out = [_dec(lvl.get("price")) for lvl in book.get(side) or [] if isinstance(lvl, dict)]
        return [p for p in out if p is not None]

    bids, asks = prices("bids"), prices("asks")
    return make_quote("polymarket", str(book["asset_id"]), max(bids, default=None),
                      min(asks, default=None), fetched)


class _Pacer:
    """Spaces request starts at least 1/rate seconds apart."""

    def __init__(self, rate: float, clock: Callable[[], float],
                 sleep: Callable[[float], Awaitable[None]]) -> None:
        self._interval, self._clock, self._sleep = 1.0 / rate, clock, sleep
        self._next = 0.0
        self._lock = asyncio.Lock()

    async def wait(self) -> None:
        async with self._lock:
            now = self._clock()
            if now < self._next:
                await self._sleep(self._next - now)
                now = self._next
            self._next = now + self._interval


def _chunks(items: list[str], n: int) -> Iterable[list[str]]:
    for i in range(0, len(items), n):
        yield items[i:i + n]


class RefCollector:
    def __init__(self, client: httpx.AsyncClient | None = None, *,
                 clock: Callable[[], float] = time.monotonic,
                 sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
                 now: Callable[[], datetime] = lambda: datetime.now(UTC)) -> None:
        self._own = client is None
        self._client = client or httpx.AsyncClient(timeout=TIMEOUT)
        self._sleep, self._now = sleep, now
        self._kalshi_pacer = _Pacer(KALSHI_RPS, clock, sleep)
        self._poly_pacer = _Pacer(POLY_RPS, clock, sleep)
        self._kalshi_host = 0

    async def aclose(self) -> None:
        if self._own:
            await self._client.aclose()

    async def __aenter__(self) -> Self:
        return self

    async def __aexit__(self, *exc: object) -> None:
        await self.aclose()

    async def _request(self, pacer: _Pacer, method: str, url: str, **kw: Any) -> Any:
        """One request with exponential backoff on 429 / 5xx / transport errors."""
        last = "no attempt"
        for attempt in range(MAX_ATTEMPTS):
            if attempt:
                await self._sleep(min(BACKOFF_BASE_S * 2 ** (attempt - 1), BACKOFF_MAX_S))
            await pacer.wait()
            try:
                resp = await self._client.request(method, url, timeout=TIMEOUT, **kw)
            except httpx.TransportError as exc:
                last = f"{type(exc).__name__}: {exc}"
                continue
            if resp.status_code == 429 or resp.status_code >= 500:
                last = f"HTTP {resp.status_code}"
                continue
            if resp.status_code != 200:
                raise VenueError(f"HTTP {resp.status_code} from {url}")
            try:
                return _loads(resp.content)
            except ValueError as exc:
                raise VenueError(f"bad JSON from {url}: {exc}") from exc
        raise VenueError(f"{url}: {last} after {MAX_ATTEMPTS} attempts")

    async def _kalshi_chunk(self, tickers: list[str]) -> Any:
        params = {"tickers": ",".join(tickers), "limit": 1000}
        err: VenueError | None = None
        for step in range(len(KALSHI_HOSTS)):
            idx = (self._kalshi_host + step) % len(KALSHI_HOSTS)
            try:
                data = await self._request(self._kalshi_pacer, "GET",
                                           f"https://{KALSHI_HOSTS[idx]}{KALSHI_PATH}",
                                           params=params)
            except VenueError as exc:
                err = exc
                continue
            self._kalshi_host = idx
            return data
        raise err or VenueError("kalshi: no host")

    async def fetch_kalshi(self, tickers: Iterable[str]) -> tuple[dict[str, RefQuote], str | None]:
        """Quotes for every requested ticker; a failed chunk yields 'error' quotes."""
        wanted = list(dict.fromkeys(tickers))
        out: dict[str, RefQuote] = {}
        error: str | None = None
        for chunk in _chunks(wanted, KALSHI_CHUNK):
            fetched = self._now()
            try:
                data = await self._kalshi_chunk(chunk)
                markets = data["markets"]
                for m in markets:
                    if m.get("ticker") in chunk:
                        out[m["ticker"]] = parse_kalshi_market(m, fetched)
                missing = MISSING
            except (VenueError, KeyError, TypeError, AttributeError) as exc:
                error, missing = f"kalshi: {exc}", ERROR
            for t in chunk:
                out.setdefault(t, make_quote("kalshi", t, None, None, fetched, missing))
        return out, error

    async def fetch_polymarket(self, tokens: Iterable[str]) -> tuple[dict[str, RefQuote], str | None]:
        wanted = list(dict.fromkeys(tokens))
        out: dict[str, RefQuote] = {}
        error: str | None = None
        for chunk in _chunks(wanted, POLY_CHUNK):
            fetched = self._now()
            chunk_failed = False
            try:
                books = await self._request(self._poly_pacer, "POST", f"{POLY_BASE}/books",
                                            json=[{"token_id": t} for t in chunk])
                for b in books:
                    if b.get("asset_id") in chunk:
                        out[b["asset_id"]] = parse_polymarket_book(b, fetched)
            except (VenueError, KeyError, TypeError, AttributeError) as exc:
                error, chunk_failed = f"polymarket: {exc}", True
            for t in chunk:
                if t in out:
                    continue
                if chunk_failed:  # venue is failing: do not hammer it with fallbacks
                    out[t] = make_quote("polymarket", t, None, None, fetched, ERROR)
                else:
                    out[t] = await self._poly_midpoint(t, fetched)
        return out, error

    async def _poly_midpoint(self, token: str, fetched: datetime) -> RefQuote:
        """Fallback when the book is absent: mid only, no spread, never used for fair value."""
        try:
            data = await self._request(self._poly_pacer, "GET", f"{POLY_BASE}/midpoint",
                                       params={"token_id": token})
            mid = _dec(data.get("mid", data.get("mid_price")))
        except (VenueError, AttributeError):
            mid = None
        if mid is None:
            return make_quote("polymarket", token, None, None, fetched, MISSING)
        return RefQuote("polymarket", token, None, None, mid, None, fetched, MID_ONLY)

    async def fetch_all(self, tickers: Iterable[str], tokens: Iterable[str]) -> RefSnapshot:
        """Both venues concurrently; one failing never blocks the other."""
        res = await asyncio.gather(self.fetch_kalshi(tickers), self.fetch_polymarket(tokens),
                                   return_exceptions=True)
        snap = RefSnapshot()
        for venue, r in zip(("kalshi", "polymarket"), res, strict=True):
            if isinstance(r, BaseException):
                snap.errors[venue] = f"{type(r).__name__}: {r}"
                continue
            quotes, err = r
            setattr(snap, venue, quotes)
            if err:
                snap.errors[venue] = err
        return snap


def _valid(q: RefQuote | None, s: RefSettings, now: datetime) -> str | None:
    """None if q is usable for fair value, else the reason it is not."""
    if q is None:
        return "no quote"
    if q.status != ACTIVE:
        return q.status
    if q.mid is None or q.spread is None:
        return "no two-sided quote"
    if q.spread > s.max_ref_spread:
        return f"spread {q.spread} > {s.max_ref_spread}"
    age = max((now - q.fetched_utc).total_seconds(), 0)
    if Decimal(str(age)) > s.max_ref_age_s:
        return f"stale ({age:.0f}s > {s.max_ref_age_s}s)"
    return None


def fair_value(kalshi: RefQuote | None, poly: RefQuote | None, settings: RefSettings,
               now: datetime | None = None) -> tuple[Decimal | None, str]:
    now = now or datetime.now(UTC)
    bad = _valid(kalshi, settings, now)
    if bad:
        return None, f"kalshi {bad}"
    assert kalshi is not None and kalshi.mid is not None
    if poly is None:
        return kalshi.mid, "kalshi only (no polymarket mapped)"
    poly_bad = _valid(poly, settings, now)
    if poly_bad:
        return kalshi.mid, f"kalshi only (polymarket {poly_bad})"
    assert poly.mid is not None
    if abs(kalshi.mid - poly.mid) <= settings.max_venue_disagreement:
        return (kalshi.mid + poly.mid) / 2, "kalshi+polymarket average"
    return None, "venues disagree"
