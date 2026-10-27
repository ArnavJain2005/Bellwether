"""A2-B boundaries: scope (T5), redaction (T18), the mismatch latch and log ownership.

The four A2-B reads reuse A2-A's machinery; these tests check that they inherit its
guarantees rather than assume it. All offline; every logger is captured and the httpx
logger is never excluded.
"""

from __future__ import annotations

import asyncio
import copy
import inspect
import logging
from collections.abc import Awaitable, Callable
from typing import Any

import httpx
import pytest
from account_kit import TOURNAMENT_PATH, exact_body, exact_response, tournament_body
from sig_testkit import (
    API,
    TOURNAMENT,
    Router,
    fixture,
    json_response,
    make_client,
    run,
    secret_leaks,
)

from engine.config import TournamentRef
from engine.sig_client.errors import (
    SigClientError,
    SigConflictError,
    SigContextMismatchError,
    SigPaginationError,
    SigResponseError,
    SigRetryExhaustedError,
)
from engine.sig_client.rate_limiter import RequestKind
from engine.sig_client.rest import SigRestClient

FILLS = f"{TOURNAMENT_PATH}/portfolio/fills"
PNL = f"{TOURNAMENT_PATH}/portfolio/pnl"
SETTLEMENTS = f"{TOURNAMENT_PATH}/portfolio/settlements"
ORDER_FILLS = f"{API}/orders/1001/fills"
OTHER_UUID = "11111111-2222-4333-8444-555555555555"

KEY = "DUMMY_SENTINEL_CODE"  # test placeholder shaped like an error code, never a credential
TITLE = "SENTINEL_TITLE_TEXT"
MESSAGE = "SENTINEL_MESSAGE_TEXT payout 987654.321"
CURSOR = "SENTINEL_CURSOR_A2B"
VALUE = "SENTINEL_VALUE_555"
SENTINELS = (KEY, TITLE, "SENTINEL_MESSAGE_TEXT", "987654.321", CURSOR, VALUE)

Call = Callable[[SigRestClient], Awaitable[Any]]
A2B_CALLS: dict[str, Call] = {
    "fills": lambda c: c.read_tournament_fills(),
    "pnl": lambda c: c.read_tournament_pnl(),
    "settlements": lambda c: c.read_tournament_settlements(),
    "order fills": lambda c: c.read_order_fills(1001),
}


# --- T5: always the configured tournament ---------------------------------------------------


def test_t5_no_a2b_read_takes_a_scope_argument() -> None:
    expected = {
        "read_tournament_fills": ["self", "page_limit", "max_pages"],
        "read_tournament_pnl": ["self", "period"],
        "read_tournament_settlements": ["self", "page_limit", "max_pages"],
        "read_order_fills": ["self", "order_id", "page_limit", "max_pages"],
    }
    for name, parameters in expected.items():
        assert list(inspect.signature(getattr(SigRestClient, name)).parameters) == parameters


def test_t5_paths_are_built_from_the_configured_tournament() -> None:
    beta = TournamentRef(slug="beta-tournament", id=OTHER_UUID)
    order_fills = exact_body("order_fills") | {"tournamentId": OTHER_UUID}
    beta_base = f"{API}/tournaments/beta-tournament"
    router = Router(
        {
            beta_base: json_response(200, tournament_body(slug=beta.slug, id=beta.id)),
            f"{beta_base}/portfolio/fills": json_response(200, fixture("tournament_fills")),
            f"{beta_base}/portfolio/pnl": json_response(200, fixture("tournament_pnl")),
            f"{beta_base}/portfolio/settlements": json_response(
                200, fixture("tournament_settlements")
            ),
            ORDER_FILLS: exact_response(200, order_fills),
        }
    )
    client, _, _ = make_client(router, tournament=beta)

    async def scenario() -> None:
        for call in A2B_CALLS.values():
            await call(client)

    run(scenario())
    assert router.paths() == [
        "/tournaments/beta-tournament",
        "/tournaments/beta-tournament/portfolio/fills",
        "/tournaments/beta-tournament/portfolio/pnl",
        "/tournaments/beta-tournament/portfolio/settlements",
        "/orders/1001/fills",  # echo-bound: checked against beta's UUID
    ]
    for path in router.paths():
        assert not path.startswith(("/account", "/portfolio"))  # never a default context


def test_t5_order_fills_echo_is_checked_against_the_configured_uuid() -> None:
    # The fixture echoes alpha's UUID; a client configured for beta must reject it.
    beta = TournamentRef(slug="beta-tournament", id=OTHER_UUID)
    router = Router(
        {
            f"{API}/tournaments/beta-tournament": json_response(
                200, tournament_body(slug=beta.slug, id=beta.id)
            )
        }
    )
    client, _, _ = make_client(router, tournament=beta)
    with pytest.raises(SigContextMismatchError):
        run(client.read_order_fills(1001))
    assert TOURNAMENT.id != beta.id


# --- the A2-A mismatch latch covers the A2-B reads -------------------------------------------


def test_after_a_latched_mismatch_no_a2b_read_sends_anything() -> None:
    router = Router({TOURNAMENT_PATH: json_response(200, tournament_body(id=OTHER_UUID))})
    client, _, _ = make_client(router)

    async def scenario() -> None:
        with pytest.raises(SigContextMismatchError):
            await client.read_tournament_balance()
        for call in A2B_CALLS.values():
            with pytest.raises(SigContextMismatchError):
                await call(client)

    run(scenario())
    assert router.paths() == ["/tournaments/alpha-tournament"]
    assert client.limiter.used(RequestKind.READ) == 1


@pytest.mark.parametrize("name", ["fills", "settlements", "order fills"])
def test_a_mismatch_latched_mid_walk_stops_the_next_page(name: str) -> None:
    # A two-page variant of each walk; the latch is set while page 1 is being served.
    path = {"fills": FILLS, "settlements": SETTLEMENTS, "order fills": ORDER_FILLS}[name]
    first = copy.deepcopy(
        exact_body(
            {
                "fills": "tournament_fills",
                "settlements": "tournament_settlements",
                "order fills": "order_fills",
            }[name]
        )
    )
    first["pagination"] = {"limit": 200, "hasMore": True, "nextCursor": "p2"}
    client_box: list[SigRestClient] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        if request.url.params.get("cursor") is None:
            client_box[0]._mismatch = SigContextMismatchError(  # stands for another read's latch
                method="GET", path="/tournaments/alpha-tournament", reason="latched"
            )
            return exact_response(200, first)
        raise AssertionError("page 2 must never be requested")

    client, router, _ = make_client(Router({path: handler}))
    client_box.append(client)
    with pytest.raises(SigContextMismatchError, match="latched"):
        run(A2B_CALLS[name](client))
    assert sum(r.url.path == path for r in router.requests) == 1


def test_a_pnl_response_arriving_after_a_latch_is_not_returned() -> None:
    release = asyncio.Event()

    async def slow(request: httpx.Request) -> httpx.Response:
        await release.wait()
        return exact_response(200, exact_body("tournament_pnl"))

    router = Router({PNL: slow})
    client, _, _ = make_client(router)

    async def scenario() -> None:
        await client.verify_tournament()
        router.routes[TOURNAMENT_PATH] = json_response(200, tournament_body(id=OTHER_UUID))
        task = asyncio.create_task(client.read_tournament_pnl())
        for _ in range(20):
            await asyncio.sleep(0)
            if any(r.url.path == PNL for r in router.requests):
                break
        with pytest.raises(SigContextMismatchError):
            await client.read_tournament_balance()
        release.set()
        with pytest.raises(SigContextMismatchError):
            await task

    run(scenario())


# --- T18: redaction --------------------------------------------------------------------------


def assert_clean(router: Router, call: Call, error_type: type[SigClientError], caplog: Any) -> None:
    client, _, _ = make_client(router, api_key=KEY)
    caplog.set_level(logging.DEBUG)
    with pytest.raises(error_type) as info:
        run(call(client))
    for sentinel in SENTINELS:
        assert secret_leaks(info.value, sentinel) == [], sentinel
        assert sentinel not in caplog.text, sentinel  # every logger, httpx included


def test_t18_malformed_pnl_409_text_never_reaches_errors_or_logs(caplog: Any) -> None:
    body = copy.deepcopy(fixture("positions_missing_valuation"))
    body["error"]["message"] = MESSAGE
    body["error"]["details"]["missingValuations"][0].update(exchangeId=VALUE, marketTitle=TITLE)
    assert_clean(
        Router({PNL: json_response(409, body)}),
        A2B_CALLS["pnl"],
        SigConflictError,
        caplog,
    )


@pytest.mark.parametrize("name", list(A2B_CALLS))
def test_t18_retried_errors_drop_message_and_details(name: str, caplog: Any) -> None:
    path = {"fills": FILLS, "pnl": PNL, "settlements": SETTLEMENTS, "order fills": ORDER_FILLS}
    body = {"error": {"code": KEY, "message": MESSAGE, "details": {TITLE: VALUE}}}
    assert_clean(
        Router({path[name]: [json_response(503, body)]}),
        A2B_CALLS[name],
        SigRetryExhaustedError,
        caplog,
    )
    assert "SIG read retry" in caplog.text


@pytest.mark.parametrize(
    ("name", "path", "mutate"),
    [
        ("fills", FILLS, lambda b: b["data"][0].update(quantity=VALUE)),
        ("settlements", SETTLEMENTS, lambda b: b["data"][0].update(marketTitle=[TITLE])),
        ("settlements", SETTLEMENTS, lambda b: b["data"][0].update(payout=VALUE)),
        ("order fills", ORDER_FILLS, lambda b: b.update(tournamentId=VALUE)),
        ("order fills", ORDER_FILLS, lambda b: b.update(totalQuantityFilled=VALUE)),
        ("pnl", PNL, lambda b: b.update(totalAccountValue=VALUE)),
    ],
)
def test_t18_malformed_values_are_not_repeated(
    name: str, path: str, mutate: Any, caplog: Any
) -> None:
    fixture_name = {
        "fills": "tournament_fills",
        "settlements": "tournament_settlements",
        "order fills": "order_fills",
        "pnl": "tournament_pnl",
    }[name]
    body = fixture(fixture_name)
    mutate(body)
    # SigContextMismatchError (a foreign echo) is a SigResponseError too.
    assert_clean(
        Router({path: json_response(200, body)}), A2B_CALLS[name], SigResponseError, caplog
    )


@pytest.mark.parametrize("name", ["fills", "settlements", "order fills"])
def test_t18_walk_cursors_never_reach_any_log(name: str, caplog: pytest.LogCaptureFixture) -> None:
    path = {"fills": FILLS, "settlements": SETTLEMENTS, "order fills": ORDER_FILLS}[name]
    fixture_name = {
        "fills": "tournament_fills",
        "settlements": "tournament_settlements",
        "order fills": "order_fills",
    }[name]
    first = exact_body(fixture_name)
    first["pagination"] = {"limit": 200, "hasMore": True, "nextCursor": CURSOR}
    second = exact_body(fixture_name)
    second["data"] = []

    def handler(request: httpx.Request) -> httpx.Response:
        cursor = request.url.params.get("cursor")
        return exact_response(200, second if cursor == CURSOR else first)

    caplog.set_level(logging.DEBUG)
    client, router, _ = make_client(Router({path: handler}))
    run(A2B_CALLS[name](client))
    assert router.requests[-1].url.params.get("cursor") == CURSOR  # it was sent
    assert CURSOR not in caplog.text
    lines = [r.getMessage() for r in caplog.records if r.name == "httpx"]
    assert sum("cursor=REDACTED" in line for line in lines) == 1  # the line is kept


def test_t18_repeated_walk_cursors_are_not_repeated_in_errors(caplog: Any) -> None:
    first = exact_body("tournament_fills")
    first["pagination"] = {"limit": 200, "hasMore": True, "nextCursor": CURSOR}
    second = copy.deepcopy(first)
    for item, new_id in zip(second["data"], (7001, 7002), strict=True):
        item["id"] = new_id
        item["filledAt"] = "2025-06-14T12:00:00.000Z"

    def handler(request: httpx.Request) -> httpx.Response:
        return exact_response(200, second if request.url.params.get("cursor") else first)

    assert_clean(Router({FILLS: handler}), A2B_CALLS["fills"], SigPaginationError, caplog)


# --- concurrency: each walk keeps its own records -----------------------------------------------


def test_concurrent_a2b_walks_keep_separate_page_records() -> None:
    first = exact_body("tournament_settlements")
    first["pagination"] = {"limit": 200, "hasMore": True, "nextCursor": "p2"}
    second = exact_body("tournament_settlements")
    second["data"] = [dict(second["data"][0], id="placeholder-settlement-3")]
    second["coverage"] = {"complete": False}

    def handler(request: httpx.Request) -> httpx.Response:
        return exact_response(200, second if request.url.params.get("cursor") else first)

    client, _, _ = make_client(Router({SETTLEMENTS: handler}))

    async def scenario() -> list[Any]:
        return await asyncio.gather(
            client.read_tournament_settlements(),
            client.read_tournament_settlements(),
            client.read_tournament_fills(),
        )

    a, b, fills = run(scenario())
    for walk in (a, b):
        assert [p.provenance.page_index for p in walk.pages] == [0, 1]
        assert walk.complete is False and walk.incomplete_pages == (1,)
    assert [p.provenance.page_index for p in fills.pages] == [0]
