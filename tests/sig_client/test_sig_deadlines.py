"""F1 area 1: deadlines in the limiter and across a whole logical read."""

import asyncio
import time
from collections.abc import AsyncIterator

import httpx
import pytest
from sig_testkit import (
    API,
    BASE_URL,
    SENTINEL_KEY,
    TOURNAMENT,
    FakeClock,
    Router,
    error_body,
    fixture,
    json_response,
    make_client,
    run,
)

from engine.config import ApiKey, SigConfig
from engine.sig_client.errors import (
    SigDeadlineExceededError,
    SigRateBudgetError,
    SigServiceUnavailableError,
)
from engine.sig_client.rate_limiter import SLEEP_PAD, RateLimiter, RequestKind
from engine.sig_client.rest import ReadRetryPolicy, SigRestClient

READ = RequestKind.READ
BOOK = f"{API}/exchanges/36/orderbook"
TOURNAMENT_PATH = f"{API}/tournaments/alpha-tournament"
OK = json_response(200, fixture("exchange_orderbook"))


# --- limiter ------------------------------------------------------------------------


@pytest.mark.parametrize("offset", [0.0, -1.0])
def test_expired_deadline_is_rejected_even_with_free_slots(offset: float) -> None:
    clock = FakeClock()
    lim = RateLimiter(clock=clock, sleep=clock.sleep)
    with pytest.raises(SigRateBudgetError, match="passed"):
        run(lim.acquire(READ, deadline=clock() + offset))
    assert lim.used(READ) == 0 and clock.sleeps == []


def test_oversleep_past_the_deadline_is_caught_after_waking() -> None:
    clock = FakeClock()

    async def oversleep(seconds: float) -> None:
        clock.sleeps.append(seconds)
        clock.now += seconds * 10  # the scheduler woke us far too late
        await asyncio.sleep(0)

    lim = RateLimiter(read_limit=1, clock=clock, sleep=oversleep)

    async def scenario() -> None:
        await lim.acquire(READ)
        with pytest.raises(SigRateBudgetError, match="passed"):
            await lim.acquire(READ, deadline=clock() + 61)

    run(scenario())
    assert len(clock.sleeps) == 1
    assert lim.used(READ) == 0  # the late waker was not admitted


def test_sleep_padding_counts_against_the_deadline() -> None:
    clock = FakeClock()
    lim = RateLimiter(read_limit=1, clock=clock, sleep=clock.sleep)

    async def scenario() -> None:
        await lim.acquire(READ)
        start = clock()
        # The slot frees exactly at the deadline: padding pushes admission past it.
        with pytest.raises(SigRateBudgetError, match="beyond"):
            await lim.acquire(READ, deadline=start + 60)
        assert clock.sleeps == []
        # A deadline just beyond wait + padding is met.
        await lim.acquire(READ, deadline=start + 60 + 2 * SLEEP_PAD)
        assert clock() < start + 60 + 2 * SLEEP_PAD

    run(scenario())


# --- whole logical read -------------------------------------------------------------


def real_time_client(router: Router, budget: float) -> SigRestClient:
    config = SigConfig(api_key=ApiKey(SENTINEL_KEY), tournament=TOURNAMENT, base_url=BASE_URL)
    http = httpx.AsyncClient(transport=httpx.MockTransport(router))
    return SigRestClient(config, http, owns_http=True, retry=ReadRetryPolicy(total_budget_s=budget))


def test_hanging_transport_is_cut_off_at_the_read_deadline() -> None:
    async def hang(request: httpx.Request) -> httpx.Response:
        await asyncio.Event().wait()
        raise AssertionError("unreachable")

    client = real_time_client(Router({BOOK: hang}), budget=0.2)

    async def scenario() -> SigDeadlineExceededError:
        try:
            await client.verify_tournament()
            started = time.monotonic()
            with pytest.raises(SigDeadlineExceededError) as info:
                await client.get_exchange_orderbook("36")
            assert time.monotonic() - started < 2.0
            return info.value
        finally:
            await client.aclose()

    error = run(scenario())
    assert error.phase == "request" and error.attempts == 1
    assert error.__cause__ is None and error.__suppress_context__


class HangingBody(httpx.AsyncByteStream):
    async def __aiter__(self) -> AsyncIterator[bytes]:
        yield b'{"exchangeId": "36"'
        await asyncio.Event().wait()


def test_hanging_body_read_is_cut_off_at_the_read_deadline() -> None:
    def slow_body(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, stream=HangingBody())

    client = real_time_client(Router({BOOK: slow_body}), budget=0.2)

    async def scenario() -> None:
        try:
            with pytest.raises(SigDeadlineExceededError, match="during request"):
                await client.get_exchange_orderbook("36")
        finally:
            await client.aclose()

    run(scenario())


def test_response_completing_after_the_deadline_is_not_returned() -> None:
    clock = FakeClock()

    def late(request: httpx.Request) -> httpx.Response:
        clock.now += 31  # the whole 30 s budget passed while waiting
        return OK

    client, _, _ = make_client(Router({BOOK: late}), clock=clock)
    with pytest.raises(SigDeadlineExceededError) as info:
        run(client.get_exchange_orderbook("36"))
    assert info.value.phase == "request" and info.value.attempts == 1


def test_retry_wait_oversleep_is_caught() -> None:
    clock = FakeClock()
    client, router, _ = make_client(
        Router({BOOK: [json_response(503, error_body("SERVICE_UNAVAILABLE")), OK]}), clock=clock
    )

    async def oversleep(seconds: float) -> None:
        clock.now += 100

    client._sleep = oversleep
    with pytest.raises(SigDeadlineExceededError) as info:
        run(client.get_exchange_orderbook("36"))
    assert info.value.phase == "retry wait" and info.value.attempts == 1
    assert isinstance(info.value.last_error, SigServiceUnavailableError)
    assert router.count("/exchanges/36/orderbook") == 1


def test_attempts_and_deadline_together_never_exceed_the_budget() -> None:
    clock = FakeClock()
    client, _, _ = make_client(
        Router({BOOK: [json_response(503, error_body("TX_CONFLICT"))]}),
        clock=clock,
        retry=ReadRetryPolicy(max_attempts=100, total_budget_s=5.0, max_backoff_s=1.0),
    )

    async def scenario() -> None:
        await client.verify_tournament()
        start = clock()
        with pytest.raises(SigDeadlineExceededError) as info:
            await client.get_exchange_orderbook("36")
        assert info.value.phase == "retry wait"
        assert clock() - start < 5.0

    run(scenario())


def test_verification_has_its_own_budget() -> None:
    clock = FakeClock()

    def slow_tournament(request: httpx.Request) -> httpx.Response:
        clock.now += 25
        return json_response(200, fixture("tournament"))

    client, _, _ = make_client(Router({TOURNAMENT_PATH: slow_tournament}), clock=clock)
    assert run(client.get_exchange_orderbook("36")).exchangeId == "36"


def test_collections_have_per_page_budgets_not_a_total_deadline() -> None:
    clock = FakeClock()
    pages = {
        None: fixture("tournament_markets_page1"),
        "cursor-page-2": fixture("tournament_markets_page2"),
    }

    def slow_page(request: httpx.Request) -> httpx.Response:
        clock.now += 20
        return json_response(200, pages[request.url.params.get("cursor")])

    client, _, _ = make_client(Router({f"{TOURNAMENT_PATH}/markets": slow_page}), clock=clock)
    markets = run(client.list_all_tournament_markets(page_limit=1))
    assert [m.id for m in markets] == ["26", "27"]
    assert clock.now - 1000.0 >= 40  # longer than one 30 s budget, by design


def test_external_cancellation_is_not_turned_into_a_deadline_error() -> None:
    async def scenario() -> None:
        entered = asyncio.Event()

        async def hang(request: httpx.Request) -> httpx.Response:
            entered.set()
            await asyncio.Event().wait()
            raise AssertionError("unreachable")

        client = real_time_client(Router({BOOK: hang}), budget=30.0)
        try:
            await client.verify_tournament()
            task = asyncio.create_task(client.get_exchange_orderbook("36"))
            await entered.wait()
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
        finally:
            await client.aclose()

    run(scenario())
