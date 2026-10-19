"""A2-A: read_open_orders (design T12-T15) and the orders `coverage: null` defect.

All offline: synthetic orders pages through httpx.MockTransport.
"""

from __future__ import annotations

import asyncio
import inspect
from typing import Any

import httpx
import pytest
from account_kit import NULL, ORDERS, by_cursor, orders_page, taking, until
from sig_testkit import (
    TOURNAMENT,
    FakeClock,
    Router,
    error_body,
    json_response,
    make_client,
    run,
)

from engine.sig_client.account_reads import (
    CoverageState,
    OrdersWalk,
    ScopeEvidence,
    VerificationUse,
)
from engine.sig_client.errors import (
    SigArgumentError,
    SigDeadlineExceededError,
    SigPaginationError,
    SigResponseError,
)
from engine.sig_client.rest import SigRestClient
from engine.sig_client.wire import OrderCoverage


def order_requests(router: Router) -> list[httpx.Request]:
    return [r for r in router.requests if r.url.path == ORDERS]


def walk(pages: dict[str | None, Any], **kwargs: Any) -> tuple[OrdersWalk, Router]:
    client, router, _ = make_client(Router({ORDERS: by_cursor(pages)}))
    return run(client.read_open_orders(**kwargs)), router


# --- T12: every page's coverage kept in order ----------------------------------------------


def test_t12_three_pages_keep_their_own_coverage_and_provenance() -> None:
    pages = {
        None: orders_page(
            [1, 2], cursor="c2", coverage={"complete": True, "projectedThroughSequence": 10}
        ),
        "c2": orders_page([3, 4], cursor="c3", coverage=None),  # omitted
        "c3": orders_page(
            [5], cursor=None, coverage={"complete": True, "projectedThroughSequence": None}
        ),
    }
    result, router = walk(pages, page_limit=2)
    assert [order.id for order in result.orders] == [1, 2, 3, 4, 5]
    assert [page.item_count for page in result.pages] == [2, 2, 1]
    provenances = [page.provenance for page in result.pages]
    assert [p.page_index for p in provenances] == [0, 1, 2]
    assert [p.coverage_state for p in provenances] == [
        CoverageState.PRESENT,
        CoverageState.OMITTED,
        CoverageState.PRESENT,
    ]
    assert provenances[0].coverage == OrderCoverage(complete=True, projectedThroughSequence=10)
    assert provenances[1].coverage is None
    assert provenances[2].coverage == OrderCoverage(complete=True, projectedThroughSequence=None)
    assert [p.cursor_sent for p in provenances] == [False, True, True]
    for provenance in provenances:
        assert provenance.route == "/orders" and provenance.path == "/orders"
        assert provenance.params == (
            ("limit", "2"),
            ("status", "open"),
            ("tournamentId", TOURNAMENT.id),
        )
        assert provenance.scope is ScopeEvidence.TOURNAMENT_ID_SENT
        assert provenance.attempts == 1 and provenance.http_status == 200
    # The one-time verification belongs to the first page only.
    assert provenances[0].verification is VerificationUse.PERFORMED
    assert [p.verification for p in provenances[1:]] == [VerificationUse.CACHED] * 2
    requests = order_requests(router)
    assert [r.url.params.get("cursor") for r in requests] == [None, "c2", "c3"]
    assert all(r.url.params["status"] == "open" for r in requests)
    assert all(r.url.params["tournamentId"] == TOURNAMENT.id for r in requests)


def test_t12_provenance_never_holds_a_cursor_value() -> None:
    pages = {
        None: orders_page([1], cursor="SENTINEL_CURSOR_VALUE"),
        "SENTINEL_CURSOR_VALUE": orders_page([2], cursor=None),
    }
    result, _ = walk(pages, page_limit=1)
    assert "SENTINEL_CURSOR_VALUE" not in repr(result.pages)


def test_t12_an_empty_walk_is_one_observed_page() -> None:
    result, router = walk({None: orders_page([], cursor=None)})
    assert result.orders == () and len(result.pages) == 1
    assert result.pages[0].item_count == 0
    assert len(order_requests(router)) == 1


# --- T13: explicit coverage null is rejected (the defect on main) ----------------------------


def test_t13_coverage_null_fails_the_first_page() -> None:
    with pytest.raises(SigResponseError, match="coverage"):
        walk({None: orders_page([1], cursor=None, coverage=NULL)})


def test_t13_coverage_null_on_a_later_page_returns_no_partial_walk() -> None:
    pages = {
        None: orders_page([1], cursor="c2"),
        "c2": orders_page([2], cursor=None, coverage=NULL),
    }
    with pytest.raises(SigResponseError, match="coverage"):
        walk(pages, page_limit=1)


def test_t13_existing_order_reads_reject_coverage_null_too() -> None:
    # On main (76bcaac) OrdersPage accepted an explicit null and reported it as None,
    # indistinguishable from an omitted coverage. Both existing reads now reject it.
    page = json_response(200, orders_page([1], cursor=None, coverage=NULL))
    client, _, _ = make_client(Router({ORDERS: page}))
    with pytest.raises(SigResponseError, match="coverage"):
        run(client.list_orders_page())
    with pytest.raises(SigResponseError, match="coverage"):
        run(client.list_all_orders())


# --- T14: omitted coverage is recorded, and the walk continues -------------------------------


def test_t14_omitted_coverage_is_recorded_and_the_walk_continues() -> None:
    pages = {
        None: orders_page([1], cursor="c2", coverage=None),
        "c2": orders_page([2], cursor=None, coverage=None),
    }
    result, _ = walk(pages, page_limit=1)
    assert [order.id for order in result.orders] == [1, 2]
    assert [p.provenance.coverage_state for p in result.pages] == [CoverageState.OMITTED] * 2


@pytest.mark.parametrize("complete", [False, None, 1])
def test_coverage_complete_must_be_json_true(complete: Any) -> None:
    coverage = {"complete": complete, "projectedThroughSequence": 1}
    with pytest.raises(SigResponseError, match="complete"):
        walk({None: orders_page([1], cursor=None, coverage=coverage)})


# --- T15: complete or error ------------------------------------------------------------------


@pytest.mark.parametrize(
    ("pages", "kwargs", "message"),
    [
        (
            {None: orders_page([1], cursor="A"), "A": orders_page([2], cursor="A")},
            {"page_limit": 1},
            "repeated",
        ),
        (
            {None: orders_page([1], cursor="A"), "A": orders_page([1], cursor=None)},
            {"page_limit": 1},
            "more than one page",
        ),
        (
            {
                None: orders_page([1], cursor=None)
                | {"pagination": {"limit": 1, "hasMore": False, "nextCursor": "dangling"}}
            },
            {"page_limit": 1},
            "hasMore is false",
        ),
        (
            {
                None: orders_page([1], cursor=None)
                | {"pagination": {"limit": 1, "hasMore": True, "nextCursor": None}}
            },
            {"page_limit": 1},
            "nextCursor is null",
        ),
        (
            {
                None: orders_page([1], cursor="A"),
                "A": orders_page([2], cursor="B"),
                "B": orders_page([3], cursor="C"),
            },
            {"page_limit": 1, "max_pages": 3},
            "more than 3 pages",
        ),
    ],
)
def test_t15_pagination_faults_return_no_partial_walk(
    pages: dict[str | None, Any], kwargs: dict[str, Any], message: str
) -> None:
    with pytest.raises(SigPaginationError, match=message):
        walk(pages, **kwargs)


def test_t15_a_page_over_the_limit_is_malformed() -> None:
    with pytest.raises(SigResponseError, match="limit was 1"):
        walk({None: orders_page([1, 2], cursor=None)}, page_limit=1)


@pytest.mark.parametrize(
    "kwargs",
    [{"page_limit": 0}, {"page_limit": 201}, {"page_limit": True}, {"max_pages": 0}],
)
def test_bad_arguments_send_nothing(kwargs: dict[str, Any]) -> None:
    client, router, _ = make_client()
    with pytest.raises(SigArgumentError):
        run(client.read_open_orders(**kwargs))
    assert router.requests == []


# --- per-page provenance under retries, deadlines and cancellation ----------------------------


def test_a_retried_page_counts_only_its_own_attempts() -> None:
    page2 = [
        json_response(503, error_body("TX_CONFLICT")),
        json_response(200, orders_page([2], cursor=None)),
    ]
    pages: dict[str | None, Any] = {None: orders_page([1], cursor="c2")}

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.params.get("cursor") == "c2":
            return page2.pop(0)
        return json_response(200, pages[None])

    client, _, clock = make_client(Router({ORDERS: handler}))
    result = run(client.read_open_orders(page_limit=1))
    assert [p.provenance.attempts for p in result.pages] == [1, 2]
    assert clock.sleeps == [0.25]
    first, second = (p.provenance for p in result.pages)
    assert first.finished_mono <= second.started_mono


def test_a_deadline_on_a_later_page_returns_nothing() -> None:
    clock = FakeClock()
    late = taking(clock, 31.0, json_response(200, orders_page([2], cursor=None)))
    pages: dict[str | None, Any] = {None: orders_page([1], cursor="c2"), "c2": late}

    def handler(request: httpx.Request) -> Any:
        page = pages[request.url.params.get("cursor")]
        return page(request) if callable(page) else json_response(200, page)

    client, _, _ = make_client(Router({ORDERS: handler}), clock=clock)
    with pytest.raises(SigDeadlineExceededError) as info:
        run(client.read_open_orders(page_limit=1))
    assert info.value.phase == "request"


def test_a_cancelled_walk_leaves_nothing_for_the_next_walk() -> None:
    gate = asyncio.Event()
    hang = {"on": True}

    async def handler(request: httpx.Request) -> httpx.Response:
        if request.url.params.get("cursor") == "c2":
            if hang["on"]:
                await gate.wait()
            return json_response(200, orders_page([2], cursor=None))
        return json_response(200, orders_page([1], cursor="c2"))

    router = Router({ORDERS: handler})
    client, _, _ = make_client(router)

    async def scenario() -> OrdersWalk:
        task = asyncio.create_task(client.read_open_orders(page_limit=1))
        await until(lambda: len(order_requests(router)) == 2)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        hang["on"] = False
        return await client.read_open_orders(page_limit=1)

    result = run(scenario())
    assert [p.provenance.page_index for p in result.pages] == [0, 1]
    assert [order.id for order in result.orders] == [1, 2]
    assert result.pages[0].provenance.verification is VerificationUse.CACHED


def test_concurrent_walks_keep_separate_page_records() -> None:
    pages = {
        None: orders_page([1], cursor="c2"),
        "c2": orders_page([2], cursor="c3"),
        "c3": orders_page([3], cursor=None),
    }
    client, _, _ = make_client(Router({ORDERS: by_cursor(pages)}))

    async def scenario() -> list[OrdersWalk]:
        return await asyncio.gather(
            client.read_open_orders(page_limit=1), client.read_open_orders(page_limit=1)
        )

    for result in run(scenario()):
        assert [p.provenance.page_index for p in result.pages] == [0, 1, 2]
        assert [order.id for order in result.orders] == [1, 2, 3]


# --- the existing orders API is unchanged ---------------------------------------------------


def test_list_all_orders_keeps_its_signature_and_return_type() -> None:
    signature = inspect.signature(SigRestClient.list_all_orders)
    assert list(signature.parameters) == [
        "self",
        "status",
        "exchange_id",
        "market_id",
        "page_limit",
        "max_pages",
    ]
    assert signature.return_annotation == "list[Order]"
    client, _, _ = make_client()
    orders = run(client.list_all_orders(status="all"))
    assert isinstance(orders, list) and [order.id for order in orders] == [1001, 1002]
