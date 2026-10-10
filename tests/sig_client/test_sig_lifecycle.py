import asyncio
import json
from pathlib import Path

import httpx
import pytest
from sig_testkit import (
    API,
    BASE_URL,
    FIXTURES,
    SENTINEL_KEY,
    TOURNAMENT,
    FakeClock,
    RealNetworkUsed,
    Router,
    fixture,
    json_response,
    make_client,
    run,
)

from engine.config import ApiKey, SigConfig
from engine.sig_client import rest
from engine.sig_client.errors import SigClientClosedError
from engine.sig_client.rest import SigRestClient

BOOK = f"{API}/exchanges/36/orderbook"
CONFIG = SigConfig(api_key=ApiKey(SENTINEL_KEY), tournament=TOURNAMENT, base_url=BASE_URL)


def test_owned_http_client_is_closed_by_the_context_manager() -> None:
    client, _, _ = make_client()

    async def scenario() -> None:
        async with client:
            await client.get_tournament()
        assert client.closed and client._http.is_closed
        await client.aclose()  # idempotent

    run(scenario())


def test_borrowed_http_client_is_left_open() -> None:
    async def scenario() -> None:
        http = httpx.AsyncClient(transport=httpx.MockTransport(Router()))
        async with SigRestClient(CONFIG, http) as client:
            await client.get_tournament()
        assert client.closed and not http.is_closed
        await http.aclose()

    run(scenario())


def test_use_after_close_is_refused_without_a_request() -> None:
    client, router, _ = make_client()

    async def scenario() -> None:
        await client.aclose()
        with pytest.raises(SigClientClosedError):
            await client.get_tournament()

    run(scenario())
    assert router.requests == []


def test_create_owns_a_client_with_safe_settings() -> None:
    async def scenario() -> None:
        client = SigRestClient.create(CONFIG, transport=httpx.MockTransport(Router()))
        http = client._http
        assert http.follow_redirects is False
        assert http.timeout == CONFIG.timeout
        assert http.trust_env is False
        await client.get_tournament()
        await client.aclose()
        assert http.is_closed

    run(scenario())


def test_requests_carry_the_configured_timeout() -> None:
    client, router, _ = make_client()
    run(client.get_tournament())
    assert router.requests[0].extensions["timeout"] == CONFIG.timeout.as_dict()


def test_cancellation_during_a_request_propagates() -> None:
    started = asyncio.Event

    async def scenario() -> None:
        entered = started()

        async def hang(request: httpx.Request) -> httpx.Response:
            entered.set()
            await asyncio.Event().wait()
            raise AssertionError("unreachable")

        client, _, _ = make_client(Router({BOOK: hang}))
        await client.verify_tournament()
        task = asyncio.create_task(client.get_exchange_orderbook("36"))
        await entered.wait()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        await client.aclose()

    run(scenario())


def test_cancellation_during_a_retry_sleep_propagates() -> None:
    async def scenario() -> None:
        sleeping = asyncio.Event()
        clock = FakeClock()

        async def stuck_sleep(seconds: float) -> None:
            sleeping.set()
            await asyncio.Event().wait()

        router = Router(
            {BOOK: [json_response(503, {"error": {"code": "TX_CONFLICT", "message": ""}})]}
        )
        client, _, _ = make_client(router, clock=clock)
        client._sleep = stuck_sleep
        await client.verify_tournament()
        task = asyncio.create_task(client.get_exchange_orderbook("36"))
        await sleeping.wait()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        await client.aclose()

    run(scenario())


def test_concurrent_first_reads_verify_the_tournament_once() -> None:
    client, router, _ = make_client()

    async def scenario() -> None:
        await asyncio.gather(*(client.get_exchange_orderbook("36") for _ in range(5)))

    run(scenario())
    assert router.count("/tournaments/alpha-tournament") == 1
    assert router.count("/exchanges/36/orderbook") == 5


def test_real_network_transport_is_blocked_in_tests() -> None:
    async def scenario() -> None:
        client = SigRestClient.create(CONFIG)  # no mock transport
        try:
            with pytest.raises(RealNetworkUsed):
                await client.get_tournament()
        finally:
            await client.aclose()

    run(scenario())


def test_no_public_write_or_arbitrary_request_methods() -> None:
    public = {name for name in dir(SigRestClient) if not name.startswith("_")}
    assert public == {
        "aclose",
        "closed",
        "create",
        "get_exchange_orderbook",
        "get_exchange_prices",
        "get_market_orderbook",
        "get_tournament",
        "limiter",  # read-only accessor (S1-T3-A: the feed checks the shared budget)
        "list_all_orders",
        "list_all_tournament_markets",
        "list_orders_page",
        "list_tournament_markets_page",
        "tournament",  # read-only accessor for the configured TournamentRef
        "verify_tournament",
    }
    for accessor in ("limiter", "tournament"):
        assert isinstance(getattr(SigRestClient, accessor), property)
        assert getattr(SigRestClient, accessor).fset is None
    source = Path(rest.__file__).read_text(encoding="utf-8")
    for verb in ('"POST"', '"DELETE"', '"PUT"', '"PATCH"'):
        assert verb not in source


def test_fixture_bodies_are_marked_synthetic() -> None:
    for path in FIXTURES.glob("*.json"):
        document = json.loads(path.read_text(encoding="utf-8"))
        assert document["provenance"].startswith("synthetic, spec-derived"), path.name
        assert document["openapi"], path.name
    assert fixture("tournament")["slug"] == TOURNAMENT.slug
