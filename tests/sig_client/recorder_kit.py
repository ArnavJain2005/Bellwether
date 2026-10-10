"""Helpers for the fixture-recorder tests. Every response served here is synthetic.

Runs go through httpx.MockTransport, so the recorder labels them "synthetic"; nothing in
these tests can produce a capture labelled as a genuine recording.
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import httpx
from sig_testkit import (
    API,
    BASE_URL,
    SENTINEL_KEY,
    TOURNAMENT,
    FakeClock,
    Router,
    fixture,
    json_response,
    run,
)

from engine.config import ApiKey, SigConfig
from engine.sig_client import record_fixtures as rf
from engine.sig_client.rate_limiter import RateLimiter

TID = TOURNAMENT.id
TOURNAMENT_PATH = f"{API}/tournaments/alpha-tournament"
MARKETS = f"{TOURNAMENT_PATH}/markets"
PRICES = f"{API}/exchanges/prices"
EXCHANGE_BOOK = f"{API}/exchanges/36/orderbook"
MARKET_BOOK = f"{API}/markets/26/orderbook"
ORDERS = f"{API}/orders"
MARKET_DETAIL = f"{API}/markets/26"
TRADES = f"{API}/exchanges/36/trades"

BUILD = {
    "code": {"recorder_version": "test", "git_commit": None, "git_dirty": None},
    "spec": {"path": "docs/api/openapi.json", "sha256": None, "version": None},
}


def fixed_now() -> datetime:
    return datetime(2026, 10, 9, 12, 0, 0, tzinfo=UTC)


def prices_route(request: httpx.Request) -> httpx.Response:
    """Prices for exactly the requested ids, in request order, from the synthetic fixture."""
    ids = request.url.params["ids"].split(",")
    known = {entry["exchangeId"]: entry for entry in fixture("exchange_prices")["data"]}
    return json_response(
        200,
        {
            "data": [known[i] for i in ids if i in known],
            "missingIds": [i for i in ids if i not in known],
        },
    )


def markets_route(request: httpx.Request) -> httpx.Response:
    """Synthetic page 1 (hasMore, cursor-page-2) or page 2 (last page)."""
    second = request.url.params.get("cursor") == "cursor-page-2"
    return json_response(
        200, fixture("tournament_markets_page2" if second else "tournament_markets_page1")
    )


def sig_router(overrides: Mapping[str, Any] | None = None) -> Router:
    routes: dict[str, Any] = {PRICES: prices_route, MARKETS: markets_route}
    routes.update(overrides or {})
    return Router(routes)


def config(api_key: str = SENTINEL_KEY) -> SigConfig:
    return SigConfig(api_key=ApiKey(api_key), tournament=TOURNAMENT, base_url=BASE_URL)


def plan(tmp_path: Path, **overrides: Any) -> rf.CapturePlan:
    values: dict[str, Any] = {
        "tournament": TOURNAMENT,
        "exchange_ids": ("36",),
        "market_ids": ("26",),
        "output_root": tmp_path / "captures",
    }
    values.update(overrides)
    return rf.CapturePlan(**values)


def capture(
    tmp_path: Path,
    router: Router | None = None,
    *,
    capture_plan: rf.CapturePlan | None = None,
    clock: FakeClock | None = None,
    limiter: RateLimiter | None = None,
    api_key: str = SENTINEL_KEY,
    run_id: str = "run-1",
) -> tuple[rf.RunResult, dict[str, Any], Router]:
    router = router or sig_router()
    clock = clock or FakeClock()
    result = run(
        rf.record(
            capture_plan or plan(tmp_path),
            config(api_key),
            run_id=run_id,
            transport=httpx.MockTransport(router),
            limiter=limiter,
            clock=clock,
            sleep=clock.sleep,
            now=fixed_now,
            build=BUILD,
        )
    )
    return result, read_manifest(result.run_dir), router


def read_manifest(run_dir: Path) -> dict[str, Any]:
    return json.loads((run_dir / rf.MANIFEST_NAME).read_bytes())


def step(manifest: dict[str, Any], name: str) -> dict[str, Any]:
    return next(s for s in manifest["steps"] if s["name"] == name)


def all_bytes(run_dir: Path) -> bytes:
    """Every file the run wrote, concatenated (for leak scans)."""
    return b"".join(path.read_bytes() for path in sorted(run_dir.rglob("*")) if path.is_file())
