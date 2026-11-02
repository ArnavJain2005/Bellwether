"""SIG write client for the bot: POST /orders, DELETE /orders/{id}, POST /orders/cancel-all.

- Its own httpx.AsyncClient (trust_env=False, follow_redirects=False, SigConfig
  timeouts), so a redirect is never followed and the key never goes to another host.
- The key is put into the Authorization header per request and nowhere else: never
  logged, never in a repr, an exception or a stored body (a body that echoes it is
  stored redacted).
- Every HTTP attempt, retries included, acquires one WRITE slot on the shared limiter.
- post_order sends the exact bytes it is given and makes one attempt. Retry policy for
  placements lives in bot/orders.py, the only caller allowed (bypass test).
- Cancels can only remove orders. cancel_order repeats on 503 until 200 or 409;
  cancel_all always sends the configured tournamentId and repeats on 503 until 200.
  Both stop after 60 s.
"""

from __future__ import annotations

import asyncio
import json
import logging
import math
import re
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation

import httpx

from engine.config import SigConfig
from engine.sig_client.errors import SigRateBudgetError, known_code
from engine.sig_client.rate_limiter import MAX_COOLDOWN_SECONDS, RateLimiter, RequestKind

logger = logging.getLogger(__name__)

CANCEL_DEADLINE_S = 60.0
DEFAULT_RATE_LIMIT_WAIT_S = 60.0  # documented RATE_LIMITED Retry-After
_RETRY_AFTER = re.compile(r"[0-9]+(\.[0-9]+)?")
_ORDER_ID = re.compile(r"[0-9]+")
REDACTED = "<redacted>"

Clock = Callable[[], float]
Sleep = Callable[[float], Awaitable[None]]


class WriteNoResponse(Exception):
    """The request got no HTTP response (connect, timeout, protocol). Outcome unknown.

    Only the httpx exception's class name is kept, never its text.
    """

    def __init__(self, *, method: str, path: str, kind: str) -> None:
        self.method = method
        self.path = path
        self.kind = kind
        super().__init__(f"{method} {path} -> no response ({kind})")


class CancelIncomplete(Exception):
    """A cancel did not reach a confirming answer within its deadline."""

    def __init__(self, *, path: str, last: str) -> None:
        self.path = path
        self.last = last
        super().__init__(f"{path} not confirmed within {CANCEL_DEADLINE_S:.0f} s; last: {last}")


@dataclass(frozen=True)
class WriteResponse:
    """One HTTP answer. `body` is decoded with Decimal floats; `text` is redacted.

    `code` is SIG's error code only if it is a documented one. `retry_after` is the
    numeric Retry-After in seconds (rounded up), None if absent or unparseable;
    `retry_after_oversized` is True when it was too large to hold.
    """

    status: int
    code: str | None
    body: object | None
    text: str
    retry_after: float | None
    retry_after_oversized: bool = False


def _parse_retry_after(value: str | None) -> tuple[float | None, bool]:
    if value is None or not _RETRY_AFTER.fullmatch(value.strip()):
        return None, False
    seconds = Decimal(value.strip())
    if seconds > MAX_COOLDOWN_SECONDS:
        return None, True
    return float(math.ceil(seconds)), False


class SigWriter:
    """Write requests for one account and the configured tournament."""

    def __init__(
        self,
        config: SigConfig,
        limiter: RateLimiter,
        *,
        transport: httpx.AsyncBaseTransport | None = None,
        clock: Clock = time.monotonic,
        sleep: Sleep = asyncio.sleep,
    ) -> None:
        self._config = config
        self._limiter = limiter
        self._clock = clock
        self._sleep = sleep
        self._http = httpx.AsyncClient(
            transport=transport,
            timeout=config.timeout,
            follow_redirects=False,
            trust_env=False,
        )

    def __repr__(self) -> str:
        return (
            f"SigWriter(base_url={self._config.base_url!r}, "
            f"tournament={self._config.tournament.slug!r})"
        )

    async def aclose(self) -> None:
        await self._http.aclose()

    @property
    def tournament_id(self) -> str:
        return self._config.tournament.id

    def redact(self, text: str) -> str:
        """`text` with any occurrence of the key replaced."""
        return text.replace(self._config.api_key.reveal(), REDACTED)

    async def _request(
        self, method: str, path: str, content: bytes | None, *, deadline: float | None = None
    ) -> WriteResponse:
        await self._limiter.acquire(RequestKind.WRITE, deadline=deadline)
        headers = {
            "Authorization": f"Bearer {self._config.api_key.reveal()}",
            "Accept": "application/json",
        }
        if content is not None:
            headers["Content-Type"] = "application/json"
        request = self._http.build_request(
            method, self._config.base_url + path, content=content, headers=headers
        )
        try:
            response = await self._http.send(request, follow_redirects=False)
        except httpx.TransportError as exc:
            logger.warning("%s %s -> no response (%s)", method, path, type(exc).__name__)
            raise WriteNoResponse(method=method, path=path, kind=type(exc).__name__) from None
        text = self.redact(response.content.decode("utf-8", errors="replace"))
        try:
            body: object | None = json.loads(text, parse_float=Decimal)
        except (ValueError, InvalidOperation):
            body = None
        code = None
        if isinstance(body, dict) and isinstance(body.get("error"), dict):
            code = known_code(body["error"].get("code"))
        retry_after, oversized = _parse_retry_after(response.headers.get("Retry-After"))
        logger.info("%s %s -> HTTP %s %s", method, path, response.status_code, code or "-")
        return WriteResponse(
            status=response.status_code,
            code=code,
            body=body,
            text=text,
            retry_after=retry_after,
            retry_after_oversized=oversized,
        )

    async def post_order(self, payload: bytes) -> WriteResponse:
        """One POST /orders attempt with exactly these bytes. Only bot/orders.py calls it."""
        if not isinstance(payload, bytes):
            raise TypeError("payload must be the exact request bytes")
        return await self._request("POST", "/orders", payload)

    async def cancel_order(self, order_id: str) -> WriteResponse:
        """DELETE /orders/{id}, repeated on 503 until 200 or 409, for at most 60 s."""
        if not isinstance(order_id, str) or not _ORDER_ID.fullmatch(order_id):
            raise ValueError("order id must be a numeric string")
        return await self._cancel_loop("DELETE", f"/orders/{order_id}", None, {200, 409})

    async def cancel_all(self) -> WriteResponse:
        """POST /orders/cancel-all for the configured tournament, repeated until 200."""
        body = json.dumps({"tournamentId": self.tournament_id}).encode()
        return await self._cancel_loop("POST", "/orders/cancel-all", body, {200})

    async def _cancel_loop(
        self, method: str, path: str, body: bytes | None, done: set[int]
    ) -> WriteResponse:
        deadline = self._clock() + CANCEL_DEADLINE_S
        attempt = 0
        last = "no attempt"
        while True:
            attempt += 1
            try:
                response = await self._request(method, path, body, deadline=deadline)
            except SigRateBudgetError:
                raise CancelIncomplete(path=path, last=last) from None
            except WriteNoResponse as exc:
                response = None
                last = f"no response ({exc.kind})"
            if response is not None:
                if response.status in done:
                    return response
                last = f"HTTP {response.status} {response.code or '-'}"
                if response.status == 429:
                    self._limiter.impose_cooldown(
                        RequestKind.WRITE, response.retry_after or DEFAULT_RATE_LIMIT_WAIT_S
                    )
                elif response.status != 503:
                    return response  # not retryable: the caller reads and records it
            wait = min(2.0 ** (attempt - 1), 8.0)
            if response is not None and response.retry_after is not None:
                wait = max(wait, response.retry_after)
            if self._clock() + wait >= deadline:
                raise CancelIncomplete(path=path, last=last)
            await self._sleep(wait)
