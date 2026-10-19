"""A2-A: read_tournament_positions (design T5-T10) and its documented 409.

All offline: synthetic, spec-derived bodies through httpx.MockTransport.
"""

from __future__ import annotations

import asyncio
import copy
from decimal import Decimal
from typing import Any

import httpx
import pytest
from account_kit import (
    ORDERS,
    POSITIONS,
    TOURNAMENT_PATH,
    exact_body,
    exact_response,
    taking,
    until,
)
from sig_testkit import (
    FakeClock,
    Router,
    error_body,
    fixture,
    json_response,
    make_client,
    raw_response,
    run,
)

from engine.sig_client.account_reads import (
    CoverageState,
    MissingValuation,
    PositionsRead,
    ScopeEvidence,
    UnvaluedHoldingsRead,
    VerificationUse,
)
from engine.sig_client.errors import (
    SigAuthError,
    SigConflictError,
    SigDeadlineExceededError,
    SigNotFoundError,
    SigResponseError,
    SigRetryExhaustedError,
)
from engine.sig_client.rate_limiter import RateLimiter, RequestKind
from engine.sig_client.wire import PositionLot, PositionsSummary, TournamentPosition

READ = RequestKind.READ


def positions_with(mutate: Any) -> dict[str, Any]:
    body = exact_body("tournament_positions")
    mutate(body)
    return body


def conflict_body(mutate: Any = None) -> dict[str, Any]:
    body = copy.deepcopy(fixture("positions_missing_valuation"))
    if mutate is not None:
        mutate(body)
    return body


def read_positions(router: Router) -> Any:
    client, _, _ = make_client(router)
    return run(client.read_tournament_positions())


# --- T6: rows kept exactly as sent -------------------------------------------------------


def test_t6_rows_lots_and_summary_are_kept_exactly_as_sent() -> None:
    result = read_positions(
        Router({POSITIONS: exact_response(200, exact_body("tournament_positions"))})
    )
    assert isinstance(result, PositionsRead)
    yes, no, flat = result.rows
    assert all(type(row) is TournamentPosition for row in result.rows)
    assert type(result.summary) is PositionsSummary
    assert yes.quantity == 10 and yes.currentPrice == Decimal("0.535")
    assert yes.unrealizedPnlPct.as_tuple() == Decimal("27.380952380952380952").as_tuple()
    assert [lot.side for lot in yes.lots] == ["yes", "yes"]
    assert type(yes.lots[0]) is PositionLot and yes.lots[1].entryPrice == Decimal("0.45")
    # A fractional NO holding: sign kept, nothing complemented or rounded.
    assert no.quantity.as_tuple() == Decimal("-2.5").as_tuple()
    assert no.avgCost == Decimal("0.61") and no.currentPrice == Decimal("0.585")
    assert no.marketValue == Decimal("-1.4625") and no.option is None
    assert no.lots[0].side == "NO" and no.lots[0].quantity == Decimal("2.5")
    # A zero row with the documented null currentPrice.
    assert flat.quantity == 0 and flat.currentPrice is None and flat.settled is True
    assert flat.lots == []
    assert result.summary.totalMarketValue == Decimal("3.8875")
    assert yes.lots[0].openedAt == "2026-10-02T12:00:00.000Z"  # timestamps kept as strings


def test_t6_unknown_fields_are_ignored_at_every_level() -> None:
    def mutate(body: dict[str, Any]) -> None:
        body["futureTopLevel"] = {"x": 1}
        body["positions"][0]["futureRowField"] = [1, 2]
        body["positions"][0]["lots"][0]["futureLotField"] = None
        body["summary"]["futureSummaryField"] = "y"

    result = read_positions(Router({POSITIONS: exact_response(200, positions_with(mutate))}))
    row = result.rows[0]
    assert not hasattr(row, "futureRowField") and row.model_extra is None
    assert row.lots[0].model_extra is None and result.summary.model_extra is None


@pytest.mark.parametrize("side", ["", "weird", "Yes", "SHORT", "no "])
def test_t6_lot_side_is_an_unconstrained_string(side: str) -> None:
    body = positions_with(lambda b: b["positions"][0]["lots"][0].update(side=side))
    result = read_positions(Router({POSITIONS: exact_response(200, body)}))
    assert result.rows[0].lots[0].side == side


@pytest.mark.parametrize(
    ("field", "literal"),
    [
        ("avgCost", "1.5"),  # no [0, 1] bound: denomination undocumented
        ("currentPrice", "-0.2"),
        ("marketValue", "-123456789.123456789"),
        ("unrealizedPnlPct", "1e3"),
        ("quantity", "0.000001"),
    ],
)
def test_t6_no_documented_range_means_no_bound(field: str, literal: str) -> None:
    body = positions_with(lambda b: b["positions"][0].update({field: Decimal(literal)}))
    result = read_positions(Router({POSITIONS: exact_response(200, body)}))
    assert getattr(result.rows[0], field).as_tuple() == Decimal(literal).as_tuple()


def test_t6_rows_breaking_a_documented_rule_are_kept_for_the_snapshot() -> None:
    # K6 (nonzero quantity with a null currentPrice) is an A3 consistency check: the raw
    # read keeps the schema-valid row as sent and decides nothing.
    body = positions_with(lambda b: b["positions"][0].update(currentPrice=None))
    result = read_positions(Router({POSITIONS: exact_response(200, body)}))
    assert result.rows[0].quantity == 10 and result.rows[0].currentPrice is None


def test_t6_an_empty_response_is_retained_as_observed() -> None:
    body = {
        "positions": [],
        "summary": {"totalMarketValue": 0, "totalCostBasis": 0, "totalUnrealizedPnl": 0},
    }
    result = read_positions(Router({POSITIONS: json_response(200, body)}))
    assert isinstance(result, PositionsRead) and result.rows == ()


@pytest.mark.parametrize(
    ("mutate", "location"),
    [
        (lambda b: b.pop("summary"), "summary"),
        (lambda b: b.update(positions=None), "positions"),
        (lambda b: b["positions"][0].update(quantity="10"), "positions.0.quantity"),
        (lambda b: b["positions"][0].update(quantity=True), "positions.0.quantity"),
        (lambda b: b["positions"][0].update(currentPrice="0.5"), "positions.0.currentPrice"),
        (lambda b: b["positions"][0].update(marketValue=None), "positions.0.marketValue"),
        (lambda b: b["positions"][0].update(settled=1), "positions.0.settled"),
        (lambda b: b["positions"][0].update(exchangeId=36), "positions.0.exchangeId"),
        (lambda b: b["positions"][0].pop("option"), "positions.0.option"),
        (lambda b: b["positions"][0].update(lots=None), "positions.0.lots"),
        (lambda b: b["positions"][0]["lots"][0].update(side=5), "lots.0.side"),
        (lambda b: b["positions"][0]["lots"][0].pop("openedAt"), "lots.0.openedAt"),
        (lambda b: b["summary"].update(totalCostBasis=None), "summary.totalCostBasis"),
    ],
)
def test_malformed_positions_are_rejected_naming_the_location(mutate: Any, location: str) -> None:
    with pytest.raises(SigResponseError) as info:
        read_positions(Router({POSITIONS: exact_response(200, positions_with(mutate))}))
    assert location in info.value.reason


@pytest.mark.parametrize(
    "content", [b"", b"not json", b'{"positions": [], "summary": NaN}', b"\xff"]
)
def test_non_json_positions_body_is_rejected(content: bytes) -> None:
    with pytest.raises(SigResponseError, match="not valid JSON"):
        read_positions(Router({POSITIONS: raw_response(200, content)}))


# --- T7: the documented 409 as a typed result ---------------------------------------------


def test_t7_documented_409_becomes_unvalued_holdings() -> None:
    client, router, _ = make_client(Router({POSITIONS: json_response(409, conflict_body())}))
    result = run(client.read_tournament_positions())
    assert isinstance(result, UnvaluedHoldingsRead)
    assert result.missing == (
        MissingValuation(exchange_id="36", market_id="26"),
        MissingValuation(exchange_id="41", market_id="29"),
    )
    assert result.provenance.http_status == 409 and result.provenance.attempts == 1
    assert result.provenance.scope is ScopeEvidence.VERIFIED_SLUG_PATH
    assert router.count("/tournaments/alpha-tournament/portfolio/positions") == 1
    # Titles, settlement options and the message are not kept anywhere in the result.
    text = repr(result)
    for dropped in ("Placeholder market", "Placeholder option", "cannot be valued"):
        assert dropped not in text


def test_t7_pairs_keep_the_order_and_duplicates_sent() -> None:
    def mutate(body: dict[str, Any]) -> None:
        items = body["error"]["details"]["missingValuations"]
        items.reverse()
        items.append(copy.deepcopy(items[0]))

    result = read_positions(Router({POSITIONS: json_response(409, conflict_body(mutate))}))
    assert [(m.exchange_id, m.market_id) for m in result.missing] == [
        ("41", "29"),
        ("36", "26"),
        ("41", "29"),
    ]


def test_t7_extra_fields_in_the_409_body_are_ignored() -> None:
    def mutate(body: dict[str, Any]) -> None:
        body["error"]["details"]["missingValuations"][0]["futureField"] = 1
        body["error"]["details"]["otherDetail"] = [1]
        body["error"]["requestId"] = "x"

    result = read_positions(Router({POSITIONS: json_response(409, conflict_body(mutate))}))
    assert isinstance(result, UnvaluedHoldingsRead) and len(result.missing) == 2


# --- T8: anything else stays a generic SigConflictError -------------------------------------


def _item(body: dict[str, Any]) -> dict[str, Any]:
    return body["error"]["details"]["missingValuations"][0]


@pytest.mark.parametrize(
    ("mutate", "code"),
    [
        (lambda b: b["error"].update(code="FORBIDDEN"), "FORBIDDEN"),
        (lambda b: b["error"].update(code="REQUEST_IN_FLIGHT"), "REQUEST_IN_FLIGHT"),
        (lambda b: b["error"].update(code="SOMETHING_ELSE"), None),
        (lambda b: b["error"].pop("code"), None),
        (lambda b: b["error"].update(code="conflict"), None),
        (lambda b: b["error"].pop("message"), "CONFLICT"),
        (lambda b: b["error"].update(message=None), "CONFLICT"),
        (lambda b: b["error"].pop("details"), "CONFLICT"),
        (lambda b: b["error"].update(details=None), "CONFLICT"),
        (lambda b: b["error"].update(details=[]), "CONFLICT"),
        (lambda b: b["error"]["details"].pop("missingValuations"), "CONFLICT"),
        (lambda b: b["error"]["details"].update(missingValuations=None), "CONFLICT"),
        (lambda b: b["error"]["details"].update(missingValuations=[]), "CONFLICT"),
        (lambda b: b["error"]["details"].update(missingValuations="36"), "CONFLICT"),
        (lambda b: b["error"]["details"].update(missingValuations=[None]), "CONFLICT"),
        (lambda b: _item(b).update(exchangeId="36a"), "CONFLICT"),
        (lambda b: _item(b).update(exchangeId=""), "CONFLICT"),
        (lambda b: _item(b).update(exchangeId=" 36"), "CONFLICT"),
        (lambda b: _item(b).update(exchangeId="36\n"), "CONFLICT"),
        (lambda b: _item(b).update(exchangeId="٣٦"), "CONFLICT"),  # Arabic-Indic
        (lambda b: _item(b).update(exchangeId=36), "CONFLICT"),
        (lambda b: _item(b).update(marketId="-26"), "CONFLICT"),
        (lambda b: _item(b).update(marketId=None), "CONFLICT"),
        (lambda b: _item(b).pop("marketTitle"), "CONFLICT"),
        (lambda b: _item(b).update(marketTitle=7), "CONFLICT"),
        (lambda b: _item(b).pop("settlementOption"), "CONFLICT"),
        (lambda b: _item(b).update(settlementOption=False), "CONFLICT"),
    ],
)
def test_t8_other_or_malformed_409s_raise_sig_conflict_error(mutate: Any, code: str | None) -> None:
    client, router, _ = make_client(Router({POSITIONS: json_response(409, conflict_body(mutate))}))
    with pytest.raises(SigConflictError) as info:
        run(client.read_tournament_positions())
    assert info.value.status == 409 and info.value.code == code
    assert router.count("/tournaments/alpha-tournament/portfolio/positions") == 1  # not retried


@pytest.mark.parametrize(
    "content", [b"", b"<html>conflict</html>", b"[]", b'{"error": []}', b'{"error": {"code": NaN}}']
)
def test_t8_non_envelope_409_bodies_raise_sig_conflict_error(content: bytes) -> None:
    with pytest.raises(SigConflictError):
        read_positions(Router({POSITIONS: raw_response(409, content)}))


def test_t8_the_typed_409_applies_to_the_positions_read_only() -> None:
    valid = json_response(409, conflict_body())
    client, _, _ = make_client(Router({ORDERS: valid}))
    with pytest.raises(SigConflictError):
        run(client.read_open_orders())
    client, _, _ = make_client(Router({TOURNAMENT_PATH: valid}))
    with pytest.raises(SigConflictError):
        run(client.read_tournament_balance())


# --- T9: 403 codes stay distinguishable ------------------------------------------------------


@pytest.mark.parametrize("code", ["FORBIDDEN", "INSUFFICIENT_SCOPES", "ACCOUNT_BANNED"])
def test_t9_403_raises_auth_error_with_its_documented_code(code: str) -> None:
    body = {"error": {"code": code, "message": "placeholder"}}
    client, router, _ = make_client(Router({POSITIONS: json_response(403, body)}))
    with pytest.raises(SigAuthError) as info:
        run(client.read_tournament_positions())
    assert info.value.status == 403 and info.value.code == code
    assert router.count("/tournaments/alpha-tournament/portfolio/positions") == 1


def test_404_raises_as_for_any_read() -> None:
    client, _, _ = make_client(Router({POSITIONS: json_response(404, error_body("NOT_FOUND"))}))
    with pytest.raises(SigNotFoundError):
        run(client.read_tournament_positions())


# --- T10: retries within one deadline, shared limiter ------------------------------------------


def test_t10_503_with_retry_after_is_retried_within_one_deadline() -> None:
    clock = FakeClock()
    shared = RateLimiter(clock=clock, sleep=clock.sleep)
    router = Router(
        {
            POSITIONS: [
                json_response(503, error_body("SERVICE_UNAVAILABLE"), {"Retry-After": "3"}),
                json_response(200, fixture("tournament_positions")),
            ]
        }
    )
    client, _, _ = make_client(router, clock=clock, limiter=shared)
    other, _, _ = make_client(Router(), clock=clock, limiter=shared)
    result = run(client.read_tournament_positions())
    assert isinstance(result, PositionsRead)
    assert result.provenance.attempts == 2 and result.provenance.http_status == 200
    assert clock.sleeps == [3.0]  # Retry-After respected, not shortened
    assert result.provenance.finished_mono - result.provenance.started_mono == 3.0
    # The verification read is its own logical read with its own attempt count.
    assert result.provenance.verification is VerificationUse.PERFORMED
    assert result.provenance.verification_read is not None
    assert result.provenance.verification_read.attempts == 1
    assert shared.used(READ) == 3 and other.limiter is shared


def test_t10_429_cooldown_is_imposed_on_the_shared_limiter() -> None:
    clock = FakeClock()
    shared = RateLimiter(clock=clock, sleep=clock.sleep)
    router = Router(
        {
            POSITIONS: [
                json_response(429, error_body("RATE_LIMITED"), {"Retry-After": "5"}),
                json_response(200, fixture("tournament_positions")),
            ]
        }
    )
    client, _, _ = make_client(router, clock=clock, limiter=shared)
    result = run(client.read_tournament_positions())
    assert result.provenance.attempts == 2
    assert clock.now >= 1005.0  # the cooldown held the retry back at least 5 s


def test_retries_exhausted_and_late_409s_return_nothing() -> None:
    client, _, _ = make_client(Router({POSITIONS: [json_response(503, error_body("TX_CONFLICT"))]}))
    with pytest.raises(SigRetryExhaustedError) as info:
        run(client.read_tournament_positions())
    assert info.value.attempts == 4

    clock = FakeClock()
    late = taking(clock, 30.0, json_response(409, conflict_body()))
    client, _, _ = make_client(Router({POSITIONS: late}), clock=clock)
    with pytest.raises(SigDeadlineExceededError) as deadline:
        run(client.read_tournament_positions())
    assert deadline.value.phase == "request"


# --- verification is distinguished from the endpoint's own attempts ---------------------------


def test_verification_attempts_are_reported_apart_from_the_positions_attempts() -> None:
    clock = FakeClock()
    router = Router(
        {
            TOURNAMENT_PATH: [
                json_response(503, error_body("SERVICE_UNAVAILABLE")),
                json_response(503, error_body("SERVICE_UNAVAILABLE")),
                json_response(200, fixture("tournament")),
            ],
        }
    )
    client, _, _ = make_client(router, clock=clock)

    async def scenario() -> tuple[Any, Any]:
        return await client.read_tournament_positions(), await client.read_tournament_positions()

    first, second = run(scenario())
    one = first.provenance
    assert one.attempts == 1 and one.verification is VerificationUse.PERFORMED
    check = one.verification_read
    assert check is not None and check.attempts == 3
    assert check.route == "/tournaments/{slug}" and check.scope is ScopeEvidence.ECHO
    assert check.finished_mono <= one.started_mono  # verification came first
    assert second.provenance.verification is VerificationUse.CACHED
    assert second.provenance.verification_read is None
    assert router.count("/tournaments/alpha-tournament") == 3


def test_concurrent_cold_reads_verify_once_and_say_which_one_did() -> None:
    client, router, _ = make_client()

    async def scenario() -> list[Any]:
        return await asyncio.gather(
            client.read_tournament_positions(), client.read_tournament_positions()
        )

    results = run(scenario())
    uses = sorted(r.provenance.verification.value for r in results)
    assert uses == ["cached", "performed"]
    assert router.count("/tournaments/alpha-tournament") == 1


def test_positions_provenance_fields() -> None:
    result = read_positions(Router())
    provenance = result.provenance
    assert provenance.route == "/tournaments/{slug}/portfolio/positions"
    assert provenance.path == "/tournaments/alpha-tournament/portfolio/positions"
    assert provenance.params == () and provenance.cursor_sent is False
    assert provenance.page_index is None
    assert provenance.coverage_state is CoverageState.NOT_APPLICABLE


# --- cancellation -----------------------------------------------------------------------------


@pytest.mark.parametrize("hang_on", [TOURNAMENT_PATH, POSITIONS])
def test_cancellation_returns_nothing_and_the_next_read_starts_clean(hang_on: str) -> None:
    gate = asyncio.Event()

    async def hanging(request: httpx.Request) -> httpx.Response:
        await gate.wait()
        raise AssertionError("unreachable")

    router = Router({hang_on: hanging})
    client, _, _ = make_client(router)

    async def scenario() -> Any:
        task = asyncio.create_task(client.read_tournament_positions())
        await until(lambda: any(r.url.path == hang_on for r in router.requests))
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        router.routes[hang_on] = Router().routes[hang_on]
        return await client.read_tournament_positions()

    result = run(scenario())
    assert isinstance(result, PositionsRead) and result.provenance.attempts == 1
    expected = VerificationUse.PERFORMED if hang_on == TOURNAMENT_PATH else VerificationUse.CACHED
    assert result.provenance.verification is expected
    if expected is VerificationUse.PERFORMED:
        assert result.provenance.verification_read.attempts == 1  # type: ignore[union-attr]


def test_the_positions_response_is_not_logged(caplog: pytest.LogCaptureFixture) -> None:
    caplog.set_level("DEBUG")
    body = exact_body("tournament_positions")
    read_positions(Router({POSITIONS: exact_response(200, body)}))
    for value in ("Placeholder market", "placeholder-lot", "0.535", "3.8875"):
        assert value not in caplog.text
