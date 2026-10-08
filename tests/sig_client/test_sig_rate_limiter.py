import asyncio

import pytest
from sig_testkit import FakeClock, run

from engine.sig_client.errors import SigRateBudgetError
from engine.sig_client.rate_limiter import (
    READ_LIMIT_PER_MINUTE,
    WRITE_LIMIT_PER_MINUTE,
    RateLimiter,
    RequestKind,
    classify_method,
)

READ = RequestKind.READ
WRITE = RequestKind.WRITE


def limiter(clock: FakeClock, **kwargs: int) -> RateLimiter:
    return RateLimiter(clock=clock, sleep=clock.sleep, **kwargs)


async def admit_times(lim: RateLimiter, clock: FakeClock, kind: RequestKind, n: int) -> list[float]:
    times = []
    for _ in range(n):
        await lim.acquire(kind)
        times.append(clock())  # acquire returns with no await after admitting
    return times


def max_in_any_window(times: list[float], window: float = 60.0) -> int:
    # Every half-open window [t, t + window) that starts at an admission.
    return max(sum(1 for u in times if t <= u < t + window) for t in times)


def test_existing_budgets_are_unchanged() -> None:
    assert READ_LIMIT_PER_MINUTE == 80
    assert WRITE_LIMIT_PER_MINUTE == 25


@pytest.mark.parametrize(
    ("method", "kind"),
    [("GET", READ), ("head", READ), ("POST", WRITE), ("DELETE", WRITE), ("PATCH", WRITE)],
)
def test_method_classification(method: str, kind: RequestKind) -> None:
    assert classify_method(method) is kind


def test_full_burst_then_waits_for_the_window() -> None:
    clock = FakeClock()
    lim = limiter(clock)
    times = run(admit_times(lim, clock, READ, 81))
    assert times[:80] == [1000.0] * 80
    assert times[80] >= 1060.0
    assert lim.used(READ) == 1


def test_rolling_window_boundary_is_half_open() -> None:
    clock = FakeClock()
    lim = limiter(clock, read_limit=4)

    async def scenario() -> None:
        await admit_times(lim, clock, READ, 4)
        clock.now = 1059.999
        with pytest.raises(SigRateBudgetError):
            await lim.acquire(READ, deadline=clock())
        clock.now = 1060.0
        await lim.acquire(READ, deadline=clock())

    run(scenario())


def test_refilled_tokens_cannot_exceed_the_window_budget() -> None:
    # A bucket alone would hold 2 new tokens at t+30 on top of the burst of 4.
    clock = FakeClock()
    lim = limiter(clock, read_limit=4)

    async def scenario() -> list[float]:
        times = await admit_times(lim, clock, READ, 4)
        clock.now += 30
        times += await admit_times(lim, clock, READ, 8)
        return times

    times = run(scenario())
    assert max_in_any_window(times) == 4
    assert times[4] >= 1060.0


def test_smaller_burst_spaces_admissions_at_the_refill_rate() -> None:
    clock = FakeClock()
    lim = limiter(clock, read_limit=4, read_burst=2)
    times = run(admit_times(lim, clock, READ, 4))
    assert times[:2] == [1000.0, 1000.0]
    assert times[2] == pytest.approx(1015.0, abs=1e-3)  # 60 s / 4 per token
    assert times[3] == pytest.approx(1030.0, abs=1e-3)


def test_long_run_never_exceeds_budget_in_any_window() -> None:
    clock = FakeClock()
    lim = limiter(clock)
    times = run(admit_times(lim, clock, READ, 400))
    assert max_in_any_window(times) == READ_LIMIT_PER_MINUTE


def test_concurrent_admission_is_atomic() -> None:
    clock = FakeClock()
    lim = limiter(clock)

    async def one() -> bool:
        try:
            await lim.acquire(READ, deadline=clock())
        except SigRateBudgetError:
            return False
        return True

    async def scenario() -> list[bool]:
        return await asyncio.gather(*(one() for _ in range(120)))

    results = run(scenario())
    assert results.count(True) == READ_LIMIT_PER_MINUTE
    assert lim.used(READ) == READ_LIMIT_PER_MINUTE


def test_concurrent_waiters_respect_the_window() -> None:
    clock = FakeClock()
    lim = limiter(clock, read_limit=5)
    times: list[float] = []

    async def one() -> None:
        await lim.acquire(READ)
        times.append(clock())

    async def scenario() -> None:
        await asyncio.gather(*(one() for _ in range(17)))

    run(scenario())
    assert len(times) == 17
    assert max_in_any_window(sorted(times)) == 5


def test_reads_and_writes_have_separate_budgets() -> None:
    clock = FakeClock()
    lim = limiter(clock)

    async def scenario() -> None:
        await admit_times(lim, clock, READ, 80)
        await admit_times(lim, clock, WRITE, 25)
        with pytest.raises(SigRateBudgetError):
            await lim.acquire(WRITE, deadline=clock())
        with pytest.raises(SigRateBudgetError):
            await lim.acquire(READ, deadline=clock())

    run(scenario())
    assert clock.now == 1000.0


def test_cooldown_blocks_only_its_budget_and_never_shortens() -> None:
    clock = FakeClock()
    lim = limiter(clock)

    async def scenario() -> None:
        lim.impose_cooldown(READ, 30)
        lim.impose_cooldown(READ, 5)  # must not shorten the 30 s cooldown
        await lim.acquire(WRITE, deadline=clock())
        with pytest.raises(SigRateBudgetError):
            await lim.acquire(READ, deadline=clock() + 29)
        await lim.acquire(READ)

    run(scenario())
    assert clock.now == pytest.approx(1030.0, abs=1e-3)


def test_deadline_failure_consumes_nothing() -> None:
    clock = FakeClock()
    lim = limiter(clock, read_limit=1)

    async def scenario() -> None:
        await lim.acquire(READ)
        with pytest.raises(SigRateBudgetError):
            await lim.acquire(READ, deadline=clock() + 10)

    run(scenario())
    assert lim.used(READ) == 1
    assert clock.sleeps == []


def test_cancelled_waiter_propagates_and_consumes_nothing() -> None:
    clock = FakeClock()
    blocked = asyncio.Event

    async def stuck_sleep(seconds: float) -> None:
        await blocked().wait()

    lim = RateLimiter(read_limit=1, clock=clock, sleep=stuck_sleep)

    async def scenario() -> None:
        await lim.acquire(READ)
        task = asyncio.create_task(lim.acquire(READ))
        await asyncio.sleep(0)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert lim.used(READ) == 1

    run(scenario())


def test_invalid_budget_configuration_is_rejected() -> None:
    clock = FakeClock()
    with pytest.raises(ValueError):
        limiter(clock, read_limit=0)
    with pytest.raises(ValueError):
        limiter(clock, read_limit=4, read_burst=5)
