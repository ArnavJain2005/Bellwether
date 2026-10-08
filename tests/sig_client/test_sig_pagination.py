import copy
from typing import Any

import httpx
import pytest
from sig_testkit import API, Router, fixture, json_response, make_client, run

from engine.sig_client.errors import SigPaginationError, SigResponseError

MARKETS = f"{API}/tournaments/alpha-tournament/markets"


def page(base: str, **pagination: Any) -> dict[str, Any]:
    body = copy.deepcopy(fixture(base))
    body["pagination"].update(pagination)
    return body


def by_cursor(pages: dict[str | None, dict[str, Any]]) -> Any:
    def handler(request: httpx.Request) -> httpx.Response:
        return json_response(200, pages[request.url.params.get("cursor")])

    return handler


def test_all_markets_follows_cursors_to_completion() -> None:
    pages = {
        None: fixture("tournament_markets_page1"),
        "cursor-page-2": fixture("tournament_markets_page2"),
    }
    client, router, _ = make_client(Router({MARKETS: by_cursor(pages)}))
    markets = run(client.list_all_tournament_markets(page_limit=1))
    assert [m.id for m in markets] == ["26", "27"]
    cursors = [r.url.params.get("cursor") for r in router.requests if r.url.path == MARKETS]
    assert cursors == [None, "cursor-page-2"]
    assert all(r.url.params["limit"] == "1" for r in router.requests if r.url.path == MARKETS)


def test_repeated_cursor_is_detected() -> None:
    pages = {
        None: page("tournament_markets_page1", nextCursor="A"),
        "A": page("tournament_markets_page2", hasMore=True, nextCursor="A"),
    }
    client, _, _ = make_client(Router({MARKETS: by_cursor(pages)}))
    with pytest.raises(SigPaginationError, match="repeated"):
        run(client.list_all_tournament_markets(page_limit=1))


def test_cursor_cycle_is_detected() -> None:
    p1 = page("tournament_markets_page1", nextCursor="A")
    pa = page("tournament_markets_page2", hasMore=True, nextCursor="B")
    pb = copy.deepcopy(pa)
    pb["data"][0]["id"] = "28"
    pb["pagination"]["nextCursor"] = "A"
    client, _, _ = make_client(Router({MARKETS: by_cursor({None: p1, "A": pa, "B": pb})}))
    with pytest.raises(SigPaginationError, match="repeated"):
        run(client.list_all_tournament_markets(page_limit=1))


def test_page_cap_raises_instead_of_returning_a_partial_list() -> None:
    def endless(request: httpx.Request) -> httpx.Response:
        n = int(request.url.params.get("cursor", "0"))
        body = page("tournament_markets_page1", nextCursor=str(n + 1))
        body["data"][0]["id"] = str(100 + n)
        return json_response(200, body)

    client, router, _ = make_client(Router({MARKETS: endless}))
    with pytest.raises(SigPaginationError, match="more than 3 pages"):
        run(client.list_all_tournament_markets(page_limit=1, max_pages=3))
    assert sum(r.url.path == MARKETS for r in router.requests) == 3


def test_has_more_without_cursor_is_malformed() -> None:
    body = page("tournament_markets_page1", nextCursor=None)
    client, _, _ = make_client(Router({MARKETS: json_response(200, body)}))
    with pytest.raises(SigPaginationError, match="nextCursor is null"):
        run(client.list_all_tournament_markets(page_limit=1))


def test_cursor_on_last_page_is_malformed() -> None:
    body = page("tournament_markets_page2", nextCursor="dangling")
    client, _, _ = make_client(Router({MARKETS: json_response(200, body)}))
    with pytest.raises(SigPaginationError, match="hasMore is false"):
        run(client.list_all_tournament_markets(page_limit=1))


def test_item_repeated_across_pages_is_detected() -> None:
    pages = {
        None: fixture("tournament_markets_page1"),
        "cursor-page-2": page("tournament_markets_page1", hasMore=False, nextCursor=None),
    }
    client, _, _ = make_client(Router({MARKETS: by_cursor(pages)}))
    with pytest.raises(SigPaginationError, match="more than one page"):
        run(client.list_all_tournament_markets(page_limit=1))


def test_page_larger_than_limit_is_malformed() -> None:
    body = fixture("tournament_markets_page1")
    body["data"].append(fixture("tournament_markets_page2")["data"][0])
    client, _, _ = make_client(Router({MARKETS: json_response(200, body)}))
    with pytest.raises(SigResponseError, match="limit was 1"):
        run(client.list_all_tournament_markets(page_limit=1))


def test_malformed_page_aborts_the_collection() -> None:
    bad = fixture("tournament_markets_page2")
    del bad["pagination"]["hasMore"]
    pages = {None: fixture("tournament_markets_page1"), "cursor-page-2": bad}
    client, _, _ = make_client(Router({MARKETS: by_cursor(pages)}))
    with pytest.raises(SigResponseError, match="hasMore"):
        run(client.list_all_tournament_markets(page_limit=1))


def test_all_orders_pages_and_keeps_scope_on_every_page() -> None:
    first = page("orders_page", hasMore=True, nextCursor="o2", limit=2)
    second = copy.deepcopy(fixture("orders_page"))
    for order, new_id in zip(second["data"], (1003, 1004), strict=True):
        order["id"] = new_id
    client, router, _ = make_client(
        Router({f"{API}/orders": by_cursor({None: first, "o2": second})})
    )
    orders = run(client.list_all_orders(status="all", page_limit=2))
    assert [o.id for o in orders] == [1001, 1002, 1003, 1004]
    order_requests = [r for r in router.requests if r.url.path == f"{API}/orders"]
    assert [r.url.params.get("cursor") for r in order_requests] == [None, "o2"]
    assert all(r.url.params["tournamentId"] for r in order_requests)
    assert all(r.url.params["status"] == "all" for r in order_requests)
