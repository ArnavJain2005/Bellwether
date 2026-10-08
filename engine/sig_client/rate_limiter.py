"""Client-side rate budget for one SIG account.

SIG limits each account (all of its keys together) to 100 reads and 30 writes per
minute. Bellwether's budget is 80 reads and 25 writes (CLAUDE.md), leaving headroom.
`GET` and `HEAD` are reads; every other method is a write (OpenAPI "Rate limits").

Each budget is a token bucket (refilling `limit` tokens per 60 s, holding at most
`burst`) plus a rolling-window guard: no more than `limit` admissions in any half-open
60-second window [t, t + 60). The guard matters because a full bucket plus its refill
would otherwise admit up to twice the limit within one minute.

Admission is a synchronous check-and-record with no `await` inside it, so concurrent
tasks on one event loop cannot both take the last slot. A limiter belongs to one event
loop. Share one instance between every client of the same account.

Every HTTP attempt, including each retry, must call `acquire` once.
"""

from __future__ import annotations

import asyncio
import time
from collections import deque
from collections.abc import Awaitable, Callable
from enum import StrEnum

from engine.sig_client.errors import SigRateBudgetError

READ_LIMIT_PER_MINUTE = 80
WRITE_LIMIT_PER_MINUTE = 25
WINDOW_SECONDS = 60.0

# Added to every wait so that, after sleeping, float rounding cannot leave the clock a
# hair short of the instant a slot frees up.
_SLEEP_PAD = 1e-6

Clock = Callable[[], float]
Sleep = Callable[[float], Awaitable[None]]


class RequestKind(StrEnum):
    READ = "read"
    WRITE = "write"


def classify_method(method: str) -> RequestKind:
    return RequestKind.READ if method.upper() in ("GET", "HEAD") else RequestKind.WRITE


class _Budget:
    def __init__(self, kind: RequestKind, limit: int, burst: int, window: float) -> None:
        if limit < 1 or not 1 <= burst <= limit or window <= 0:
            raise ValueError("need limit >= 1, 1 <= burst <= limit and window > 0")
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
            self._tokens = min(self.burst, self._tokens + (now - self._refilled_at) * rate)
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
    """Read and write budgets for one account."""

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
        self._clock = clock
        self._sleep = sleep
        self._budgets = {
            RequestKind.READ: _Budget(
                RequestKind.READ, read_limit, read_burst or read_limit, window
            ),
            RequestKind.WRITE: _Budget(
                RequestKind.WRITE, write_limit, write_burst or write_limit, window
            ),
        }

    async def acquire(self, kind: RequestKind, *, deadline: float | None = None) -> None:
        """Wait for, then consume, one slot of `kind`.

        Raises SigRateBudgetError, without consuming anything, if the wait would end after
        `deadline` (a value of the injected clock).
        """
        budget = self._budgets[kind]
        while True:
            now = self._clock()
            wait = budget.wait_time(now)
            if wait <= 0.0:
                budget.admit(now)
                return
            if deadline is not None and now + wait > deadline:
                raise SigRateBudgetError(
                    f"{kind} budget needs {wait:.3f}s, beyond the caller's deadline"
                )
            await self._sleep(wait + _SLEEP_PAD)

    def impose_cooldown(self, kind: RequestKind, seconds: float) -> None:
        """Block every `kind` admission for `seconds` (after a 429). Never shortens one."""
        budget = self._budgets[kind]
        budget.cooldown_until = max(budget.cooldown_until, self._clock() + seconds)

    def used(self, kind: RequestKind) -> int:
        """Admissions of `kind` inside the current rolling window."""
        return self._budgets[kind].in_window(self._clock())
