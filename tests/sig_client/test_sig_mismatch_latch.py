"""A2-A-F1 finding 2: once a tournament mismatch is latched, nothing new is sent.

The latch is permanent for the client's life and is the first mismatch observed. A read
that is already waiting (on the verification lock, its verification read, rate
admission or a retry wait) when another read latches a mismatch sends no further
request: no endpoint request after a verification that completed too late, no attempt
after rate admission, and no retry.

Every race here is forced with events, never with timing.
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from typing import Any

import httpx
import pytest
from account_kit import (
    CALLER,
    ORDERS,
    POSITIONS,
    TOURNAMENT_PATH,
    as_caller,
    per_caller,
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
    run,
)

from engine.sig_client.errors import SigContextMismatchError
from engine.sig_client.rate_limiter import RateLimiter, RequestKind
from engine.sig_client.rest import SigRestClient

OTHER_UUID = "11111111-2222-4333-8444-555555555555"
MATCHING = json_response(200, tournament_body())
MISMATCHED = json_response(200, tournament_body(id=OTHER_UUID))


def held(gate: asyncio.Event, response: httpx.Response) -> Callable[[], Awaitable[Any]]:
    async def serve() -> httpx.Response:
        await gate.wait()
        return response

    return serve


def immediate(response: httpx.Response) -> Callable[[], Awaitable[Any]]:
    async def serve() -> httpx.Response:
        return response

    return serve


def count(router: Router, path: str) -> int:
    return sum(r.url.path == path for r in router.requests)


# --- the reported race: a cold read's verification completes after a latch ------------


@pytest.mark.parametrize(
    ("call", "endpoint"),
    [
        (lambda c: c.read_tournament_positions(), POSITIONS),
        (lambda c: c.read_open_orders(), ORDERS),
        (lambda c: c.list_orders_page(), ORDERS),
        (lambda c: c.get_exchange_orderbook("36"), f"{API}/exchanges/36/orderbook"),
    ],
)
def test_no_endpoint_request_after_a_mismatch_latched_during_verification(
    call: Callable[[SigRestClient], Awaitable[Any]], endpoint: str
) -> None:
    release = asyncio.Event()
    scripts = {"cold": [held(release, MATCHING)], "balance": [immediate(MISMATCHED)]}
    router = Router({TOURNAMENT_PATH: per_caller(scripts)})
    client, _, _ = make_client(router)

    async def scenario() -> None:
        cold = asyncio.create_task(as_caller("cold", call(client)))
        await until(lambda: count(router, TOURNAMENT_PATH) == 1)  # verification waits
        with pytest.raises(SigContextMismatchError) as latched:
            await as_caller("balance", client.read_tournament_balance())
        release.set()  # the cold read's verification now matches
        with pytest.raises(SigContextMismatchError) as cold_error:
            await cold
        assert cold_error.value is latched.value  # the permanent, first latch

    run(scenario())
    assert count(router, endpoint) == 0  # nothing was sent after the latch
    assert count(router, TOURNAMENT_PATH) == 2


def test_a_waiter_on_the_verification_lock_sends_nothing_after_a_latch() -> None:
    release = asyncio.Event()
    scripts = {
        "first": [held(release, MATCHING)],
        "balance": [immediate(MISMATCHED)],
    }
    router = Router({TOURNAMENT_PATH: per_caller(scripts)})
    client, _, _ = make_client(router)

    async def scenario() -> None:
        first = asyncio.create_task(as_caller("first", client.read_tournament_positions()))
        await until(lambda: count(router, TOURNAMENT_PATH) == 1)
        second = asyncio.create_task(as_caller("second", client.read_tournament_positions()))
        for _ in range(5):
            await asyncio.sleep(0)  # second now waits for the verification lock
        with pytest.raises(SigContextMismatchError):
            await as_caller("balance", client.read_tournament_balance())
        release.set()
        for task in (first, second):
            with pytest.raises(SigContextMismatchError):
                await task

    run(scenario())
    assert count(router, POSITIONS) == 0
    assert count(router, TOURNAMENT_PATH) == 2


# --- retries and rate admission ---------------------------------------------------------


class GatedSleepClock(FakeClock):
    """Fake time whose sleeps also wait for `gate`, so a retry wait can be held open."""

    def __init__(self) -> None:
        super().__init__()
        self.gate = asyncio.Event()

    async def sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)
        self.now += seconds
        await self.gate.wait()


def test_no_retry_after_a_mismatch_latched_during_the_retry_wait() -> None:
    clock = GatedSleepClock()
    router = Router(
        {
            TOURNAMENT_PATH: MATCHING,
            POSITIONS: [
                json_response(503, error_body("SERVICE_UNAVAILABLE")),
                json_response(200, fixture("tournament_positions")),
            ],
        }
    )
    client, _, _ = make_client(router, clock=clock)

    async def scenario() -> None:
        await client.verify_tournament()
        positions = asyncio.create_task(client.read_tournament_positions())
        await until(lambda: clock.sleeps == [0.25])  # first attempt failed; waiting
        router.routes[TOURNAMENT_PATH] = MISMATCHED
        with pytest.raises(SigContextMismatchError):
            await client.read_tournament_balance()
        clock.gate.set()
        with pytest.raises(SigContextMismatchError):
            await positions

    run(scenario())
    assert count(router, POSITIONS) == 1  # the retry was never sent


class GatedLimiter(RateLimiter):
    """Holds rate admission for one named caller until `gate` is set."""

    def __init__(self, caller: str, clock: FakeClock) -> None:
        super().__init__(clock=clock, sleep=clock.sleep)
        self.caller = caller
        self.gate = asyncio.Event()
        self.holding = False

    async def acquire(self, kind: RequestKind, *, deadline: float | None = None) -> None:
        if CALLER.get(None) == self.caller:
            self.holding = True
            await self.gate.wait()
        await super().acquire(kind, deadline=deadline)


def test_no_request_after_a_mismatch_latched_during_rate_admission() -> None:
    clock = FakeClock()
    limiter = GatedLimiter("positions", clock)
    router = Router({TOURNAMENT_PATH: MATCHING})
    client, _, _ = make_client(router, clock=clock, limiter=limiter)

    async def scenario() -> None:
        await client.verify_tournament()
        positions = asyncio.create_task(as_caller("positions", client.read_tournament_positions()))
        await until(lambda: limiter.holding)
        router.routes[TOURNAMENT_PATH] = MISMATCHED
        with pytest.raises(SigContextMismatchError):
            await client.read_tournament_balance()
        limiter.gate.set()
        with pytest.raises(SigContextMismatchError):
            await positions

    run(scenario())
    assert count(router, POSITIONS) == 0


# --- the latch is the first mismatch, and it is permanent ---------------------------------


def test_the_first_mismatch_stays_latched_when_a_second_one_arrives() -> None:
    gate = asyncio.Event()
    scripts = {
        "a": [immediate(json_response(200, tournament_body(id=OTHER_UUID)))],
        "b": [held(gate, json_response(200, tournament_body(slug="another-slug")))],
    }
    router = Router({TOURNAMENT_PATH: per_caller(scripts)})
    client, _, _ = make_client(router)

    async def scenario() -> list[SigContextMismatchError]:
        b = asyncio.create_task(as_caller("b", client.read_tournament_balance()))
        await until(lambda: count(router, TOURNAMENT_PATH) == 1)
        errors = []
        with pytest.raises(SigContextMismatchError) as first:
            await as_caller("a", client.read_tournament_balance())
        errors.append(first.value)
        gate.set()
        with pytest.raises(SigContextMismatchError) as second:
            await b
        errors.append(second.value)
        with pytest.raises(SigContextMismatchError) as later:
            await client.get_tournament()
        errors.append(later.value)
        return errors

    errors = run(scenario())
    assert all(error is errors[0] for error in errors)
    # The first mismatch (a different id) is kept, not the later one (a different slug).
    assert errors[0].reason.startswith("SIG reports a different tournament id than")


def test_no_public_read_sends_anything_after_the_latch() -> None:
    router = Router({TOURNAMENT_PATH: MISMATCHED})
    client, _, _ = make_client(router)
    calls: list[Callable[[SigRestClient], Awaitable[Any]]] = [
        lambda c: c.get_tournament(),
        lambda c: c.verify_tournament(),
        lambda c: c.read_tournament_balance(),
        lambda c: c.read_tournament_positions(),
        lambda c: c.read_open_orders(),
        lambda c: c.list_orders_page(),
        lambda c: c.list_all_orders(),
        lambda c: c.list_tournament_markets_page(),
        lambda c: c.list_all_tournament_markets(),
        lambda c: c.get_market("26"),
        lambda c: c.get_exchange_prices(["36"]),
        lambda c: c.get_exchange_orderbook("36"),
        lambda c: c.get_market_orderbook("26"),
        lambda c: c.list_exchange_trades_page("36"),
        lambda c: c.list_all_exchange_trades("36"),
    ]

    async def scenario() -> None:
        with pytest.raises(SigContextMismatchError):
            await client.read_tournament_balance()
        for call in calls:
            with pytest.raises(SigContextMismatchError):
                await call(client)

    run(scenario())
    assert router.paths() == ["/tournaments/alpha-tournament"]


# --- controls: normal concurrent use is unchanged ---------------------------------------------


def test_control_concurrent_matching_reads_verify_once_and_all_succeed() -> None:
    client, router, _ = make_client()

    async def scenario() -> list[Any]:
        return await asyncio.gather(
            client.read_tournament_positions(),
            client.read_open_orders(),
            client.read_tournament_balance(),
            client.get_exchange_orderbook("36"),
        )

    positions, orders, balance, book = run(scenario())
    assert positions.rows and orders.orders and book.exchangeId == "36"
    assert balance.tournament.slug == "alpha-tournament"
    assert count(router, POSITIONS) == 1 and count(router, ORDERS) == 1
    # One verification read shared by the scoped reads, plus the balance read's own.
    assert count(router, TOURNAMENT_PATH) == 2


def test_control_retries_still_happen_without_a_mismatch() -> None:
    router = Router(
        {
            POSITIONS: [
                json_response(503, error_body("SERVICE_UNAVAILABLE")),
                json_response(200, fixture("tournament_positions")),
            ]
        }
    )
    client, _, clock = make_client(router)
    result = run(client.read_tournament_positions())
    assert result.provenance.attempts == 2 and clock.sleeps == [0.25]
    assert count(router, POSITIONS) == 2
