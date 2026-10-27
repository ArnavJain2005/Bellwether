"""A2-B: read_tournament_settlements (GET /tournaments/{slug}/portfolio/settlements).

Design T11 and T16. All offline: synthetic, spec-derived bodies through
httpx.MockTransport. Nothing here is validated against a real SIG response.
"""

from __future__ import annotations

import copy
from decimal import Decimal
from typing import Any

import httpx
import pytest
from account_kit import TOURNAMENT_PATH, exact_body, exact_response
from sig_testkit import Router, error_body, json_response, make_client, run

from engine.sig_client.account_reads import (
    CoverageState,
    PageRecord,
    ReadProvenance,
    ScopeEvidence,
    SettlementsWalk,
)
from engine.sig_client.errors import SigPaginationError, SigResponseError
from engine.sig_client.wire import EconomicCommandCoverage, TournamentSettlementEntry

SETTLEMENTS = f"{TOURNAMENT_PATH}/portfolio/settlements"


def entry(entry_id: str, **changes: Any) -> dict[str, Any]:
    base = copy.deepcopy(exact_body("tournament_settlements")["data"][0])
    return base | {"id": entry_id} | changes


def page(
    entries: list[dict[str, Any]], *, cursor: str | None, complete: Any = True, limit: int = 200
) -> dict[str, Any]:
    return {
        "data": entries,
        "pagination": {"limit": limit, "hasMore": cursor is not None, "nextCursor": cursor},
        "coverage": {"complete": complete},
    }


def by_cursor(pages: dict[str | None, Any]) -> Any:
    def handler(request: httpx.Request) -> httpx.Response:
        found = pages[request.url.params.get("cursor")]
        return found if isinstance(found, httpx.Response) else exact_response(200, found)

    return handler


def walk(pages: dict[str | None, Any], **kwargs: Any) -> tuple[SettlementsWalk, Router]:
    client, router, _ = make_client(Router({SETTLEMENTS: by_cursor(pages)}))
    return run(client.read_tournament_settlements(**kwargs)), router


def test_entries_are_kept_exactly_as_sent() -> None:
    result, router = walk({None: exact_body("tournament_settlements")})
    win, correction = result.entries
    assert all(type(e) is TournamentSettlementEntry for e in result.entries)
    assert win.type == "win" and win.shares == 100 and win.avgEntryPrice == Decimal("0.42")
    assert win.payout == 100 and win.realizedPnl == 58
    assert correction.type is None and correction.shares is None and correction.payout == 0
    assert result.complete is True and result.incomplete_pages == ()
    provenance = result.pages[0].provenance
    assert provenance.route == "/tournaments/{slug}/portfolio/settlements"
    assert provenance.scope is ScopeEvidence.VERIFIED_SLUG_PATH
    assert provenance.coverage_state is CoverageState.PRESENT
    assert provenance.coverage == EconomicCommandCoverage(complete=True)
    assert router.requests[-1].url.params["limit"] == "200"


# --- T16: incomplete economic coverage is never labelled complete -----------------------------


def test_t16_a_page_reporting_incomplete_marks_the_walk_incomplete() -> None:
    pages = {
        None: page([entry("s1")], cursor="c2", complete=True),
        "c2": page([entry("s2")], cursor="c3", complete=False),
        "c3": page([entry("s3")], cursor=None, complete=True),
    }
    result, router = walk(pages)
    assert [e.id for e in result.entries] == ["s1", "s2", "s3"]  # never truncated
    assert result.complete is False and result.incomplete_pages == (1,)
    assert [p.provenance.coverage.complete for p in result.pages] == [True, False, True]
    assert sum(r.url.path == SETTLEMENTS for r in router.requests) == 3


def test_t16_every_incomplete_page_is_listed() -> None:
    pages = {
        None: page([entry("s1")], cursor="c2", complete=False),
        "c2": page([entry("s2")], cursor=None, complete=False),
    }
    result, _ = walk(pages)
    assert result.complete is False and result.incomplete_pages == (0, 1)


def test_t16_a_projection_checkpoint_on_settlements_is_ignored_not_kept() -> None:
    body = page([entry("s1")], cursor=None)
    body["coverage"]["projectedThroughSequence"] = 184233
    result, _ = walk({None: body})
    coverage = result.pages[0].provenance.coverage
    assert coverage == EconomicCommandCoverage(complete=True)
    assert not hasattr(coverage, "projectedThroughSequence") and coverage.model_extra is None


def test_the_walk_result_cannot_claim_completeness_it_does_not_have() -> None:
    result, _ = walk({None: page([entry("s1")], cursor=None, complete=False)})
    # The incomplete page is listed correctly, so only the completeness rule applies.
    with pytest.raises(ValueError, match="complete is True exactly when no page reported false"):
        SettlementsWalk(
            entries=result.entries, pages=result.pages, complete=True, incomplete_pages=(0,)
        )
    with pytest.raises(ValueError, match="incomplete_pages must list exactly"):
        SettlementsWalk(
            entries=result.entries, pages=result.pages, complete=False, incomplete_pages=(3,)
        )
    with pytest.raises(ValueError, match="incomplete_pages must list exactly"):
        SettlementsWalk(
            entries=result.entries, pages=result.pages, complete=False, incomplete_pages=()
        )


def test_a_page_without_economic_coverage_cannot_form_a_settlements_walk() -> None:
    result, _ = walk({None: page([entry("s1")], cursor=None)})
    provenance = result.pages[0].provenance
    stripped = ReadProvenance(
        **{
            **{f: getattr(provenance, f) for f in provenance.__dataclass_fields__},
            "coverage_state": CoverageState.OMITTED,
            "coverage": None,
        }
    )
    with pytest.raises(TypeError, match="economic-command coverage"):
        SettlementsWalk(
            entries=(),
            pages=(PageRecord(provenance=stripped, item_count=0),),
            complete=True,
            incomplete_pages=(),
        )


@pytest.mark.parametrize(
    "coverage", [None, {}, {"complete": None}, {"complete": 1}, {"complete": "true"}]
)
def test_coverage_is_required_with_a_boolean_complete(coverage: Any) -> None:
    body = page([entry("s1")], cursor=None)
    if coverage is None:
        del body["coverage"]
    else:
        body["coverage"] = coverage
    with pytest.raises(SigResponseError, match="coverage"):
        walk({None: body})


# --- T11: a 503 the endpoint does not list is still retried (documented globally) --------------


def test_t11_settlements_503_is_retried_within_one_deadline() -> None:
    router = Router(
        {
            SETTLEMENTS: [
                json_response(503, error_body("SERVICE_UNAVAILABLE"), {"Retry-After": "2"}),
                exact_response(200, page([entry("s1")], cursor=None)),
            ]
        }
    )
    client, _, clock = make_client(router)
    result = run(client.read_tournament_settlements())
    assert result.pages[0].provenance.attempts == 2
    assert clock.sleeps == [2.0]


# --- complete or error -----------------------------------------------------------------------


@pytest.mark.parametrize(
    ("pages", "message"),
    [
        ({None: page([entry("s1")], cursor="A"), "A": page([entry("s2")], cursor="A")}, "repeated"),
        (
            {None: page([entry("s1")], cursor="A"), "A": page([entry("s1")], cursor=None)},
            "more than one page",
        ),
        (
            {
                None: page([entry("s1")], cursor="A"),
                "A": page([entry("s2")], cursor="B"),
                "B": page([entry("s3")], cursor="C"),
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


@pytest.mark.parametrize(
    ("change", "location"),
    [
        ({"type": "WIN"}, "data.0.type"),
        ({"payout": None}, "data.0.payout"),
        ({"payout": "100"}, "data.0.payout"),
        ({"shares": True}, "data.0.shares"),
        ({"id": 7}, "data.0.id"),
    ],
)
def test_malformed_entries_are_rejected_naming_the_location(
    change: dict[str, Any], location: str
) -> None:
    with pytest.raises(SigResponseError) as info:
        walk({None: page([entry("s1", **change)], cursor=None)})
    assert location in info.value.reason


@pytest.mark.parametrize("field", ["type", "createdAt", "shares", "realizedPnl"])
def test_nullable_fields_are_still_required(field: str) -> None:
    row = entry("s1")
    del row[field]
    with pytest.raises(SigResponseError, match=field):
        walk({None: page([row], cursor=None)})
