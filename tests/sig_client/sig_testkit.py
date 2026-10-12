"""Shared helpers for the SIG client tests: fake time, mock routing, fixtures.

All traffic goes through httpx.MockTransport. The base URL uses the reserved `.invalid`
domain, so even a leaked request could not resolve. conftest.py also blocks httpx's real
transports and DNS for every test in this folder.
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import Awaitable, Callable
from datetime import datetime
from pathlib import Path
from typing import Any

import httpx

from engine.config import ApiKey, SigConfig, TournamentRef
from engine.sig_client.rate_limiter import RateLimiter
from engine.sig_client.rest import ReadRetryPolicy, SigRestClient, utc_now

FIXTURES = Path(__file__).resolve().parent.parent / "fixtures" / "sig"

SENTINEL_KEY = "dummy-sentinel-not-a-real-key"  # test placeholder, never a credential
BASE_URL = "https://sig.invalid/api/v1"
# Matches tournament.json (the spec's example tournament), not the real competition.
TOURNAMENT = TournamentRef(slug="alpha-tournament", id="550e8400-e29b-41d4-a716-446655440000")
API = "/api/v1"


class RealNetworkUsed(RuntimeError):
    """Raised by conftest.py's guard if a test reaches a real httpx transport or DNS."""


def fixture(name: str) -> Any:
    """The `body` of a synthetic fixture file."""
    return json.loads((FIXTURES / f"{name}.json").read_text(encoding="utf-8"))["body"]


def error_body(code: str) -> Any:
    return fixture("error_envelopes")[code]


def json_response(status: int, body: Any, headers: dict[str, str] | None = None) -> httpx.Response:
    return httpx.Response(
        status,
        content=json.dumps(body).encode(),
        headers={"content-type": "application/json", **(headers or {})},
    )


def raw_response(
    status: int, content: bytes, headers: dict[str, str] | None = None
) -> httpx.Response:
    return httpx.Response(status, content=content, headers=headers or {})


class FakeClock:
    """Monotonic time that moves only when the code under test sleeps."""

    def __init__(self, start: float = 1000.0) -> None:
        self.now = start
        self.sleeps: list[float] = []

    def __call__(self) -> float:
        return self.now

    async def sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)
        self.now += seconds
        await asyncio.sleep(0)


Handler = Callable[[httpx.Request], httpx.Response | Awaitable[httpx.Response]]


class Router:
    """Serves the standard fixtures; tests override paths with a response, a callable, or
    a list of responses served in order. Records every request."""

    def __init__(self, overrides: dict[str, Any] | None = None) -> None:
        self.requests: list[httpx.Request] = []
        self.routes: dict[str, Any] = {
            f"{API}/tournaments/alpha-tournament": json_response(200, fixture("tournament")),
            f"{API}/tournaments/alpha-tournament/markets": json_response(
                200, fixture("tournament_markets_page1")
            ),
            f"{API}/exchanges/prices": json_response(200, fixture("exchange_prices")),
            f"{API}/exchanges/36/orderbook": json_response(200, fixture("exchange_orderbook")),
            f"{API}/markets/26/orderbook": json_response(200, fixture("market_orderbook")),
            f"{API}/orders": json_response(200, fixture("orders_page")),
            f"{API}/markets/26": json_response(200, fixture("market")),
            f"{API}/exchanges/36/trades": json_response(200, fixture("exchange_trades")),
            f"{API}/tournaments/alpha-tournament/portfolio/positions": json_response(
                200, fixture("tournament_positions")
            ),
        }
        self.routes.update(overrides or {})

    async def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        route = self.routes.get(request.url.path)
        if route is None:
            return json_response(404, error_body("NOT_FOUND"))
        if isinstance(route, list):
            route = route.pop(0) if len(route) > 1 else route[0]
        if callable(route):
            result = route(request)
            if asyncio.iscoroutine(result):
                result = await result
            route = result
        if isinstance(route, BaseException):
            raise route
        return route

    def paths(self) -> list[str]:
        return [r.url.path.removeprefix(API) for r in self.requests]

    def count(self, path: str) -> int:
        return self.paths().count(path)


def make_client(
    router: Router | None = None,
    *,
    clock: FakeClock | None = None,
    retry: ReadRetryPolicy | None = None,
    limiter: RateLimiter | None = None,
    jitter: Callable[[float], float] = lambda delay: delay,
    tournament: TournamentRef = TOURNAMENT,
    api_key: str = SENTINEL_KEY,
    wall_clock: Callable[[], datetime] | None = None,
) -> tuple[SigRestClient, Router, FakeClock]:
    router = router or Router()
    clock = clock or FakeClock()
    config = SigConfig(api_key=ApiKey(api_key), tournament=tournament, base_url=BASE_URL)
    http = httpx.AsyncClient(transport=httpx.MockTransport(router))
    client = SigRestClient(
        config,
        http,
        owns_http=True,
        clock=clock,
        sleep=clock.sleep,
        jitter=jitter,
        limiter=limiter or RateLimiter(clock=clock, sleep=clock.sleep),
        retry=retry,
        wall_clock=wall_clock or utc_now,
    )
    return client, router, clock


def run(coro: Awaitable[Any]) -> Any:
    async def main() -> Any:
        return await coro

    return asyncio.run(main())


def secret_leaks(error: BaseException, secret: str) -> list[str]:
    """Every place in an exception (text, attributes, chained errors) holding `secret`."""
    found: list[str] = []
    seen: set[int] = set()

    def visit(value: object, where: str) -> None:
        if id(value) in seen:
            return
        seen.add(id(value))
        if isinstance(value, BaseException):
            for label, text in (("str", str(value)), ("repr", repr(value))):
                if secret in text:
                    found.append(f"{where}.{label}")
            for name, attr in vars(value).items():
                visit(attr, f"{where}.{name}")
            if value.__cause__ is not None:
                visit(value.__cause__, f"{where}.__cause__")
            if value.__context__ is not None and not value.__suppress_context__:
                visit(value.__context__, f"{where}.__context__")
        elif isinstance(value, (list, tuple, set, frozenset)):
            for i, item in enumerate(value):
                visit(item, f"{where}[{i}]")
        elif isinstance(value, dict):
            for key, item in value.items():
                visit(key, f"{where}<key>")
                visit(item, f"{where}[{key!r}]")
        elif secret in repr(value):
            found.append(where)

    visit(error, "error")
    return found
