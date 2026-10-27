"""A2-B: read_tournament_pnl (GET /tournaments/{slug}/portfolio/pnl), design T19.

All offline: synthetic, spec-derived bodies through httpx.MockTransport. Nothing here is
validated against a real SIG response (the P&L read has never been captured).
"""

from __future__ import annotations

import copy
from decimal import Decimal
from typing import Any

import httpx
import pytest
from account_kit import TOURNAMENT_PATH, exact_body, exact_response
from sig_testkit import FakeClock, Router, error_body, fixture, json_response, make_client, run

from engine.sig_client.account_reads import (
    CoverageState,
    MissingValuation,
    PnlRead,
    ScopeEvidence,
    UnvaluedHoldingsRead,
    VerificationUse,
)
from engine.sig_client.errors import (
    SigArgumentError,
    SigAuthError,
    SigConflictError,
    SigDeadlineExceededError,
    SigResponseError,
)
from engine.sig_client.wire import TournamentPnl

PNL = f"{TOURNAMENT_PATH}/portfolio/pnl"
PERIODS = ["day", "week", "month", "quarter", "year", "all"]


def pnl_body(**changes: Any) -> dict[str, Any]:
    return exact_body("tournament_pnl") | changes


def echoing(**changes: Any) -> Any:
    """A handler that answers for whatever period was requested."""

    def handler(request: httpx.Request) -> httpx.Response:
        return exact_response(200, pnl_body(period=request.url.params["period"], **changes))

    return handler


def conflict_body(mutate: Any = None) -> dict[str, Any]:
    body = copy.deepcopy(fixture("positions_missing_valuation"))
    if mutate is not None:
        mutate(body)
    return body


# --- T19: period sent explicitly ---------------------------------------------------------------


def test_t19_period_all_is_sent_explicitly_by_default() -> None:
    client, router, _ = make_client(Router({PNL: echoing()}))
    result = run(client.read_tournament_pnl())
    assert isinstance(result, PnlRead) and type(result.pnl) is TournamentPnl
    request = router.requests[-1]
    assert request.url.path == PNL and dict(request.url.params) == {"period": "all"}
    assert result.provenance.params == (("period", "all"),)
    assert result.provenance.route == "/tournaments/{slug}/portfolio/pnl"
    assert result.provenance.scope is ScopeEvidence.VERIFIED_SLUG_PATH
    assert result.provenance.coverage_state is CoverageState.NOT_APPLICABLE
    assert result.provenance.page_index is None
    assert result.provenance.verification is VerificationUse.PERFORMED


@pytest.mark.parametrize("period", PERIODS)
def test_every_documented_period_is_sent_as_given(period: str) -> None:
    client, router, _ = make_client(Router({PNL: echoing()}))
    assert run(client.read_tournament_pnl(period=period)).pnl.period == period
    assert router.requests[-1].url.params["period"] == period


@pytest.mark.parametrize("period", ["", "ALL", "quarterly", None, 1])
def test_an_undocumented_period_sends_nothing(period: Any) -> None:
    client, router, _ = make_client()
    with pytest.raises(SigArgumentError):
        run(client.read_tournament_pnl(period=period))
    assert router.requests == []


def test_a_response_for_another_period_is_rejected() -> None:
    client, _, _ = make_client(Router({PNL: exact_response(200, pnl_body(period="quarter"))}))
    with pytest.raises(SigResponseError, match="different period"):
        run(client.read_tournament_pnl())


# --- fields kept exactly as sent ---------------------------------------------------------------


def test_fields_are_kept_exactly_as_sent() -> None:
    body = pnl_body(
        totalAccountValue=Decimal("12345678901234567890.123456789"),
        roi=Decimal("-6.95"),
        sharpe=None,
        periodPnl=None,
        futureField={"x": 1},
    )
    client, _, _ = make_client(Router({PNL: exact_response(200, body)}))
    pnl = run(client.read_tournament_pnl()).pnl
    assert pnl.totalAccountValue.as_tuple() == Decimal("12345678901234567890.123456789").as_tuple()
    assert pnl.roi == Decimal("-6.95") and pnl.sharpe is None and pnl.periodPnl is None
    assert pnl.periodStart is None and pnl.periodEnd == "2026-10-20T00:00:00.000Z"
    assert pnl.unrealizedPnl == 30 and pnl.model_extra is None


@pytest.mark.parametrize(
    ("change", "location"),
    [
        ({"totalAccountValue": None}, "totalAccountValue"),
        ({"totalAccountValue": "930.5"}, "totalAccountValue"),
        ({"unrealizedPnl": True}, "unrealizedPnl"),
        ({"period": "decade"}, "period"),
        ({"periodEnd": None}, "periodEnd"),
        ({"roi": "x"}, "roi"),
    ],
)
def test_malformed_pnl_is_rejected_naming_the_location(
    change: dict[str, Any], location: str
) -> None:
    client, _, _ = make_client(Router({PNL: exact_response(200, pnl_body(**change))}))
    with pytest.raises(SigResponseError) as info:
        run(client.read_tournament_pnl())
    assert location in info.value.reason


@pytest.mark.parametrize(
    "field", ["periodStart", "periodPnl", "roi", "sharpe", "totalHoldingsValue"]
)
def test_required_fields_cannot_be_missing(field: str) -> None:
    body = pnl_body()
    del body[field]
    client, _, _ = make_client(Router({PNL: exact_response(200, body)}))
    with pytest.raises(SigResponseError, match=field):
        run(client.read_tournament_pnl())


# --- the documented 409 ------------------------------------------------------------------------


def test_documented_409_becomes_unvalued_holdings() -> None:
    client, _, _ = make_client(Router({PNL: json_response(409, conflict_body())}))
    result = run(client.read_tournament_pnl())
    assert isinstance(result, UnvaluedHoldingsRead)
    assert result.missing == (
        MissingValuation(exchange_id="36", market_id="26"),
        MissingValuation(exchange_id="41", market_id="29"),
    )
    assert result.provenance.route == "/tournaments/{slug}/portfolio/pnl"
    assert result.provenance.http_status == 409 and result.provenance.params == (("period", "all"),)
    assert "Placeholder market" not in repr(result)


def _item(body: dict[str, Any]) -> dict[str, Any]:
    return body["error"]["details"]["missingValuations"][0]


@pytest.mark.parametrize(
    ("mutate", "code"),
    [
        (lambda b: b["error"].update(code="FORBIDDEN"), "FORBIDDEN"),
        (lambda b: b["error"]["details"].update(missingValuations=[]), "CONFLICT"),
        (lambda b: _item(b).update(exchangeId="36a"), "CONFLICT"),
        (lambda b: _item(b).update(marketId=26), "CONFLICT"),
        (lambda b: b["error"].pop("details"), "CONFLICT"),
        (lambda b: b["error"].pop("message"), "CONFLICT"),
    ],
)
def test_other_or_malformed_409s_raise_sig_conflict_error(mutate: Any, code: str) -> None:
    client, router, _ = make_client(Router({PNL: json_response(409, conflict_body(mutate))}))
    with pytest.raises(SigConflictError) as info:
        run(client.read_tournament_pnl())
    assert info.value.status == 409 and info.value.code == code
    assert sum(r.url.path == PNL for r in router.requests) == 1


@pytest.mark.parametrize("code", ["FORBIDDEN", "INSUFFICIENT_SCOPES"])
def test_403_codes_stay_distinguishable(code: str) -> None:
    body = {"error": {"code": code, "message": "placeholder"}}
    client, _, _ = make_client(Router({PNL: json_response(403, body)}))
    with pytest.raises(SigAuthError) as info:
        run(client.read_tournament_pnl())
    assert info.value.code == code


def test_503_is_retried_and_a_late_409_is_not_returned() -> None:
    router = Router(
        {
            PNL: [
                json_response(503, error_body("SERVICE_UNAVAILABLE"), {"Retry-After": "3"}),
                echoing(),
            ]
        }
    )
    client, _, clock = make_client(router)
    result = run(client.read_tournament_pnl())
    assert result.provenance.attempts == 2 and clock.sleeps == [3.0]

    late_clock = FakeClock()

    def late(request: httpx.Request) -> httpx.Response:
        late_clock.now += 31.0
        return json_response(409, conflict_body())

    client, _, _ = make_client(Router({PNL: late}), clock=late_clock)
    with pytest.raises(SigDeadlineExceededError):
        run(client.read_tournament_pnl())
