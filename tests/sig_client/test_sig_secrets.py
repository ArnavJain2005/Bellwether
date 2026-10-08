import logging

import httpx
import pytest
from sig_testkit import (
    API,
    BASE_URL,
    SENTINEL_KEY,
    TOURNAMENT,
    Router,
    error_body,
    fixture,
    json_response,
    make_client,
    run,
)

from engine.config import ApiKey, SigConfig
from engine.sig_client.errors import SigClientError
from engine.sig_client.rest import SigRestClient

BOOK = f"{API}/exchanges/36/orderbook"


def test_key_is_sent_only_in_the_authorization_header() -> None:
    client, router, _ = make_client()
    run(client.get_exchange_orderbook("36"))
    for request in router.requests:
        assert request.headers["Authorization"] == f"Bearer {SENTINEL_KEY}"
        assert "x-api-key" not in request.headers
        assert SENTINEL_KEY not in str(request.url)


def test_key_never_reaches_logs_errors_or_reprs(caplog: pytest.LogCaptureFixture) -> None:
    echo = {"error": {"code": "INVALID_API_KEY", "message": f"bad key Bearer {SENTINEL_KEY}"}}
    router = Router(
        {
            BOOK: [
                json_response(503, error_body("SERVICE_UNAVAILABLE"), {"Retry-After": "1"}),
                httpx.ReadTimeout(f"timeout with {SENTINEL_KEY}"),
                json_response(401, echo),
            ]
        }
    )
    client, _, _ = make_client(router)
    caplog.set_level(logging.DEBUG)
    with pytest.raises(SigClientError) as info:
        run(client.get_exchange_orderbook("36"))
    error = info.value
    texts = [str(error), repr(error), repr(client), caplog.text]
    texts += [str(error.__cause__), str(error.__context__)]
    assert all(SENTINEL_KEY not in text for text in texts)
    assert "bad key" not in str(error)  # SIG's free-form message is never kept
    assert "SIG read retry" in caplog.text  # the retries were logged, sanitized


def test_transport_error_text_is_not_propagated() -> None:
    client, _, _ = make_client(Router({BOOK: [httpx.UnsupportedProtocol(SENTINEL_KEY)]}))
    with pytest.raises(SigClientError) as info:
        run(client.get_exchange_orderbook("36"))
    assert SENTINEL_KEY not in str(info.value)
    assert info.value.__cause__ is None
    assert info.value.__suppress_context__


def test_validation_errors_do_not_dump_response_values() -> None:
    body = fixture("exchange_orderbook") | {"bestBid": f"{SENTINEL_KEY}"}
    client, _, _ = make_client(Router({BOOK: json_response(200, body)}))
    with pytest.raises(SigClientError) as info:
        run(client.get_exchange_orderbook("36"))
    assert SENTINEL_KEY not in str(info.value)
    assert "bestBid" in str(info.value)


@pytest.mark.parametrize(
    "http",
    [
        lambda: httpx.AsyncClient(auth=("user", "pass")),
        lambda: httpx.AsyncClient(headers={"Authorization": "Bearer other"}),
        lambda: httpx.AsyncClient(headers={"X-API-Key": "other"}),
    ],
)
def test_injected_client_with_its_own_credentials_is_refused(http: object) -> None:
    config = SigConfig(api_key=ApiKey(SENTINEL_KEY), tournament=TOURNAMENT, base_url=BASE_URL)

    async def scenario() -> None:
        injected = http()  # type: ignore[operator]
        try:
            with pytest.raises(ValueError):
                SigRestClient(config, injected)
        finally:
            await injected.aclose()

    run(scenario())
