"""Client-side rate budget for one SIG account.

SIG limits each account (all of its keys together) to 100 reads and 30 writes per
minute. Bellwether's budget is 80 reads and 25 writes (CLAUDE.md), leaving headroom.
`GET` and `HEAD` are reads; every other method is a write (OpenAPI "Rate limits").

Each budget is a token bucket (refilling `limit` tokens per window, holding at most
`burst`) plus a rolling-window guard: no more than `limit` admissions in any half-open
window [t, t + window). The guard matters because a full bucket plus its refill would
otherwise admit up to twice the limit within one window.

Admission is a synchronous check-and-record with no `await` inside it, so concurrent
tasks on one event loop cannot both take the last slot. A limiter belongs to one event
loop.

Sharing is the caller's job. Every client of the same account must be given the same
RateLimiter instance. A client built without one creates a private limiter that knows
nothing about other clients, processes or keys, so it cannot protect the account-wide
budget on its own.

Every HTTP attempt, including each retry, must call `acquire` once.
"""

from __future__ import annotations

import asyncio
import math
import time
from collections import deque
from collections.abc import Awaitable, Callable
from enum import StrEnum

from engine.sig_client.errors import SigRateBudgetError

READ_LIMIT_PER_MINUTE = 80
WRITE_LIMIT_PER_MINUTE = 25
WINDOW_SECONDS = 60.0

# Added to every wait so that, after sleeping, float rounding cannot leave the clock a
# hair short of the instant a slot frees up. Deadline checks include it.
SLEEP_PAD = 1e-6

Clock = Callable[[], float]
Sleep = Callable[[float], Awaitable[None]]


class RequestKind(StrEnum):
    READ = "read"
    WRITE = "write"


def classify_method(method: str) -> RequestKind:
    return RequestKind.READ if method.upper() in ("GET", "HEAD") else RequestKind.WRITE


def check_positive_int(name: str, value: object) -> int:
    """A whole number >= 1. Rejects bools, strings, floats (even 3.0) and None."""
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        raise ValueError(f"{name} must be an integer of at least 1")
    return value


def check_positive_seconds(name: str, value: object) -> float:
    """A finite number of seconds above 0. Rejects bools, strings, NaN and infinity."""
    if (
        isinstance(value, bool)
        or not isinstance(value, (int, float))
        or not math.isfinite(value)
        or value <= 0
    ):
        raise ValueError(f"{name} must be a finite number of seconds above 0")
    return float(value)


class _Budget:
    def __init__(self, kind: RequestKind, limit: int, burst: int, window: float) -> None:
        self.kind = kind
        self.limit = limit
        self.burst = burst
        self.window = window
        self._tokens = float(burst)
        self._refilled_at: float | None = None
        self._admitted: deque[float] = deque()
        self.cooldown_until = float("-inf")

    def wait_time(self, now: float) -> float:
        """Seconds until one admission is allowed; 0 if allowed now. Updates state."""
        if self._refilled_at is not None:
            rate = self.limit / self.window
            elapsed = max(0.0, now - self._refilled_at)
            self._tokens = min(self.burst, self._tokens + elapsed * rate)
        self._refilled_at = now
        while self._admitted and self._admitted[0] + self.window <= now:
            self._admitted.popleft()
        wait = 0.0
        if now < self.cooldown_until:
            wait = max(wait, self.cooldown_until - now)
        if len(self._admitted) >= self.limit:
            wait = max(wait, self._admitted[0] + self.window - now)
        if self._tokens < 1.0:
            wait = max(wait, (1.0 - self._tokens) * self.window / self.limit)
        return wait

    def admit(self, now: float) -> None:
        self._tokens -= 1.0
        self._admitted.append(now)

    def in_window(self, now: float) -> int:
        return sum(1 for t in self._admitted if t + self.window > now)


class RateLimiter:
    """Read and write budgets for one account. Share one instance per account."""

    def __init__(
        self,
        *,
        read_limit: int = READ_LIMIT_PER_MINUTE,
        write_limit: int = WRITE_LIMIT_PER_MINUTE,
        read_burst: int | None = None,
        write_burst: int | None = None,
        window: float = WINDOW_SECONDS,
        clock: Clock = time.monotonic,
        sleep: Sleep = asyncio.sleep,
    ) -> None:
        window = check_positive_seconds("window", window)
        self._clock = clock
        self._sleep = sleep
        self._budgets = {
            RequestKind.READ: self._budget(RequestKind.READ, read_limit, read_burst, window),
            RequestKind.WRITE: self._budget(RequestKind.WRITE, write_limit, write_burst, window),
        }

    @staticmethod
    def _budget(kind: RequestKind, limit: object, burst: object, window: float) -> _Budget:
        limit = check_positive_int(f"{kind} limit", limit)
        # Only None means "default to the limit"; an explicit 0 is rejected, not replaced.
        burst = limit if burst is None else check_positive_int(f"{kind} burst", burst)
        if burst > limit:
            raise ValueError(f"{kind} burst must not exceed the {kind} limit")
        return _Budget(kind, limit, burst, window)

    async def acquire(self, kind: RequestKind, *, deadline: float | None = None) -> None:
        """Wait for, then consume, one slot of `kind`.

        With a `deadline` (a value of the injected clock), admission happens only while
        the clock reads strictly before it. The deadline is checked before every
        admission, even when a slot is free, and again after every wait, so a sleep that
        overruns is caught. If it has passed, or the next wait plus padding would reach
        it, SigRateBudgetError is raised and nothing is consumed.
        """
        if deadline is not None and (
            isinstance(deadline, bool)
            or not isinstance(deadline, (int, float))
            or not math.isfinite(deadline)
        ):
            raise ValueError("deadline must be a finite clock value or None")
        budget = self._budgets[kind]
        while True:
            now = self._clock()
            if deadline is not None and now >= deadline:
                raise SigRateBudgetError(f"{kind} admission deadline has passed")
            wait = budget.wait_time(now)
            if wait <= 0.0:
                budget.admit(now)
                return
            if deadline is not None and now + wait + SLEEP_PAD >= deadline:
                raise SigRateBudgetError(
                    f"{kind} budget needs {wait:.3f}s, beyond the caller's deadline"
                )
            await self._sleep(wait + SLEEP_PAD)

    def impose_cooldown(self, kind: RequestKind, seconds: float) -> None:
        """Block every `kind` admission for `seconds` (after a 429). Never shortens one."""
        if (
            isinstance(seconds, bool)
            or not isinstance(seconds, (int, float))
            or not math.isfinite(seconds)
            or seconds < 0
        ):
            raise ValueError("cooldown must be a finite number of seconds, at least 0")
        budget = self._budgets[kind]
        budget.cooldown_until = max(budget.cooldown_until, self._clock() + seconds)

    def used(self, kind: RequestKind) -> int:
        """Admissions of `kind` inside the current rolling window."""
        return self._budgets[kind].in_window(self._clock())
