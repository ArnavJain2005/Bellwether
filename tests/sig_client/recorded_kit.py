"""Shared helpers for replaying fixtures derived from recorded SIG captures.

Each tests/fixtures/sig/recorded/<run>/ directory holds bodies, an effective fixture
manifest (fixtures.json) and sanitized source provenance. See tests/fixtures/sig/README.md.
All traffic goes through httpx.MockTransport; conftest.py blocks real network use.
"""

from __future__ import annotations

import hashlib
import json
from decimal import Decimal
from pathlib import Path
from typing import Any

import httpx
from sig_testkit import API, BASE_URL, FIXTURES, SENTINEL_KEY, FakeClock, run

from engine.config import ApiKey, SigConfig, TournamentRef
from engine.sig_client.rate_limiter import RateLimiter
from engine.sig_client.rest import SigRestClient

RECORDED = FIXTURES / "recorded"
RUNS = sorted(p for p in RECORDED.iterdir() if p.is_dir())
TOURNAMENT = TournamentRef(slug="midterm-elections", id="bda92870-621e-47b0-bc3c-3602c5c26f55")
REDACTED = "derived from recorded response, redacted"
UNMODIFIED = "recorded response, unmodified"
CREATOR = ("11111111-1111-4111-8111-111111111111", "redacted-creator-1")
THUMBNAIL_SEGMENT = "22222222-2222-4222-8222-222222222222"


def sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def effective_manifest(run_dir: Path) -> dict[str, Any]:
    return json.loads((run_dir / "fixtures.json").read_bytes())


def fixtures_by_name(run_dir: Path) -> dict[str, dict[str, Any]]:
    return {f["name"]: f for f in effective_manifest(run_dir)["fixtures"]}


def raw_body(run_dir: Path, name: str) -> Any:
    data = (run_dir / fixtures_by_name(run_dir)[name]["file"]).read_bytes()
    return json.loads(data, parse_float=Decimal)


def source_provenance(run_dir: Path) -> tuple[bytes, dict[str, Any]]:
    name = effective_manifest(run_dir)["source_capture"]["provenance_file"]
    data = (run_dir / name).read_bytes()
    return data, json.loads(data)


class Replay:
    """Serves each fixture only for its recorded path and exact recorded query.

    Routes are keyed on path and query together, because pages of one collection share
    a path and differ only by cursor.
    """

    def __init__(self, run_dir: Path) -> None:
        self.requests: list[httpx.Request] = []
        self.unexpected: list[str] = []
        self.routes: dict[tuple[str, frozenset[tuple[str, str]]], tuple[dict[str, Any], bytes]]
        self.routes = {}
        for f in effective_manifest(run_dir)["fixtures"]:
            key = (API + f["request"]["path"], frozenset(f["request"]["params"].items()))
            assert key not in self.routes, f["name"]
            self.routes[key] = (f, (run_dir / f["file"]).read_bytes())

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        route = self.routes.get((request.url.path, frozenset(request.url.params.items())))
        if request.method != "GET" or route is None:
            self.unexpected.append(f"{request.method} {request.url.path}")
            return httpx.Response(418, content=b"{}")
        f, body = route
        return httpx.Response(
            f["response"]["http_status"],
            content=body,
            headers={"content-type": f["response"]["content_type"]},
        )

    def paths(self) -> list[str]:
        return [r.url.path.removeprefix(API) for r in self.requests]

    def sent(self) -> list[tuple[str, dict[str, str]]]:
        return [(r.url.path.removeprefix(API), dict(r.url.params)) for r in self.requests]


def make_replay_client(run_dir: Path) -> tuple[SigRestClient, Replay]:
    served = Replay(run_dir)
    clock = FakeClock()
    config = SigConfig(api_key=ApiKey(SENTINEL_KEY), tournament=TOURNAMENT, base_url=BASE_URL)
    client = SigRestClient(
        config,
        httpx.AsyncClient(transport=httpx.MockTransport(served)),
        owns_http=True,
        clock=clock,
        sleep=clock.sleep,
        limiter=RateLimiter(clock=clock, sleep=clock.sleep),
    )
    return client, served


def replay(run_dir: Path, call: str, *args: Any, **kwargs: Any) -> tuple[Any, Replay]:
    client, served = make_replay_client(run_dir)

    async def scenario() -> Any:
        async with client:
            return await getattr(client, call)(*args, **kwargs)

    result = run(scenario())
    assert served.unexpected == []
    return result, served


def decimals(values: list[Any]) -> list[str]:
    assert all(type(v) is Decimal for v in values)
    return [str(v) for v in values]


def levels(side: list[Any]) -> list[tuple[str, str]]:
    prices = decimals([level.price for level in side])
    quantities = decimals([level.quantity for level in side])
    return list(zip(prices, quantities, strict=True))
