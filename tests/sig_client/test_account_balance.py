"""A2-A: read_tournament_balance (design T1-T5) and the provenance of every A2-A read.

All offline: synthetic bodies through httpx.MockTransport, fake monotonic and wall clocks.
"""

from __future__ import annotations

import asyncio
import inspect
import json
from datetime import UTC, datetime, timedelta, timezone
from decimal import Decimal
from typing import Any

import httpx
import pytest
from account_kit import (
    POSITIONS,
    TOURNAMENT_PATH,
    WallClock,
    as_caller,
    per_caller,
    taking,
    tournament_body,
    until,
)
from sig_testkit import (
    API,
    FakeClock,
    Router,
    error_body,
    fixture,
    json_response,
    make_client,
    raw_response,
    run,
)

from engine.config import TournamentRef
from engine.sig_client.account_reads import (
    CoverageState,
    ScopeEvidence,
    TournamentBalanceRead,
    VerificationUse,
)
from engine.sig_client.errors import (
    SigClientClosedError,
    SigContextMismatchError,
    SigDeadlineExceededError,
    SigResponseError,
    SigRetryExhaustedError,
)
from engine.sig_client.rate_limiter import RequestKind
from engine.sig_client.rest import ReadRetryPolicy, SigRestClient
from engine.sig_client.wire import Tournament

OTHER_UUID = "11111111-2222-4333-8444-555555555555"


def unavailable() -> httpx.Response:
    return json_response(503, error_body("SERVICE_UNAVAILABLE"))


# --- T1: always fresh, never the verification cache -----------------------------------


def test_t1_every_balance_read_sends_a_request_even_with_a_verified_cache() -> None:
    clock = FakeClock()
    served = [
        tournament_body(myBalance=100),  # consumed by verify_tournament
        tournament_body(myBalance=200),
        tournament_body(myBalance=300),
    ]

    def handler(request: httpx.Request) -> httpx.Response:
        clock.now += 1.5  # each request takes 1.5 s on the fake monotonic clock
        return json_response(200, served.pop(0))

    wall = WallClock()
    client, router, _ = make_client(
        Router({TOURNAMENT_PATH: handler}), clock=clock, wall_clock=wall
    )

    async def scenario() -> tuple[Tournament, TournamentBalanceRead, TournamentBalanceRead]:
        cached = await client.verify_tournament()
        first = await client.read_tournament_balance()
        await clock.sleep(10)
        second = await client.read_tournament_balance()
        return cached, first, second

    cached, first, second = run(scenario())
    assert router.count("/tournaments/alpha-tournament") == 3
    assert cached.myBalance == Decimal(100)
    # Neither result is the cached object or its value.
    assert first.tournament is not cached and second.tournament is not cached
    assert (first.tournament.myBalance, second.tournament.myBalance) == (200, 300)
    # Each provenance describes its own request.
    one, two = first.provenance, second.provenance
    assert (one.started_mono, one.finished_mono) == (1001.5, 1003.0)
    assert (two.started_mono, two.finished_mono) == (1013.0, 1014.5)
    assert one.finished_utc < two.finished_utc and one.finished_utc.tzinfo is UTC
    for provenance in (one, two):
        assert provenance.route == "/tournaments/{slug}"
        assert provenance.path == "/tournaments/alpha-tournament"
        assert provenance.params == () and provenance.cursor_sent is False
        assert provenance.scope is ScopeEvidence.ECHO
        assert provenance.attempts == 1 and provenance.http_status == 200
        assert provenance.page_index is None
        assert provenance.coverage_state is CoverageState.NOT_APPLICABLE
        assert provenance.coverage is None
        assert provenance.verification is VerificationUse.NOT_APPLICABLE
        assert provenance.verification_read is None


def test_t1_identity_is_checked_on_every_balance_read() -> None:
    client, router, _ = make_client(
        Router(
            {
                TOURNAMENT_PATH: [
                    json_response(200, tournament_body()),
                    json_response(200, tournament_body(id=OTHER_UUID)),
                ]
            }
        )
    )

    async def scenario() -> None:
        await client.read_tournament_balance()
        with pytest.raises(SigContextMismatchError):
            await client.read_tournament_balance()

    run(scenario())
    assert router.count("/tournaments/alpha-tournament") == 2


# --- T2: mismatch raises and latches -----------------------------------------------------


@pytest.mark.parametrize(
    "change", [{"slug": "other-tournament"}, {"id": OTHER_UUID}, {"slug": "x", "id": OTHER_UUID}]
)
def test_t2_mismatch_raises_and_latches_for_every_later_read(change: dict[str, Any]) -> None:
    client, router, _ = make_client(
        Router({TOURNAMENT_PATH: json_response(200, tournament_body(**change))})
    )

    async def scenario() -> list[SigContextMismatchError]:
        errors = []
        with pytest.raises(SigContextMismatchError) as info:
            await client.read_tournament_balance()
        errors.append(info.value)
        for call in (
            client.read_tournament_balance,
            client.read_tournament_positions,
            client.read_open_orders,
            client.get_tournament,
            client.verify_tournament,
        ):
            with pytest.raises(SigContextMismatchError) as info:
                await call()
            errors.append(info.value)
        return errors

    errors = run(scenario())
    assert all(error is errors[0] for error in errors)  # the same latched error
    assert router.paths() == ["/tournaments/alpha-tournament"]  # nothing else was sent


def test_t2_a_mismatch_after_verification_still_latches_cached_reads() -> None:
    client, router, _ = make_client(
        Router(
            {
                TOURNAMENT_PATH: [
                    json_response(200, tournament_body()),
                    json_response(200, tournament_body(slug="renamed")),
                ]
            }
        )
    )

    async def scenario() -> None:
        await client.verify_tournament()
        await client.read_tournament_positions()  # verified: served from the cache
        with pytest.raises(SigContextMismatchError):
            await client.read_tournament_balance()
        with pytest.raises(SigContextMismatchError):
            await client.read_tournament_positions()

    run(scenario())
    assert router.count("/tournaments/alpha-tournament/portfolio/positions") == 1


def test_a_mismatch_latched_while_a_read_is_in_flight_fails_that_read() -> None:
    release = asyncio.Event()

    async def slow_positions(request: httpx.Request) -> httpx.Response:
        await release.wait()
        return json_response(200, fixture("tournament_positions"))

    router = Router({POSITIONS: slow_positions})
    client, _, _ = make_client(router)

    async def scenario() -> None:
        await client.verify_tournament()
        router.routes[TOURNAMENT_PATH] = json_response(200, tournament_body(id=OTHER_UUID))
        positions = asyncio.create_task(client.read_tournament_positions())
        await until(lambda: router.count("/tournaments/alpha-tournament/portfolio/positions") == 1)
        with pytest.raises(SigContextMismatchError):
            await client.read_tournament_balance()
        release.set()
        with pytest.raises(SigContextMismatchError):
            await positions  # its response arrived after the latch: not returned

    run(scenario())


# --- T3: exact values ------------------------------------------------------------------


@pytest.mark.parametrize(
    "literal",
    [
        "847.5",
        "1e2",
        "1E-7",
        "12345678901234567890.12345678901234567890",
        "-0.005",
        "-250",
        "100000",
        "0.30000000000000000001",
    ],
)
def test_t3_balance_is_kept_as_the_exact_decimal_sent(literal: str) -> None:
    text = json.dumps(tournament_body(myBalance="@@")).replace('"@@"', literal)
    client, _, _ = make_client(Router({TOURNAMENT_PATH: raw_response(200, text.encode())}))
    balance = run(client.read_tournament_balance()).tournament.myBalance
    assert isinstance(balance, Decimal)
    assert balance.as_tuple() == Decimal(literal).as_tuple()  # same digits and exponent


# --- T4: malformed balance fields --------------------------------------------------------


MISSING = object()


@pytest.mark.parametrize(
    ("field", "value"),
    [
        *[("myBalance", v) for v in (MISSING, None, "SENTINEL_VALUE_847", "847.5", True, [1])],
        *[("isPendingEnrolment", v) for v in (MISSING, None, "SENTINEL_VALUE_847", "true", 1, 0)],
    ],
)
def test_t4_missing_null_or_wrongly_typed_balance_fields_are_rejected(
    field: str, value: Any
) -> None:
    body = tournament_body()
    if value is MISSING:
        del body[field]
    else:
        body[field] = value
    client, _, _ = make_client(Router({TOURNAMENT_PATH: json_response(200, body)}))
    with pytest.raises(SigResponseError) as info:
        run(client.read_tournament_balance())
    assert field in info.value.reason  # the location is named ...
    assert "SENTINEL_VALUE_847" not in str(info.value)  # ... never the value


# --- T5: always the configured tournament --------------------------------------------------


def test_t5_account_reads_take_no_scope_argument() -> None:
    for name, expected in (
        ("read_tournament_balance", []),
        ("read_tournament_positions", []),
        ("read_open_orders", ["page_limit", "max_pages"]),
    ):
        parameters = list(inspect.signature(getattr(SigRestClient, name)).parameters)
        assert parameters == ["self", *expected], name


def test_t5_requests_are_built_from_the_configured_tournament_only() -> None:
    beta = TournamentRef(slug="beta-tournament", id=OTHER_UUID)
    tournament = tournament_body(slug=beta.slug, id=beta.id)
    router = Router(
        {
            f"{API}/tournaments/beta-tournament": json_response(200, tournament),
            f"{API}/tournaments/beta-tournament/portfolio/positions": json_response(
                200, fixture("tournament_positions")
            ),
        }
    )
    client, _, _ = make_client(router, tournament=beta)

    async def scenario() -> None:
        await client.read_tournament_balance()
        await client.read_tournament_positions()
        await client.read_open_orders()

    run(scenario())
    assert router.paths() == [
        "/tournaments/beta-tournament",
        "/tournaments/beta-tournament/portfolio/positions",
        "/orders",
    ]
    for request in router.requests:
        path = request.url.path.removeprefix(API)
        assert not path.startswith(("/account", "/portfolio"))  # never a default context
        if path == "/orders":
            assert request.url.params["tournamentId"] == beta.id
            assert request.url.params["status"] == "open"
        else:
            assert request.url.params == httpx.QueryParams()  # slug-bound, no query


# --- per-call provenance: concurrency, retries, deadlines, cancellation -------------------


def test_concurrent_reads_keep_their_own_attempts_times_and_values() -> None:
    # Two balance reads that both retry, with their attempts strictly interleaved:
    # a1 < b1 < a2 < b2 < a3. Each result must report only its own attempts, value and
    # times; a counter or "last response" shared across calls would mix them.
    steps = {name: asyncio.Event() for name in ("a1", "b1", "a2", "b2")}

    def attempt(after: str | None, done: str | None, response: httpx.Response) -> Any:
        async def serve() -> httpx.Response:
            if after is not None:
                await steps[after].wait()
            if done is not None:
                steps[done].set()
            return response

        return serve

    scripts = {
        "a": [
            attempt(None, "a1", unavailable()),
            attempt("b1", "a2", unavailable()),
            attempt("b2", None, json_response(200, tournament_body(myBalance=111))),
        ],
        "b": [
            attempt("a1", "b1", unavailable()),
            attempt("a2", "b2", json_response(200, tournament_body(myBalance=222))),
        ],
    }
    client, _, clock = make_client(Router({TOURNAMENT_PATH: per_caller(scripts)}))

    async def scenario() -> list[TournamentBalanceRead]:
        return await asyncio.gather(
            as_caller("a", client.read_tournament_balance()),
            as_caller("b", client.read_tournament_balance()),
        )

    a, b = run(scenario())
    assert (a.tournament.myBalance, a.provenance.attempts) == (111, 3)
    assert (b.tournament.myBalance, b.provenance.attempts) == (222, 2)
    assert a.provenance.started_mono <= b.provenance.started_mono
    assert b.provenance.finished_mono <= a.provenance.finished_mono
    assert sorted(clock.sleeps) == [0.25, 0.25, 0.5]  # a: 0.25, 0.5; b: 0.25
    assert client.limiter.used(RequestKind.READ) == 5


def test_retries_are_counted_for_the_read_that_made_them() -> None:
    client, router, clock = make_client(
        Router(
            {
                TOURNAMENT_PATH: [
                    unavailable(),
                    json_response(503, error_body("TX_CONFLICT"), {"Retry-After": "2"}),
                    json_response(200, tournament_body()),
                ]
            }
        )
    )
    read = run(client.read_tournament_balance())
    assert read.provenance.attempts == 3 and read.provenance.http_status == 200
    assert clock.sleeps == [0.25, 2.0]  # Retry-After is never shortened
    assert read.provenance.started_mono == 1000.0
    assert read.provenance.finished_mono == 1002.25
    # A later read starts its own count.
    assert run(client.read_tournament_balance()).provenance.attempts == 1
    assert router.count("/tournaments/alpha-tournament") == 4


def test_exhausted_retries_return_no_result() -> None:
    client, _, _ = make_client(Router({TOURNAMENT_PATH: [unavailable()]}))
    with pytest.raises(SigRetryExhaustedError) as info:
        run(client.read_tournament_balance())
    assert info.value.attempts == 4


def test_a_response_after_the_deadline_is_not_returned() -> None:
    clock = FakeClock()
    late = taking(clock, 31.0, json_response(200, tournament_body()))
    client, _, _ = make_client(Router({TOURNAMENT_PATH: late}), clock=clock)
    with pytest.raises(SigDeadlineExceededError) as info:
        run(client.read_tournament_balance())
    assert info.value.phase == "request" and info.value.attempts == 1


def test_a_retry_wait_beyond_the_deadline_ends_the_read() -> None:
    client, _, clock = make_client(
        Router(
            {TOURNAMENT_PATH: json_response(503, error_body("TX_CONFLICT"), {"Retry-After": "40"})}
        ),
        retry=ReadRetryPolicy(total_budget_s=30.0),
    )
    with pytest.raises(SigDeadlineExceededError) as info:
        run(client.read_tournament_balance())
    assert info.value.phase == "retry wait" and info.value.attempts == 1
    assert clock.sleeps == []


def test_cancellation_leaves_nothing_behind_for_the_next_read() -> None:
    hang = asyncio.Event()

    async def hanging(request: httpx.Request) -> httpx.Response:
        await hang.wait()
        raise AssertionError("unreachable")

    router = Router({TOURNAMENT_PATH: hanging})
    client, _, _ = make_client(router)

    async def scenario() -> TournamentBalanceRead:
        task = asyncio.create_task(client.read_tournament_balance())
        await until(lambda: router.count("/tournaments/alpha-tournament") == 1)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        router.routes[TOURNAMENT_PATH] = json_response(200, tournament_body(myBalance=5))
        return await client.read_tournament_balance()

    read = run(scenario())
    assert read.tournament.myBalance == 5 and read.provenance.attempts == 1


def test_closed_client_sends_nothing() -> None:
    client, router, _ = make_client()

    async def scenario() -> None:
        await client.aclose()
        for call in (
            client.read_tournament_balance,
            client.read_tournament_positions,
            client.read_open_orders,
        ):
            with pytest.raises(SigClientClosedError):
                await call()

    run(scenario())
    assert router.requests == []


@pytest.mark.parametrize(
    "moment",
    [
        datetime.fromisoformat("2026-10-13T12:00:00"),  # naive
        datetime(2026, 10, 13, 17, 30, tzinfo=timezone(timedelta(hours=5, minutes=30))),  # IST
    ],
)
def test_wall_clock_must_be_utc(moment: datetime) -> None:
    client, _, _ = make_client(wall_clock=lambda: moment)
    with pytest.raises(ValueError, match="finished_utc"):
        run(client.read_tournament_balance())
