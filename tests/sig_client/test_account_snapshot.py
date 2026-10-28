"""A3: assemble_account_snapshot over the A2 reads, design T21 to T38 and T41 to T44.

All offline: synthetic, spec-derived bodies through httpx.MockTransport. Nothing here is
validated against a real SIG response.
"""

from __future__ import annotations

from decimal import Decimal

import pytest
from account_kit import ORDERS, POSITIONS, TOURNAMENT_PATH, exact_response, taking
from sig_testkit import API, FakeClock, fixture, json_response, make_client, run
from snapshot_kit import (
    FILLS,
    FULL,
    ORDER_FILLS,
    OTHER_UUID,
    PNL,
    SETTLEMENTS,
    assemble,
    clean_router,
    error,
    fills,
    forbidden,
    malformed,
    missing_valuation,
    open_order,
    order_fills,
    orders,
    position_row,
    positions,
    sequence,
    settlements,
    tournament,
)

from engine.account import snapshot as snap
from engine.account.snapshot import (
    CANNOT_VOUCH_FOR,
    CLOSING,
    DOES_NOT_COVER,
    OPEN_ORDERS,
    OPENING,
    ORDER_FILLS_ANY,
    VALUATION,
    AccountStatus,
    BalanceObservation,
    CashSource,
    ComponentId,
    ComponentKind,
    ComponentResult,
    FindingKind,
    Identity,
    Membership,
    Outcome,
    ReservationKnowledge,
    SeedOrCurrent,
    SnapshotRequest,
    TimingSource,
    assemble_account_snapshot,
    build_account_snapshot,
)
from engine.sig_client.account_reads import (
    OrdersWalk,
    PositionsRead,
    TournamentBalanceRead,
    UnvaluedHoldingsRead,
)

ORDER_1001 = ComponentId(ComponentKind.ORDER_FILLS, 1001)
PENDING = {"isPendingEnrolment": True, "myBalance": 1000, "initialBalance": 1000}


def checks(snapshot, check_id):
    return [f for f in snapshot.findings if f.check_id == check_id]


def only_check_ids(snapshot):
    return sorted(f.check_id for f in snapshot.findings)


# --- baseline ----------------------------------------------------------------------------


def test_default_assembly_reads_required_components_in_order():
    snapshot, router, _ = assemble()
    assert router.paths() == [
        "/tournaments/alpha-tournament",
        "/tournaments/alpha-tournament/portfolio/positions",
        "/orders",
        "/tournaments/alpha-tournament",
    ]
    assert router.requests[2].url.params["status"] == "open"
    assert snapshot.requested == (OPENING, snap.POSITIONS, OPEN_ORDERS, CLOSING)
    assert snapshot.status is AccountStatus.OBSERVED_UNRESOLVED
    assert len(snapshot.reads) == 4


def test_full_assembly_reads_every_optional_component_between_the_balances():
    snapshot, router, _ = assemble(request=FULL)
    assert router.paths() == [
        "/tournaments/alpha-tournament",
        "/tournaments/alpha-tournament/portfolio/positions",
        "/orders",
        "/tournaments/alpha-tournament/portfolio/fills",
        "/tournaments/alpha-tournament/portfolio/pnl",
        "/tournaments/alpha-tournament/portfolio/settlements",
        "/orders/1001/fills",
        "/tournaments/alpha-tournament",
    ]
    assert router.requests[4].url.params["period"] == "all"
    assert all(r.outcome is Outcome.OBSERVED for r in snapshot.components)
    assert snapshot.status is AccountStatus.OBSERVED_UNRESOLVED
    assert snapshot.findings == ()


# --- T21 to T28: enrolment, access, optional components ----------------------------------


def test_t21_pending_and_positions_forbidden():
    snapshot, _, _ = assemble({TOURNAMENT_PATH: tournament(**PENDING), POSITIONS: forbidden()})
    result = snapshot.positions
    assert result.outcome is Outcome.HTTP_ERROR
    assert (result.failure.http_status, result.failure.code) == (403, "FORBIDDEN")
    assert snapshot.status is AccountStatus.UNAVAILABLE
    assert snapshot.conclusions.membership is Membership.NOT_DETERMINED
    assert snapshot.conclusions.seed_or_current is SeedOrCurrent.NOT_DETERMINED
    # No "not a member" or "seed" value exists in the model.
    assert [m.name for m in Membership] == ["NOT_DETERMINED"]
    assert [m.name for m in SeedOrCurrent] == ["NOT_DETERMINED"]
    assert snapshot.cash.value == Decimal(1000)
    assert snapshot.cash.reported_pending_at_source is True
    assert snapshot.findings == ()  # K4 needs isPendingEnrolment = false
    access = snapshot.portfolio_access
    assert [(a.component, a.outcome, a.http_status, a.code) for a in access] == [
        (snap.POSITIONS, Outcome.HTTP_ERROR, 403, "FORBIDDEN")
    ]


def test_t22_pending_and_empty_positions():
    snapshot, _, _ = assemble({TOURNAMENT_PATH: tournament(**PENDING), POSITIONS: positions([])})
    assert snapshot.positions.outcome is Outcome.OBSERVED
    assert isinstance(snapshot.positions.read, PositionsRead)
    assert snapshot.positions.read.rows == ()
    k3 = checks(snapshot, "K3")
    assert len(k3) == 2  # one per balance observation reporting pending
    assert {f.kind for f in k3} == {FindingKind.UNEXPLAINED}
    assert snapshot.status is AccountStatus.OBSERVED_UNRESOLVED
    assert any("empty response" in line for line in snapshot.cannot_vouch_for)


def test_t23_not_pending_and_fills_forbidden():
    snapshot, _, _ = assemble({FILLS: forbidden()}, SnapshotRequest(fills=True))
    k4 = checks(snapshot, "K4")
    assert len(k4) == 2 and {f.kind for f in k4} == {FindingKind.UNEXPLAINED}
    assert k4[0].components == (OPENING, snap.FILLS)
    assert snapshot.status is AccountStatus.UNAVAILABLE


def test_t24_positions_unvalued():
    snapshot, _, _ = assemble({POSITIONS: missing_valuation()})
    result = snapshot.positions
    assert result.outcome is Outcome.UNVALUED
    assert isinstance(result.read, UnvaluedHoldingsRead)
    assert [(m.exchange_id, m.market_id) for m in result.read.missing] == [
        ("36", "26"),
        ("41", "29"),
    ]
    assert snapshot.status is AccountStatus.INCOMPLETE
    assert snapshot.portfolio_access[0].http_status == 409
    assert snapshot.candidate_equity is None
    assert not any("26" in line or "29" in line for line in snapshot.reasons)


def test_t25_requested_fills_deadline_is_unavailable():
    clock = FakeClock()
    snapshot, _, _ = assemble(
        {FILLS: taking(clock, 10_000, fills())}, SnapshotRequest(fills=True), clock=clock
    )
    result = snapshot.fills
    assert result.outcome is Outcome.FAILED
    assert result.failure.error_class == "SigDeadlineExceededError"
    assert result.failure.phase == "request"
    assert snapshot.status is AccountStatus.UNAVAILABLE
    line = next(r for r in snapshot.reasons if r.startswith("fills:"))
    assert "FAILED" in line and "phase request" in line


@pytest.mark.parametrize(
    ("request_", "path"),
    [
        (SnapshotRequest(fills=True), FILLS),
        (SnapshotRequest(valuation=True), PNL),
        (SnapshotRequest(settlements=True), SETTLEMENTS),
        (SnapshotRequest(order_fill_ids=(1001,)), ORDER_FILLS),
    ],
)
@pytest.mark.parametrize("failure", ["http", "failed"])
def test_t26_every_optional_failure_is_unavailable(request_, path, failure):
    response = error(500, "INTERNAL_ERROR") if failure == "http" else malformed()
    snapshot, _, _ = assemble({path: response}, request_)
    optional = [r for r in snapshot.components if r.component in request_.components()][3]
    assert optional.outcome is (Outcome.HTTP_ERROR if failure == "http" else Outcome.FAILED)
    assert snapshot.status is AccountStatus.UNAVAILABLE
    assert snapshot.balance_closing.outcome is Outcome.OBSERVED  # assembly continued


def test_t27_settlements_incomplete():
    snapshot, _, _ = assemble(
        {SETTLEMENTS: settlements(complete=False)}, SnapshotRequest(settlements=True)
    )
    assert snapshot.settlements.outcome is Outcome.OBSERVED_INCOMPLETE
    assert snapshot.settlements.read.incomplete_pages == (0,)
    assert snapshot.status is AccountStatus.INCOMPLETE


def test_t27_valuation_unvalued():
    snapshot, _, _ = assemble({PNL: missing_valuation()}, SnapshotRequest(valuation=True))
    assert snapshot.valuation.outcome is Outcome.UNVALUED
    assert snapshot.status is AccountStatus.INCOMPLETE
    assert snapshot.candidate_equity is None


@pytest.mark.parametrize("echoed", [OTHER_UUID, None])
def test_t27_order_fills_echo_is_scope_mismatch_without_latching(echoed):
    snapshot, router, _ = assemble(
        {ORDER_FILLS: order_fills(tournamentId=echoed)}, SnapshotRequest(order_fill_ids=(1001,))
    )
    assert snapshot.order_fills[1001].outcome is Outcome.SCOPE_MISMATCH
    assert snapshot.status is AccountStatus.SCOPE_MISMATCH
    [k12] = checks(snapshot, "K12")
    assert k12.kind is FindingKind.SCOPE and k12.components == (ORDER_1001,)
    # Not the client's latch: the closing balance is still read and matches.
    assert router.paths()[-1] == "/tournaments/alpha-tournament"
    assert snapshot.identity.closing is Identity.MATCH


def test_t28_unrequested_components():
    snapshot, router, _ = assemble()
    for component in (snap.FILLS, VALUATION, snap.SETTLEMENTS, ORDER_FILLS_ANY):
        result = snapshot.component(component)
        assert result.outcome is Outcome.NOT_REQUESTED
        assert (result.read, result.failure, result.timing) == (None, None, None)
        assert any(
            line.startswith(f"{component.label}: not requested")
            for line in snapshot.cannot_vouch_for
        )
        assert component not in snapshot.requested
    assert snapshot.status is AccountStatus.OBSERVED_UNRESOLVED
    assert snapshot.order_fills == {}
    assert not any("fills" in path for path in router.paths())
    assert not any("fills" in line for line in snapshot.vouches_for)
    assert [a.component for a in snapshot.portfolio_access] == [snap.POSITIONS]


# --- T30 to T33: balance reads and identity ------------------------------------------------


def test_t30_opening_failed_after_retries():
    snapshot, router, _ = assemble(
        {TOURNAMENT_PATH: sequence(*[error(503, "SERVICE_UNAVAILABLE")] * 4, tournament())},
        request=FULL,
    )
    opening = snapshot.balance_opening
    assert opening.outcome is Outcome.HTTP_ERROR
    assert opening.failure.error_class == "SigRetryExhaustedError"
    assert (opening.failure.http_status, opening.failure.attempts) == (503, 4)
    assert opening.identity is Identity.NOT_OBSERVED
    assert opening.provenance is None and opening.my_balance is None
    assert opening.timing.source is TimingSource.CALL
    assert snapshot.status is AccountStatus.UNAVAILABLE
    assert snapshot.cash.value is None and snapshot.cash.source is CashSource.NONE
    # Every other component still has an outcome; positions made the verification read.
    assert all(r.outcome is Outcome.OBSERVED for r in snapshot.components[1:8])
    assert snapshot.positions.read.provenance.verification_read is not None
    assert router.count("/tournaments/alpha-tournament") == 4 + 2


def test_t30_opening_malformed_is_failed_not_observed():
    snapshot, _, _ = assemble({TOURNAMENT_PATH: sequence(malformed(), tournament())})
    assert snapshot.balance_opening.outcome is Outcome.FAILED
    assert snapshot.balance_opening.failure.error_class == "SigResponseError"
    assert snapshot.identity.opening is Identity.NOT_OBSERVED
    assert snapshot.status is AccountStatus.UNAVAILABLE


def test_t31_closing_http_error_keeps_opening():
    calls = []

    def handler(request):
        calls.append(request)
        return tournament() if len(calls) == 1 else error(503, "SERVICE_UNAVAILABLE")

    snapshot, _, _ = assemble({TOURNAMENT_PATH: handler})
    closing = snapshot.balance_closing
    assert closing.outcome is Outcome.HTTP_ERROR and closing.failure.http_status == 503
    assert snapshot.identity.closing is Identity.NOT_OBSERVED
    assert snapshot.status is AccountStatus.UNAVAILABLE
    assert snapshot.cash.value is None
    assert snapshot.balance_opening.my_balance == Decimal("847.5")
    assert snapshot.balance_opening.provenance is not None


def test_t32_closing_mismatch_with_other_failures():
    k6_row = position_row(quantity=5, currentPrice=None, lots=[])
    snapshot, _, _ = assemble(
        {
            TOURNAMENT_PATH: sequence(tournament(), tournament(id=OTHER_UUID)),
            POSITIONS: positions([k6_row]),
            FILLS: malformed(),
        },
        SnapshotRequest(fills=True),
    )
    assert snapshot.status is AccountStatus.SCOPE_MISMATCH
    assert snapshot.identity.closing is Identity.MISMATCH
    assert snapshot.balance_closing.outcome is Outcome.SCOPE_MISMATCH
    assert snapshot.component_outcomes[snap.FILLS] is Outcome.FAILED
    assert checks(snapshot, "K6")
    assert any(r.startswith("fills: FAILED") for r in snapshot.reasons)
    assert any(r.startswith("K6 CONTRADICTION") for r in snapshot.reasons)
    assert any("balance_closing identity MISMATCH" in r for r in snapshot.reasons)


def test_scope_latch_fails_every_later_component_without_requests():
    snapshot, router, _ = assemble({TOURNAMENT_PATH: tournament(id=OTHER_UUID)}, FULL)
    assert router.paths() == ["/tournaments/alpha-tournament"]  # nothing after the latch
    assert all(r.outcome is Outcome.SCOPE_MISMATCH for r in snapshot.components[:8])
    assert snapshot.identity.opening is snapshot.identity.closing is Identity.MISMATCH
    assert snapshot.status is AccountStatus.SCOPE_MISMATCH
    [k12] = checks(snapshot, "K12")
    assert "latch was set earlier" in k12.description
    assert snapshot.reads == ()
    assert snapshot.cash.value is None


def test_t33_not_observed_and_mismatch_are_distinct():
    failed, _, _ = assemble({TOURNAMENT_PATH: sequence(malformed(), tournament())})
    mismatched, _, _ = assemble({TOURNAMENT_PATH: tournament(slug="other-slug")})
    assert failed.identity.opening is Identity.NOT_OBSERVED
    assert mismatched.identity.opening is Identity.MISMATCH
    assert failed.identity.overall is Identity.NOT_OBSERVED
    assert mismatched.identity.overall is Identity.MISMATCH
    assert failed.status is AccountStatus.UNAVAILABLE
    assert mismatched.status is AccountStatus.SCOPE_MISMATCH


# --- T34, T35: findings ---------------------------------------------------------------------


def test_t34_k6_null_price_on_nonzero_position():
    row = position_row(quantity=5, currentPrice=None, lots=[])
    snapshot, _, _ = assemble({POSITIONS: positions([position_row(), row])})
    [k6] = checks(snapshot, "K6")
    assert k6.kind is FindingKind.CONTRADICTION and k6.locations == ("row 1",)
    assert snapshot.status is AccountStatus.CONTRADICTED
    assert snapshot.positions.read.rows[1].currentPrice is None  # retained as sent
    assert any(r.startswith("K6 CONTRADICTION") and "OQ-D3" in r for r in snapshot.reasons)


@pytest.mark.parametrize("source", ["fills", "order_fills"])
def test_t34_k8_fill_sign_disagrees_with_side(source):
    if source == "fills":
        bad = dict(fixture("tournament_fills")["data"][0], quantity=-3)
        overrides, request_ = {FILLS: fills([bad])}, SnapshotRequest(fills=True)
        component = snap.FILLS
    else:
        body = fixture("order_fills")
        body["data"][0]["quantity"] = -100
        overrides = {ORDER_FILLS: json_response(200, body)}
        request_, component = SnapshotRequest(order_fill_ids=(1001,)), ORDER_1001
    snapshot, _, _ = assemble(overrides, request_)
    [k8] = checks(snapshot, "K8")
    assert k8.kind is FindingKind.CONTRADICTION and k8.components == (component,)
    assert k8.locations == ("fill 0",)
    assert snapshot.status is AccountStatus.CONTRADICTED
    assert any(r.startswith("K8") and "OQ-W4" in r for r in snapshot.reasons)


def test_t34_k9_closed_row_in_open_response():
    snapshot, _, _ = assemble({ORDERS: orders([open_order(), open_order(id=1002, open=False)])})
    [k9] = checks(snapshot, "K9")
    assert k9.kind is FindingKind.CONTRADICTION and k9.locations == ("order 1",)
    assert snapshot.status is AccountStatus.CONTRADICTED
    assert snapshot.open_orders.read.orders[1].open is False


def test_t35_k3_unexplained_does_not_change_status():
    snapshot, _, _ = assemble({TOURNAMENT_PATH: tournament(**PENDING)})
    assert only_check_ids(snapshot) == ["K3", "K3"]
    assert snapshot.status is AccountStatus.OBSERVED_UNRESOLVED


def test_t35_k5_pending_balance_differs_from_initial():
    snapshot, _, _ = assemble(
        {TOURNAMENT_PATH: tournament(isPendingEnrolment=True), POSITIONS: forbidden()}
    )
    k5 = checks(snapshot, "K5")
    assert [f.components for f in k5] == [(OPENING,), (CLOSING,)]
    assert snapshot.status is AccountStatus.UNAVAILABLE  # from the 403 only


def test_t35_k7_lots_disagree_with_quantity():
    row = position_row(lots=[dict(position_row()["lots"][0], side="no")])
    snapshot, _, _ = assemble({POSITIONS: positions([row])})
    [k7] = checks(snapshot, "K7")
    assert k7.kind is FindingKind.UNEXPLAINED
    assert snapshot.status is AccountStatus.OBSERVED_UNRESOLVED


def test_k7_lot_side_case_and_sizes():
    # The shared fixture's NO lot is spelled "NO" and its sizes add up: no finding.
    clean, _, _ = assemble()
    assert checks(clean, "K7") == []
    short = position_row(lots=[position_row()["lots"][0]])  # 6 of 10
    snapshot, _, _ = assemble({POSITIONS: positions([short])})
    assert checks(snapshot, "K7")[0].locations == ("row 0",)


@pytest.mark.parametrize("expiration", ["2020-01-01T00:00:00.000Z", "not a timestamp"])
def test_t35_k10_expired_or_uncomparable_open_order(expiration):
    snapshot, _, _ = assemble({ORDERS: orders([open_order(expirationDate=expiration)])})
    [k10] = checks(snapshot, "K10")
    assert k10.kind is FindingKind.UNEXPLAINED
    assert snapshot.status is AccountStatus.OBSERVED_UNRESOLVED


def test_k10_future_expiration_is_not_a_finding():
    snapshot, _, _ = assemble({ORDERS: orders([open_order(expirationDate="2999-01-01T00:00:00Z")])})
    assert checks(snapshot, "K10") == []


@pytest.mark.parametrize("unvalued", [POSITIONS, PNL])
def test_t35_k11_positions_and_valuation_disagree(unvalued):
    snapshot, _, _ = assemble({unvalued: missing_valuation()}, SnapshotRequest(valuation=True))
    [k11] = checks(snapshot, "K11")
    assert k11.kind is FindingKind.UNEXPLAINED
    assert snapshot.status is AccountStatus.INCOMPLETE  # from UNVALUED, not from K11


def test_reasons_keep_every_failure_under_a_more_severe_status():
    snapshot, _, _ = assemble(
        {
            TOURNAMENT_PATH: tournament(**PENDING),
            POSITIONS: positions([position_row(quantity=5, currentPrice=None, lots=[])]),
            FILLS: malformed(),
            SETTLEMENTS: settlements(complete=False),
            ORDER_FILLS: order_fills(tournamentId=None),
        },
        FULL,
    )
    assert snapshot.status is AccountStatus.SCOPE_MISMATCH
    assert {"K6", "K12", "K3"} <= set(only_check_ids(snapshot))
    starts = [r.split(":")[0].split(" ")[0] for r in snapshot.reasons]
    for expected in ("fills", "settlements", "order_fills[1001]", "K6", "K12", "K3"):
        assert expected in starts


# --- T36 to T38: balance differences and the cash candidate -------------------------------


def test_t36_balance_changed_during_assembly():
    snapshot, _, _ = assemble(
        {TOURNAMENT_PATH: sequence(tournament(myBalance=900), tournament(myBalance=850))}
    )
    assert snapshot.balance_differences == ("myBalance",)
    [k13] = checks(snapshot, "K13")
    assert k13.kind is FindingKind.UNEXPLAINED
    assert snapshot.status is AccountStatus.INCOMPLETE
    assert snapshot.cash.value is None and snapshot.cash.source is CashSource.NONE
    assert snapshot.balance_opening.my_balance == Decimal(900)
    assert snapshot.balance_closing.my_balance == Decimal(850)
    for observation in (snapshot.balance_opening, snapshot.balance_closing):
        assert observation.currency_name == "coins"
        assert observation.is_pending_enrolment is False
        assert observation.provenance is not None
    assert any("balance difference: myBalance" in r for r in snapshot.reasons)


@pytest.mark.parametrize(
    ("change", "field"),
    [
        ({"isPendingEnrolment": True}, "isPendingEnrolment"),
        ({"currencyName": "tokens"}, "currencyName"),
        ({"initialBalance": 2000}, "initialBalance"),
    ],
)
def test_t37_equal_balances_other_field_differs(change, field):
    snapshot, _, _ = assemble({TOURNAMENT_PATH: sequence(tournament(), tournament(**change))})
    assert snapshot.balance_differences == (field,)
    assert snapshot.status is AccountStatus.INCOMPLETE
    assert snapshot.cash.value is None


def test_t37_same_value_different_spelling_is_a_difference():
    body = fixture("tournament")
    first = exact_response(200, body | {"myBalance": Decimal("847.5")})
    second = exact_response(200, body | {"myBalance": Decimal("847.50")})
    snapshot, _, _ = assemble({TOURNAMENT_PATH: sequence(first, second)})
    assert snapshot.balance_differences == ("myBalance",)
    assert snapshot.cash.value is None


def test_t38_agreeing_balances_give_a_candidate_from_the_closing_read():
    snapshot, _, _ = assemble(
        {
            TOURNAMENT_PATH: exact_response(
                200, fixture("tournament") | {"myBalance": Decimal("847.125")}
            )
        }
    )
    cash = snapshot.cash
    assert cash.value == Decimal("847.125") and str(cash.value) == "847.125"
    assert cash.source is CashSource.CLOSING
    assert cash.currency == "coins"
    assert cash.reported_pending_at_source is False
    assert cash.source_established is False
    assert cash.excludes_resting_orders is ReservationKnowledge.UNKNOWN
    assert snapshot.balance_opening.provenance is not snapshot.balance_closing.provenance


def test_cash_candidate_cannot_be_marked_established():
    from engine.account.snapshot import CashCandidate

    with pytest.raises(ValueError):
        CashCandidate(Decimal(1), CashSource.CLOSING, "coins", False, source_established=True)
    with pytest.raises(ValueError):
        CashCandidate(None, CashSource.CLOSING, None, None)


# --- T41 to T44 ------------------------------------------------------------------------------


def test_t41_best_status_is_observed_unresolved_and_there_is_no_ready():
    snapshot, _, _ = assemble(request=FULL)
    assert snapshot.status is AccountStatus.OBSERVED_UNRESOLVED
    assert "READY" not in AccountStatus.__members__
    assert not any("ready" in member.value.lower() for member in AccountStatus)


@pytest.mark.parametrize(
    "overrides", [{}, {POSITIONS: forbidden()}, {TOURNAMENT_PATH: tournament(id=OTHER_UUID)}]
)
def test_t42_fixed_cash_labels_and_vouching_on_every_snapshot(overrides):
    snapshot, _, _ = assemble(overrides)
    assert snapshot.cash.source_established is False
    assert snapshot.cash.excludes_resting_orders is ReservationKnowledge.UNKNOWN
    for line in CANNOT_VOUCH_FOR:
        assert line in snapshot.cannot_vouch_for
    assert snapshot.does_not_cover == DOES_NOT_COVER


def test_t44_balance_observations_are_fresh_reads_never_the_cache():
    clock = FakeClock()
    router = clean_router({TOURNAMENT_PATH: taking(clock, 1, tournament())})
    client, router, clock = make_client(router, clock=clock)

    async def go():
        async with client:
            cached = await client.verify_tournament()  # fills the verification cache
            earlier = await client.read_tournament_balance()
            return cached, earlier, await assemble_account_snapshot(client, clock=clock)

    cached, earlier, snapshot = run(go())
    # Verification, one earlier read, then two fresh reads made by the assembly.
    assert router.count("/tournaments/alpha-tournament") == 4
    assert snapshot.balance_opening.provenance.started_mono >= earlier.provenance.finished_mono
    # A Tournament as the cache holds it is not accepted as a balance observation.
    with pytest.raises(TypeError):
        ComponentResult.observed(OPENING, cached)
    with pytest.raises(TypeError):
        BalanceObservation.from_result(snapshot.positions)
    # A balance read made before the assembly's other reads cannot be its closing read.
    results = list(snapshot.components[:3]) + [ComponentResult.observed(CLOSING, earlier)]
    with pytest.raises(ValueError, match="one after another"):
        build_account_snapshot(
            client.tournament,
            SnapshotRequest(),
            results,
            evaluated_mono=snapshot.evaluated_mono,
            evaluated_utc=snapshot.evaluated_utc,
        )
    # The genuine results rebuild the same status.
    rebuilt = build_account_snapshot(
        client.tournament,
        SnapshotRequest(),
        snapshot.components[:4],
        evaluated_mono=snapshot.evaluated_mono,
        evaluated_utc=snapshot.evaluated_utc,
    )
    assert rebuilt == snapshot


def test_observed_reads_are_kept_exactly_as_returned():
    snapshot, _, _ = assemble(request=FULL)
    assert isinstance(snapshot.balance_opening.provenance, type(snapshot.reads[0]))
    assert isinstance(snapshot.open_orders.read, OrdersWalk)
    assert isinstance(snapshot.component(OPENING).read, TournamentBalanceRead)
    assert snapshot.candidate_equity.value == Decimal("930.5")
    assert snapshot.candidate_equity.label == "candidate, OQ-D1; unused"


def test_request_validation():
    with pytest.raises(TypeError):
        SnapshotRequest(fills=1)
    with pytest.raises(ValueError):
        SnapshotRequest(order_fill_ids=(1001, 1001))
    with pytest.raises(ValueError):
        SnapshotRequest(order_fill_ids=(True,))
    with pytest.raises(ValueError):
        SnapshotRequest(order_fill_ids=(-1,))
    with pytest.raises(TypeError):
        SnapshotRequest(order_fill_ids=[1001])


def test_order_fills_one_component_per_order():
    body_2002 = fixture("order_fills") | {"orderId": 2002, "data": []}
    snapshot, router, _ = assemble(
        {f"{API}/orders/2002/fills": json_response(200, body_2002)},
        SnapshotRequest(order_fill_ids=(1001, 2002)),
    )
    assert list(snapshot.order_fills) == [1001, 2002]
    assert snapshot.order_fills[2002].read.fills == ()
    assert router.paths()[3:5] == ["/orders/1001/fills", "/orders/2002/fills"]
    assert snapshot.status is AccountStatus.OBSERVED_UNRESOLVED
