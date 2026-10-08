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
    raw_response,
    run,
)

from engine.sig_client.errors import (
    SigAuthError,
    SigBadRequestError,
    SigDeadlineExceededError,
    SigNotFoundError,
    SigRateLimitedError,
    SigRetryExhaustedError,
    SigServerError,
    SigServiceUnavailableError,
    SigTransportError,
    SigUnexpectedStatusError,
)
from engine.sig_client.rate_limiter import RateLimiter, RequestKind
from engine.sig_client.rest import ReadRetryPolicy

BOOK = f"{API}/exchanges/36/orderbook"
OK = json_response(200, fixture("exchange_orderbook"))


def unavailable(code: str = "SERVICE_UNAVAILABLE", **headers: str) -> httpx.Response:
    return json_response(503, error_body(code), headers)


def rate_limited(**headers: str) -> httpx.Response:
    return json_response(429, error_body("RATE_LIMITED"), headers)


def book_requests(router: Router) -> int:
    return sum(r.url.path == BOOK for r in router.requests)


def test_transient_503_is_retried_and_each_attempt_uses_read_budget() -> None:
    client, router, clock = make_client(Router({BOOK: [unavailable("TX_CONFLICT"), OK]}))
    book = run(client.get_exchange_orderbook("36"))
    assert book.exchangeId == "36"
    assert book_requests(router) == 2
    assert client._limiter.used(RequestKind.READ) == 3  # tournament + two attempts
    assert clock.sleeps == [0.25]  # first backoff, jitter is identity in tests


def test_attempt_limit_raises_with_last_error() -> None:
    client, router, clock = make_client(Router({BOOK: [unavailable()]}))
    with pytest.raises(SigRetryExhaustedError) as info:
        run(client.get_exchange_orderbook("36"))
    error = info.value
    assert error.attempts == 4 and "attempt limit" in str(error)
    assert isinstance(error.last_error, SigServiceUnavailableError)
    assert error.last_error.code == "SERVICE_UNAVAILABLE"
    assert book_requests(router) == 4
    assert clock.sleeps == [0.25, 0.5, 1.0]


def test_time_budget_stops_retries_before_sleeping_past_it() -> None:
    retry = ReadRetryPolicy(max_attempts=10, total_budget_s=1.0)
    client, router, clock = make_client(Router({BOOK: [unavailable()]}), retry=retry)
    with pytest.raises(SigDeadlineExceededError) as info:
        run(client.get_exchange_orderbook("36"))
    assert info.value.phase == "retry wait" and info.value.attempts == 3
    assert isinstance(info.value.last_error, SigServiceUnavailableError)
    assert book_requests(router) == 3  # sleeps 0.25 + 0.5, then 1.0 would overrun
    assert sum(clock.sleeps) <= 1.0


def test_retry_after_is_a_floor_on_the_delay() -> None:
    client, _, clock = make_client(Router({BOOK: [unavailable(**{"Retry-After": "3"}), OK]}))
    run(client.get_exchange_orderbook("36"))
    assert clock.sleeps == [3.0]


def test_jitter_is_injected_and_never_undercuts_retry_after() -> None:
    seen: list[float] = []

    def jitter(delay: float) -> float:
        seen.append(delay)
        return delay / 2

    router = Router({BOOK: [unavailable(), unavailable(**{"Retry-After": "2"}), OK]})
    client, _, clock = make_client(router, jitter=jitter)
    run(client.get_exchange_orderbook("36"))
    assert seen == [0.25, 0.5]
    assert clock.sleeps == [0.125, 2.0]


def test_429_waits_retry_after_and_cools_down_all_reads() -> None:
    client, router, clock = make_client(Router({BOOK: [rate_limited(**{"Retry-After": "5"}), OK]}))

    async def scenario() -> None:
        await client.get_exchange_orderbook("36")

    run(scenario())
    assert book_requests(router) == 2
    assert clock.sleeps[0] == 5.0
    assert clock.now >= 1005.0


def test_429_beyond_the_budget_fails_fast_and_keeps_the_cooldown() -> None:
    router = Router({BOOK: [rate_limited(**{"Retry-After": "60"}), OK]})
    client, router, clock = make_client(router)

    async def scenario() -> None:
        with pytest.raises(SigDeadlineExceededError) as info:
            await client.get_exchange_orderbook("36")
        assert info.value.phase == "retry wait"
        assert isinstance(info.value.last_error, SigRateLimitedError)
        assert info.value.last_error.retry_after == 60.0
        assert clock.sleeps == []
        # The shared cooldown now makes the next read wait out the instructed minute.
        with pytest.raises(SigDeadlineExceededError) as info:
            await client.get_exchange_orderbook("36")
        assert info.value.phase == "rate admission" and info.value.attempts == 0

    run(scenario())
    assert book_requests(router) == 1


def test_429_without_retry_after_assumes_the_documented_sixty_seconds() -> None:
    retry = ReadRetryPolicy(total_budget_s=120.0)
    client, _, clock = make_client(Router({BOOK: [rate_limited(), OK]}), retry=retry)
    run(client.get_exchange_orderbook("36"))
    assert clock.sleeps[0] == 60.0


def test_cooldown_is_shared_with_other_clients_of_the_account() -> None:
    first, _, clock = make_client(Router({BOOK: [rate_limited(**{"Retry-After": "60"})]}))
    shared = first._limiter
    second, router2, _ = make_client(clock=clock, limiter=shared)

    async def scenario() -> None:
        with pytest.raises(SigDeadlineExceededError):
            await first.get_exchange_orderbook("36")
        with pytest.raises(SigDeadlineExceededError) as info:
            await second.get_exchange_orderbook("36")
        assert info.value.phase == "rate admission"

    run(scenario())
    assert router2.requests == []


@pytest.mark.parametrize("value", ["Wed, 21 Oct 2026 07:28:00 GMT", "soon", "-1"])
def test_unparseable_retry_after_is_not_retried(value: str) -> None:
    client, router, clock = make_client(Router({BOOK: [unavailable(**{"Retry-After": value}), OK]}))
    with pytest.raises(SigServiceUnavailableError):
        run(client.get_exchange_orderbook("36"))
    assert book_requests(router) == 1 and clock.sleeps == []


def test_429_with_unparseable_retry_after_still_cools_down_reads() -> None:
    router = Router({BOOK: [rate_limited(**{"Retry-After": "later"}), OK]})
    client, router, clock = make_client(router)

    async def scenario() -> None:
        with pytest.raises(SigRateLimitedError):
            await client.get_exchange_orderbook("36")
        with pytest.raises(SigDeadlineExceededError, match="rate admission"):
            await client.get_exchange_orderbook("36")

    run(scenario())
    assert book_requests(router) == 1 and clock.sleeps == []


@pytest.mark.parametrize(
    "exc",
    [
        httpx.ConnectError("refused"),
        httpx.ConnectTimeout("slow"),
        httpx.ReadTimeout("slow"),
        httpx.ReadError("reset"),
        httpx.RemoteProtocolError("bad"),
        httpx.PoolTimeout("pool"),
    ],
)
def test_selected_transport_failures_are_retried(exc: Exception) -> None:
    client, router, _ = make_client(Router({BOOK: [exc, OK]}))
    assert run(client.get_exchange_orderbook("36")).exchangeId == "36"
    assert book_requests(router) == 2


def test_exhausted_transport_failures_report_the_kind() -> None:
    client, _, _ = make_client(Router({BOOK: [httpx.ConnectError("refused")]}))
    with pytest.raises(SigRetryExhaustedError) as info:
        run(client.get_exchange_orderbook("36"))
    assert isinstance(info.value.last_error, SigTransportError)
    assert info.value.last_error.kind == "ConnectError"


@pytest.mark.parametrize(
    "exc",
    [httpx.UnsupportedProtocol("ftp"), httpx.ProxyError("proxy"), httpx.LocalProtocolError("x")],
)
def test_other_transport_failures_are_not_retried(exc: Exception) -> None:
    client, router, _ = make_client(Router({BOOK: [exc, OK]}))
    with pytest.raises(SigTransportError):
        run(client.get_exchange_orderbook("36"))
    assert book_requests(router) == 1


@pytest.mark.parametrize(
    ("status", "code", "error_type"),
    [
        (400, "VALIDATION_ERROR", SigBadRequestError),
        (401, "INVALID_API_KEY", SigAuthError),
        (403, "INSUFFICIENT_SCOPES", SigAuthError),
        (404, "NOT_FOUND", SigNotFoundError),
        (500, "INTERNAL_ERROR", SigServerError),
    ],
)
def test_permanent_errors_are_not_retried(status: int, code: str, error_type: type) -> None:
    router = Router({BOOK: [json_response(status, error_body(code)), OK]})
    client, router, clock = make_client(router)
    with pytest.raises(error_type) as info:
        run(client.get_exchange_orderbook("36"))
    assert info.value.status == status and info.value.code == code
    assert book_requests(router) == 1 and clock.sleeps == []


def test_502_is_not_a_documented_read_failure_and_is_not_retried() -> None:
    body = {"error": {"code": "ORDER_STATUS_UNKNOWN", "message": "x"}}
    client, router, _ = make_client(Router({BOOK: [json_response(502, body), OK]}))
    with pytest.raises(SigServerError):
        run(client.get_exchange_orderbook("36"))
    assert book_requests(router) == 1


def test_unknown_error_code_is_flagged_but_not_kept() -> None:
    body = {"error": {"code": "BRAND_NEW_CODE", "message": "something new"}}
    client, _, _ = make_client(Router({BOOK: json_response(418, body)}))
    with pytest.raises(SigUnexpectedStatusError) as info:
        run(client.get_exchange_orderbook("36"))
    assert info.value.status == 418
    assert info.value.code is None and info.value.unrecognized_code is True
    assert "BRAND_NEW_CODE" not in str(info.value)
    assert "something new" not in str(info.value)


def test_unknown_code_on_503_is_still_retried() -> None:
    body = {"error": {"code": "SOMETHING_ELSE", "message": "x"}}
    client, router, _ = make_client(Router({BOOK: [json_response(503, body), OK]}))
    run(client.get_exchange_orderbook("36"))
    assert book_requests(router) == 2


@pytest.mark.parametrize(
    ("content", "message"),
    [
        (b"<html>502 Bad Gateway</html>", "non-JSON error body"),
        (b'{"error": "flat string"}', "error body without an error object"),
        (b'{"error": {"code": 42, "message": ["x"]}}', "error envelope"),
        (b'{"error": {"code": "lower case <script>", "message": "m"}}', "error envelope"),
    ],
)
def test_malformed_error_bodies_are_handled(content: bytes, message: str) -> None:
    client, _, _ = make_client(Router({BOOK: raw_response(404, content)}))
    with pytest.raises(SigNotFoundError) as info:
        run(client.get_exchange_orderbook("36"))
    assert info.value.code is None
    assert info.value.detail == message
    assert "<html>" not in str(info.value) and "<script>" not in str(info.value)


def test_redirects_are_never_followed() -> None:
    redirect = httpx.Response(307, headers={"Location": "https://elsewhere.invalid/steal"})
    client, router, _ = make_client(Router({BOOK: redirect}))
    with pytest.raises(SigUnexpectedStatusError) as info:
        run(client.get_exchange_orderbook("36"))
    assert info.value.status == 307
    assert all(r.url.host == "sig.invalid" for r in router.requests)


def test_limiter_wait_beyond_the_budget_is_a_clear_error() -> None:
    clock = FakeClock()
    tight = RateLimiter(read_limit=1, clock=clock, sleep=clock.sleep)
    client, router, _ = make_client(clock=clock, limiter=tight)
    with pytest.raises(SigDeadlineExceededError) as info:
        run(client.get_exchange_orderbook("36"))  # tournament check took the only slot
    assert info.value.phase == "rate admission" and info.value.attempts == 0
    assert [r.url.path for r in router.requests] == [f"{API}/tournaments/alpha-tournament"]


def test_retry_policy_validation() -> None:
    with pytest.raises(ValueError):
        ReadRetryPolicy(max_attempts=0)
    with pytest.raises(ValueError):
        ReadRetryPolicy(total_budget_s=0)
