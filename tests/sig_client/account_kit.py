"""Helpers for the A2-A account-read tests. Offline only: synthetic bodies, MockTransport.

`exact_body` / `exact_response` keep every JSON number exactly as written in the fixture
file (the standard `fixture()` helper goes through float and back, which is fine for
shape tests but not for exactness tests).
"""

from __future__ import annotations

import asyncio
import contextvars
import copy
import json
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any

import httpx
from sig_testkit import API, FIXTURES, FakeClock, fixture, json_response

TOURNAMENT_PATH = f"{API}/tournaments/alpha-tournament"
POSITIONS = f"{TOURNAMENT_PATH}/portfolio/positions"
ORDERS = f"{API}/orders"


def exact_body(name: str) -> Any:
    """A fixture body with every non-integer number as the exact Decimal written."""
    text = (FIXTURES / f"{name}.json").read_text(encoding="utf-8")
    return json.loads(text, parse_float=Decimal)["body"]


def _encode(value: Any) -> str:
    if isinstance(value, Decimal):
        return str(value)
    if isinstance(value, dict):
        return "{" + ",".join(f"{json.dumps(k)}:{_encode(v)}" for k, v in value.items()) + "}"
    if isinstance(value, list):
        return "[" + ",".join(_encode(v) for v in value) + "]"
    return json.dumps(value)


def exact_response(status: int, body: Any, headers: dict[str, str] | None = None) -> httpx.Response:
    """A JSON response whose Decimal numbers are written out digit for digit."""
    return httpx.Response(
        status,
        content=_encode(body).encode(),
        headers={"content-type": "application/json", **(headers or {})},
    )


def tournament_body(**changes: Any) -> dict[str, Any]:
    return fixture("tournament") | changes


NULL = object()  # sentinel: send `"coverage": null` explicitly


def orders_page(
    ids: list[int],
    *,
    cursor: str | None,
    coverage: Any = "default",
    limit: int = 200,
) -> dict[str, Any]:
    """A synthetic /orders page holding copies of fixture order 1001 with the given ids.

    `coverage="default"` keeps the fixture's coverage; `coverage=None` omits the key;
    any other value is sent as given (a dict, or the JSON null via `coverage=NULL`).
    """
    template = fixture("orders_page")["data"][0]
    body: dict[str, Any] = {
        "data": [dict(copy.deepcopy(template), id=order_id) for order_id in ids],
        "pagination": {"limit": limit, "hasMore": cursor is not None, "nextCursor": cursor},
    }
    if coverage == "default":
        body["coverage"] = fixture("orders_page")["coverage"]
    elif coverage is NULL:
        body["coverage"] = None
    elif coverage is not None:
        body["coverage"] = coverage
    return body


def by_cursor(pages: dict[str | None, Any]) -> Callable[[httpx.Request], httpx.Response]:
    """Serve /orders pages keyed by the request's cursor (None for the first page)."""

    def handler(request: httpx.Request) -> httpx.Response:
        page = pages[request.url.params.get("cursor")]
        return page if isinstance(page, httpx.Response) else json_response(200, page)

    return handler


def taking(clock: FakeClock, seconds: float, response: Any) -> Callable[[httpx.Request], Any]:
    """A handler that advances the fake clock by `seconds` before answering."""

    def handler(request: httpx.Request) -> Any:
        clock.now += seconds
        return response

    return handler


class WallClock:
    """A display clock that ticks one second per call, in UTC."""

    def __init__(self) -> None:
        self.now = datetime(2026, 10, 13, 12, 0, 0, tzinfo=UTC)
        self.calls = 0

    def __call__(self) -> datetime:
        self.calls += 1
        self.now += timedelta(seconds=1)
        return self.now


# Per-task scripted responses: the MockTransport handler runs inside the calling task, so
# a context variable set in that task tells the handler which caller it is serving.
CALLER: contextvars.ContextVar[str] = contextvars.ContextVar("CALLER")


def per_caller(
    scripts: dict[str, list[Callable[[], Awaitable[httpx.Response]]]],
) -> Callable[[httpx.Request], Awaitable[httpx.Response]]:
    """Serve each caller (named by CALLER) its own scripted responses, in order."""

    async def handler(request: httpx.Request) -> httpx.Response:
        return await scripts[CALLER.get()].pop(0)()

    return handler


async def as_caller(name: str, call: Awaitable[Any]) -> Any:
    CALLER.set(name)
    return await call


async def until(predicate: Callable[[], bool], *, rounds: int = 200) -> None:
    """Yield to the event loop until `predicate()` holds (bounded)."""
    for _ in range(rounds):
        if predicate():
            return
        await asyncio.sleep(0)
    raise AssertionError("condition not reached")
