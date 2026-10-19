"""A2-A-F1 finding 1: response-derived cursors never reach any log (design 4.1 R8).

httpx writes one INFO line per request on the "httpx" logger, with the full request URL.
For a cursor walk that URL carries the previous page's `nextCursor`, which is response
data. The client redacts that value in httpx's line for its own requests only:
- the line itself is kept (method, scheme, host, path, configuration-derived query
  parameters, status), so the diagnostic stays useful;
- requests made by any other httpx client in the process, including one running
  concurrently in another task, are logged exactly as before;
- no logger is disabled and no level, handler or propagation setting is changed.

Every test captures every logger at DEBUG and checks the whole captured text.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

import httpcore
import httpx
import pytest
from account_kit import ORDERS, by_cursor, orders_page
from sig_testkit import (
    API,
    TOURNAMENT,
    Router,
    fixture,
    json_response,
    make_client,
    run,
)

CURSOR = "SENTINEL_CURSOR_7f3a"  # synthetic; stands for an opaque cursor sent by SIG
MARKETS = f"{API}/tournaments/alpha-tournament/markets"
TRADES = f"{API}/exchanges/36/trades"

# Saved before conftest.py's autouse guard replaces it (see the real-transport test).
REAL_HANDLE = httpx.AsyncHTTPTransport.handle_async_request


def httpx_lines(caplog: pytest.LogCaptureFixture) -> list[str]:
    return [r.getMessage() for r in caplog.records if r.name == "httpx"]


def walk_pages() -> dict[str | None, Any]:
    return {None: orders_page([1], cursor=CURSOR), CURSOR: orders_page([2], cursor=None)}


def test_open_orders_walk_never_logs_the_cursor(caplog: pytest.LogCaptureFixture) -> None:
    caplog.set_level(logging.DEBUG)
    client, router, _ = make_client(Router({ORDERS: by_cursor(walk_pages())}))
    walk = run(client.read_open_orders(page_limit=1))
    assert [order.id for order in walk.orders] == [1, 2]
    assert router.requests[-1].url.params["cursor"] == CURSOR  # it was sent
    assert CURSOR not in caplog.text
    # Positive control: httpx's line for the cursor request is still there and still
    # useful; only the cursor value is replaced.
    lines = [line for line in httpx_lines(caplog) if "/orders?" in line]
    assert len(lines) == 2
    assert "cursor=" not in lines[0]
    assert "cursor=REDACTED" in lines[1]
    for expected in (
        "GET https://sig.invalid/api/v1/orders?",
        "status=open",
        "limit=1",
        f"tournamentId={TOURNAMENT.id}",
        '"HTTP/1.1 200 OK"',
    ):
        assert expected in lines[1]


def test_every_existing_cursor_walk_is_redacted(caplog: pytest.LogCaptureFixture) -> None:
    caplog.set_level(logging.DEBUG)
    markets_first = fixture("tournament_markets_page1")
    markets_first["pagination"]["nextCursor"] = CURSOR
    trades_first = fixture("exchange_trades")
    trades_first["pagination"].update(hasMore=True, nextCursor=CURSOR)
    trades_second = fixture("exchange_trades") | {"data": []}

    def trades(request: httpx.Request) -> httpx.Response:
        body = trades_second if request.url.params.get("cursor") else trades_first
        return json_response(200, body)

    def markets(request: httpx.Request) -> httpx.Response:
        if request.url.params.get("cursor"):
            return json_response(200, fixture("tournament_markets_page2"))
        return json_response(200, markets_first)

    router = Router({MARKETS: markets, TRADES: trades, ORDERS: by_cursor(walk_pages())})
    client, _, _ = make_client(router)

    async def scenario() -> None:
        await client.list_all_tournament_markets(page_limit=1)
        await client.list_all_exchange_trades("36", page_limit=200)
        await client.list_all_orders(page_limit=1)

    run(scenario())
    assert sum(r.url.params.get("cursor") == CURSOR for r in router.requests) == 3
    assert CURSOR not in caplog.text
    assert sum("cursor=REDACTED" in line for line in httpx_lines(caplog)) == 3


def test_unrelated_httpx_clients_are_logged_unchanged(caplog: pytest.LogCaptureFixture) -> None:
    caplog.set_level(logging.DEBUG)
    other_url = f"https://other.invalid/feed?cursor={CURSOR}"

    async def scenario() -> None:
        sig, _, _ = make_client(Router({ORDERS: by_cursor(walk_pages())}))
        await sig.read_open_orders(page_limit=1)  # installs the client's filter
        transport = httpx.MockTransport(lambda request: httpx.Response(200))
        async with httpx.AsyncClient(transport=transport) as other:
            await other.get(other_url)

    run(scenario())
    assert f"GET {other_url}" in "\n".join(httpx_lines(caplog))  # not altered


def test_concurrent_unrelated_request_keeps_its_line_while_ours_is_redacted(
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level(logging.DEBUG)
    other_url = f"https://other.invalid/feed?cursor={CURSOR}"
    sig_in_flight = asyncio.Event()
    other_done = asyncio.Event()

    async def held_second_page(request: httpx.Request) -> httpx.Response:
        if request.url.params.get("cursor") == CURSOR:
            sig_in_flight.set()
            await other_done.wait()  # the other client logs while ours is mid-request
        return by_cursor(walk_pages())(request)

    async def other_request() -> None:
        await sig_in_flight.wait()
        transport = httpx.MockTransport(lambda request: httpx.Response(200))
        async with httpx.AsyncClient(transport=transport) as other:
            await other.get(other_url)
        other_done.set()

    sig, _, _ = make_client(Router({ORDERS: held_second_page}))

    async def scenario() -> None:
        await asyncio.gather(sig.read_open_orders(page_limit=1), other_request())

    run(scenario())
    lines = httpx_lines(caplog)
    other = [i for i, line in enumerate(lines) if f"GET {other_url}" in line]
    ours = [i for i, line in enumerate(lines) if "cursor=REDACTED" in line]
    assert len(other) == 1 and len(ours) == 1
    assert other[0] < ours[0]  # logged while our request was in flight, unredacted
    assert CURSOR not in lines[ours[0]]


def test_logging_is_not_disabled_or_reconfigured(caplog: pytest.LogCaptureFixture) -> None:
    caplog.set_level(logging.INFO)
    httpx_logger = logging.getLogger("httpx")
    before = (
        httpx_logger.level,
        httpx_logger.disabled,
        httpx_logger.propagate,
        list(httpx_logger.handlers),
    )
    for _ in range(3):  # several clients share one filter
        client, _, _ = make_client(Router({ORDERS: by_cursor(walk_pages())}))
        run(client.read_open_orders(page_limit=1))
    after = (
        httpx_logger.level,
        httpx_logger.disabled,
        httpx_logger.propagate,
        list(httpx_logger.handlers),
    )
    assert before == after
    assert len(httpx_logger.filters) <= 1
    assert len(httpx_lines(caplog)) == 3 * 3  # tournament + two pages, per client


def test_real_httpx_transport_and_httpcore_debug_logs_hold_no_cursor(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    # The real httpx transport and httpcore's HTTP/1.1 code, over httpcore's in-memory
    # mock network backend: no socket is opened (conftest.py still blocks DNS and
    # connections), but every log line production would write is produced.
    def response(body: Any) -> bytes:
        payload = json_response(200, body).content
        head = (
            b"HTTP/1.1 200 OK\r\nContent-Type: application/json\r\n"
            + f"Content-Length: {len(payload)}\r\n\r\n".encode()
        )
        return head + payload

    backend = httpcore.AsyncMockBackend(
        [
            response(fixture("tournament")),
            response(orders_page([1], cursor=CURSOR)),
            response(orders_page([2], cursor=None)),
        ]
    )
    transport = httpx.AsyncHTTPTransport()
    transport._pool = httpcore.AsyncConnectionPool(network_backend=backend)  # test-only
    monkeypatch.setattr(httpx.AsyncHTTPTransport, "handle_async_request", REAL_HANDLE)

    client, _, _ = make_client()
    client._http = httpx.AsyncClient(transport=transport)  # same request path otherwise
    caplog.set_level(logging.DEBUG)

    async def scenario() -> Any:
        async with client:
            return await client.read_open_orders(page_limit=1)

    walk = run(scenario())
    assert [order.id for order in walk.orders] == [1, 2]
    assert any(r.name.startswith("httpcore") for r in caplog.records)  # DEBUG captured
    assert CURSOR not in caplog.text
    assert any("cursor=REDACTED" in line for line in httpx_lines(caplog))
