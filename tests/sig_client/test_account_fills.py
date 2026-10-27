"""A2-B: read_tournament_fills (GET /tournaments/{slug}/portfolio/fills).

All offline: synthetic, spec-derived bodies through httpx.MockTransport. Nothing here is
validated against a real SIG response (the fills read has never been captured).
"""

from __future__ import annotations

import copy
from decimal import Decimal
from typing import Any

import httpx
import pytest
from account_kit import NULL, TOURNAMENT_PATH, exact_body, exact_response
from sig_testkit import API, Router, error_body, json_response, make_client, run

from engine.sig_client.account_reads import (
    CoverageState,
    FillsWalk,
    ScopeEvidence,
    VerificationUse,
)
from engine.sig_client.errors import (
    SigArgumentError,
    SigPaginationError,
    SigResponseError,
)
from engine.sig_client.wire import FillsCoverage, TournamentFill

FILLS = f"{TOURNAMENT_PATH}/portfolio/fills"


def fill(fill_id: int, filled_at: str, **changes: Any) -> dict[str, Any]:
    base = copy.deepcopy(exact_body("tournament_fills")["data"][0])
    return base | {"id": fill_id, "filledAt": filled_at} | changes


def page(
    fills: list[dict[str, Any]], *, cursor: str | None, coverage: Any = "default", limit: int = 200
) -> dict[str, Any]:
    body: dict[str, Any] = {
        "data": fills,
        "pagination": {"limit": limit, "hasMore": cursor is not None, "nextCursor": cursor},
    }
    if coverage == "default":
        body["coverage"] = {"complete": True, "projectedThroughSequence": 184233}
    elif coverage is NULL:
        body["coverage"] = None
    elif coverage is not None:
        body["coverage"] = coverage
    return body


def by_cursor(pages: dict[str | None, Any]) -> Any:
    def handler(request: httpx.Request) -> httpx.Response:
        return exact_response(200, pages[request.url.params.get("cursor")])

    return handler


def walk(pages: dict[str | None, Any], **kwargs: Any) -> tuple[FillsWalk, Router]:
    client, router, _ = make_client(Router({FILLS: by_cursor(pages)}))
    return run(client.read_tournament_fills(**kwargs)), router


T0 = "2026-10-20T12:00:00.000Z"
T1 = "2026-10-20T11:00:00.000Z"
T2 = "2026-10-20T10:00:00.000Z"


# --- rows kept exactly as sent ---------------------------------------------------------------


def test_fills_are_kept_exactly_as_sent() -> None:
    result, router = walk({None: exact_body("tournament_fills")})
    yes, no = result.fills
    assert all(type(f) is TournamentFill for f in result.fills)
    assert (yes.id, yes.orderId, yes.exchangeId, yes.marketId) == (5001, 1001, "36", "26")
    assert yes.price == Decimal("0.42") and yes.quantity == 100 and yes.side == "yes"
    # A NO fill: negative, fractional quantity, null price and null orderId, kept as sent.
    assert no.quantity.as_tuple() == Decimal("-2.5").as_tuple()
    assert no.price is None and no.orderId is None and no.side == "no"
    assert no.filledAt == "2025-06-14T12:04:00.000Z"  # timestamps stay strings
    request = router.requests[-1]
    assert request.url.params["limit"] == "200" and "cursor" not in request.url.params


@pytest.mark.parametrize(
    "literal", ["0.4235", "1.5", "-0.2", "1e-7", "123456789.000000001"]
)  # no documented range or grid: nothing is bounded, rounded or complemented
def test_fill_price_has_no_bound_and_keeps_its_digits(literal: str) -> None:
    body = page([fill(1, T0, price=Decimal(literal))], cursor=None)
    result, _ = walk({None: body})
    assert result.fills[0].price.as_tuple() == Decimal(literal).as_tuple()


def test_unknown_fields_are_ignored() -> None:
    body = page([fill(1, T0, futureField=[1])], cursor=None)
    body["futureTop"] = {"x": 1}
    body["coverage"]["futureCoverage"] = 2
    result, _ = walk({None: body})
    assert result.fills[0].model_extra is None


# --- the walk: pages, provenance and coverage -------------------------------------------------


def test_three_pages_keep_order_coverage_and_provenance() -> None:
    pages = {
        None: page([fill(1, T0), fill(2, T0)], cursor="c2", limit=2),  # equal times allowed
        "c2": page([fill(3, T1)], cursor="c3", coverage=None, limit=2),  # omitted
        "c3": page(
            [fill(4, T2)],
            cursor=None,
            coverage={"complete": True, "projectedThroughSequence": None},
            limit=2,
        ),
    }
    result, router = walk(pages, page_limit=2)
    assert [f.id for f in result.fills] == [1, 2, 3, 4]
    assert [p.item_count for p in result.pages] == [2, 1, 1]
    provenances = [p.provenance for p in result.pages]
    assert [p.page_index for p in provenances] == [0, 1, 2]
    assert [p.coverage_state for p in provenances] == [
        CoverageState.PRESENT,
        CoverageState.OMITTED,
        CoverageState.PRESENT,
    ]
    assert provenances[0].coverage == FillsCoverage(complete=True, projectedThroughSequence=184233)
    assert provenances[2].coverage == FillsCoverage(complete=True, projectedThroughSequence=None)
    assert [p.cursor_sent for p in provenances] == [False, True, True]
    for provenance in provenances:
        assert provenance.route == "/tournaments/{slug}/portfolio/fills"
        assert provenance.path == "/tournaments/alpha-tournament/portfolio/fills"
        assert provenance.params == (("limit", "2"),)
        assert provenance.scope is ScopeEvidence.VERIFIED_SLUG_PATH
        assert provenance.attempts == 1 and provenance.http_status == 200
    assert provenances[0].verification is VerificationUse.PERFORMED
    assert [p.verification for p in provenances[1:]] == [VerificationUse.CACHED] * 2
    cursors = [r.url.params.get("cursor") for r in router.requests if r.url.path == FILLS]
    assert cursors == [None, "c2", "c3"]


def test_an_empty_history_is_one_observed_page() -> None:
    result, router = walk({None: page([], cursor=None)})
    assert result.fills == () and len(result.pages) == 1
    assert sum(r.url.path == FILLS for r in router.requests) == 1


@pytest.mark.parametrize("coverage", [NULL, {"complete": False, "projectedThroughSequence": 1}])
def test_explicit_null_or_incomplete_projection_coverage_is_rejected(coverage: Any) -> None:
    pages = {
        None: page([fill(1, T0)], cursor="c2"),
        "c2": page([fill(2, T1)], cursor=None, coverage=coverage),
    }
    with pytest.raises(SigResponseError, match="coverage"):
        walk(pages)


# --- complete or error -----------------------------------------------------------------------


@pytest.mark.parametrize(
    ("pages", "message"),
    [
        ({None: page([fill(1, T0)], cursor="A"), "A": page([fill(2, T1)], cursor="A")}, "repeated"),
        (
            {None: page([fill(1, T0)], cursor="A"), "A": page([fill(1, T1)], cursor=None)},
            "more than one page",
        ),
        (
            {
                None: page([fill(1, T0)], cursor=None)
                | {"pagination": {"limit": 1, "hasMore": False, "nextCursor": "x"}}
            },
            "hasMore is false",
        ),
        (
            {
                None: page([fill(1, T0)], cursor="A"),
                "A": page([fill(2, T1)], cursor="B"),
                "B": page([fill(3, T2)], cursor="C"),
            },
            "more than 3 pages",
        ),
    ],
)
def test_pagination_faults_return_no_partial_walk(
    pages: dict[str | None, Any], message: str
) -> None:
    with pytest.raises(SigPaginationError, match=message):
        walk(pages, max_pages=3)


def test_fills_out_of_newest_first_order_are_rejected() -> None:
    with pytest.raises(SigResponseError, match="newest first"):
        walk({None: page([fill(1, T1), fill(2, T0)], cursor=None)})
    pages = {None: page([fill(1, T1)], cursor="c2"), "c2": page([fill(2, T0)], cursor=None)}
    with pytest.raises(SigPaginationError, match="newer than the previous page"):
        walk(pages)


def test_a_page_over_the_limit_is_malformed() -> None:
    with pytest.raises(SigResponseError, match="limit was 1"):
        walk({None: page([fill(1, T0), fill(2, T1)], cursor=None)}, page_limit=1)


@pytest.mark.parametrize(
    ("change", "location"),
    [
        ({"quantity": "100"}, "data.0.quantity"),
        ({"quantity": True}, "data.0.quantity"),
        ({"price": "0.42"}, "data.0.price"),
        ({"side": "YES"}, "data.0.side"),
        ({"id": "5001"}, "data.0.id"),
        ({"orderId": 1.5}, "data.0.orderId"),
        ({"exchangeId": 36}, "data.0.exchangeId"),
        ({"filledAt": "not a time"}, "filledAt"),
    ],
)
def test_malformed_fills_are_rejected_naming_the_location(
    change: dict[str, Any], location: str
) -> None:
    with pytest.raises(SigResponseError) as info:
        walk({None: page([fill(1, T0, **change)], cursor=None)})
    assert location in info.value.reason


@pytest.mark.parametrize("field", ["orderId", "price", "marketId", "filledAt"])
def test_missing_required_fields_are_rejected(field: str) -> None:
    row = fill(1, T0)
    del row[field]
    with pytest.raises(SigResponseError, match=field):
        walk({None: page([row], cursor=None)})


@pytest.mark.parametrize(
    "kwargs", [{"page_limit": 0}, {"page_limit": 201}, {"page_limit": True}, {"max_pages": 0}]
)
def test_bad_arguments_send_nothing(kwargs: dict[str, Any]) -> None:
    client, router, _ = make_client()
    with pytest.raises(SigArgumentError):
        run(client.read_tournament_fills(**kwargs))
    assert router.requests == []


def test_a_retried_page_counts_only_its_own_attempts() -> None:
    second = [json_response(503, error_body("SERVICE_UNAVAILABLE"), {"Retry-After": "2"})]

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.params.get("cursor") == "c2" and second:
            return second.pop()
        if request.url.params.get("cursor") == "c2":
            return exact_response(200, page([fill(2, T1)], cursor=None))
        return exact_response(200, page([fill(1, T0)], cursor="c2"))

    client, _, clock = make_client(Router({FILLS: handler}))
    result = run(client.read_tournament_fills())
    assert [p.provenance.attempts for p in result.pages] == [1, 2]
    assert clock.sleeps == [2.0]


def test_only_the_slug_bound_path_is_requested() -> None:
    client, router, _ = make_client()
    run(client.read_tournament_fills())
    assert router.paths() == [
        "/tournaments/alpha-tournament",
        "/tournaments/alpha-tournament/portfolio/fills",
    ]
    assert all(not p.startswith(("/portfolio", "/account")) for p in router.paths())
    assert f"{API}/portfolio/fills" not in [r.url.path for r in router.requests]
