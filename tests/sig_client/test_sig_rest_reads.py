import copy
from decimal import Decimal
from typing import Any

import pytest
from sig_testkit import (
    API,
    TOURNAMENT,
    Router,
    fixture,
    json_response,
    make_client,
    raw_response,
    run,
)

from engine.config import TournamentRef
from engine.sig_client.errors import (
    SigArgumentError,
    SigContextMismatchError,
    SigResponseError,
)
from engine.sig_client.rest import SigRestClient

TID = TOURNAMENT.id


def params(router: Router, path: str) -> dict[str, str]:
    request = next(r for r in router.requests if r.url.path == API + path)
    return dict(request.url.params)


def serve(path: str, body: Any, status: int = 200) -> Router:
    return Router({API + path: json_response(status, body)})


# --- endpoints, parameters, scoping ------------------------------------------------------


def test_tournament_read_and_echo_check() -> None:
    client, router, _ = make_client()
    tournament = run(client.get_tournament())
    assert router.paths() == ["/tournaments/alpha-tournament"]
    assert router.requests[0].method == "GET"
    assert tournament.id == TID
    assert tournament.myBalance == Decimal("847.5")
    assert tournament.description is None and tournament.endDate is None


def test_tournament_is_verified_once_before_scoped_reads() -> None:
    client, router, _ = make_client()

    async def scenario() -> None:
        await client.get_exchange_prices(["36", "37", "999999999"])
        await client.get_exchange_orderbook("36")
        await client.get_market_orderbook("26")
        await client.list_orders_page()
        await client.list_tournament_markets_page()

    run(scenario())
    assert router.paths() == [
        "/tournaments/alpha-tournament",
        "/exchanges/prices",
        "/exchanges/36/orderbook",
        "/markets/26/orderbook",
        "/orders",
        "/tournaments/alpha-tournament/markets",
    ]


def test_every_uuid_scoped_read_sends_the_configured_tournament_id() -> None:
    client, router, _ = make_client()

    async def scenario() -> None:
        await client.get_exchange_prices(["36", "37", "999999999"])
        await client.get_exchange_orderbook("36", depth=200)
        await client.get_market_orderbook("26", depth=5)
        await client.list_orders_page(status="all", exchange_id="36", limit=10, cursor="c")

    run(scenario())
    assert params(router, "/exchanges/prices") == {"ids": "36,37,999999999", "tournamentId": TID}
    assert params(router, "/exchanges/36/orderbook") == {"depth": "200", "tournamentId": TID}
    assert params(router, "/markets/26/orderbook") == {"depth": "5", "tournamentId": TID}
    assert params(router, "/orders") == {
        "status": "all",
        "limit": "10",
        "tournamentId": TID,
        "exchangeId": "36",
        "cursor": "c",
    }


def test_markets_page_parameters() -> None:
    client, router, _ = make_client()
    page = run(client.list_tournament_markets_page(limit=1, status="open", search="Senate"))
    assert params(router, "/tournaments/alpha-tournament/markets") == {
        "limit": "1",
        "status": "open",
        "search": "Senate",
    }
    assert page.pagination.nextCursor == "cursor-page-2"
    assert page.data[0].creator is not None and page.data[0].creator.username is None


@pytest.mark.parametrize(
    ("slug", "tid"),
    [
        ("alpha-tournament", "11111111-2222-4333-8444-555555555555"),
        ("other-tournament", TID),
    ],
)
def test_slug_uuid_mismatch_is_permanent_and_blocks_scoped_reads(slug: str, tid: str) -> None:
    configured = TournamentRef(slug=slug, id=tid)
    router = Router({f"{API}/tournaments/{slug}": json_response(200, fixture("tournament"))})
    client, router, _ = make_client(router, tournament=configured)

    async def scenario() -> None:
        with pytest.raises(SigContextMismatchError):
            await client.get_exchange_orderbook("36")
        with pytest.raises(SigContextMismatchError):
            await client.list_orders_page()
        with pytest.raises(SigContextMismatchError):
            await client.get_tournament()

    run(scenario())
    assert router.paths() == [f"/tournaments/{slug}"]  # one read, then refused locally


def test_tournament_id_that_is_not_a_uuid_is_malformed() -> None:
    body = fixture("tournament") | {"id": "not-a-uuid"}
    client, _, _ = make_client(serve("/tournaments/alpha-tournament", body))
    with pytest.raises(SigResponseError):
        run(client.get_tournament())


def test_uppercase_echoed_uuid_is_the_same_tournament() -> None:
    body = fixture("tournament") | {"id": TID.upper()}
    client, _, _ = make_client(serve("/tournaments/alpha-tournament", body))
    assert run(client.get_tournament()).id == TID.upper()


def market_book(mutate: Any) -> dict[str, Any]:
    body = copy.deepcopy(fixture("market_orderbook"))
    mutate(body)
    return body


@pytest.mark.parametrize(
    "mutate",
    [
        lambda b: b["contexts"][0]["tournament"].update(id="11111111-2222-4333-8444-555555555555"),
        lambda b: b["contexts"][0]["tournament"].update(slug="other"),
        lambda b: b["contexts"][0].update(type="public", tournament=None),
        lambda b: b["contexts"].append(copy.deepcopy(b["contexts"][0])),
        lambda b: b.update(contexts=[]),
    ],
)
def test_market_orderbook_context_must_be_the_configured_tournament(mutate: Any) -> None:
    client, _, _ = make_client(serve("/markets/26/orderbook", market_book(mutate)))
    with pytest.raises(SigContextMismatchError):
        run(client.get_market_orderbook("26"))


# --- wire values -------------------------------------------------------------------------


def test_prices_are_exact_decimals_and_nulls_are_preserved() -> None:
    client, _, _ = make_client()
    book = run(client.get_exchange_orderbook("36"))
    assert [level.price for level in book.asks] == [Decimal("0.535"), Decimal("0.55")]
    assert book.bids[1].quantity == Decimal("12.5")
    assert book.spread == Decimal("0.125")
    assert book.asOf is not None and book.asOf.sequence == 4817
    assert book.asOf.at == "2026-09-28T09:15:02.125+00:00"  # kept as sent

    market = run(client.get_market_orderbook("26"))
    scoped = market.tournament_orderbook()
    assert scoped.exchanges[0].asOf is None
    assert scoped.overround is None
    assert market.overround == Decimal("1.05")


def test_bulk_prices_keep_nulls_and_missing_ids() -> None:
    client, _, _ = make_client()
    prices = run(client.get_exchange_prices(["36", "37", "999999999"]))
    assert prices.missingIds == ["999999999"]
    assert prices.data[0].bestAsk == Decimal("0.55")
    assert prices.data[1].latestPrice is None and prices.data[1].spread is None
    assert prices.data[1].option is None


def test_orders_page_wire_values() -> None:
    client, _, _ = make_client()
    page = run(client.list_orders_page())
    first, second = page.data
    assert first.priceLimit == Decimal("0.42") and first.quantity == Decimal(100)
    assert second.priceLimit is None and second.quantity == Decimal("7.5")
    assert second.expirationDate == "2025-06-15T00:00:00.000Z"
    assert page.coverage is not None and page.coverage.projectedThroughSequence == 184233


def test_orders_coverage_is_optional_but_complete_must_be_true() -> None:
    body = fixture("orders_page")
    del body["coverage"]
    client, _, _ = make_client(serve("/orders", body))
    assert run(client.list_orders_page()).coverage is None

    body = fixture("orders_page")
    body["coverage"]["complete"] = False
    client, _, _ = make_client(serve("/orders", body))
    with pytest.raises(SigResponseError):
        run(client.list_orders_page())


def exchange_book(change: dict[str, Any]) -> dict[str, Any]:
    return fixture("exchange_orderbook") | change


@pytest.mark.parametrize(
    "change",
    [
        {"bestBid": "0.41"},  # number as string
        {"bestBid": True},  # boolean as number
        {"bestBid": 1.5},  # outside documented [0, 1]
        {"depth": 20.0},  # integer field as float
        {"depth": "20"},
        {"exchangeId": 36},  # id as number
        {"asOf": {"sequence": 4817}},  # BookVersion missing `at`
        {"bids": [{"price": 0.41}]},  # level missing quantity
        {"bids": None},
    ],
)
def test_malformed_values_are_rejected(change: dict[str, Any]) -> None:
    client, _, _ = make_client(serve("/exchanges/36/orderbook", exchange_book(change)))
    with pytest.raises(SigResponseError):
        run(client.get_exchange_orderbook("36"))


@pytest.mark.parametrize("field", ["asOf", "bestBid", "spread", "marketId"])
def test_missing_required_field_is_rejected_not_defaulted(field: str) -> None:
    body = fixture("exchange_orderbook")
    del body[field]
    client, _, _ = make_client(serve("/exchanges/36/orderbook", body))
    with pytest.raises(SigResponseError, match=field):
        run(client.get_exchange_orderbook("36"))


def test_missing_timestamp_is_rejected_not_filled_in() -> None:
    body = fixture("orders_page")
    del body["data"][0]["createdAt"]
    client, _, _ = make_client(serve("/orders", body))
    with pytest.raises(SigResponseError, match="createdAt"):
        run(client.list_orders_page())


@pytest.mark.parametrize("content", [b"<html>oops</html>", b"", b'{"bestBid": NaN}', b"\xff\xfe"])
def test_non_json_success_body_is_rejected(content: bytes) -> None:
    router = Router({f"{API}/exchanges/36/orderbook": raw_response(200, content)})
    client, _, _ = make_client(router)
    with pytest.raises(SigResponseError, match="not valid JSON"):
        run(client.get_exchange_orderbook("36"))


def test_unknown_extra_fields_are_ignored() -> None:
    body = exchange_book({"newField": {"anything": 1}})
    client, _, _ = make_client(serve("/exchanges/36/orderbook", body))
    assert run(client.get_exchange_orderbook("36")).exchangeId == "36"


# --- documented constraints on responses -------------------------------------------------


@pytest.mark.parametrize(
    "body",
    [
        {"data": [], "missingIds": ["36", "37", "999999999", "5"]},  # unrequested id
        {"data": [], "missingIds": ["36", "37"]},  # one id unaccounted for
        {"data": [], "missingIds": ["36", "36", "37", "999999999"]},  # duplicate
    ],
)
def test_bulk_prices_must_account_for_every_requested_id(body: dict[str, Any]) -> None:
    client, _, _ = make_client(serve("/exchanges/prices", body))
    with pytest.raises(SigResponseError):
        run(client.get_exchange_prices(["36", "37", "999999999"]))


def test_bulk_prices_must_preserve_request_order() -> None:
    body = fixture("exchange_prices")
    body["data"].reverse()
    client, _, _ = make_client(serve("/exchanges/prices", body))
    with pytest.raises(SigResponseError, match="order"):
        run(client.get_exchange_prices(["36", "37", "999999999"]))


def test_bulk_prices_id_both_priced_and_missing_is_rejected() -> None:
    body = fixture("exchange_prices")
    body["missingIds"] = ["999999999", "36"]
    client, _, _ = make_client(serve("/exchanges/prices", body))
    with pytest.raises(SigResponseError):
        run(client.get_exchange_prices(["36", "37", "999999999"]))


def test_orderbook_for_another_exchange_is_rejected() -> None:
    client, _, _ = make_client(
        serve("/exchanges/36/orderbook", exchange_book({"exchangeId": "37"}))
    )
    with pytest.raises(SigResponseError, match="different exchange"):
        run(client.get_exchange_orderbook("36"))


def test_orderbook_levels_beyond_requested_depth_are_rejected() -> None:
    client, _, _ = make_client()
    with pytest.raises(SigResponseError, match="price levels"):
        run(client.get_exchange_orderbook("36", depth=1))


@pytest.mark.parametrize(
    "change",
    [
        {"bids": [{"price": 0.4, "quantity": 1}, {"price": 0.41, "quantity": 1}]},
        {"asks": [{"price": 0.6, "quantity": 1}, {"price": 0.55, "quantity": 1}]},
    ],
)
def test_unsorted_levels_are_rejected(change: dict[str, Any]) -> None:
    client, _, _ = make_client(serve("/exchanges/36/orderbook", exchange_book(change)))
    with pytest.raises(SigResponseError, match="sorted"):
        run(client.get_exchange_orderbook("36"))


def test_market_orderbook_depth_is_checked_in_every_book() -> None:
    def deepen(body: dict[str, Any]) -> None:
        exchange = body["contexts"][0]["orderbook"]["exchanges"][0]
        exchange["asks"] = [{"price": 0.55, "quantity": 1}, {"price": 0.56, "quantity": 1}]

    client, _, _ = make_client(serve("/markets/26/orderbook", market_book(deepen)))
    with pytest.raises(SigResponseError, match="price levels"):
        run(client.get_market_orderbook("26", depth=1))


def test_orders_filtered_by_exchange_must_match_it() -> None:
    client, _, _ = make_client()
    with pytest.raises(SigResponseError, match="another exchange"):
        run(client.list_orders_page(exchange_id="37"))


# --- argument validation (nothing is sent) -----------------------------------------------


@pytest.mark.parametrize(
    "call",
    [
        lambda c: c.get_exchange_prices([]),
        lambda c: c.get_exchange_prices([str(i) for i in range(101)]),
        lambda c: c.get_exchange_prices(["36", "36"]),
        lambda c: c.get_exchange_prices(["36", "x"]),
        lambda c: c.get_exchange_prices("36"),
        lambda c: c.get_exchange_prices([36]),
        lambda c: c.get_exchange_orderbook("36", depth=0),
        lambda c: c.get_exchange_orderbook("36", depth=201),
        lambda c: c.get_exchange_orderbook("36", depth=True),
        lambda c: c.get_exchange_orderbook("../orders"),
        lambda c: c.get_exchange_orderbook("３６"),  # full-width digits
        lambda c: c.get_market_orderbook("26", depth=201),
        lambda c: c.get_market_orderbook("26a"),
        lambda c: c.list_tournament_markets_page(limit=101),
        lambda c: c.list_tournament_markets_page(limit=0),
        lambda c: c.list_tournament_markets_page(status="resolved"),
        lambda c: c.list_tournament_markets_page(cursor=""),
        lambda c: c.list_tournament_markets_page(search=""),
        lambda c: c.list_orders_page(limit=201),
        lambda c: c.list_orders_page(status="pending"),
        lambda c: c.list_orders_page(market_id="x"),
        lambda c: c.list_all_orders(max_pages=0),
    ],
)
def test_out_of_range_arguments_send_nothing(call: Any) -> None:
    client, router, _ = make_client()

    async def scenario(c: SigRestClient) -> None:
        await call(c)

    with pytest.raises(SigArgumentError):
        run(scenario(client))
    assert all(path == "/tournaments/alpha-tournament" for path in router.paths())


def test_documented_maximums_are_accepted() -> None:
    ids = [str(i) for i in range(1, 101)]
    body = {"data": [], "missingIds": ids}
    client, router, _ = make_client(serve("/exchanges/prices", body))
    run(client.get_exchange_prices(ids))
    assert params(router, "/exchanges/prices")["ids"] == ",".join(ids)
