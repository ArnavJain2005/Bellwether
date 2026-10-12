"""A2-A design T18: no balance, id, title, message, detail or cursor reaches errors or logs.

Every response here carries sentinel strings in the places a careless client might copy
from: `message`, `details`, titles, ids and values. None may appear in an exception (its
text, repr, attributes or chained errors) or in any log line the client writes.

Known pre-existing gap, not changed here: httpx's own "httpx" logger writes each request
URL at INFO, including a `cursor` query value taken from the previous page. That applies
to every cursor walk on main, not only to A2-A, and is reported in the A2-A pull request.
The checks below therefore cover every logger except "httpx".
"""

from __future__ import annotations

import copy
import logging
from collections.abc import Awaitable, Callable
from typing import Any

import pytest
from account_kit import ORDERS, POSITIONS, TOURNAMENT_PATH, by_cursor, orders_page, tournament_body
from sig_testkit import (
    Router,
    fixture,
    json_response,
    make_client,
    run,
    secret_leaks,
)

from engine.sig_client.account_reads import UnvaluedHoldingsRead
from engine.sig_client.errors import (
    SigAuthError,
    SigClientError,
    SigConflictError,
    SigContextMismatchError,
    SigPaginationError,
    SigResponseError,
    SigRetryExhaustedError,
)
from engine.sig_client.rest import SigRestClient

KEY = "DUMMY_SENTINEL_CODE"  # test placeholder shaped like an error code, never a credential
TITLE = "SENTINEL_TITLE_TEXT"
MESSAGE = "SENTINEL_MESSAGE_TEXT balance 987654.321"
DETAIL = "SENTINEL_DETAIL_TEXT"
BALANCE = "SENTINEL_BALANCE_987654"
CURSOR = "SENTINEL_CURSOR_VALUE"
SENTINELS = (KEY, TITLE, "SENTINEL_MESSAGE_TEXT", "987654.321", DETAIL, BALANCE, CURSOR)

Call = Callable[[SigRestClient], Awaitable[Any]]


def client_log(caplog: Any) -> str:
    """Every captured log message except httpx's own request lines (see the docstring)."""
    return "\n".join(r.getMessage() for r in caplog.records if r.name != "httpx")


def assert_clean(router: Router, call: Call, error_type: type[SigClientError], caplog: Any) -> None:
    client, _, _ = make_client(router, api_key=KEY)
    caplog.set_level(logging.DEBUG)
    with pytest.raises(error_type) as info:
        run(call(client))
    for sentinel in SENTINELS:
        assert secret_leaks(info.value, sentinel) == [], sentinel
        assert sentinel not in client_log(caplog), sentinel
    assert KEY not in caplog.text  # the key never appears, not even in httpx's lines


def missing_valuation(**item_changes: Any) -> dict[str, Any]:
    body = copy.deepcopy(fixture("positions_missing_valuation"))
    body["error"]["message"] = MESSAGE
    body["error"]["details"]["extra"] = DETAIL
    item = body["error"]["details"]["missingValuations"][0]
    item.update(marketTitle=TITLE, settlementOption=TITLE, **item_changes)
    return body


@pytest.mark.parametrize(
    "body",
    [
        missing_valuation(exchangeId=KEY),  # non-digit id: generic conflict
        missing_valuation(marketId=f"26{KEY}"),
        {"error": {"code": KEY, "message": MESSAGE, "details": {"missingValuations": DETAIL}}},
        {"error": {"code": "CONFLICT", "message": MESSAGE, "details": {"x": [TITLE, BALANCE]}}},
    ],
)
def test_malformed_409_text_never_reaches_the_error(body: Any, caplog: Any) -> None:
    assert_clean(
        Router({POSITIONS: json_response(409, body)}),
        lambda c: c.read_tournament_positions(),
        SigConflictError,
        caplog,
    )


def test_valid_409_keeps_only_the_pairs(caplog: pytest.LogCaptureFixture) -> None:
    caplog.set_level(logging.DEBUG)
    client, _, _ = make_client(Router({POSITIONS: json_response(409, missing_valuation())}))
    result = run(client.read_tournament_positions())
    assert isinstance(result, UnvaluedHoldingsRead)
    for sentinel in SENTINELS:
        assert sentinel not in repr(result)
        assert sentinel not in caplog.text
    # A documented 409 is a result, not a logged failure: the client logs nothing.
    assert [r for r in caplog.records if r.name.startswith("engine")] == []


@pytest.mark.parametrize("status", [403, 401, 404, 500])
def test_other_error_statuses_drop_message_and_details(status: int, caplog: Any) -> None:
    body = {"error": {"code": "FORBIDDEN", "message": MESSAGE, "details": {TITLE: BALANCE}}}
    expected = SigAuthError if status in (401, 403) else SigClientError
    assert_clean(
        Router({POSITIONS: json_response(status, body)}),
        lambda c: c.read_tournament_positions(),
        expected,
        caplog,
    )


@pytest.mark.parametrize("path", [TOURNAMENT_PATH, POSITIONS, ORDERS])
def test_retried_errors_stay_out_of_logs(path: str, caplog: Any) -> None:
    body = {"error": {"code": KEY, "message": MESSAGE, "details": {"d": DETAIL}}}
    calls: dict[str, Call] = {
        TOURNAMENT_PATH: lambda c: c.read_tournament_balance(),
        POSITIONS: lambda c: c.read_tournament_positions(),
        ORDERS: lambda c: c.read_open_orders(),
    }
    assert_clean(
        Router({path: [json_response(503, body)]}), calls[path], SigRetryExhaustedError, caplog
    )
    assert "SIG read retry" in client_log(caplog)  # retries were logged, without the text


@pytest.mark.parametrize(
    "mutate",
    [
        lambda b: b["positions"][0].update(quantity=BALANCE),
        lambda b: b["positions"][0].update(marketTitle=[TITLE]),
        lambda b: b["positions"][0]["lots"][0].update(quantity={TITLE: 1}),
        lambda b: b["summary"].update(totalMarketValue=TITLE),
        lambda b: b.update(positions={TITLE: BALANCE}),
    ],
)
def test_malformed_positions_values_are_not_repeated(mutate: Any, caplog: Any) -> None:
    body = fixture("tournament_positions")
    mutate(body)
    assert_clean(
        Router({POSITIONS: json_response(200, body)}),
        lambda c: c.read_tournament_positions(),
        SigResponseError,
        caplog,
    )


@pytest.mark.parametrize(
    ("change", "error_type"),
    [
        ({"myBalance": BALANCE}, SigResponseError),
        ({"isPendingEnrolment": TITLE}, SigResponseError),
        ({"slug": KEY}, SigContextMismatchError),
        ({"id": KEY}, SigResponseError),
    ],
)
def test_balance_read_errors_do_not_repeat_values(
    change: dict[str, Any], error_type: type[SigClientError], caplog: Any
) -> None:
    assert_clean(
        Router({TOURNAMENT_PATH: json_response(200, tournament_body(**change))}),
        lambda c: c.read_tournament_balance(),
        error_type,
        caplog,
    )


def test_orders_walk_errors_do_not_repeat_cursors_or_ids(caplog: Any) -> None:
    pages = {
        None: orders_page([1], cursor=CURSOR),
        CURSOR: orders_page([2], cursor=CURSOR),  # repeated cursor
    }
    assert_clean(
        Router({ORDERS: by_cursor(pages)}),
        lambda c: c.read_open_orders(page_limit=1),
        SigPaginationError,
        caplog,
    )
