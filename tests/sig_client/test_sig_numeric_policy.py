"""F1 area 3: numeric policy validation and Retry-After overflow."""

import math
from typing import Any

import httpx
import pytest
from sig_testkit import (
    API,
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

from engine.config import ApiKey, ConfigError, SigConfig
from engine.sig_client.errors import (
    SigDeadlineExceededError,
    SigRateLimitedError,
    SigRetryExhaustedError,
    SigServiceUnavailableError,
)
from engine.sig_client.rate_limiter import (
    READ_LIMIT_PER_MINUTE,
    WRITE_LIMIT_PER_MINUTE,
    RateLimiter,
    RequestKind,
)
from engine.sig_client.rest import ReadRetryPolicy

BOOK = f"{API}/exchanges/36/orderbook"
OK = json_response(200, fixture("exchange_orderbook"))
BAD_COUNTS: list[Any] = [0, -1, True, False, "80", 80.0, 2.5, None, math.inf]
BAD_SECONDS: list[Any] = [0, -1, -0.5, math.inf, -math.inf, math.nan, True, "60", None]


def test_default_budgets_are_unchanged() -> None:
    assert (READ_LIMIT_PER_MINUTE, WRITE_LIMIT_PER_MINUTE) == (80, 25)
    clock = FakeClock()
    lim = RateLimiter(clock=clock, sleep=clock.sleep)
    assert lim._budgets[RequestKind.READ].limit == 80
    assert lim._budgets[RequestKind.WRITE].limit == 25
    assert lim._budgets[RequestKind.READ].burst == 80


# --- RateLimiter --------------------------------------------------------------------


@pytest.mark.parametrize("name", ["read_limit", "write_limit"])
@pytest.mark.parametrize("value", BAD_COUNTS)
def test_limits_must_be_positive_integers(name: str, value: Any) -> None:
    with pytest.raises(ValueError):
        RateLimiter(**{name: value})


@pytest.mark.parametrize("name", ["read_burst", "write_burst"])
@pytest.mark.parametrize("value", [0, -1, True, 2.5, "1", 1.0, 81])
def test_bursts_must_be_positive_integers_within_the_limit(name: str, value: Any) -> None:
    with pytest.raises(ValueError):
        RateLimiter(**{name: value})


def test_explicit_zero_burst_is_rejected_not_defaulted() -> None:
    with pytest.raises(ValueError, match="burst"):
        RateLimiter(read_burst=0)
    with pytest.raises(ValueError, match="burst"):
        RateLimiter(write_burst=0)


@pytest.mark.parametrize("value", BAD_SECONDS)
def test_window_must_be_finite_and_positive(value: Any) -> None:
    with pytest.raises(ValueError):
        RateLimiter(window=value)


def test_valid_boundaries_are_accepted() -> None:
    lim = RateLimiter(read_limit=1, read_burst=1, write_limit=1, window=0.5)
    assert lim._budgets[RequestKind.READ].burst == 1


@pytest.mark.parametrize("value", [math.inf, math.nan, -1, True, "5"])
def test_cooldown_must_be_finite_and_not_negative(value: Any) -> None:
    lim = RateLimiter()
    with pytest.raises(ValueError):
        lim.impose_cooldown(RequestKind.READ, value)
    lim.impose_cooldown(RequestKind.READ, 0)  # zero is allowed


@pytest.mark.parametrize("value", [math.inf, -math.inf, math.nan, True, "1"])
def test_acquire_deadline_must_be_finite(value: Any) -> None:
    clock = FakeClock()
    lim = RateLimiter(clock=clock, sleep=clock.sleep)
    with pytest.raises(ValueError):
        run(lim.acquire(RequestKind.READ, deadline=value))
    assert lim.used(RequestKind.READ) == 0


# --- ReadRetryPolicy ----------------------------------------------------------------


@pytest.mark.parametrize("value", BAD_COUNTS)
def test_max_attempts_must_be_a_positive_integer(value: Any) -> None:
    with pytest.raises(ValueError):
        ReadRetryPolicy(max_attempts=value)


@pytest.mark.parametrize(
    "name",
    [
        "total_budget_s",
        "base_backoff_s",
        "max_backoff_s",
        "rate_limited_default_wait_s",
        "max_retry_after_s",
    ],
)
@pytest.mark.parametrize("value", BAD_SECONDS)
def test_policy_times_must_be_finite_and_positive(name: str, value: Any) -> None:
    with pytest.raises(ValueError):
        ReadRetryPolicy(**{name: value})


def test_policy_cross_field_rules() -> None:
    with pytest.raises(ValueError, match="base_backoff_s"):
        ReadRetryPolicy(base_backoff_s=5.0, max_backoff_s=4.0)
    with pytest.raises(ValueError, match="rate_limited_default_wait_s"):
        ReadRetryPolicy(rate_limited_default_wait_s=120.0, max_retry_after_s=60.0)


def test_backoff_cannot_overflow() -> None:
    policy = ReadRetryPolicy(max_attempts=10**6)
    assert policy.backoff(10**6) == policy.max_backoff_s


def test_single_attempt_policy_never_retries() -> None:
    body = json_response(503, error_body("SERVICE_UNAVAILABLE"))
    client, router, clock = make_client(
        Router({BOOK: [body, OK]}), retry=ReadRetryPolicy(max_attempts=1)
    )
    with pytest.raises(SigRetryExhaustedError) as info:
        run(client.get_exchange_orderbook("36"))
    assert info.value.attempts == 1 and clock.sleeps == []
    assert router.count("/exchanges/36/orderbook") == 1


@pytest.mark.parametrize("value", [math.inf, math.nan, -1.0, True])
def test_invalid_jitter_output_never_reaches_sleep(value: Any) -> None:
    body = json_response(503, error_body("SERVICE_UNAVAILABLE"))
    client, _, clock = make_client(Router({BOOK: [body, OK]}), jitter=lambda delay: value)
    with pytest.raises(ValueError, match="jitter"):
        run(client.get_exchange_orderbook("36"))
    assert clock.sleeps == []


# --- Retry-After overflow -----------------------------------------------------------


def test_huge_retry_after_on_429_is_capped_for_the_cooldown() -> None:
    huge = "9" * 32
    client, _, clock = make_client(
        Router({BOOK: [json_response(429, error_body("RATE_LIMITED"), {"Retry-After": huge}), OK]})
    )

    async def scenario() -> None:
        with pytest.raises(SigDeadlineExceededError) as info:
            await client.get_exchange_orderbook("36")
        last = info.value.last_error
        assert isinstance(last, SigRateLimitedError)
        assert last.retry_after == 3600.0 and math.isfinite(last.retry_after)
        cooldown = client._limiter._budgets[RequestKind.READ].cooldown_until
        assert math.isfinite(cooldown) and cooldown == pytest.approx(clock() + 3600.0)
        assert clock.sleeps == []
        clock.now += 3600.0  # once the capped cooldown has passed, reads resume
        assert (await client.get_exchange_orderbook("36")).exchangeId == "36"

    run(scenario())


def test_huge_retry_after_on_503_is_capped_and_not_slept() -> None:
    body = json_response(503, error_body("SERVICE_UNAVAILABLE"), {"Retry-After": "9" * 32})
    client, _, clock = make_client(Router({BOOK: [body, OK]}))
    with pytest.raises(SigDeadlineExceededError) as info:
        run(client.get_exchange_orderbook("36"))
    assert info.value.phase == "retry wait"
    assert info.value.last_error.retry_after == 3600.0
    assert clock.sleeps == []


@pytest.mark.parametrize("value", ["9" * 33, "1e999", "inf", "nan", "1.5e3", "-0", "5s"])
def test_out_of_format_retry_after_is_not_retried(value: str) -> None:
    body = json_response(503, error_body("SERVICE_UNAVAILABLE"), {"Retry-After": value})
    client, router, clock = make_client(Router({BOOK: [body, OK]}))
    with pytest.raises(SigServiceUnavailableError) as info:
        run(client.get_exchange_orderbook("36"))
    assert info.value.retry_after is None
    assert router.count("/exchanges/36/orderbook") == 1 and clock.sleeps == []


def test_retry_after_zero_is_valid() -> None:
    body = json_response(429, error_body("RATE_LIMITED"), {"Retry-After": "0"})
    client, router, _ = make_client(Router({BOOK: [body, OK]}))
    assert run(client.get_exchange_orderbook("36")).exchangeId == "36"
    assert router.count("/exchanges/36/orderbook") == 2


# --- config timeouts ----------------------------------------------------------------


@pytest.mark.parametrize(
    "timeout",
    [
        httpx.Timeout(None),
        httpx.Timeout(5.0, read=None),
        httpx.Timeout(math.inf),
        httpx.Timeout(math.nan),
        httpx.Timeout(0),
        httpx.Timeout(-1.0),
        5.0,
    ],
)
def test_config_timeouts_must_be_finite_and_positive(timeout: Any) -> None:
    with pytest.raises(ConfigError):
        SigConfig(api_key=ApiKey(SENTINEL_KEY), tournament=TOURNAMENT, timeout=timeout)
