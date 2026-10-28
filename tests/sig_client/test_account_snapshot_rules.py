"""A3: status precedence, timing, scope checks, isolation, failures (design T29, T39, T40,
T43, T45, T46, plus failure timing, cancellation, redaction and pagination failures).

All offline: synthetic, spec-derived bodies through httpx.MockTransport.
"""

from __future__ import annotations

import asyncio
import dataclasses
import inspect
import itertools
import re
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from types import MappingProxyType

import httpx
import pytest
from account_kit import ORDERS, POSITIONS, TOURNAMENT_PATH, orders_page
from pydantic import ValidationError
from sig_testkit import FakeClock, fixture, json_response, make_client, run
from snapshot_kit import (
    FILLS,
    FULL,
    ORDER_FILLS,
    SETTLEMENTS,
    assemble,
    clean_router,
    error,
    order_fills,
    tournament,
)

from engine.account import snapshot as snap
from engine.account.snapshot import (
    CLOSING,
    OPEN_ORDERS,
    OPENING,
    AccountStatus,
    ComponentResult,
    FindingKind,
    Identity,
    Outcome,
    SnapshotRequest,
    TimingSource,
    account_status,
    assemble_account_snapshot,
    build_account_snapshot,
)
from engine.sig_client.account_reads import (
    CoverageState,
    ReadProvenance,
    ScopeEvidence,
    VerificationUse,
)
from engine.sig_client.wire import EconomicCommandCoverage, TournamentPosition

ENGINE_ACCOUNT = Path(snap.__file__).resolve().parent


# --- T29: the status rule over every combination -------------------------------------------


def expected_status(opening, closing, outcomes, kinds, differs):
    """An independent statement of design 5.5, written as the table reads."""
    if (
        Identity.MISMATCH in (opening, closing)
        or Outcome.SCOPE_MISMATCH in outcomes
        or FindingKind.SCOPE in kinds
    ):
        return AccountStatus.SCOPE_MISMATCH
    if FindingKind.CONTRADICTION in kinds:
        return AccountStatus.CONTRADICTED
    if Identity.NOT_OBSERVED in (opening, closing) or {Outcome.HTTP_ERROR, Outcome.FAILED} & set(
        outcomes
    ):
        return AccountStatus.UNAVAILABLE
    if {Outcome.UNVALUED, Outcome.OBSERVED_INCOMPLETE} & set(outcomes) or differs:
        return AccountStatus.INCOMPLETE
    return AccountStatus.OBSERVED_UNRESOLVED


def test_t29_status_over_every_combination():
    kinds_subsets = [
        combo
        for size in range(len(FindingKind) + 1)
        for combo in itertools.combinations(FindingKind, size)
    ]
    count = 0
    for opening, closing, first, second, kinds, differs in itertools.product(
        Identity, Identity, Outcome, Outcome, kinds_subsets, (False, True)
    ):
        outcomes = (first, second)
        got = account_status(
            opening=opening,
            closing=closing,
            outcomes=outcomes,
            finding_kinds=kinds,
            balance_differs=differs,
        )
        assert got is expected_status(opening, closing, outcomes, kinds, differs)
        unresolved = (
            opening is closing is Identity.MATCH
            and all(o in (Outcome.OBSERVED, Outcome.NOT_REQUESTED) for o in outcomes)
            and FindingKind.CONTRADICTION not in kinds
            and FindingKind.SCOPE not in kinds
            and not differs
        )
        assert (got is AccountStatus.OBSERVED_UNRESOLVED) is unresolved
        count += 1
    assert count == 3 * 3 * 7 * 7 * 8 * 2


def test_t29_inputs_outside_the_enums_have_no_class_and_raise():
    good = {
        "opening": Identity.MATCH,
        "closing": Identity.MATCH,
        "outcomes": (),
        "finding_kinds": (),
        "balance_differs": False,
    }
    for name, bad in (
        ("opening", "MATCH"),
        ("outcomes", ("OBSERVED",)),
        ("finding_kinds", ("UNEXPLAINED",)),
        ("balance_differs", 0),
    ):
        with pytest.raises(TypeError):
            account_status(**(good | {name: bad}))


# --- T39, T40: times and ages ----------------------------------------------------------------


def test_t39_oldest_component_includes_the_opening_balance():
    # Opening balance starts at 0, positions at 1 (to 2), closing finishes at 900, and the
    # snapshot is evaluated at 900 (design 5.8 as corrected by A3).
    clock = FakeClock(start=0.0)
    calls = []

    def balance(request):
        calls.append(request)
        clock.now = 1.0 if len(calls) == 1 else 900.0
        return tournament()

    def advance(response):
        def handler(request):
            clock.now += 1.0
            return response

        return handler

    snapshot, _, _ = assemble(
        {
            TOURNAMENT_PATH: balance,
            POSITIONS: advance(json_response(200, fixture("tournament_positions"))),
            ORDERS: advance(json_response(200, orders_page([1001], cursor=None))),
        },
        clock=clock,
    )
    at = snapshot.evaluated_mono
    assert at == 900.0
    assert snapshot.completion_age(at) == 0.0
    ages = {age.component: age for age in snapshot.component_ages(at)}
    assert ages[OPENING].age == 900.0
    assert ages[snap.POSITIONS].age == 899.0
    assert ages[snap.POSITIONS].since_first_response == 898.0
    assert ages[OPEN_ORDERS].age == 898.0
    oldest = snapshot.oldest_component(at)
    assert (oldest.component, oldest.age) == (OPENING, 900.0)
    assert snapshot.assembly_span == 900.0
    assert (snapshot.assembled_from_mono, snapshot.assembled_to_mono) == (0.0, 900.0)


def test_t39_ages_cover_every_requested_component_in_order():
    # The opening balance is a component like any other; on equal start times the
    # earliest in assembly order is named.
    snapshot, _, _ = assemble()
    ages = snapshot.component_ages(snapshot.evaluated_mono + 10)
    assert [a.component for a in ages][:3] == [OPENING, snap.POSITIONS, OPEN_ORDERS]
    assert snapshot.oldest_component(snapshot.evaluated_mono + 10).component is OPENING


def test_failed_component_timing_is_the_call_boundary_and_has_no_provenance():
    clock = FakeClock(start=0.0)

    def slow_failure(request):
        clock.now += 5.0
        return error(500, "INTERNAL_ERROR")

    snapshot, _, _ = assemble({POSITIONS: slow_failure}, clock=clock)
    result = snapshot.positions
    assert result.outcome is Outcome.HTTP_ERROR
    assert result.requests == ()
    timing = result.timing
    assert timing.source is TimingSource.CALL
    assert (timing.started_mono, timing.finished_mono) == (0.0, 5.0)
    assert (timing.first_finished_mono, timing.last_started_mono) == (None, None)
    assert all(p.route != "/tournaments/{slug}/portfolio/positions" for p in snapshot.reads)
    age = next(a for a in snapshot.component_ages(10.0) if a.component == snap.POSITIONS)
    assert (age.age, age.since_first_response, age.timing_source) == (10.0, None, TimingSource.CALL)
    assert snapshot.oldest_component(10.0).age == 10.0  # ties keep assembly order
    assert snapshot.oldest_component(10.0).component is OPENING


def test_failed_balance_observation_keeps_call_timing_without_provenance():
    snapshot, _, _ = assemble({TOURNAMENT_PATH: error(500, "INTERNAL_ERROR")})
    for observation in (snapshot.balance_opening, snapshot.balance_closing):
        assert observation.provenance is None
        assert observation.failure is not None
        assert observation.timing.source is TimingSource.CALL


def test_t40_no_freshness_claim_and_no_maximum_age():
    snapshot, _, _ = assemble(request=FULL)
    for line in snapshot.reasons + snapshot.vouches_for:
        assert "fresh" not in line.lower()
    assert not [name for name in vars(snap) if re.search("max.*age|age.*max", name, re.IGNORECASE)]
    assert not [
        f.name for f in dataclasses.fields(snapshot) if "fresh" in f.name or "stale" in f.name
    ]


# --- T43: coverage kept, never compared ----------------------------------------------------


def test_t43_coverage_checkpoints_are_kept_and_never_compared():
    first = orders_page(
        [1], cursor="c1", coverage={"complete": True, "projectedThroughSequence": 7}
    )
    second = orders_page(
        [2], cursor=None, coverage={"complete": True, "projectedThroughSequence": 3}
    )

    def by_cursor(request):
        return json_response(200, second if request.url.params.get("cursor") else first)

    snapshot, _, _ = assemble({ORDERS: by_cursor}, FULL)
    pages = snapshot.open_orders.read.pages
    assert [p.provenance.coverage.projectedThroughSequence for p in pages] == [7, 3]
    assert snapshot.status is AccountStatus.OBSERVED_UNRESOLVED  # a decrease is not judged
    settlement = snapshot.settlements.read.pages[0].provenance.coverage
    assert isinstance(settlement, EconomicCommandCoverage)
    assert set(type(settlement).model_fields) == {"complete"}
    assert ".projectedThroughSequence" not in inspect.getsource(snap)


# --- K1: scope of every recorded request ---------------------------------------------------


def _rebuild(snapshot, results):
    return build_account_snapshot(
        snapshot.tournament,
        SnapshotRequest(),
        results,
        evaluated_mono=snapshot.evaluated_mono,
        evaluated_utc=snapshot.evaluated_utc,
    )


@pytest.mark.parametrize(
    "change",
    [
        {"route": "/account", "path": "/account"},
        {"route": "/portfolio/positions", "path": "/portfolio/positions"},
        {"path": "/tournaments/other-slug/portfolio/positions"},
        {"scope": ScopeEvidence.TOURNAMENT_ID_SENT},
    ],
)
def test_k1_a_request_not_bound_to_the_tournament_is_scope_mismatch(change):
    snapshot, _, _ = assemble()
    positions = snapshot.positions
    bad = dataclasses.replace(positions.read.provenance, **change)
    read = dataclasses.replace(positions.read, provenance=bad)
    timing = dataclasses.replace(positions.timing)
    replaced = dataclasses.replace(positions, read=read, timing=timing)
    rebuilt = _rebuild(snapshot, [snapshot.components[0], replaced, *snapshot.components[2:4]])
    [k1] = [f for f in rebuilt.findings if f.check_id == "K1"]
    assert k1.kind is FindingKind.SCOPE
    assert k1.locations[0].startswith("reads[1]")
    assert rebuilt.status is AccountStatus.SCOPE_MISMATCH


def test_k1_orders_without_the_configured_tournament_id():
    snapshot, _, _ = assemble()
    orders = snapshot.open_orders
    page = orders.read.pages[0]
    params = tuple(p for p in page.provenance.params if p[0] != "tournamentId")
    bad_page = dataclasses.replace(
        page, provenance=dataclasses.replace(page.provenance, params=params)
    )
    read = dataclasses.replace(orders.read, pages=(bad_page,))
    replaced = dataclasses.replace(orders, read=read)
    rebuilt = _rebuild(snapshot, [*snapshot.components[:2], replaced, snapshot.components[3]])
    assert [f.check_id for f in rebuilt.findings] == ["K1"]
    assert rebuilt.status is AccountStatus.SCOPE_MISMATCH


def test_every_real_request_is_tournament_bound():
    snapshot, _, _ = assemble(request=FULL)
    assert all(f.check_id != "K1" for f in snapshot.findings)
    assert {p.scope for p in snapshot.reads} == set(ScopeEvidence)
    assert all(isinstance(p, ReadProvenance) for p in snapshot.reads)


# --- T45, T46: isolation and no domain conversion --------------------------------------------


def _sources() -> list[tuple[Path, str]]:
    return [(path, path.read_text(encoding="utf-8")) for path in ENGINE_ACCOUNT.glob("*.py")]


def test_t45_account_snapshot_and_market_readiness_are_independent():
    for path, text in _sources():
        for module in ("readiness", "recovery", "realtime"):
            assert not re.search(rf"^from engine\.sig_client\.{module}\b", text, re.MULTILINE), path
            assert not re.search(rf"^import engine\.sig_client\.{module}\b", text, re.MULTILINE), (
                path
            )
        assert "MarketReadiness" not in text, path
    readiness = (ENGINE_ACCOUNT.parent / "sig_client" / "readiness.py").read_text(encoding="utf-8")
    assert "engine.account" not in readiness
    assert "MarketReadiness" not in {f.name for f in dataclasses.fields(snap.AccountSnapshot)}


def test_t46_no_shared_domain_types_and_no_unit_conversion():
    for path, text in _sources():
        assert "shared" not in re.findall(r"^(?:from|import) (\w+)", text, re.MULTILINE), path
        assert not re.search(r"\b(ticks?|milli)\b", text.replace("milliseconds", "")), path
        assert not re.search(r"\*\s*(200|1000)\b|\bfloat\(", text), path


# --- cancellation and other exceptions -----------------------------------------------------


def test_external_cancellation_propagates_and_builds_no_snapshot():
    entered = asyncio.Event()

    async def hang(request: httpx.Request) -> httpx.Response:
        entered.set()
        await asyncio.Event().wait()
        raise AssertionError("unreachable")

    client, router, clock = make_client(clean_router({POSITIONS: hang}))

    async def go():
        async with client:
            task = asyncio.create_task(assemble_account_snapshot(client, clock=clock))
            await entered.wait()
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
            assert task.cancelled()

    run(go())
    assert router.paths() == [
        "/tournaments/alpha-tournament",
        "/tournaments/alpha-tournament/portfolio/positions",
    ]


def test_a_non_sig_exception_propagates_unclassified():
    def broken(request):
        raise RuntimeError("programming error in a handler")

    client, _, clock = make_client(clean_router({POSITIONS: broken}))

    async def go():
        async with client:
            await assemble_account_snapshot(client, clock=clock)

    with pytest.raises(RuntimeError):
        run(go())


def test_closed_client_is_a_failed_component_for_every_read():
    client, router, clock = make_client(clean_router())

    async def go():
        await client.aclose()
        return await assemble_account_snapshot(client, clock=clock)

    snapshot = run(go())
    assert router.paths() == []
    assert {r.failure.error_class for r in snapshot.components[:4]} == {"SigClientClosedError"}
    assert snapshot.status is AccountStatus.UNAVAILABLE


# --- pagination failures ---------------------------------------------------------------------


def test_repeated_cursor_in_open_orders_fails_the_component():
    page = orders_page([1], cursor="same")
    second = orders_page([2], cursor="same")

    def handler(request):
        return json_response(200, second if request.url.params.get("cursor") else page)

    snapshot, _, _ = assemble({ORDERS: handler})
    result = snapshot.open_orders
    assert result.outcome is Outcome.FAILED
    assert result.failure.error_class == "SigPaginationError"
    assert result.read is None  # no partial walk
    assert snapshot.status is AccountStatus.UNAVAILABLE
    assert snapshot.balance_closing.outcome is Outcome.OBSERVED


def test_duplicate_fill_across_pages_fails_the_fills_component():
    body = fixture("tournament_fills")
    first = dict(body, pagination={"limit": 200, "hasMore": True, "nextCursor": "c1"})

    def handler(request):
        return json_response(200, body if request.url.params.get("cursor") else first)

    snapshot, _, _ = assemble({FILLS: handler}, SnapshotRequest(fills=True))
    assert snapshot.fills.outcome is Outcome.FAILED
    assert snapshot.fills.failure.error_class == "SigPaginationError"
    assert "the failing page is not reported" in next(
        r for r in snapshot.reasons if r.startswith("fills:")
    )


def test_order_fills_totals_differ_between_pages():
    first = fixture("order_fills") | {
        "pagination": {"limit": 200, "hasMore": True, "nextCursor": "c1"}
    }
    second = fixture("order_fills") | {"data": [], "totalQuantityFilled": 50}

    def handler(request):
        return json_response(200, second if request.url.params.get("cursor") else first)

    snapshot, _, _ = assemble({ORDER_FILLS: handler}, SnapshotRequest(order_fill_ids=(1001,)))
    assert snapshot.order_fills[1001].outcome is Outcome.FAILED
    assert snapshot.status is AccountStatus.UNAVAILABLE


def test_settlements_multi_page_incomplete_lists_the_page():
    body = fixture("tournament_settlements")
    first = dict(body, pagination={"limit": 200, "hasMore": True, "nextCursor": "c1"})
    first["data"] = body["data"][:1]
    second = dict(body, coverage={"complete": False})
    second["data"] = body["data"][1:]

    def handler(request):
        return json_response(200, second if request.url.params.get("cursor") else first)

    snapshot, _, _ = assemble({SETTLEMENTS: handler}, SnapshotRequest(settlements=True))
    assert snapshot.settlements.outcome is Outcome.OBSERVED_INCOMPLETE
    assert snapshot.settlements.read.incomplete_pages == (1,)
    assert any("coverage.complete false: 1" in r for r in snapshot.reasons)
    pages = snapshot.settlements.read.pages
    assert [p.provenance.coverage_state for p in pages] == [CoverageState.PRESENT] * 2


# --- redaction --------------------------------------------------------------------------------

SECRET = "zz-secret-response-text-9931"


def _all_text(snapshot) -> str:
    parts = list(snapshot.reasons) + list(snapshot.vouches_for) + list(snapshot.cannot_vouch_for)
    for finding in snapshot.findings:
        parts.append(repr(finding))
    for result in snapshot.components:
        parts.append(repr(result.failure))
        if result.failure is not None:
            parts.append(result.failure.describe())
    return "\n".join(parts)


def test_reasons_findings_and_failures_carry_no_response_values():
    pending = {"isPendingEnrolment": True, "myBalance": 31415.926, "initialBalance": 27182.818}
    positions = fixture("tournament_positions")
    positions["positions"][0] |= {"marketTitle": SECRET, "quantity": 77.125, "currentPrice": None}
    snapshot, _, _ = assemble(
        {
            TOURNAMENT_PATH: tournament(**pending),
            POSITIONS: json_response(200, positions),
            FILLS: error(500, SECRET, SECRET),
            ORDERS: error(400, "VALIDATION_ERROR", SECRET),
            ORDER_FILLS: order_fills(tournamentId="99999999-8888-4777-8666-555555555555"),
            SETTLEMENTS: json_response(409, {"error": {"code": "CONFLICT", "message": SECRET}}),
        },
        FULL,
    )
    text = _all_text(snapshot)
    for value in (SECRET, "31415.926", "27182.818", "77.125", "99999999-8888"):
        assert value not in text
    assert snapshot.fills.failure.unrecognized_code is True
    assert snapshot.fills.failure.code is None
    # The values themselves are still kept, for engine use only.
    assert str(snapshot.balance_opening.my_balance) == "31415.926"


def test_failure_records_hold_no_exception_objects():
    snapshot, _, _ = assemble({POSITIONS: error(500, "INTERNAL_ERROR")})
    failure = snapshot.positions.failure
    for field in dataclasses.fields(failure):
        assert isinstance(getattr(failure, field.name), (str, int, bool, type(None)))


def test_unrelated_wall_clock_is_only_for_display():
    snapshot, _, _ = assemble()
    assert snapshot.evaluated_utc.tzinfo is not None
    assert snapshot.evaluated_utc.utcoffset().total_seconds() == 0
    assert isinstance(snapshot.evaluated_utc, datetime)
    assert snapshot.evaluated_utc <= datetime.now(UTC)


def test_verification_read_made_by_a_later_component_is_listed_in_reads():
    calls = []

    def first_fails(request):
        calls.append(request)
        return error(500, "INTERNAL_ERROR") if len(calls) == 1 else tournament()

    snapshot, _, _ = assemble({TOURNAMENT_PATH: first_fails})
    routes = [(p.route, p.verification) for p in snapshot.reads]
    assert routes[0] == ("/tournaments/{slug}", VerificationUse.NOT_APPLICABLE)
    assert routes[1] == ("/tournaments/{slug}/portfolio/positions", VerificationUse.PERFORMED)
    assert snapshot.positions.timing.started_mono == snapshot.reads[0].started_mono
    assert snapshot.balance_closing.identity is Identity.MATCH
    assert snapshot.identity.opening is Identity.NOT_OBSERVED
    assert CLOSING in snapshot.requested


# --- immutability of retained observations (A3-F1) --------------------------------------


def _mutable_paths(root: object) -> list[str]:
    """Every list, dict, set or bytearray reachable from `root` (an independent walk)."""
    found: list[str] = []
    seen: set[int] = set()

    def visit(value: object, where: str) -> None:
        if id(value) in seen or isinstance(value, (str, bytes, int, float, type)):
            return
        seen.add(id(value))
        if isinstance(value, (list, dict, set, bytearray)):
            found.append(where)
            return
        if isinstance(value, (tuple, frozenset)):
            for index, item in enumerate(value):
                visit(item, f"{where}[{index}]")
        elif isinstance(value, MappingProxyType):
            for key, item in value.items():
                visit(item, f"{where}[{key!r}]")
        elif hasattr(value, "__dict__"):
            for name, item in vars(value).items():
                visit(item, f"{where}.{name}")

    visit(root, "snapshot")
    return found


def test_retained_lots_cannot_be_changed_through_the_snapshot():
    # Reviewed head 52cefea: `lots` was a list, `clear()` emptied the retained row and the
    # snapshot kept findings computed from the lots it had before.
    snapshot, _, _ = assemble()
    for row in (snapshot.positions.read.rows[0], snapshot.components[1].read.rows[0]):
        assert isinstance(row.lots, tuple) and len(row.lots) == 2
        with pytest.raises(AttributeError):
            row.lots.clear()
        with pytest.raises(AttributeError):
            row.lots.append(row.lots[0])
        with pytest.raises(ValidationError):
            row.lots = ()
        with pytest.raises(ValidationError):
            row.lots[0].quantity = Decimal(0)
    with pytest.raises(dataclasses.FrozenInstanceError):
        snapshot.positions.read.rows = ()
    with pytest.raises(dataclasses.FrozenInstanceError):
        snapshot.components = ()
    assert len(snapshot.positions.read.rows[0].lots) == 2
    # The kept findings are still those of the kept data.
    rebuilt = build_account_snapshot(
        snapshot.tournament,
        SnapshotRequest(),
        snapshot.components[:4],
        evaluated_mono=snapshot.evaluated_mono,
        evaluated_utc=snapshot.evaluated_utc,
    )
    assert rebuilt.findings == snapshot.findings == ()


def test_nothing_mutable_is_reachable_from_any_snapshot():
    full, _, _ = assemble(request=FULL)
    failed, _, _ = assemble(
        {
            POSITIONS: json_response(409, fixture("positions_missing_valuation")),
            FILLS: error(500, "INTERNAL_ERROR"),
        },
        SnapshotRequest(fills=True),
    )
    for snapshot in (full, failed):
        assert _mutable_paths(snapshot) == []


def test_a_read_holding_a_mutable_container_is_refused():
    snapshot, _, _ = assemble()
    read = snapshot.positions.read
    row = read.rows[0]
    listed = TournamentPosition.model_construct(**(dict(row) | {"lots": list(row.lots)}))
    for bad in (
        dataclasses.replace(read, rows=(listed,)),
        dataclasses.replace(read, rows=list(read.rows)),
    ):
        with pytest.raises(TypeError, match="positions.read"):
            ComponentResult.observed(snap.POSITIONS, bad)
