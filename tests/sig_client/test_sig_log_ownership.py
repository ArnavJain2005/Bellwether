"""A2-A-F2: cursor redaction belongs to the SIG request itself, not to a time window.

httpx logs one INFO line per request on the "httpx" logger. The SIG client must redact
the response-derived cursor in the line for its own request, and must leave every other
request's line alone, including requests that run while a SIG request is being sent:
- a request made by another httpx client inside an event hook of the SIG http client
  (request and response hooks run inside httpx's `send`);
- a request made by a task created inside such a hook, which inherits that task's
  context, both while the SIG request is still in flight and after it has finished.

All clients here are real `httpx.AsyncClient`s over `httpx.MockTransport`; every
interleaving is forced with events, never with sleeps. Every logger is captured at DEBUG
and the whole captured text is checked; the httpx logger is never excluded.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable
from typing import Any

import httpx
import pytest
from account_kit import ORDERS, by_cursor, orders_page
from sig_testkit import (
    BASE_URL,
    SENTINEL_KEY,
    TOURNAMENT,
    FakeClock,
    Router,
)

from engine.config import ApiKey, SigConfig
from engine.sig_client import rest
from engine.sig_client.rate_limiter import RateLimiter
from engine.sig_client.rest import SigRestClient

SIG_CURSOR = "SENTINEL_SIG_CURSOR_41d2"  # synthetic SIG cursor: must never be logged
OTHER_CURSOR = "UNRELATED_CURSOR_b2c9"  # another client's own query value: logged as sent
OTHER_URL = f"https://other.invalid/feed?cursor={OTHER_CURSOR}"
OTHER_LINE = f'HTTP Request: GET {OTHER_URL} "HTTP/1.1 200 OK"'

Hook = Callable[[Any], Awaitable[None]]


def run(coro: Awaitable[Any]) -> Any:
    """Run a scenario with a wall-clock guard, so a regression fails instead of hanging.

    The guard only bounds the whole test; no step of any scenario is timed by it.
    """

    async def main() -> Any:
        return await asyncio.wait_for(coro, timeout=10)

    return asyncio.run(main())


def httpx_lines(caplog: pytest.LogCaptureFixture) -> list[str]:
    return [r.getMessage() for r in caplog.records if r.name == "httpx"]


def pages() -> dict[str | None, Any]:
    return {None: orders_page([1], cursor=SIG_CURSOR), SIG_CURSOR: orders_page([2], cursor=None)}


def other_client() -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.MockTransport(lambda request: httpx.Response(200)))


def sig_client(handler: Any, hooks: dict[str, list[Hook]] | None = None) -> SigRestClient:
    """A SigRestClient over a caller-built httpx client that may carry event hooks."""
    clock = FakeClock()
    config = SigConfig(api_key=ApiKey(SENTINEL_KEY), tournament=TOURNAMENT, base_url=BASE_URL)
    http = httpx.AsyncClient(
        transport=httpx.MockTransport(Router({ORDERS: handler})), event_hooks=hooks or {}
    )
    return SigRestClient(
        config,
        http,
        owns_http=True,
        clock=clock,
        sleep=clock.sleep,
        jitter=lambda delay: delay,
        limiter=RateLimiter(clock=clock, sleep=clock.sleep),
    )


def is_cursor_page(subject: httpx.Request | httpx.Response) -> bool:
    request = subject.request if isinstance(subject, httpx.Response) else subject
    return request.url.params.get("cursor") == SIG_CURSOR


def assert_isolated(caplog: pytest.LogCaptureFixture, unrelated: int = 1) -> None:
    lines = httpx_lines(caplog)
    assert SIG_CURSOR not in caplog.text  # the SIG cursor is never logged ...
    assert sum("cursor=REDACTED" in line for line in lines) == 1  # ... but its line stays
    assert lines.count(OTHER_LINE) == unrelated  # unrelated lines exactly as sent


# --- 1. nested requests inside a SIG request's event hooks ----------------------------------


@pytest.mark.parametrize("hook_kind", ["request", "response"])
def test_a_request_nested_in_a_sig_hook_is_logged_unchanged(
    hook_kind: str, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.DEBUG)
    other = other_client()

    async def hook(subject: Any) -> None:
        if is_cursor_page(subject):
            await other.get(OTHER_URL)  # inside httpx's send of the SIG request

    client = sig_client(by_cursor(pages()), {hook_kind: [hook]})

    async def scenario() -> None:
        async with client, other:
            walk = await client.read_open_orders(page_limit=1)
            assert [order.id for order in walk.orders] == [1, 2]

    run(scenario())
    assert_isolated(caplog)


# --- 2. tasks created inside a SIG hook inherit its context -----------------------------------


@pytest.mark.parametrize("when", ["while the SIG request is in flight", "after it finished"])
def test_a_task_spawned_in_a_sig_hook_logs_unchanged(
    when: str, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.DEBUG)
    other = other_client()
    sig_finished = asyncio.Event()  # set by the test once the SIG walk has returned
    child_logged = asyncio.Event()
    children: list[asyncio.Task[None]] = []
    in_flight = when.startswith("while")

    async def child() -> None:
        if not in_flight:
            await sig_finished.wait()
        await other.get(OTHER_URL)
        child_logged.set()

    async def hook(request: httpx.Request) -> None:
        if is_cursor_page(request):
            children.append(asyncio.create_task(child()))  # copies this task's context

    serve = by_cursor(pages())

    async def handler(request: httpx.Request) -> httpx.Response:
        if in_flight and request.url.params.get("cursor") == SIG_CURSOR:
            await child_logged.wait()  # hold the SIG request open until the child logs
        return serve(request)

    client = sig_client(handler, {"request": [hook]})

    async def scenario() -> None:
        async with client, other:
            await client.read_open_orders(page_limit=1)
            sig_finished.set()
            await asyncio.gather(*children)

    run(scenario())
    assert child_logged.is_set()
    assert_isolated(caplog)


# --- positive controls --------------------------------------------------------------------------


def test_control_normal_pagination_stays_redacted_with_hooks_present(
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level(logging.DEBUG)
    seen: list[str] = []

    async def hook(request: httpx.Request) -> None:
        seen.append(request.url.path)  # a hook that makes no request of its own

    client = sig_client(by_cursor(pages()), {"request": [hook]})
    walk = run(client.read_open_orders(page_limit=1))
    assert [order.id for order in walk.orders] == [1, 2]
    assert len(seen) == 3  # tournament verification + two pages
    lines = httpx_lines(caplog)
    assert SIG_CURSOR not in caplog.text
    assert len(lines) == 3 and "cursor=REDACTED" in lines[2]
    assert f"tournamentId={TOURNAMENT.id}" in lines[2] and "status=open" in lines[2]


def test_control_unrelated_sibling_requests_are_unchanged_even_for_an_identical_url(
    caplog: pytest.LogCaptureFixture,
) -> None:
    # A sibling task's request to the very same URL string as the SIG cursor page, sent
    # while that page is in flight, is another request: its line is left as sent.
    caplog.set_level(logging.DEBUG)
    sig_in_flight = asyncio.Event()
    sibling_done = asyncio.Event()
    serve = by_cursor(pages())
    sig_urls: list[str] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        if request.url.params.get("cursor") == SIG_CURSOR:
            sig_urls.append(str(request.url))
            sig_in_flight.set()
            await sibling_done.wait()
        return serve(request)

    client = sig_client(handler)
    other = other_client()

    async def sibling() -> None:
        await sig_in_flight.wait()
        await other.get(OTHER_URL)
        await other.get(sig_urls[0])  # identical text, different request
        sibling_done.set()

    async def scenario() -> None:
        async with client, other:
            await asyncio.gather(client.read_open_orders(page_limit=1), sibling())

    run(scenario())
    lines = httpx_lines(caplog)
    assert lines.count(OTHER_LINE) == 1
    assert sum(f"GET {sig_urls[0]} " in line for line in lines) == 1  # the sibling's own
    assert sum("cursor=REDACTED" in line for line in lines) == 1  # the SIG request's


@pytest.mark.parametrize("ending", ["cancelled", "transport failure"])
def test_control_nothing_stays_owned_after_a_request_ends_abnormally(
    ending: str, caplog: pytest.LogCaptureFixture
) -> None:
    # After a SIG request ends by cancellation or failure, a later request in the same
    # task (same context) by another client is logged as sent.
    caplog.set_level(logging.DEBUG)
    started = asyncio.Event()
    serve = by_cursor(pages())

    async def handler(request: httpx.Request) -> httpx.Response:
        if request.url.params.get("cursor") == SIG_CURSOR:
            started.set()
            if ending == "cancelled":
                await asyncio.Event().wait()  # never answers
            raise httpx.UnsupportedProtocol("synthetic, not retried")
        return serve(request)

    client = sig_client(handler)
    other = other_client()

    async def scenario() -> None:
        async with client, other:
            walk = asyncio.create_task(client.read_open_orders(page_limit=1))
            await started.wait()
            if ending == "cancelled":
                walk.cancel()
            with pytest.raises((asyncio.CancelledError, Exception)):
                await walk
            await other.get(OTHER_URL)

    run(scenario())
    assert httpx_lines(caplog).count(OTHER_LINE) == 1
    assert SIG_CURSOR not in caplog.text


# --- ownership details: replaced URLs, fail-closed handling, cleanup, concurrency --------------


def test_control_a_hook_replacing_the_sig_url_is_still_redacted(
    caplog: pytest.LogCaptureFixture,
) -> None:
    # Ownership follows the SIG request object, so a hook that swaps in a new (equal) URL
    # object cannot move the cursor out of the redaction.
    caplog.set_level(logging.DEBUG)

    async def hook(request: httpx.Request) -> None:
        if is_cursor_page(request):
            request.url = request.url.copy_with()  # a new URL object, same text

    client = sig_client(by_cursor(pages()), {"request": [hook]})
    run(client.read_open_orders(page_limit=1))
    assert SIG_CURSOR not in caplog.text
    assert sum("cursor=REDACTED" in line for line in httpx_lines(caplog)) == 1


def test_control_a_redaction_failure_withholds_the_url(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.DEBUG)

    def broken(url: httpx.URL) -> httpx.URL:
        raise RuntimeError("synthetic redaction failure")

    monkeypatch.setattr(rest, "_redacted_url", broken)
    other = other_client()

    async def hook(request: httpx.Request) -> None:
        if is_cursor_page(request):
            await other.get(OTHER_URL)

    client = sig_client(by_cursor(pages()), {"request": [hook]})

    async def scenario() -> None:
        async with client, other:
            walk = await client.read_open_orders(page_limit=1)
            assert len(walk.orders) == 2  # logging never breaks the request

    run(scenario())
    lines = httpx_lines(caplog)
    assert SIG_CURSOR not in caplog.text
    assert sum("<url withheld>" in line for line in lines) == 3  # every SIG line
    assert lines.count(OTHER_LINE) == 1  # an unrelated line is not withheld


@pytest.mark.parametrize("ending", ["success", "cancelled", "transport failure"])
def test_control_the_ownership_registry_is_emptied(ending: str) -> None:
    started = asyncio.Event()
    serve = by_cursor(pages())
    seen: list[int] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        is_cursor = request.url.params.get("cursor") == SIG_CURSOR
        if is_cursor:
            started.set()
        registry = getattr(rest, "_SIG_REQUESTS_IN_FLIGHT", None)
        seen.append(-1 if registry is None else len(registry))  # owned while being sent
        if ending != "success" and is_cursor:
            if ending == "cancelled":
                await asyncio.Event().wait()
            raise httpx.UnsupportedProtocol("synthetic, not retried")
        return serve(request)

    client = sig_client(handler)

    async def scenario() -> None:
        async with client:
            walk = asyncio.create_task(client.read_open_orders(page_limit=1))
            if ending == "cancelled":
                await started.wait()
                walk.cancel()
            if ending == "success":
                await walk
            else:
                with pytest.raises((asyncio.CancelledError, Exception)):
                    await walk

    run(scenario())
    assert seen and all(count == 1 for count in seen)
    assert getattr(rest, "_SIG_REQUESTS_IN_FLIGHT", None) == {}


def test_control_concurrent_sig_clients_and_an_unrelated_sibling(
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level(logging.DEBUG)
    both_in_flight = asyncio.Event()
    in_flight: list[str] = []
    other_done = asyncio.Event()

    def held(name: str) -> Any:
        serve = by_cursor(pages())

        async def handler(request: httpx.Request) -> httpx.Response:
            if request.url.params.get("cursor") == SIG_CURSOR:
                in_flight.append(name)
                if len(in_flight) == 2:
                    both_in_flight.set()
                await other_done.wait()
            return serve(request)

        return handler

    first, second = sig_client(held("first")), sig_client(held("second"))
    other = other_client()

    async def sibling() -> None:
        await both_in_flight.wait()
        await other.get(OTHER_URL)
        other_done.set()

    async def scenario() -> None:
        async with first, second, other:
            await asyncio.gather(
                first.read_open_orders(page_limit=1),
                second.read_open_orders(page_limit=1),
                sibling(),
            )

    run(scenario())
    lines = httpx_lines(caplog)
    assert SIG_CURSOR not in caplog.text
    assert sum("cursor=REDACTED" in line for line in lines) == 2
    assert lines.count(OTHER_LINE) == 1
