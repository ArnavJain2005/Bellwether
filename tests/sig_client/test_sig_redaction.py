"""F1 area 2: no response-derived value reaches errors or logs.

The dummy key is shaped like an SIG error code and a slug, so it survives any filter
that only checks the shape of a value.
"""

import copy
import logging
from typing import Any

import httpx
import pytest
from sig_testkit import (
    API,
    Router,
    fixture,
    json_response,
    make_client,
    run,
    secret_leaks,
)

from engine.sig_client.errors import (
    SigApiError,
    SigAuthError,
    SigClientError,
    SigContextMismatchError,
    SigPaginationError,
    SigResponseError,
    SigRetryExhaustedError,
)

CODE_LIKE_KEY = "DUMMY_SENTINEL_CODE"  # test placeholder, never a credential
BOOK = f"{API}/exchanges/36/orderbook"
TOURNAMENT_PATH = f"{API}/tournaments/alpha-tournament"
MARKETS = f"{TOURNAMENT_PATH}/markets"


def leak_free(
    router: Router, call: Any, error_type: type[SigClientError], caplog: pytest.LogCaptureFixture
) -> SigClientError:
    client, _, _ = make_client(router, api_key=CODE_LIKE_KEY)
    caplog.set_level(logging.DEBUG)
    with pytest.raises(error_type) as info:
        run(call(client))
    assert secret_leaks(info.value, CODE_LIKE_KEY) == []
    assert CODE_LIKE_KEY not in caplog.text
    assert CODE_LIKE_KEY not in repr(client)
    return info.value


def test_error_code_equal_to_the_key_is_not_kept(caplog: pytest.LogCaptureFixture) -> None:
    body = {"error": {"code": CODE_LIKE_KEY, "message": CODE_LIKE_KEY}}
    error = leak_free(
        Router({BOOK: json_response(401, body)}),
        lambda c: c.get_exchange_orderbook("36"),
        SigAuthError,
        caplog,
    )
    assert isinstance(error, SigApiError)
    assert error.code is None and error.unrecognized_code is True and error.status == 401


def test_retried_error_code_equal_to_the_key_stays_out_of_logs(
    caplog: pytest.LogCaptureFixture,
) -> None:
    body = {"error": {"code": CODE_LIKE_KEY, "message": f"Bearer {CODE_LIKE_KEY}"}}
    error = leak_free(
        Router({BOOK: [json_response(503, body)]}),
        lambda c: c.get_exchange_orderbook("36"),
        SigRetryExhaustedError,
        caplog,
    )
    assert "SIG read retry" in caplog.text
    assert isinstance(error, SigRetryExhaustedError) and error.last_error.status == 503


def test_echoed_tournament_slug_is_not_repeated(caplog: pytest.LogCaptureFixture) -> None:
    body = fixture("tournament") | {"slug": CODE_LIKE_KEY}
    client, _, _ = make_client(
        Router({TOURNAMENT_PATH: json_response(200, body)}), api_key=CODE_LIKE_KEY
    )
    caplog.set_level(logging.DEBUG)

    async def scenario() -> list[SigContextMismatchError]:
        errors = []
        for _ in range(2):  # the first mismatch, then its sticky re-raise
            with pytest.raises(SigContextMismatchError) as info:
                await client.get_exchange_orderbook("36")
            errors.append(info.value)
        return errors

    for error in run(scenario()):
        assert secret_leaks(error, CODE_LIKE_KEY) == []
    assert CODE_LIKE_KEY not in caplog.text


def test_echoed_tournament_id_is_not_repeated(caplog: pytest.LogCaptureFixture) -> None:
    body = fixture("tournament") | {"id": CODE_LIKE_KEY}
    leak_free(
        Router({TOURNAMENT_PATH: json_response(200, body)}),
        lambda c: c.get_tournament(),
        SigResponseError,
        caplog,
    )


def market_book_with(mutate: Any) -> dict[str, Any]:
    body = copy.deepcopy(fixture("market_orderbook"))
    mutate(body["contexts"][0]["tournament"])
    return body


@pytest.mark.parametrize(
    ("mutate", "error_type"),
    [
        (lambda t: t.update(slug=CODE_LIKE_KEY), SigContextMismatchError),
        (lambda t: t.update(id=CODE_LIKE_KEY), SigResponseError),
        (lambda t: t.update(name=CODE_LIKE_KEY, slug="other"), SigContextMismatchError),
    ],
)
def test_echoed_context_values_are_not_repeated(
    mutate: Any, error_type: type[SigClientError], caplog: pytest.LogCaptureFixture
) -> None:
    router = Router({f"{API}/markets/26/orderbook": json_response(200, market_book_with(mutate))})
    leak_free(router, lambda c: c.get_market_orderbook("26"), error_type, caplog)


def test_duplicate_item_ids_are_not_repeated(caplog: pytest.LogCaptureFixture) -> None:
    first = fixture("tournament_markets_page1")
    first["data"][0]["id"] = CODE_LIKE_KEY
    second = copy.deepcopy(first)
    second["pagination"] = {"total": 2, "limit": 1, "hasMore": False, "nextCursor": None}
    pages = {None: first, "cursor-page-2": second}

    def handler(request: httpx.Request) -> httpx.Response:
        return json_response(200, pages[request.url.params.get("cursor")])

    leak_free(
        Router({MARKETS: handler}),
        lambda c: c.list_all_tournament_markets(page_limit=1),
        SigPaginationError,
        caplog,
    )


@pytest.mark.parametrize(
    "change",
    [
        {"bestBid": CODE_LIKE_KEY},
        {"exchangeId": CODE_LIKE_KEY},  # wrong exchange echoed back
        {"asOf": {CODE_LIKE_KEY: 1}},
        {"bids": [{"price": 0.4, "quantity": CODE_LIKE_KEY}]},
    ],
)
def test_validation_and_echo_errors_do_not_repeat_values(
    change: dict[str, Any], caplog: pytest.LogCaptureFixture
) -> None:
    body = fixture("exchange_orderbook") | change
    leak_free(
        Router({BOOK: json_response(200, body)}),
        lambda c: c.get_exchange_orderbook("36"),
        SigResponseError,
        caplog,
    )


def test_orders_validation_errors_do_not_repeat_values(caplog: pytest.LogCaptureFixture) -> None:
    body = fixture("orders_page")
    body["data"][0]["side"] = CODE_LIKE_KEY
    body["data"][1]["exchangeId"] = CODE_LIKE_KEY
    leak_free(
        Router({f"{API}/orders": json_response(200, body)}),
        lambda c: c.list_orders_page(exchange_id="36"),
        SigResponseError,
        caplog,
    )
