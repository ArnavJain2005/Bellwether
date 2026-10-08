"""F2 area 2: the read deadline actively ends waits that never return.

These tests use the real monotonic clock with short budgets and sleepers that hang
forever. A watchdog bounds each test; if it fires, `asyncio.wait_for` raises
TimeoutError, which the `pytest.raises(SigDeadlineExceededError)` blocks do not accept,
so a watchdog stop can never pass as success.
"""

import asyncio
import time
from collections.abc import Awaitable
from typing import Any

import httpx
import pytest
from sig_testkit import (
    API,
    BASE_URL,
    SENTINEL_KEY,
    TOURNAMENT,
    Router,
    error_body,
    fixture,
    json_response,
    run,
)

from engine.config import ApiKey, SigConfig
from engine.sig_client.errors import SigDeadlineExceededError, SigServiceUnavailableError
from engine.sig_client.rate_limiter import RateLimiter, RequestKind
from engine.sig_client.rest import ReadRetryPolicy, SigRestClient

READ = RequestKind.READ
BOOK = f"{API}/exchanges/36/orderbook"
OK = json_response(200, fixture("exchange_orderbook"))
WATCHDOG_S = 5.0


async def hang(seconds: float) -> None:
    await asyncio.Event().wait()


def client_with(
    router: Router, *, budget: float, limiter: RateLimiter | None = None
) -> SigRestClient:
    config = SigConfig(api_key=ApiKey(SENTINEL_KEY), tournament=TOURNAMENT, base_url=BASE_URL)
    http = httpx.AsyncClient(transport=httpx.MockTransport(router))
    return SigRestClient(
        config,
        http,
        owns_http=True,
        limiter=limiter,
        retry=ReadRetryPolicy(total_budget_s=budget),
        jitter=lambda delay: delay,
    )


async def watchdog(awaitable: Awaitable[Any]) -> Any:
    return await asyncio.wait_for(awaitable, WATCHDOG_S)


def tight_limiter(sleep: Any) -> RateLimiter:
    # One read per 0.2 s: after the tournament check, the next read must wait ~0.2 s,
    # which fits inside a 0.6 s budget, so the limiter calls its (hanging) sleeper.
    return RateLimiter(read_limit=1, window=0.2, clock=time.monotonic, sleep=sleep)


def test_hanging_admission_sleeper_is_ended_by_the_read_deadline() -> None:
    limiter = tight_limiter(hang)
    client = client_with(Router(), budget=0.6, limiter=limiter)

    async def scenario() -> SigDeadlineExceededError:
        try:
            await client.verify_tournament()
            started = time.monotonic()
            with pytest.raises(SigDeadlineExceededError) as info:
                await watchdog(client.get_exchange_orderbook("36"))
            assert time.monotonic() - started < WATCHDOG_S / 2
            return info.value
        finally:
            await client.aclose()

    error = run(scenario())
    assert error.phase == "rate admission" and error.attempts == 0
    assert error.__suppress_context__
    # Only the tournament check was ever admitted; the cut-off wait consumed nothing.
    assert len(limiter._budgets[READ]._admitted) == 1


def test_cut_off_admission_leaves_no_orphaned_waiter() -> None:
    sleeper: dict[str, Any] = {"fn": hang}

    async def switchable(seconds: float) -> None:
        await sleeper["fn"](seconds)

    limiter = tight_limiter(switchable)
    client = client_with(Router(), budget=0.6, limiter=limiter)

    async def scenario() -> None:
        try:
            await client.verify_tournament()
            with pytest.raises(SigDeadlineExceededError, match="rate admission"):
                await watchdog(client.get_exchange_orderbook("36"))
            sleeper["fn"] = asyncio.sleep  # the limiter works normally afterwards
            book = await watchdog(client.get_exchange_orderbook("36"))
            assert book.exchangeId == "36"
        finally:
            await client.aclose()

    run(scenario())


def test_hanging_retry_sleeper_is_ended_by_the_read_deadline() -> None:
    router = Router({BOOK: [json_response(503, error_body("SERVICE_UNAVAILABLE")), OK]})
    client = client_with(router, budget=0.6)
    client._sleep = hang

    async def scenario() -> SigDeadlineExceededError:
        try:
            await client.verify_tournament()
            with pytest.raises(SigDeadlineExceededError) as info:
                await watchdog(client.get_exchange_orderbook("36"))
            return info.value
        finally:
            await client.aclose()

    error = run(scenario())
    assert error.phase == "retry wait" and error.attempts == 1
    assert isinstance(error.last_error, SigServiceUnavailableError)
    assert router.count("/exchanges/36/orderbook") == 1


def test_external_cancellation_during_admission_propagates_and_consumes_nothing() -> None:
    limiter = tight_limiter(hang)
    client = client_with(Router(), budget=30.0, limiter=limiter)

    async def scenario() -> None:
        try:
            await client.verify_tournament()
            task = asyncio.create_task(client.get_exchange_orderbook("36"))
            await asyncio.sleep(0.05)  # let it reach the hanging admission wait
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await watchdog(task)
        finally:
            await client.aclose()

    run(scenario())
    assert len(limiter._budgets[READ]._admitted) == 1


def test_external_cancellation_during_retry_sleep_propagates() -> None:
    router = Router({BOOK: [json_response(503, error_body("TX_CONFLICT")), OK]})
    client = client_with(router, budget=30.0)
    client._sleep = hang

    async def scenario() -> None:
        try:
            await client.verify_tournament()
            task = asyncio.create_task(client.get_exchange_orderbook("36"))
            await asyncio.sleep(0.05)
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await watchdog(task)
        finally:
            await client.aclose()

    run(scenario())


def test_timeout_error_from_the_sleeper_itself_is_not_relabelled() -> None:
    async def broken_sleep(seconds: float) -> None:
        raise TimeoutError("raised by the sleeper, not by the read deadline")

    router = Router({BOOK: [json_response(503, error_body("TX_CONFLICT")), OK]})
    client = client_with(router, budget=30.0)
    client._sleep = broken_sleep

    async def scenario() -> None:
        try:
            with pytest.raises(TimeoutError, match="raised by the sleeper"):
                await watchdog(client.get_exchange_orderbook("36"))
        finally:
            await client.aclose()

    run(scenario())
