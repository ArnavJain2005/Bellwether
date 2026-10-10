"""Realtime token minting: POST /realtime/token (S1-T3-A).

[documented] One authenticated call returns the per-user access token, its `expiresAt`,
the Realtime connection (`supabaseUrl` + `anonKey`) and the caller's user channel.
Tokens last 3 hours; refreshing means minting again and calling setAuth with the new
token. The endpoint takes no parameters, so there is no `tournamentId` to pass: channel
authorization comes from the token's claims and the topic name.

Rate budget: POST /realtime/token "is one write". Every attempt takes one WRITE slot
from the account's shared RateLimiter, the same instance the REST client uses.

Retries: none here. `mint()` makes exactly one attempt and raises on any failure; the
caller (the realtime feed) decides when to try again. A 429 still imposes its
Retry-After on the shared WRITE budget, exactly as the read client does for reads.

Secrets: the API key, the minted token and the anon key never appear in a repr, log line
or exception. The user channel names the profile id and is also kept out of reprs.
Errors carry no text from the response body (see errors.py).
"""

from __future__ import annotations

import asyncio
import re
import time
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any, Self

import httpx
from pydantic import ValidationError

from engine.config import SigConfig
from engine.sig_client.errors import (
    SigClientClosedError,
    SigDeadlineExceededError,
    SigRateBudgetError,
    SigRateLimitedError,
    SigResponseError,
    SigTransportError,
)
from engine.sig_client.rate_limiter import (
    Clock,
    RateLimiter,
    RequestKind,
    check_positive_seconds,
)
from engine.sig_client.realtime_wire import parse_instant

# Shared error and Retry-After handling, so a write is classified exactly like a read.
from engine.sig_client.rest import SigRestClient, _parse_retry_after, _validation_summary
from engine.sig_client.wire import WireModel

TOKEN_PATH = "/realtime/token"
DOCUMENTED_TOKEN_LIFETIME_S = 3 * 3600.0
DEFAULT_RATE_LIMITED_WAIT_S = 60.0  # the documented `Retry-After: 60`

_VISIBLE_ASCII = re.compile(r"[\x21-\x7e]+")
_USER_CHANNEL = re.compile(r"user:[0-9A-Za-z-]+")


class RealtimeSecret:
    """A realtime credential (token or anon key). Never shown in repr, str or logs."""

    __slots__ = ("_value",)

    def __init__(self, value: str) -> None:
        if not isinstance(value, str) or not _VISIBLE_ASCII.fullmatch(value):
            raise ValueError("realtime credential must be non-empty visible ASCII")
        self._value = value

    def reveal(self) -> str:
        """Only the transport adapter calls this, where the value goes on the wire."""
        return self._value

    def __eq__(self, other: object) -> bool:
        return isinstance(other, RealtimeSecret) and other._value == self._value

    def __hash__(self) -> int:
        return hash(self._value)

    def __repr__(self) -> str:
        return "RealtimeSecret(<redacted>)"

    __str__ = __repr__


@dataclass(frozen=True)
class RealtimeEndpoint:
    """Where to connect: `createClient(supabaseUrl, anonKey)`."""

    supabase_url: str
    anon_key: RealtimeSecret = field(repr=False)


@dataclass(frozen=True)
class RealtimeCredentials:
    """One minted token. `expires_at` is SIG's string; `expires_epoch` its exact instant."""

    endpoint: RealtimeEndpoint
    token: RealtimeSecret = field(repr=False)
    expires_at: str
    expires_epoch: Decimal
    user_channel: str = field(repr=False)


class _Channels(WireModel):
    user: str


class _TokenResponse(WireModel):
    # #/paths/~1realtime~1token/post/responses/200/content/application~1json/schema
    token: str
    expiresAt: str
    supabaseUrl: str
    anonKey: str
    channels: _Channels


def _check_supabase_url(url: str) -> bool:
    # [client policy] the transport connects here with the token: https only, a host,
    # and no credentials, query or fragment.
    if not _VISIBLE_ASCII.fullmatch(url):
        return False
    try:
        parsed = httpx.URL(url)
    except httpx.InvalidURL:
        return False
    return (
        parsed.scheme == "https"
        and bool(parsed.host)
        and not parsed.userinfo
        and not parsed.query
        and not parsed.fragment
    )


class RealtimeTokenClient:
    """Mints realtime tokens for one SIG account.

    Pass the account's shared RateLimiter (the REST client's `limiter`). The minting
    request is bounded by `total_budget_s` from its start, covering rate admission and
    the request itself.
    """

    def __init__(
        self,
        config: SigConfig,
        http: httpx.AsyncClient,
        *,
        limiter: RateLimiter,
        owns_http: bool = False,
        clock: Clock = time.monotonic,
        total_budget_s: float = 30.0,
    ) -> None:
        if http.auth is not None:
            raise ValueError("the injected httpx client must not carry its own auth")
        if "authorization" in http.headers or "x-api-key" in http.headers:
            raise ValueError("the injected httpx client must not carry credential headers")
        if not isinstance(limiter, RateLimiter):
            raise TypeError("limiter must be the account's shared RateLimiter")
        self._config = config
        self._http = http
        self._owns_http = owns_http
        self._limiter = limiter
        self._clock = clock
        self._budget = check_positive_seconds("total_budget_s", total_budget_s)
        self._closed = False

    @classmethod
    def create(
        cls,
        config: SigConfig,
        *,
        limiter: RateLimiter,
        transport: httpx.AsyncBaseTransport | None = None,
        **kwargs: Any,
    ) -> RealtimeTokenClient:
        """Build a client that owns its httpx client and closes it in `aclose()`."""
        http = httpx.AsyncClient(
            transport=transport, timeout=config.timeout, follow_redirects=False, trust_env=False
        )
        return cls(config, http, limiter=limiter, owns_http=True, **kwargs)

    def __repr__(self) -> str:
        return f"RealtimeTokenClient(base_url={self._config.base_url!r}, closed={self._closed})"

    async def __aenter__(self) -> Self:
        return self

    async def __aexit__(self, *exc_info: object) -> None:
        await self.aclose()

    async def aclose(self) -> None:
        if self._closed:
            return
        self._closed = True
        if self._owns_http:
            await self._http.aclose()

    @property
    def limiter(self) -> RateLimiter:
        return self._limiter

    async def mint(self) -> RealtimeCredentials:
        """POST /realtime/token once. Raises a SigClientError on any failure."""
        method, path = "POST", TOKEN_PATH
        if self._closed:
            raise SigClientClosedError("the realtime token client is closed")
        deadline = self._clock() + self._budget
        attempts = 0

        def expired(phase: str) -> SigDeadlineExceededError:
            return SigDeadlineExceededError(
                method=method, path=path, phase=phase, attempts=attempts
            )

        scope = asyncio.timeout(self._budget)
        try:
            async with scope:
                try:
                    await self._limiter.acquire(RequestKind.WRITE, deadline=deadline)
                except SigRateBudgetError:
                    raise expired("rate admission") from None
                attempts = 1
                response = await self._send(method, path)
        except TimeoutError:
            if scope.expired():
                raise expired("request" if attempts else "rate admission") from None
            raise
        except httpx.TransportError as exc:
            raise SigTransportError(method=method, path=path, kind=type(exc).__name__) from None
        if self._clock() >= deadline:
            raise expired("request")  # a late response is not used
        if 200 <= response.status_code < 300:
            return self._credentials(method, path, SigRestClient._decode(method, path, response))
        retry_after = _parse_retry_after(response.headers.get("Retry-After"))
        error = SigRestClient._api_error(method, path, response, retry_after)
        if isinstance(error, SigRateLimitedError):
            if retry_after.oversized:
                self._limiter.block(RequestKind.WRITE)
            else:
                wait = (
                    retry_after.seconds
                    if retry_after.seconds is not None
                    else DEFAULT_RATE_LIMITED_WAIT_S
                )
                self._limiter.impose_cooldown(RequestKind.WRITE, wait)
        raise error

    async def _send(self, method: str, path: str) -> httpx.Response:
        request = self._http.build_request(
            method,
            self._config.base_url + path,
            headers={
                "Authorization": f"Bearer {self._config.api_key.reveal()}",
                "Accept": "application/json",
            },
            timeout=self._config.timeout,
        )
        return await self._http.send(request, follow_redirects=False)

    @staticmethod
    def _credentials(method: str, path: str, data: object) -> RealtimeCredentials:
        def bad(reason: str) -> SigResponseError:
            return SigResponseError(method=method, path=path, reason=reason)

        try:
            body = _TokenResponse.model_validate(data)
        except ValidationError as exc:
            raise bad(_validation_summary(exc)) from None
        # Field names only in every reason; never the values.
        if not _VISIBLE_ASCII.fullmatch(body.token):
            raise bad("token is not visible ASCII")
        if not _VISIBLE_ASCII.fullmatch(body.anonKey):
            raise bad("anonKey is not visible ASCII")
        if not _check_supabase_url(body.supabaseUrl):
            raise bad("supabaseUrl is not a plain https URL")
        if not _USER_CHANNEL.fullmatch(body.channels.user):
            raise bad("channels.user is not a user:{profile_id} channel")
        try:
            expires_epoch = parse_instant(body.expiresAt)
        except ValueError:
            raise bad("expiresAt is not an ISO-8601 timestamp with an offset") from None
        return RealtimeCredentials(
            endpoint=RealtimeEndpoint(body.supabaseUrl, RealtimeSecret(body.anonKey)),
            token=RealtimeSecret(body.token),
            expires_at=body.expiresAt,
            expires_epoch=expires_epoch,
            user_channel=body.channels.user,
        )
