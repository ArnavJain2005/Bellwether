"""S1-T3-B: GET /markets/{id} and GET /exchanges/{id}/trades.

Synthetic, spec-derived fixtures only (market.json, exchange_trades.json); neither
endpoint has a recorded response yet, so these check the client against the spec's
shapes, not against SIG's actual behaviour.
"""

import copy
import logging
from datetime import UTC, datetime, timedelta, timezone
from decimal import Decimal
from typing import Any

import httpx
import pytest
from sig_testkit import (
    API,
    TOURNAMENT,
    FakeClock,
    Router,
    error_body,
    fixture,
    json_response,
    make_client,
    run,
    secret_leaks,
)

from engine.sig_client.errors import (
    SigArgumentError,
    SigClientError,
    SigContextMismatchError,
    SigDeadlineExceededError,
    SigNotFoundError,
    SigPaginationError,
    SigResponseError,
    SigRetryExhaustedError,
    SigServiceUnavailableError,
)
from engine.sig_client.rest import TRADES_PAGE_MAX, ExchangeTradeHistory, ReadRetryPolicy

TID = TOURNAMENT.id
MARKET = f"{API}/markets/26"
TRADES = f"{API}/exchanges/36/trades"
TOURNAMENT_PATH = f"{API}/tournaments/alpha-tournament"
SINCE = datetime(2026, 4, 1, tzinfo=UTC)
UNTIL = datetime(2026, 4, 4, tzinfo=UTC)


def market_body() -> dict[str, Any]:
    return copy.deepcopy(fixture("market"))


def trades_body(**changes: Any) -> dict[str, Any]:
    body = copy.deepcopy(fixture("exchange_trades"))
    body.update(changes)
    return body


def trade(trade_id: str, created_at: str, **changes: Any) -> dict[str, Any]:
    item = {
        "id": trade_id,
        "createdAt": created_at,
        "price": 0.5,
        "size": 1,
        "side": "YES",
        "volume": 0.5,
    }
    item.update(changes)
    return item


def tape_page(
    trades: list[dict[str, Any]],
    *,
    cursor: str | None = None,
    coverage: Any = "default",
    **changes: Any,
) -> dict[str, Any]:
    body = trades_body(
        data=trades,
        pagination={"limit": 2, "hasMore": cursor is not None, "nextCursor": cursor},
        **changes,
    )
    if coverage is None:
        del body["coverage"]
    elif coverage != "default":
        body["coverage"] = coverage
    return body


def by_cursor(pages: dict[str | None, dict[str, Any]]) -> Any:
    def handler(request: httpx.Request) -> httpx.Response:
        return json_response(200, pages[request.url.params.get("cursor")])

    return handler


def requests_to(router: Router, path: str) -> list[httpx.Request]:
    return [r for r in router.requests if r.url.path == path]


# --- GET /markets/{id}: request and parsing ----------------------------------------------


def test_market_detail_is_scoped_and_parsed_exactly() -> None:
    client, router, _ = make_client()
    market = run(client.get_market("26"))
    assert router.paths() == ["/tournaments/alpha-tournament", "/markets/26"]
    request = router.requests[1]
    assert request.method == "GET"
    assert dict(request.url.params) == {"tournamentId": TID}
    assert market.id == "26" and market.status == "open"
    yes, no = market.exchanges
    assert yes.latestPrice == Decimal("0.42") and isinstance(yes.latestPrice, Decimal)
    assert yes.initialPrice == Decimal("0.5")
    assert no.latestPrice is None
    context = market.tournament_context()
    assert context.tournament is not None and context.tournament.slug == TOURNAMENT.slug
    assert [(e.id, e.latestPrice) for e in context.exchanges] == [
        ("36", Decimal("0.42")),
        ("37", None),
    ]
    assert market.settledWith is None and market.creator is not None
    assert market.creator.username is None


def test_market_detail_keeps_settlement_and_unbounded_initial_price() -> None:
    body = market_body()
    body["status"] = body["contexts"][0]["status"] = "settled"
    body["settledWith"] = body["contexts"][0]["settledWith"] = "YES"
    body["settledOn"] = body["contexts"][0]["settledOn"] = "2026-01-02T12:00:00.000Z"
    body["exchanges"][0]["initialPrice"] = 12.5  # no documented range
    body["creator"] = None
    body["unknownField"] = {"x": 1}
    client, _, _ = make_client(Router({MARKET: json_response(200, body)}))
    market = run(client.get_market("26"))
    assert market.status == "settled" and market.settledWith == "YES"
    assert market.tournament_context().settledOn == "2026-01-02T12:00:00.000Z"
    assert market.exchanges[0].initialPrice == Decimal("12.5")
    assert market.creator is None


@pytest.mark.parametrize("bad", ["", "abc", 26, None, "2 6", "26\n", "٢٦", "-1"])
def test_market_id_is_validated_before_any_request(bad: Any) -> None:
    client, router, _ = make_client()
    with pytest.raises(SigArgumentError):
        run(client.get_market(bad))
    assert router.requests == []


@pytest.mark.parametrize(
    ("mutate", "field"),
    [
        (lambda b: b.pop("thumbnailUrl"), "thumbnailUrl"),
        (lambda b: b.pop("contexts"), "contexts"),
        (lambda b: b.update(status="resolved"), "status"),
        (lambda b: b["exchanges"][0].update(latestPrice=1.5), "latestPrice"),
        (lambda b: b["exchanges"][0].update(latestPrice="0.42"), "latestPrice"),
        (lambda b: b["exchanges"][0].update(latestPrice=True), "latestPrice"),
        (lambda b: b["contexts"][0]["exchanges"][0].update(latestPrice=-0.1), "latestPrice"),
        (lambda b: b["contexts"][0].pop("status"), "status"),
        (lambda b: b.update(isComposite=0), "isComposite"),
    ],
)
def test_market_detail_is_parsed_strictly(mutate: Any, field: str) -> None:
    body = market_body()
    mutate(body)
    client, _, _ = make_client(Router({MARKET: json_response(200, body)}))
    with pytest.raises(SigResponseError, match=field):
        run(client.get_market("26"))


def test_market_not_found_is_raised_without_retry() -> None:
    client, router, _ = make_client(Router({MARKET: json_response(404, error_body("NOT_FOUND"))}))
    with pytest.raises(SigNotFoundError):
        run(client.get_market("26"))
    assert router.count("/markets/26") == 1


# --- GET /markets/{id}: identity and scope ------------------------------------------------


def test_market_detail_for_another_market_is_rejected() -> None:
    body = market_body()
    body["id"] = "27"
    client, _, _ = make_client(Router({MARKET: json_response(200, body)}))
    with pytest.raises(SigResponseError, match="different market"):
        run(client.get_market("26"))


def other_tournament(body: dict[str, Any]) -> None:
    body["contexts"][0]["tournament"]["id"] = "11111111-2222-4333-8444-555555555555"


def other_slug(body: dict[str, Any]) -> None:
    body["contexts"][0]["tournament"]["slug"] = "beta-tournament"


def public_context(body: dict[str, Any]) -> None:
    body["contexts"][0].update(type="public", tournament=None)


def two_contexts(body: dict[str, Any]) -> None:
    body["contexts"].append(copy.deepcopy(body["contexts"][0]))


def no_context(body: dict[str, Any]) -> None:
    body["contexts"] = []


@pytest.mark.parametrize(
    "mutate", [other_tournament, other_slug, public_context, two_contexts, no_context]
)
def test_market_detail_must_hold_exactly_the_configured_tournament(mutate: Any) -> None:
    body = market_body()
    mutate(body)
    client, _, _ = make_client(Router({MARKET: json_response(200, body)}))
    with pytest.raises(SigContextMismatchError):
        run(client.get_market("26"))


def test_market_context_uuid_is_compared_case_insensitively() -> None:
    body = market_body()
    body["contexts"][0]["tournament"]["id"] = TID.upper()
    client, _, _ = make_client(Router({MARKET: json_response(200, body)}))
    assert run(client.get_market("26")).id == "26"


def test_market_context_with_a_non_uuid_id_is_malformed() -> None:
    body = market_body()
    body["contexts"][0]["tournament"]["id"] = "not-a-uuid"
    client, _, _ = make_client(Router({MARKET: json_response(200, body)}))
    with pytest.raises(SigResponseError, match="canonical UUID"):
        run(client.get_market("26"))


@pytest.mark.parametrize(
    ("mutate", "reason"),
    [
        (lambda b: b.update(status="closed"), "status or settlement"),
        (lambda b: b["contexts"][0].update(settledWith="NO"), "status or settlement"),
        (lambda b: b["contexts"][0].update(settledOn="2026-01-02T12:00:00.000Z"), "settlement"),
        (lambda b: b["contexts"][0]["exchanges"].pop(), "differ from the tournament"),
        (lambda b: b["contexts"][0]["exchanges"][1].update(id="38"), "differ from the tournament"),
        (
            lambda b: b["contexts"][0]["exchanges"].append({"id": "36", "latestPrice": 0.42}),
            "duplicate",
        ),
        (
            lambda b: b["exchanges"].append(copy.deepcopy(b["exchanges"][0])),
            "duplicate",
        ),
        (lambda b: b["contexts"][0]["exchanges"][0].update(latestPrice=0.43), "latestPrice"),
        (lambda b: b["contexts"][0]["exchanges"][1].update(latestPrice=0.5), "latestPrice"),
    ],
)
def test_market_top_level_must_agree_with_the_tournament_context(mutate: Any, reason: str) -> None:
    body = market_body()
    mutate(body)
    client, _, _ = make_client(Router({MARKET: json_response(200, body)}))
    with pytest.raises(SigResponseError, match=reason):
        run(client.get_market("26"))


def test_market_prices_compare_by_value_not_spelling() -> None:
    body = market_body()
    body["contexts"][0]["exchanges"][0]["latestPrice"] = 0.420
    raw = json_response(200, body).content.replace(
        b'"latestPrice": 0.42}', b'"latestPrice": 0.420}'
    )
    client, _, _ = make_client(Router({MARKET: httpx.Response(200, content=raw)}))
    market = run(client.get_market("26"))
    assert str(market.tournament_context().exchanges[0].latestPrice) == "0.420"
    assert str(market.exchanges[0].latestPrice) == "0.42"


# --- GET /exchanges/{id}/trades: request and parsing ---------------------------------------


def test_trade_page_defaults_and_exact_values() -> None:
    client, router, _ = make_client()
    page = run(client.list_exchange_trades_page("36"))
    assert router.paths() == ["/tournaments/alpha-tournament", "/exchanges/36/trades"]
    assert dict(router.requests[1].url.params) == {"tournamentId": TID, "limit": "200"}
    assert TRADES_PAGE_MAX == 200
    first, second = page.data
    assert (first.id, first.side, first.price, first.size, first.volume) == (
        "98342",
        "YES",
        Decimal("0.42"),
        Decimal(100),
        Decimal(42),
    )
    assert isinstance(first.size, Decimal)
    assert second.price is None and second.volume is None and second.side == "NO"
    assert page.from_ == "1970-01-01T00:00:00.000Z" and page.to == "2026-04-04T00:00:00.000Z"
    assert page.coverage is not None
    assert page.coverage.complete is True
    assert page.coverage.projectedThroughSequence == 184233


def test_trade_page_sends_window_limit_and_cursor() -> None:
    body = trades_body(**{"from": "2026-04-01T00:00:00.000Z"})
    client, router, _ = make_client(Router({TRADES: json_response(200, body)}))
    ist = timezone(timedelta(hours=5, minutes=30))
    run(
        client.list_exchange_trades_page(
            "36",
            since=datetime(2026, 4, 1, 5, 30, tzinfo=ist),
            until=datetime(2026, 4, 4, 0, 0, 0, 0, tzinfo=UTC),
            limit=2,
            cursor="opaque/+=",
        )
    )
    assert dict(router.requests[1].url.params) == {
        "tournamentId": TID,
        "limit": "2",
        "from": "2026-04-01T00:00:00.000Z",
        "to": "2026-04-04T00:00:00.000Z",
        "cursor": "opaque/+=",
    }


def test_window_milliseconds_are_sent_exactly() -> None:
    since = datetime(2026, 4, 3, 18, 20, 0, 7000, tzinfo=UTC)
    body = trades_body(**{"from": "2026-04-03T18:20:00.007Z"})
    body["data"] = body["data"][:1]
    client, router, _ = make_client(Router({TRADES: json_response(200, body)}))
    run(client.list_exchange_trades_page("36", since=since))
    assert router.requests[1].url.params["from"] == "2026-04-03T18:20:00.007Z"


@pytest.mark.parametrize(
    "kwargs",
    [
        {"since": datetime(2026, 4, 1)},  # noqa: DTZ001 (naive on purpose)
        {"until": datetime(2026, 4, 1)},  # noqa: DTZ001
        {"since": datetime(2026, 4, 1, 0, 0, 0, 1, tzinfo=UTC)},  # sub-millisecond
        {"since": "2026-04-01T00:00:00.000Z"},
        {"since": 0},
        {"since": UNTIL, "until": UNTIL},
        {"since": UNTIL, "until": SINCE},
        {"limit": 0},
        {"limit": TRADES_PAGE_MAX + 1},
        {"limit": True},
        {"limit": 2.0},
        {"cursor": ""},
        {"cursor": 5},
    ],
)
def test_trade_page_arguments_are_validated_before_any_request(kwargs: dict[str, Any]) -> None:
    client, router, _ = make_client()
    with pytest.raises(SigArgumentError):
        run(client.list_exchange_trades_page("36", **kwargs))
    assert router.requests == []


@pytest.mark.parametrize("bad", ["", "x36", 36, "3 6", "٣٦"])
def test_trade_exchange_id_is_validated_before_any_request(bad: Any) -> None:
    client, router, _ = make_client()
    with pytest.raises(SigArgumentError):
        run(client.list_exchange_trades_page(bad))
    with pytest.raises(SigArgumentError):
        run(client.list_all_exchange_trades(bad))
    assert router.requests == []


def test_window_offsets_that_overflow_utc_are_argument_errors() -> None:
    client, router, _ = make_client()
    edge = datetime.max.replace(tzinfo=timezone(timedelta(hours=-1)))
    with pytest.raises(SigArgumentError):
        run(client.list_exchange_trades_page("36", until=edge))
    assert router.requests == []


def test_integral_size_is_kept_as_sent() -> None:
    body = trades_body()
    raw = json_response(200, body).content.replace(b'"size": 100', b'"size": 100.0')
    client, _, _ = make_client(Router({TRADES: httpx.Response(200, content=raw)}))
    page = run(client.list_exchange_trades_page("36"))
    assert str(page.data[0].size) == "100.0"


@pytest.mark.parametrize(
    ("mutate", "field"),
    [
        (lambda b: b["data"][0].update(size=1.5), "size"),
        (lambda b: b["data"][0].update(size=-1), "size"),
        (lambda b: b["data"][0].update(size=True), "size"),
        (lambda b: b["data"][0].update(size="100"), "size"),
        (lambda b: b["data"][0].update(side="yes"), "side"),
        (lambda b: b["data"][0].update(price=1.01), "price"),
        (lambda b: b["data"][0].update(price=False), "price"),
        (lambda b: b["data"][0].update(volume="42"), "volume"),
        (lambda b: b["data"][0].pop("volume"), "volume"),
        (lambda b: b["data"][0].update(id=98342), "id"),
        (lambda b: b.pop("from"), "from"),
        (lambda b: b.pop("marketId"), "marketId"),
        (lambda b: b["pagination"].pop("nextCursor"), "nextCursor"),
        (lambda b: b["coverage"].update(complete="true"), "complete"),
        (lambda b: b["coverage"].update(complete=1), "complete"),
        (lambda b: b["coverage"].pop("projectedThroughSequence"), "projectedThroughSequence"),
        (lambda b: b["coverage"].update(projectedThroughSequence=1.5), "projectedThrough"),
        (lambda b: b["coverage"].update(projectedThroughSequence="184233"), "projectedThrough"),
    ],
)
def test_trade_page_is_parsed_strictly(mutate: Any, field: str) -> None:
    body = trades_body()
    mutate(body)
    client, _, _ = make_client(Router({TRADES: json_response(200, body)}))
    with pytest.raises(SigResponseError, match=field):
        run(client.list_exchange_trades_page("36"))


@pytest.mark.parametrize(
    ("coverage", "expected"),
    [
        (None, None),  # optional: absent stays absent
        ({"complete": True, "projectedThroughSequence": None}, (True, None)),
        ({"complete": True, "projectedThroughSequence": 0}, (True, 0)),
        ({"complete": True, "projectedThroughSequence": 184233}, (True, 184233)),
    ],
)
def test_coverage_metadata_is_preserved_exactly(coverage: Any, expected: Any) -> None:
    body = trades_body()
    if coverage is None:
        del body["coverage"]
    else:
        body["coverage"] = coverage
    client, _, _ = make_client(Router({TRADES: json_response(200, body)}))
    page = run(client.list_exchange_trades_page("36"))
    if expected is None:
        assert page.coverage is None
    else:
        assert page.coverage is not None
        assert (page.coverage.complete, page.coverage.projectedThroughSequence) == expected


def test_coverage_reporting_truncation_is_rejected() -> None:
    body = trades_body(coverage={"complete": False, "projectedThroughSequence": 1})
    client, _, _ = make_client(Router({TRADES: json_response(200, body)}))
    with pytest.raises(SigResponseError, match=r"coverage\.complete is false"):
        run(client.list_exchange_trades_page("36"))


# `coverage` is optional (not in `required`) but its schema is a plain object, not
# nullable: omitted is allowed, an explicit null is not. Inside a present object,
# `projectedThroughSequence` is documented as integer or null.


def test_omitted_coverage_is_none() -> None:
    body = trades_body()
    del body["coverage"]
    client, _, _ = make_client(Router({TRADES: json_response(200, body)}))
    assert run(client.list_exchange_trades_page("36")).coverage is None


def test_explicit_null_coverage_is_rejected(caplog: pytest.LogCaptureFixture) -> None:
    body = trades_body(coverage=None)
    body["marketId"] = CODE_LIKE_KEY  # response text must not reach the error
    error = leak_free(
        Router({TRADES: json_response(200, body)}),
        lambda c: c.list_exchange_trades_page("36"),
        SigResponseError,
        caplog,
    )
    assert "coverage" in str(error)


def test_explicit_null_coverage_on_a_later_page_fails_the_tape() -> None:
    pages = three_pages()
    pages["c2"]["coverage"] = None
    client, _, _ = make_client(Router({TRADES: by_cursor(pages)}))
    with pytest.raises(SigResponseError, match="coverage"):
        run(client.list_all_exchange_trades("36", page_limit=2))


def test_coverage_object_with_a_null_checkpoint_is_kept() -> None:
    body = trades_body(coverage={"complete": True, "projectedThroughSequence": None})
    client, _, _ = make_client(Router({TRADES: json_response(200, body)}))
    coverage = run(client.list_exchange_trades_page("36")).coverage
    assert coverage is not None
    assert coverage.complete is True and coverage.projectedThroughSequence is None


# --- GET /exchanges/{id}/trades: identity, window and order --------------------------------


def test_trade_tape_for_another_exchange_is_rejected() -> None:
    body = trades_body(exchangeId="37")
    client, _, _ = make_client(Router({TRADES: json_response(200, body)}))
    with pytest.raises(SigResponseError, match="different exchange"):
        run(client.list_exchange_trades_page("36"))


@pytest.mark.parametrize(
    ("changes", "kwargs", "reason"),
    [
        ({"from": "2026-04-01T00:00:00.001Z"}, {"since": SINCE}, "echoed from differs"),
        ({"to": "2026-04-05T00:00:00.000Z"}, {"until": UNTIL}, "echoed to differs"),
        ({"from": "2026-04-05T00:00:00.000Z"}, {}, "from is later than"),
        ({"from": "2026-04-01"}, {}, "from is not an ISO-8601"),
        ({"to": "2026-04-04T00:00:00.000"}, {}, "to is not an ISO-8601"),
    ],
)
def test_echoed_window_must_match_the_request(
    changes: dict[str, Any], kwargs: dict[str, Any], reason: str
) -> None:
    body = trades_body(**changes)
    body["data"] = []
    client, _, _ = make_client(Router({TRADES: json_response(200, body)}))
    with pytest.raises(SigResponseError, match=reason):
        run(client.list_exchange_trades_page("36", **kwargs))


def test_echoed_window_is_compared_as_an_instant() -> None:
    body = trades_body(**{"from": "2026-04-01T05:30:00+05:30", "to": "2026-04-04T00:00:00Z"})
    client, _, _ = make_client(Router({TRADES: json_response(200, body)}))
    page = run(client.list_exchange_trades_page("36", since=SINCE, until=UNTIL))
    assert page.from_ == "2026-04-01T05:30:00+05:30"  # kept as sent


@pytest.mark.parametrize(
    ("trades", "reason"),
    [
        (
            [trade("1", "2026-04-03T00:00:00.000Z"), trade("2", "2026-04-03T00:00:00.001Z")],
            "newest first",
        ),
        ([trade("1", "2026-04-04T00:00:00.000Z")], "outside"),  # `to` is exclusive
        ([trade("1", "1969-12-31T23:59:59.999Z")], "outside"),
        ([trade("1", "yesterday")], "createdAt is not an ISO-8601"),
    ],
)
def test_trades_must_be_newest_first_and_inside_the_window(
    trades: list[dict[str, Any]], reason: str
) -> None:
    client, _, _ = make_client(Router({TRADES: json_response(200, trades_body(data=trades))}))
    with pytest.raises(SigResponseError, match=reason):
        run(client.list_exchange_trades_page("36"))


def test_window_start_is_inclusive_and_equal_times_are_allowed() -> None:
    trades = [trade("2", "1970-01-01T00:00:00.000Z"), trade("1", "1970-01-01T00:00:00.000Z")]
    trades.reverse()
    client, _, _ = make_client(Router({TRADES: json_response(200, trades_body(data=trades))}))
    page = run(client.list_exchange_trades_page("36"))
    assert [t.id for t in page.data] == ["1", "2"]


def test_trade_page_larger_than_limit_is_rejected() -> None:
    client, _, _ = make_client()
    with pytest.raises(SigResponseError, match="limit was 1"):
        run(client.list_exchange_trades_page("36", limit=1))


# --- the whole tape ----------------------------------------------------------------------


def three_pages() -> dict[str | None, dict[str, Any]]:
    return {
        None: tape_page(
            [trade("6", "2026-04-03T06:00:00.000Z"), trade("5", "2026-04-03T05:00:00.000Z")],
            cursor="c2",
            coverage={"complete": True, "projectedThroughSequence": 900},
        ),
        "c2": tape_page(
            [trade("4", "2026-04-03T05:00:00.000Z"), trade("3", "2026-04-03T03:00:00.000Z")],
            cursor="c3",
            coverage=None,
        ),
        "c3": tape_page(
            [trade("2", "2026-04-03T02:00:00.000Z")],
            coverage={"complete": True, "projectedThroughSequence": None},
        ),
    }


def test_whole_tape_follows_cursors_and_keeps_coverage_per_page() -> None:
    client, router, _ = make_client(Router({TRADES: by_cursor(three_pages())}))
    history = run(client.list_all_exchange_trades("36", until=UNTIL, page_limit=2))
    assert isinstance(history, ExchangeTradeHistory)
    assert [t.id for t in history.trades] == ["6", "5", "4", "3", "2"]
    assert (history.exchange_id, history.market_id) == ("36", "26")
    assert history.window_from == "1970-01-01T00:00:00.000Z"
    assert history.window_to == "2026-04-04T00:00:00.000Z"
    first, second, third = history.page_coverage
    assert first is not None and first.projectedThroughSequence == 900
    assert second is None  # absent on that page, not filled in from another
    assert third is not None and third.projectedThroughSequence is None
    sent = requests_to(router, TRADES)
    assert [r.url.params.get("cursor") for r in sent] == [None, "c2", "c3"]
    for request in sent:
        assert request.url.params["tournamentId"] == TID
        assert request.url.params["limit"] == "2"
        assert request.url.params["to"] == "2026-04-04T00:00:00.000Z"
        assert "from" not in request.url.params


def test_projection_checkpoints_are_not_reconciled_across_pages() -> None:
    pages = three_pages()
    pages["c2"]["coverage"] = {"complete": True, "projectedThroughSequence": 5}  # went back
    client, _, _ = make_client(Router({TRADES: by_cursor(pages)}))
    history = run(client.list_all_exchange_trades("36", page_limit=2))
    assert [c.projectedThroughSequence if c else c for c in history.page_coverage] == [
        900,
        5,
        None,
    ]


def test_single_empty_page_is_a_complete_empty_tape() -> None:
    body = tape_page([])
    client, _, _ = make_client(Router({TRADES: json_response(200, body)}))
    history = run(client.list_all_exchange_trades("36"))
    assert history.trades == () and len(history.page_coverage) == 1


def test_repeated_cursor_fails_the_tape() -> None:
    pages = three_pages()
    pages["c3"]["pagination"] = {"limit": 2, "hasMore": True, "nextCursor": "c2"}
    client, _, _ = make_client(Router({TRADES: by_cursor(pages)}))
    with pytest.raises(SigPaginationError, match="repeated"):
        run(client.list_all_exchange_trades("36", page_limit=2))


def test_duplicate_trade_across_pages_fails_the_tape() -> None:
    pages = three_pages()
    pages["c2"]["data"][0]["id"] = "5"
    client, _, _ = make_client(Router({TRADES: by_cursor(pages)}))
    with pytest.raises(SigPaginationError, match="more than one page"):
        run(client.list_all_exchange_trades("36", page_limit=2))


def test_page_cap_fails_instead_of_returning_a_partial_tape() -> None:
    client, router, _ = make_client(Router({TRADES: by_cursor(three_pages())}))
    with pytest.raises(SigPaginationError, match="more than 2 pages"):
        run(client.list_all_exchange_trades("36", page_limit=2, max_pages=2))
    assert len(requests_to(router, TRADES)) == 2


@pytest.mark.parametrize(
    ("mutate", "reason"),
    [
        (lambda p: p["c2"].update(marketId="27"), "different market"),
        (lambda p: p["c3"].update(to="2026-04-04T00:00:00.001Z"), "different from/to window"),
        (lambda p: p["c2"].update(**{"from": "1970-01-01T00:00:00.001Z"}), "window"),
        (lambda p: p["c2"]["data"][0].update(createdAt="2026-04-03T05:00:00.001Z"), "newer"),
        (
            lambda p: p["c3"]["pagination"].update(nextCursor="dangling"),
            "hasMore is false but nextCursor",
        ),
        (lambda p: p["c2"]["pagination"].update(nextCursor=None), "nextCursor is null"),
    ],
)
def test_inconsistent_pages_fail_the_tape(mutate: Any, reason: str) -> None:
    pages = three_pages()
    mutate(pages)
    client, _, _ = make_client(Router({TRADES: by_cursor(pages)}))
    with pytest.raises(SigPaginationError, match=reason):
        run(client.list_all_exchange_trades("36", page_limit=2))


def test_window_moving_between_pages_is_compared_as_an_instant() -> None:
    pages = three_pages()
    pages["c2"]["to"] = "2026-04-04T05:30:00+05:30"  # same instant, other spelling
    client, _, _ = make_client(Router({TRADES: by_cursor(pages)}))
    history = run(client.list_all_exchange_trades("36", page_limit=2))
    assert len(history.trades) == 5


def test_truncated_coverage_on_a_later_page_fails_the_tape() -> None:
    pages = three_pages()
    pages["c3"]["coverage"] = {"complete": False, "projectedThroughSequence": 1}
    client, _, _ = make_client(Router({TRADES: by_cursor(pages)}))
    with pytest.raises(SigResponseError, match="coverage"):
        run(client.list_all_exchange_trades("36", page_limit=2))


@pytest.mark.parametrize("kwargs", [{"max_pages": 0}, {"page_limit": 201}, {"since": SINCE.date()}])
def test_whole_tape_arguments_are_validated_before_any_request(kwargs: dict[str, Any]) -> None:
    client, router, _ = make_client()
    with pytest.raises(SigArgumentError):
        run(client.list_all_exchange_trades("36", **kwargs))
    assert router.requests == []


# --- retries, deadlines and redaction ------------------------------------------------------


def test_reporting_unavailable_is_retried_as_a_transient() -> None:
    unavailable = json_response(503, error_body("SERVICE_UNAVAILABLE"), {"Retry-After": "2"})
    ok = json_response(200, trades_body())
    client, router, clock = make_client(Router({TRADES: [unavailable, ok]}))
    page = run(client.list_exchange_trades_page("36"))
    assert len(page.data) == 2
    assert len(requests_to(router, TRADES)) == 2
    assert clock.sleeps == [2.0]


def test_reporting_unavailable_until_attempts_run_out_is_raised() -> None:
    unavailable = json_response(503, error_body("SERVICE_UNAVAILABLE"))
    retry = ReadRetryPolicy(max_attempts=2)
    client, router, _ = make_client(Router({TRADES: [unavailable]}), retry=retry)
    with pytest.raises(SigRetryExhaustedError) as info:
        run(client.list_exchange_trades_page("36"))
    assert isinstance(info.value.last_error, SigServiceUnavailableError)
    assert len(requests_to(router, TRADES)) == 2


def test_a_late_trade_page_is_not_returned() -> None:
    clock = FakeClock()

    def late(request: httpx.Request) -> httpx.Response:
        clock.now += 31  # the whole 30 s budget passed while waiting
        return json_response(200, trades_body())

    client, _, _ = make_client(Router({TRADES: late}), clock=clock)
    with pytest.raises(SigDeadlineExceededError) as info:
        run(client.list_exchange_trades_page("36"))
    assert info.value.phase == "request"


def test_a_late_market_detail_is_not_returned() -> None:
    clock = FakeClock()

    def late(request: httpx.Request) -> httpx.Response:
        clock.now += 31
        return json_response(200, market_body())

    client, _, _ = make_client(Router({MARKET: late}), clock=clock)
    with pytest.raises(SigDeadlineExceededError):
        run(client.get_market("26"))


def test_each_tape_page_has_its_own_deadline_and_a_late_page_fails_the_tape() -> None:
    clock = FakeClock()
    pages = three_pages()

    def slow(request: httpx.Request) -> httpx.Response:
        cursor = request.url.params.get("cursor")
        clock.now += 31 if cursor == "c3" else 20
        return json_response(200, pages[cursor])

    client, router, _ = make_client(Router({TRADES: slow}), clock=clock)
    with pytest.raises(SigDeadlineExceededError):
        run(client.list_all_exchange_trades("36", page_limit=2))
    # Two pages took 40 s in all, beyond one budget: the budget is per page.
    assert len(requests_to(router, TRADES)) == 3


CODE_LIKE_KEY = "DUMMY_SENTINEL_CODE"  # test placeholder, never a credential


def leak_free(
    router: Router, call: Any, error_type: type[SigClientError], caplog: pytest.LogCaptureFixture
) -> SigClientError:
    client, _, _ = make_client(router, api_key=CODE_LIKE_KEY)
    caplog.set_level(logging.DEBUG)
    with pytest.raises(error_type) as info:
        run(call(client))
    assert secret_leaks(info.value, CODE_LIKE_KEY) == []
    assert CODE_LIKE_KEY not in caplog.text
    return info.value


@pytest.mark.parametrize(
    "mutate",
    [
        lambda b: b.update(id=CODE_LIKE_KEY),
        lambda b: b["contexts"][0]["tournament"].update(slug=CODE_LIKE_KEY),
        lambda b: b["contexts"][0]["exchanges"][0].update(id=CODE_LIKE_KEY),
        lambda b: b.update(status=CODE_LIKE_KEY),
    ],
)
def test_market_errors_carry_no_response_values(
    mutate: Any, caplog: pytest.LogCaptureFixture
) -> None:
    body = market_body()
    mutate(body)
    leak_free(
        Router({MARKET: json_response(200, body)}),
        lambda c: c.get_market("26"),
        SigResponseError,
        caplog,
    )


@pytest.mark.parametrize(
    ("mutate", "error_type"),
    [
        (lambda p: p[None].update(exchangeId=CODE_LIKE_KEY), SigResponseError),
        (lambda p: p["c2"].update(marketId=CODE_LIKE_KEY), SigPaginationError),
        (lambda p: p["c2"].update(to=CODE_LIKE_KEY), SigResponseError),
        (lambda p: p["c2"]["data"][0].update(createdAt=CODE_LIKE_KEY), SigResponseError),
        (lambda p: p["c2"]["data"][0].update(side=CODE_LIKE_KEY), SigResponseError),
        (lambda p: p["c2"]["data"][0].update(id="6"), SigPaginationError),
        (lambda p: p["c2"]["pagination"].update(nextCursor="c2"), SigPaginationError),
    ],
)
def test_trade_tape_errors_carry_no_response_values(
    mutate: Any, error_type: type[SigClientError], caplog: pytest.LogCaptureFixture
) -> None:
    pages = three_pages()
    pages["c2"]["pagination"]["nextCursor"] = "c3"
    mutate(pages)
    error = leak_free(
        Router({TRADES: by_cursor(pages)}),
        lambda c: c.list_all_exchange_trades("36", page_limit=2),
        error_type,
        caplog,
    )
    assert "98342" not in str(error) and "26" not in str(error).replace("/26", "")


def test_api_errors_on_the_tape_keep_only_documented_codes(
    caplog: pytest.LogCaptureFixture,
) -> None:
    body = {"error": {"code": CODE_LIKE_KEY, "message": CODE_LIKE_KEY, "details": {"x": ["y"]}}}
    error = leak_free(
        Router({TRADES: json_response(400, body)}),
        lambda c: c.list_exchange_trades_page("36"),
        SigClientError,
        caplog,
    )
    assert getattr(error, "code", "missing") is None
    assert getattr(error, "unrecognized_code", False) is True


def test_tournament_mismatch_blocks_both_reads() -> None:
    body = copy.deepcopy(fixture("tournament"))
    body["slug"] = "beta-tournament"
    client, router, _ = make_client(Router({TOURNAMENT_PATH: json_response(200, body)}))

    async def scenario() -> None:
        with pytest.raises(SigContextMismatchError):
            await client.get_market("26")
        with pytest.raises(SigContextMismatchError):
            await client.list_exchange_trades_page("36")

    run(scenario())
    assert router.count("/markets/26") == 0 and router.count("/exchanges/36/trades") == 0
