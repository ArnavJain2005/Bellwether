"""RealtimeTokenClient: POST /realtime/token, offline (S1-T3-A).

Token responses are synthetic, shaped like the spec's 200 schema; values are sentinels.
"""

from __future__ import annotations

import asyncio
from typing import Any

import httpx
import pytest
from realtime_kit import ANON_VALUE, TOKEN_VALUE, USER_CHANNEL, token_body
from sig_testkit import (
    BASE_URL,
    SENTINEL_KEY,
    TOURNAMENT,
    FakeClock,
    Router,
    error_body,
    json_response,
    raw_response,
    run,
    secret_leaks,
)

from engine.config import ApiKey, SigConfig
from engine.sig_client.errors import (
    SigAuthError,
    SigClientClosedError,
    SigDeadlineExceededError,
    SigRateLimitedError,
    SigResponseError,
    SigServiceUnavailableError,
    SigTransportError,
    SigUnexpectedStatusError,
)
from engine.sig_client.rate_limiter import RateLimiter, RequestKind
from engine.sig_client.realtime_token import RealtimeSecret, RealtimeTokenClient

TOKEN = "/realtime/token"


def make(route: Any) -> tuple[RealtimeTokenClient, Router, FakeClock, RateLimiter]:
    router = Router({f"/api/v1{TOKEN}": route})
    clock = FakeClock()
    limiter = RateLimiter(clock=clock, sleep=clock.sleep)
    config = SigConfig(api_key=ApiKey(SENTINEL_KEY), tournament=TOURNAMENT, base_url=BASE_URL)
    client = RealtimeTokenClient(
        config,
        httpx.AsyncClient(transport=httpx.MockTransport(router)),
        owns_http=True,
        limiter=limiter,
        clock=clock,
    )
    return client, router, clock, limiter


def mint(route: Any) -> tuple[Any, Router, RateLimiter]:
    client, router, _, limiter = make(route)

    async def scenario() -> Any:
        async with client:
            return await client.mint()

    return run(scenario()), router, limiter


def mint_error(route: Any) -> tuple[BaseException, Router, RateLimiter]:
    client, router, _, limiter = make(route)

    async def scenario() -> BaseException:
        async with client:
            with pytest.raises(Exception) as caught:
                await client.mint()
            return caught.value

    return run(scenario()), router, limiter


def test_mint_is_one_post_and_one_write() -> None:
    credentials, router, limiter = mint(json_response(200, token_body()))
    (request,) = router.requests
    assert request.method == "POST"
    assert str(request.url) == f"{BASE_URL}{TOKEN}"  # no query: the endpoint takes none
    assert request.content == b""
    assert request.headers["authorization"] == f"Bearer {SENTINEL_KEY}"
    assert (limiter.used(RequestKind.WRITE), limiter.used(RequestKind.READ)) == (1, 0)
    assert credentials.token.reveal() == TOKEN_VALUE
    assert credentials.endpoint.anon_key.reveal() == ANON_VALUE
    assert credentials.endpoint.supabase_url == "https://realtime.invalid"
    assert credentials.user_channel == USER_CHANNEL
    assert credentials.expires_at == "2026-10-10T15:00:00.000Z"  # kept as sent


def test_credentials_never_show_secrets() -> None:
    credentials, _, _ = mint(json_response(200, token_body()))
    for text in (repr(credentials), str(credentials), repr(credentials.endpoint)):
        for secret in (TOKEN_VALUE, ANON_VALUE, USER_CHANNEL, SENTINEL_KEY):
            assert secret not in text
    assert str(RealtimeSecret("abc")) == "RealtimeSecret(<redacted>)"


@pytest.mark.parametrize(
    ("body", "reason"),
    [
        (lambda b: b.pop("token"), "token"),
        (lambda b: b.update(token="has space"), "token"),
        (lambda b: b.update(anonKey=""), "anonKey"),
        (lambda b: b.update(supabaseUrl="http://realtime.invalid"), "supabaseUrl"),
        (lambda b: b.update(supabaseUrl="https://u:p@realtime.invalid"), "supabaseUrl"),
        (lambda b: b.update(supabaseUrl="https://realtime.invalid/?k=v"), "supabaseUrl"),
        (lambda b: b.update(expiresAt="2026-10-10T15:00:00"), "expiresAt"),
        (lambda b: b.update(expiresAt=1760108400), "expiresAt"),
        (lambda b: b.update(channels={"user": "market:1"}), "channels.user"),
        (lambda b: b.update(channels={}), "channels"),
    ],
)
def test_invalid_token_response_is_rejected_without_echoing_values(body: Any, reason: str) -> None:
    payload = token_body()
    body(payload)
    error, _, _ = mint_error(json_response(200, payload))
    assert isinstance(error, SigResponseError)
    assert reason in str(error)
    for secret in (TOKEN_VALUE, ANON_VALUE, SENTINEL_KEY, "realtime.invalid"):
        assert secret_leaks(error, secret) == []


@pytest.mark.parametrize(
    ("headers", "cooldown"),
    [({"Retry-After": "30"}, 30.0), ({}, 60.0)],  # 60 s is the documented default
)
def test_rate_limited_mint_cools_the_shared_write_budget(
    headers: dict[str, str], cooldown: float
) -> None:
    client, router, clock, limiter = make(
        json_response(429, error_body("RATE_LIMITED"), headers)
    )

    async def scenario() -> None:
        async with client:
            with pytest.raises(SigRateLimitedError):
                await client.mint()
        start = clock.now
        await limiter.acquire(RequestKind.READ)  # reads are untouched
        assert clock.now == start
        await limiter.acquire(RequestKind.WRITE)
        assert clock.now - start >= cooldown

    run(scenario())
    assert len(router.requests) == 1  # never retried automatically


def test_oversized_retry_after_blocks_writes() -> None:
    _, _, limiter = mint_error(
        json_response(429, error_body("RATE_LIMITED"), {"Retry-After": "99999999999"})
    )
    assert limiter.is_blocked(RequestKind.WRITE)
    assert not limiter.is_blocked(RequestKind.READ)


@pytest.mark.parametrize(
    ("response", "kind"),
    [
        (json_response(401, error_body("INVALID_API_KEY")), SigAuthError),
        (json_response(503, error_body("SERVICE_UNAVAILABLE")), SigServiceUnavailableError),
        (raw_response(307, b"", {"location": "https://elsewhere.invalid/x"}), SigUnexpectedStatusError),
        (httpx.ConnectError("refused"), SigTransportError),
        (raw_response(200, b"not json"), SigResponseError),
    ],
)
def test_failures_raise_once_without_retry(response: Any, kind: type[Exception]) -> None:
    error, router, _ = mint_error(response)
    assert isinstance(error, kind)
    assert len(router.requests) == 1
    assert secret_leaks(error, SENTINEL_KEY) == []


def test_error_text_from_sig_is_not_echoed() -> None:
    body = {"error": {"code": "FORBIDDEN", "message": f"leak {TOKEN_VALUE}"}}
    error, _, _ = mint_error(json_response(403, body))
    assert isinstance(error, SigAuthError) and error.code == "FORBIDDEN"
    assert secret_leaks(error, TOKEN_VALUE) == []


def test_blocked_write_budget_is_a_deadline_error_and_sends_nothing() -> None:
    client, router, _, limiter = make(json_response(200, token_body()))
    limiter.block(RequestKind.WRITE)

    async def scenario() -> None:
        async with client:
            with pytest.raises(SigDeadlineExceededError) as caught:
                await client.mint()
            assert caught.value.phase == "rate admission"

    run(scenario())
    assert router.requests == []


def test_hanging_request_is_cut_off() -> None:
    async def hang(request: httpx.Request) -> httpx.Response:
        await asyncio.Event().wait()
        raise AssertionError

    router = Router({f"/api/v1{TOKEN}": hang})
    clock = FakeClock()
    config = SigConfig(api_key=ApiKey(SENTINEL_KEY), tournament=TOURNAMENT, base_url=BASE_URL)
    client = RealtimeTokenClient(
        config,
        httpx.AsyncClient(transport=httpx.MockTransport(router)),
        owns_http=True,
        limiter=RateLimiter(clock=clock, sleep=clock.sleep),
        clock=clock,
        total_budget_s=0.05,
    )

    async def scenario() -> None:
        async with client:
            with pytest.raises(SigDeadlineExceededError) as caught:
                await client.mint()
            assert caught.value.phase == "request"

    run(scenario())


def test_closed_client_and_credential_carrying_http_are_refused() -> None:
    client, router, _, _ = make(json_response(200, token_body()))

    async def scenario() -> None:
        await client.aclose()
        with pytest.raises(SigClientClosedError):
            await client.mint()

    run(scenario())
    assert router.requests == []
    config = SigConfig(api_key=ApiKey(SENTINEL_KEY), tournament=TOURNAMENT, base_url=BASE_URL)
    limiter = RateLimiter()
    for http in (
        httpx.AsyncClient(headers={"Authorization": "Bearer x"}),
        httpx.AsyncClient(auth=("u", "p")),
    ):
        with pytest.raises(ValueError):
            RealtimeTokenClient(config, http, limiter=limiter)
    with pytest.raises(TypeError):
        RealtimeTokenClient(config, httpx.AsyncClient(), limiter=object())  # type: ignore[arg-type]
