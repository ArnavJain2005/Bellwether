"""A2-B: read_order_fills (GET /orders/{id}/fills), design T17 and T20.

All offline: synthetic, spec-derived bodies through httpx.MockTransport. Nothing here is
validated against a real SIG response (per-order fills have never been captured).
"""

from __future__ import annotations

import copy
from decimal import Decimal
from typing import Any

import httpx
import pytest
from account_kit import TOURNAMENT_PATH, exact_body, exact_response
from sig_testkit import API, TOURNAMENT, Router, make_client, run

from engine.sig_client.account_reads import (
    CoverageState,
    OrderFillsWalk,
    ScopeEvidence,
    VerificationUse,
)
from engine.sig_client.errors import (
    SigArgumentError,
    SigContextMismatchError,
    SigPaginationError,
    SigResponseError,
)
from engine.sig_client.rate_limiter import RequestKind
from engine.sig_client.wire import OrderFill, OrderFillsCoverage

OTHER_UUID = "11111111-2222-4333-8444-555555555555"
T0 = "2026-10-20T12:00:00.000Z"
T1 = "2026-10-20T11:00:00.000Z"


def fills_path(order_id: int) -> str:
    return f"{API}/orders/{order_id}/fills"


def ofill(fill_id: int, filled_at: str, **changes: Any) -> dict[str, Any]:
    base = copy.deepcopy(exact_body("order_fills")["data"][0])
    return base | {"id": fill_id, "filledAt": filled_at} | changes


def page(
    order_id: int,
    fills: list[dict[str, Any]],
    *,
    cursor: str | None,
    total: Decimal = Decimal(100),
    avg: Decimal | None = Decimal("0.42"),
    **changes: Any,
) -> dict[str, Any]:
    body = copy.deepcopy(exact_body("order_fills"))
    body.update(
        orderId=order_id,
        data=fills,
        pagination={"limit": 200, "hasMore": cursor is not None, "nextCursor": cursor},
        totalQuantityFilled=total,
        avgFillPrice=avg,
    )
    body.update(changes)
    return body


def by_cursor(pages: dict[str | None, Any]) -> Any:
    def handler(request: httpx.Request) -> httpx.Response:
        return exact_response(200, pages[request.url.params.get("cursor")])

    return handler


def walk(order_id: int, pages: dict[str | None, Any]) -> tuple[OrderFillsWalk, Router]:
    client, router, _ = make_client(Router({fills_path(order_id): by_cursor(pages)}))
    return run(client.read_order_fills(order_id)), router


def test_one_page_is_kept_exactly_as_sent() -> None:
    result, router = walk(1001, {None: exact_body("order_fills")})
    assert (result.order_id, result.exchange_id) == (1001, "36")
    assert result.echoed_tournament_id == TOURNAMENT.id
    assert result.total_quantity_filled == 100 and result.avg_fill_price == Decimal("0.42")
    (only,) = result.fills
    assert type(only) is OrderFill and only.id == 5001 and only.side == "yes"
    provenance = result.pages[0].provenance
    assert provenance.route == "/orders/{id}/fills" and provenance.path == "/orders/1001/fills"
    assert provenance.scope is ScopeEvidence.ECHO
    assert provenance.params == (("limit", "200"),)
    assert provenance.coverage_state is CoverageState.PRESENT
    assert provenance.coverage == OrderFillsCoverage(complete=True, projectedThroughSequence=184233)
    assert provenance.verification is VerificationUse.PERFORMED
    request = router.requests[-1]
    assert "tournamentId" not in request.url.params  # the order id gives the context


# --- T20: two pages, then an order with no fills -----------------------------------------------


def test_t20_two_pages_then_an_order_with_no_fills() -> None:
    total, avg = Decimal("-7.5"), Decimal("0.385")
    two_pages = {
        None: page(
            2001, [ofill(1, T0, quantity=Decimal(-5), side="no")], cursor="p2", total=total, avg=avg
        ),
        "p2": page(
            2001,
            [ofill(2, T1, quantity=Decimal("-2.5"), side="no")],
            cursor=None,
            total=total,
            avg=avg,
            coverage={"complete": True, "projectedThroughSequence": None},
        ),
    }
    empty = page(2002, [], cursor=None, total=Decimal(0), avg=None)
    router = Router(
        {fills_path(2001): by_cursor(two_pages), fills_path(2002): exact_response(200, empty)}
    )
    client, _, _ = make_client(router)

    async def scenario() -> tuple[OrderFillsWalk, OrderFillsWalk]:
        return await client.read_order_fills(2001), await client.read_order_fills(2002)

    first, second = run(scenario())
    assert [f.id for f in first.fills] == [1, 2]
    assert [p.provenance.page_index for p in first.pages] == [0, 1]
    assert [p.provenance.coverage.projectedThroughSequence for p in first.pages] == [184233, None]
    assert first.total_quantity_filled.as_tuple() == total.as_tuple()  # lifetime, as sent
    assert first.avg_fill_price == avg
    # The empty order still costs one request and returns an empty walk.
    assert second.fills == () and len(second.pages) == 1
    assert second.total_quantity_filled == 0 and second.avg_fill_price is None
    assert sum(r.url.path == fills_path(2001) for r in router.requests) == 2
    assert sum(r.url.path == fills_path(2002) for r in router.requests) == 1


# --- T17: the echoed tournament must be the configured one --------------------------------------


@pytest.mark.parametrize(
    ("echoed", "reason"),
    [
        (None, "global order"),
        (OTHER_UUID, "different tournament"),
        ("not-a-uuid", "different tournament"),
    ],
)
def test_t17_a_null_or_foreign_echo_raises_without_latching(echoed: Any, reason: str) -> None:
    body = page(1001, [ofill(1, T0)], cursor=None, tournamentId=echoed)
    client, router, _ = make_client(Router({fills_path(1001): exact_response(200, body)}))

    async def scenario() -> None:
        with pytest.raises(SigContextMismatchError, match=reason) as info:
            await client.read_order_fills(1001)
        if echoed is not None:
            assert echoed not in str(info.value)  # the echoed value is not repeated
        # This is about one order, not the client's tournament: other reads still work.
        await client.read_tournament_positions()
        await client.read_tournament_balance()

    run(scenario())
    assert client._mismatch is None
    assert "/tournaments/alpha-tournament/portfolio/positions" in router.paths()


def test_t17_a_foreign_echo_on_a_later_page_fails_the_whole_walk() -> None:
    pages = {
        None: page(1001, [ofill(1, T0)], cursor="p2"),
        "p2": page(1001, [ofill(2, T1)], cursor=None, tournamentId=OTHER_UUID),
    }
    with pytest.raises(SigContextMismatchError):
        walk(1001, pages)


def test_the_echoed_uuid_is_compared_case_insensitively() -> None:
    body = page(1001, [ofill(1, T0)], cursor=None, tournamentId=TOURNAMENT.id.upper())
    result, _ = walk(1001, {None: body})
    assert result.echoed_tournament_id == TOURNAMENT.id.upper()  # kept as sent


# --- per-page consistency -----------------------------------------------------------------------


@pytest.mark.parametrize(
    ("second_changes", "error", "message"),
    [
        ({"total": Decimal(101)}, SigPaginationError, "lifetime totals"),
        ({"avg": Decimal("0.43")}, SigPaginationError, "lifetime totals"),
        ({"avg": None}, SigPaginationError, "lifetime totals"),
        ({"exchangeId": "37"}, SigPaginationError, "different exchange"),
        ({"orderId": 1002}, SigResponseError, "different order"),
    ],
)
def test_pages_must_agree_on_order_exchange_and_lifetime_totals(
    second_changes: dict[str, Any], error: type[Exception], message: str
) -> None:
    order_id = second_changes.pop("orderId", 1001)
    pages = {
        None: page(1001, [ofill(1, T0)], cursor="p2"),
        "p2": page(order_id, [ofill(2, T1)], cursor=None, **second_changes),
    }
    with pytest.raises(error, match=message):
        walk(1001, pages)


def test_fills_out_of_newest_first_order_are_rejected() -> None:
    with pytest.raises(SigResponseError, match="newest first"):
        walk(1001, {None: page(1001, [ofill(1, T1), ofill(2, T0)], cursor=None)})
    pages = {
        None: page(1001, [ofill(1, T1)], cursor="p2"),
        "p2": page(1001, [ofill(2, T0)], cursor=None),
    }
    with pytest.raises(SigPaginationError, match="newer than the previous page"):
        walk(1001, pages)


@pytest.mark.parametrize(
    ("pages", "message"),
    [
        (
            {
                None: page(1001, [ofill(1, T0)], cursor="A"),
                "A": page(1001, [ofill(2, T1)], cursor="A"),
            },
            "repeated",
        ),
        (
            {
                None: page(1001, [ofill(1, T0)], cursor="A"),
                "A": page(1001, [ofill(1, T1)], cursor=None),
            },
            "more than one page",
        ),
    ],
)
def test_pagination_faults_return_no_partial_walk(
    pages: dict[str | None, Any], message: str
) -> None:
    with pytest.raises(SigPaginationError, match=message):
        walk(1001, pages)


@pytest.mark.parametrize(
    "change",
    [
        {"coverage": None},
        {"coverage": {"complete": False, "projectedThroughSequence": 1}},
        {"tournamentId": 7},
        {"totalQuantityFilled": None},
        {"orderId": "1001"},
    ],
)
def test_malformed_pages_are_rejected(change: dict[str, Any]) -> None:
    body = page(1001, [ofill(1, T0)], cursor=None)
    body.update(change)
    with pytest.raises(SigResponseError):
        walk(1001, {None: body})


def test_missing_required_coverage_is_rejected() -> None:
    body = page(1001, [ofill(1, T0)], cursor=None)
    del body["coverage"]
    with pytest.raises(SigResponseError, match="coverage"):
        walk(1001, {None: body})


@pytest.mark.parametrize("order_id", ["1001", True, -1, 1.0, None])
def test_the_order_id_must_be_a_non_negative_integer(order_id: Any) -> None:
    client, router, _ = make_client()
    with pytest.raises(SigArgumentError):
        run(client.read_order_fills(order_id))
    assert router.requests == []
    assert client.limiter.used(RequestKind.READ) == 0


def test_no_tournament_path_or_default_context_is_requested() -> None:
    client, router, _ = make_client()
    run(client.read_order_fills(1001))
    assert router.paths() == ["/tournaments/alpha-tournament", "/orders/1001/fills"]
    assert TOURNAMENT_PATH.removeprefix(API) in router.paths()  # identity verified first
