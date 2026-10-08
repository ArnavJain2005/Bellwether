"""F2 area 1: a server's Retry-After is never shortened."""

import math
from decimal import Decimal

import httpx
import pytest
from sig_testkit import (
    API,
    FakeClock,
    Router,
    error_body,
    fixture,
    json_response,
    make_client,
    run,
)

from engine.sig_client.errors import (
    SigDeadlineExceededError,
    SigRateBudgetError,
    SigRateLimitedError,
    SigServiceUnavailableError,
)
from engine.sig_client.rate_limiter import MAX_COOLDOWN_SECONDS, RateLimiter, RequestKind
from engine.sig_client.rest import ReadRetryPolicy, _parse_retry_after

READ = RequestKind.READ
BOOK = f"{API}/exchanges/36/orderbook"
OK = json_response(200, fixture("exchange_orderbook"))


def limited(value: str) -> httpx.Response:
    return json_response(429, error_body("RATE_LIMITED"), {"Retry-After": value})


def unavailable(value: str) -> httpx.Response:
    return json_response(503, error_body("SERVICE_UNAVAILABLE"), {"Retry-After": value})


# --- parsing -----------------------------------------------------------------------


@pytest.mark.parametrize(
    "value", ["7200", "3601", "0.1", "1.0000000000000000001", "999999999", "1000000000"]
)
def test_parsed_wait_is_never_below_the_header(value: str) -> None:
    parsed = _parse_retry_after(value)
    assert not parsed.malformed and not parsed.oversized
    assert parsed.seconds is not None and math.isfinite(parsed.seconds)
    assert Decimal(parsed.seconds) >= Decimal(value)


def test_7200_is_not_reduced_to_any_cap() -> None:
    assert _parse_retry_after("7200").seconds == 7200.0


@pytest.mark.parametrize(
    "value", ["1000000000.000001", "1000000001", "9" * 33, "9" * 400, "0" * 50 + "9" * 11]
)
def test_valid_numbers_above_the_ceiling_are_oversized_not_malformed(value: str) -> None:
    parsed = _parse_retry_after(value)
    assert parsed.oversized and not parsed.malformed and parsed.seconds is None


def test_leading_zeros_do_not_make_a_small_value_oversized() -> None:
    assert _parse_retry_after("0" * 60 + "5").seconds == 5.0


@pytest.mark.parametrize("value", ["", "abc", "1e3", "-5", "inf", "Wed, 21 Oct 2026 07:28:00 GMT"])
def test_malformed_headers_are_distinguished(value: str) -> None:
    parsed = _parse_retry_after(value)
    assert parsed.malformed and not parsed.oversized and parsed.seconds is None


def test_missing_header_is_neither() -> None:
    parsed = _parse_retry_after(None)
    assert not parsed.malformed and not parsed.oversized and parsed.seconds is None


# --- 429 cooldown is the full server wait -------------------------------------------


def test_retry_after_7200_does_not_permit_admission_after_3600() -> None:
    client, router, clock = make_client(Router({BOOK: [limited("7200"), OK]}))

    async def scenario() -> None:
        start = clock()
        with pytest.raises(SigDeadlineExceededError) as info:
            await client.get_exchange_orderbook("36")
        # The wait does not fit the 30 s budget: the call fails promptly, without sleeping.
        assert info.value.phase == "retry wait" and clock.sleeps == []
        last = info.value.last_error
        assert isinstance(last, SigRateLimitedError) and last.retry_after == 7200.0
        for elapsed in (3600.0, 7169.0):  # the rest of the wait exceeds the 30 s budget
            clock.now = start + elapsed
            with pytest.raises(SigDeadlineExceededError, match="rate admission"):
                await client.get_exchange_orderbook("36")
        # Within reach of the end, the read waits out the remainder, never less.
        clock.now = start + 7190.0
        assert (await client.get_exchange_orderbook("36")).exchangeId == "36"
        assert clock() >= start + 7200.0

    run(scenario())
    assert router.count("/exchanges/36/orderbook") == 2  # nothing sent while cooling down


def test_cooldown_outlives_the_call_and_binds_every_sharing_client() -> None:
    clock = FakeClock()
    shared = RateLimiter(clock=clock, sleep=clock.sleep)
    first, _, _ = make_client(Router({BOOK: [limited("7200")]}), clock=clock, limiter=shared)
    second, second_router, _ = make_client(clock=clock, limiter=shared)
    third, third_router, _ = make_client(clock=clock, limiter=shared)

    async def scenario() -> None:
        start = clock()
        with pytest.raises(SigDeadlineExceededError):
            await first.get_exchange_orderbook("36")
        await first.aclose()  # the requesting client is gone; the cooldown is not
        clock.now = start + 3600.0
        for client in (second, third):
            with pytest.raises(SigDeadlineExceededError, match="rate admission"):
                await client.get_exchange_orderbook("36")
        with pytest.raises(SigRateBudgetError):
            await shared.acquire(READ, deadline=clock() + 1.0)
        clock.now = start + 7200.0
        assert (await second.get_exchange_orderbook("36")).exchangeId == "36"

    run(scenario())
    assert third_router.requests == []
    assert second_router.count("/tournaments/alpha-tournament") == 1


def test_wait_without_a_deadline_lasts_the_full_server_delay() -> None:
    clock = FakeClock()
    lim = RateLimiter(clock=clock, sleep=clock.sleep)
    lim.impose_cooldown(READ, 7200.0)
    start = clock()
    run(lim.acquire(READ))
    assert clock() >= start + 7200.0
    assert all(math.isfinite(s) for s in clock.sleeps)


def test_fitting_429_wait_is_slept_in_full() -> None:
    retry = ReadRetryPolicy(total_budget_s=120.0)
    client, _, clock = make_client(Router({BOOK: [limited("90"), OK]}), retry=retry)
    run(client.get_exchange_orderbook("36"))
    assert clock.sleeps[0] >= 90.0


# --- oversized numbers block automatic resumption ----------------------------------


@pytest.mark.parametrize("value", ["9" * 33, "9" * 400, "1000000001"])
def test_oversized_429_blocks_reads_without_infinity_or_early_resume(value: str) -> None:
    clock = FakeClock()
    shared = RateLimiter(clock=clock, sleep=clock.sleep)
    client, router, _ = make_client(
        Router({BOOK: [limited(value), OK]}), clock=clock, limiter=shared
    )
    other, other_router, _ = make_client(clock=clock, limiter=shared)

    async def scenario() -> None:
        with pytest.raises(SigRateLimitedError) as info:
            await client.get_exchange_orderbook("36")
        assert info.value.retry_after is None and info.value.retry_after_oversized is True
        assert shared.is_blocked(READ)
        assert shared._budgets[READ].cooldown_until == float("-inf")  # no infinite deadline
        for jump in (60.0, 61.0, 3600.0, 1e12):  # neither the 60 s fallback nor anything later
            clock.now = 1000.0 + jump
            with pytest.raises(SigDeadlineExceededError, match="rate admission"):
                await client.get_exchange_orderbook("36")
            with pytest.raises(SigDeadlineExceededError, match="rate admission"):
                await other.get_exchange_orderbook("36")
        with pytest.raises(SigRateBudgetError, match="blocked"):
            await shared.acquire(READ)  # even with no deadline, it refuses at once
        shared.clear_block(READ)  # an explicit decision resumes reads
        assert (await client.get_exchange_orderbook("36")).exchangeId == "36"

    run(scenario())
    assert clock.sleeps == [] and all(math.isfinite(s) for s in clock.sleeps)
    assert router.count("/exchanges/36/orderbook") == 2
    assert other_router.requests == []


def test_malformed_429_still_uses_the_documented_sixty_seconds() -> None:
    client, _, clock = make_client(Router({BOOK: [limited("soon"), OK]}))

    async def scenario() -> None:
        start = clock()
        with pytest.raises(SigRateLimitedError):
            await client.get_exchange_orderbook("36")
        assert not client._limiter.is_blocked(READ)
        clock.now = start + 59.0
        with pytest.raises(SigRateBudgetError):
            await client._limiter.acquire(READ, deadline=clock() + 0.5)
        assert (await client.get_exchange_orderbook("36")).exchangeId == "36"
        assert clock() >= start + 60.0  # admitted only once the 60 s had passed

    run(scenario())


# --- 503 waits are never shortened -------------------------------------------------


def test_503_retry_after_longer_than_the_budget_is_not_shortened() -> None:
    client, router, clock = make_client(Router({BOOK: [unavailable("7200"), OK]}))
    with pytest.raises(SigDeadlineExceededError) as info:
        run(client.get_exchange_orderbook("36"))
    assert info.value.phase == "retry wait"
    assert info.value.last_error is not None and info.value.last_error.retry_after == 7200.0
    assert clock.sleeps == [] and router.count("/exchanges/36/orderbook") == 1


@pytest.mark.parametrize("value", ["2.5", "0.1", "9.000000000000000001"])
def test_503_sleep_is_at_least_the_server_delay(value: str) -> None:
    client, _, clock = make_client(
        Router({BOOK: [unavailable(value), OK]}), jitter=lambda delay: 0.0
    )
    run(client.get_exchange_orderbook("36"))
    assert Decimal(clock.sleeps[0]) >= Decimal(value)


def test_oversized_503_is_not_retried_and_does_not_block() -> None:
    client, router, clock = make_client(Router({BOOK: [unavailable("9" * 40), OK]}))
    with pytest.raises(SigServiceUnavailableError) as info:
        run(client.get_exchange_orderbook("36"))
    assert info.value.retry_after_oversized is True
    assert not client._limiter.is_blocked(READ)
    assert clock.sleeps == [] and router.count("/exchanges/36/orderbook") == 1


# --- limiter API ------------------------------------------------------------------


@pytest.mark.parametrize("seconds", [MAX_COOLDOWN_SECONDS * 1.5, math.inf])
def test_limiter_refuses_cooldowns_it_cannot_hold(seconds: float) -> None:
    with pytest.raises(ValueError):
        RateLimiter().impose_cooldown(READ, seconds)


def test_maximum_cooldown_is_finite_and_accepted() -> None:
    clock = FakeClock()
    lim = RateLimiter(clock=clock, sleep=clock.sleep)
    lim.impose_cooldown(READ, MAX_COOLDOWN_SECONDS)
    assert math.isfinite(lim._budgets[READ].cooldown_until)
