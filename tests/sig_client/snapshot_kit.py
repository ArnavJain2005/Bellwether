"""Helpers for the A3 AccountSnapshot tests. Offline only: synthetic bodies, MockTransport.

`clean_router` serves spec-derived synthetic bodies for every A2 read with no finding
in them (the shared `orders_page` fixture holds a closed order, which would fire K9 and
K10, so open orders here are copies of its open order 1001 only).
"""

from __future__ import annotations

import copy
from collections.abc import Callable
from typing import Any

import httpx
from account_kit import ORDERS, POSITIONS, TOURNAMENT_PATH, orders_page
from sig_testkit import API, FakeClock, Router, fixture, json_response, make_client, run

from engine.account.snapshot import AccountSnapshot, SnapshotRequest, assemble_account_snapshot

FILLS = f"{TOURNAMENT_PATH}/portfolio/fills"
PNL = f"{TOURNAMENT_PATH}/portfolio/pnl"
SETTLEMENTS = f"{TOURNAMENT_PATH}/portfolio/settlements"
ORDER_FILLS = f"{API}/orders/1001/fills"
OTHER_UUID = "11111111-2222-4333-8444-555555555555"

FULL = SnapshotRequest(fills=True, valuation=True, settlements=True, order_fill_ids=(1001,))


def error(status: int, code: str, message: str = "synthetic error text") -> httpx.Response:
    return json_response(status, {"error": {"code": code, "message": message}})


def forbidden() -> httpx.Response:
    return error(403, "FORBIDDEN", "Not a member of this tournament")


def malformed() -> httpx.Response:
    return json_response(200, {"unexpected": True})


def tournament(**changes: Any) -> httpx.Response:
    return json_response(200, fixture("tournament") | changes)


def positions(rows: list[dict[str, Any]] | None = None) -> httpx.Response:
    body = fixture("tournament_positions")
    if rows is not None:
        body["positions"] = rows
    return json_response(200, body)


def position_row(**changes: Any) -> dict[str, Any]:
    return copy.deepcopy(fixture("tournament_positions")["positions"][0]) | changes


def missing_valuation() -> httpx.Response:
    return json_response(409, fixture("positions_missing_valuation"))


def open_order(**changes: Any) -> dict[str, Any]:
    return copy.deepcopy(fixture("orders_page")["data"][0]) | changes


def orders(rows: list[dict[str, Any]] | None = None) -> httpx.Response:
    body = orders_page([1001], cursor=None)
    if rows is not None:
        body["data"] = rows
    return json_response(200, body)


def fills(rows: list[dict[str, Any]] | None = None) -> httpx.Response:
    body = fixture("tournament_fills")
    if rows is not None:
        body["data"] = rows
    return json_response(200, body)


def settlements(complete: bool = True) -> httpx.Response:
    body = fixture("tournament_settlements")
    body["coverage"] = {"complete": complete}
    return json_response(200, body)


def order_fills(**changes: Any) -> httpx.Response:
    return json_response(200, fixture("order_fills") | changes)


def clean_router(overrides: dict[str, Any] | None = None) -> Router:
    routes: dict[str, Any] = {
        TOURNAMENT_PATH: tournament(),
        POSITIONS: positions(),
        ORDERS: orders(),
        FILLS: fills(),
        PNL: json_response(200, fixture("tournament_pnl")),
        SETTLEMENTS: settlements(),
        ORDER_FILLS: order_fills(),
    }
    routes.update(overrides or {})
    return Router(routes)


def sequence(*responses: httpx.Response) -> Callable[[httpx.Request], httpx.Response]:
    """Serve `responses` in order, repeating the last one."""
    queue = list(responses)

    def handler(request: httpx.Request) -> httpx.Response:
        return queue.pop(0) if len(queue) > 1 else queue[0]

    return handler


def assemble(
    overrides: dict[str, Any] | None = None,
    request: SnapshotRequest | None = None,
    *,
    clock: FakeClock | None = None,
    **client_kwargs: Any,
) -> tuple[AccountSnapshot, Router, FakeClock]:
    router = clean_router(overrides)
    client, router, clock = make_client(router, clock=clock, **client_kwargs)

    async def go() -> AccountSnapshot:
        async with client:
            return await assemble_account_snapshot(client, request, clock=clock)

    return run(go()), router, clock
