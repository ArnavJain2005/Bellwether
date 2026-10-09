"""S1-T4-A recorder: routes, bounds, exact bodies, exclusions and failure manifests.

Every response is synthetic and served through httpx.MockTransport; conftest.py blocks
the real network. The recorder therefore labels every run here "synthetic".
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
from decimal import Decimal
from pathlib import Path
from typing import Any

import httpx
import pytest
from recorder_kit import (
    BUILD,
    EXCHANGE_BOOK,
    MARKET_BOOK,
    MARKETS,
    ORDERS,
    PRICES,
    TID,
    TOURNAMENT_PATH,
    all_bytes,
    capture,
    config,
    fixed_now,
    markets_route,
    plan,
    prices_route,
    read_manifest,
    sig_router,
    step,
)
from sig_testkit import (
    API,
    BASE_URL,
    SENTINEL_KEY,
    TOURNAMENT,
    FakeClock,
    Router,
    fixture,
    json_response,
    raw_response,
    run,
)

from engine.config import ApiKey, TournamentRef
from engine.sig_client import record_fixtures as rf
from engine.sig_client.rate_limiter import RateLimiter, RequestKind
from engine.sig_client.rest import SigRestClient

COOKIE_SENTINEL = "DUMMY_COOKIE_SENTINEL"  # test placeholder, never a credential
ERROR_TEXT_SENTINEL = "DUMMY_ERROR_TEXT_SENTINEL"  # test placeholder

DEFAULT_REQUESTS = [
    ("GET", "/tournaments/alpha-tournament", {}),
    ("GET", "/tournaments/alpha-tournament/markets", {"limit": "10", "status": "any"}),
    ("GET", "/exchanges/prices", {"ids": "36", "tournamentId": TID}),
    ("GET", "/exchanges/36/orderbook", {"depth": "5", "tournamentId": TID}),
    ("GET", "/markets/26/orderbook", {"depth": "5", "tournamentId": TID}),
    ("GET", "/orders", {"status": "open", "limit": "10", "tournamentId": TID}),
]


def sent(router: Router) -> list[tuple[str, str, dict[str, str]]]:
    return [
        (r.method, r.url.path.removeprefix(API), dict(r.url.params)) for r in router.requests
    ]


# --- routes, scope, manifest ------------------------------------------------------------


def test_full_run_uses_only_the_six_get_routes_with_explicit_scope(tmp_path: Path) -> None:
    result, manifest, router = capture(tmp_path)
    assert result.status == "succeeded" and result.exit_code == 0
    assert sent(router) == DEFAULT_REQUESTS
    assert all(r.headers["authorization"] == f"Bearer {SENTINEL_KEY}" for r in router.requests)
    assert [s["name"] for s in manifest["steps"]] == [
        "tournament",
        "tournament-markets-p1",
        "exchange-prices",
        "exchange-orderbook-36",
        "market-orderbook-26",
        "orders-p1",
    ]
    for done in manifest["steps"]:
        assert done["outcome"] == "passed" and done["client_validation_passed"]
    assert manifest["not_run"] == []
    assert manifest["refused_requests"] == 0


def test_manifest_records_provenance_identity_and_review_state(tmp_path: Path) -> None:
    result, manifest, _ = capture(tmp_path)
    assert manifest["format"] == rf.CAPTURE_FORMAT
    assert manifest["provenance"]["kind"] == "synthetic"
    assert "NOT a SIG recording" in manifest["provenance"]["detail"]
    assert manifest["run"] == {
        "id": "run-1",
        "status": "succeeded",
        "stop_reason": None,
        "started_at": "2026-10-09T12:00:00.000Z",
        "finished_at": "2026-10-09T12:00:00.000Z",
    }
    assert manifest["code"] == BUILD["code"] and manifest["spec"] == BUILD["spec"]
    assert manifest["target"] == {
        "base_url": BASE_URL,
        "tournament": {"slug": TOURNAMENT.slug, "id": TID},
    }
    assert manifest["selection"]["exchange_ids"] == ["36"]
    assert manifest["review"]["sanitized"] is False
    assert manifest["review"]["required_before_commit"] is True
    assert manifest["transformations"]
    assert [p["name"] for p in manifest["plan"]][:2] == ["tournament", "tournament-markets"]
    tournament = step(manifest, "tournament")
    assert tournament["route"] == "GET /tournaments/{slug}"
    attempt = tournament["attempts"][0]
    assert attempt["http_status"] == 200
    assert attempt["headers"] == {"content-type": "application/json"}
    assert attempt["body"]["recorded"] is True
    assert attempt["body"]["file"] == "bodies/01-tournament-a1.json"
    assert (result.run_dir / ".gitignore").read_text(encoding="utf-8").splitlines()[-1] == "*"


def test_real_provenance_only_without_an_injected_transport(tmp_path: Path) -> None:
    # Every test injects a transport, so it can never claim a genuine recording.
    _, manifest, _ = capture(tmp_path)
    assert manifest["provenance"]["kind"] != "recorded"


def test_skip_orders_never_reads_orders(tmp_path: Path) -> None:
    result, manifest, router = capture(tmp_path, capture_plan=plan(tmp_path, include_orders=False))
    assert result.status == "succeeded"
    assert "/orders" not in router.paths()
    assert "orders" not in manifest["collections"]


def test_one_shared_limiter_admits_every_attempt(tmp_path: Path) -> None:
    clock = FakeClock()
    limiter = RateLimiter(read_limit=15, write_limit=1, clock=clock, sleep=clock.sleep)
    _, manifest, router = capture(tmp_path, clock=clock, limiter=limiter)
    attempts = sum(len(s["attempts"]) for s in manifest["steps"])
    assert limiter.used(RequestKind.READ) == attempts == len(router.requests) == 6
    assert limiter.used(RequestKind.WRITE) == 0


def test_default_limiter_is_the_small_recorder_budget(tmp_path: Path) -> None:
    exchanges = tuple(str(i) for i in range(30, 40))
    markets = tuple(str(i) for i in range(20, 30))

    def any_book(request: httpx.Request) -> httpx.Response:
        body = fixture("exchange_orderbook")
        body["exchangeId"] = request.url.path.split("/")[-2]
        return json_response(200, body)

    routes: dict[str, Any] = {f"{API}/exchanges/{e}/orderbook": any_book for e in exchanges}
    routes.update({f"{API}/markets/{m}/orderbook": json_response(404, {}) for m in markets})
    clock = FakeClock()
    _, manifest, router = capture(
        tmp_path,
        sig_router(routes),
        capture_plan=plan(tmp_path, exchange_ids=exchanges, market_ids=markets),
        clock=clock,
    )
    # 24 reads, all admitted: the recorder's own limiter spaces them 4 s apart, so no
    # wait comes near the client's 30 s read deadline.
    assert len(router.requests) == 24
    assert [s["outcome"] for s in manifest["steps"]].count("passed") == 14
    assert max(clock.sleeps) < 4.01
    assert clock.now - 1000.0 >= 23 * 4.0


# --- pagination bounds ------------------------------------------------------------------


def test_one_page_by_default_is_recorded_as_truncated(tmp_path: Path) -> None:
    result, manifest, router = capture(tmp_path)
    assert router.count("/tournaments/alpha-tournament/markets") == 1
    markets = manifest["collections"]["tournament_markets"]
    assert markets["complete"] is False and markets["truncated"] is True
    assert "not the full collection" in markets["note"]
    assert step(manifest, "tournament-markets-p1")["page"] == {
        "items": 1,
        "has_more": True,
        "next_cursor_present": True,
    }
    assert manifest["collections"]["orders"]["complete"] is True
    assert result.status == "succeeded"  # truncation by design is not a failure


def test_cursor_is_followed_up_to_the_page_cap(tmp_path: Path) -> None:
    _, manifest, router = capture(tmp_path, capture_plan=plan(tmp_path, markets_max_pages=2))
    assert router.count("/tournaments/alpha-tournament/markets") == 2
    second = step(manifest, "tournament-markets-p2")
    assert second["params"]["cursor"] == "cursor-page-2"
    assert second["attempts"][0]["params"]["cursor"] == "cursor-page-2"
    markets = manifest["collections"]["tournament_markets"]
    assert markets == {
        "max_pages": 2,
        "pages_fetched": 2,
        "complete": True,
        "truncated": False,
        "note": None,
    }


def test_orders_never_exceed_the_page_cap(tmp_path: Path) -> None:
    counter = iter(range(1, 100))

    def endless(request: httpx.Request) -> httpx.Response:
        body = fixture("orders_page")
        body["data"] = body["data"][:1]
        body["data"][0]["id"] = next(counter)
        cursor = f"c{body['data'][0]['id']}"
        body["pagination"] = {"limit": 10, "hasMore": True, "nextCursor": cursor}
        return json_response(200, body)

    _, manifest, router = capture(
        tmp_path, sig_router({ORDERS: endless}), capture_plan=plan(tmp_path, orders_max_pages=3)
    )
    assert router.count("/orders") == 3
    orders = manifest["collections"]["orders"]
    assert orders["pages_fetched"] == 3 and orders["truncated"] is True
    assert orders["complete"] is False


def test_a_repeated_cursor_stops_pagination_as_an_anomaly(tmp_path: Path) -> None:
    page = fixture("tournament_markets_page1")  # hasMore with cursor-page-2, every time
    result, manifest, router = capture(
        tmp_path,
        sig_router({MARKETS: json_response(200, page)}),
        capture_plan=plan(tmp_path, markets_max_pages=3),
    )
    assert router.count("/tournaments/alpha-tournament/markets") == 2
    assert manifest["collections"]["tournament_markets"]["note"] == (
        "SIG repeated a pagination cursor"
    )
    assert manifest["anomalies"]
    assert result.status == "partial" and result.exit_code == 1


# --- exact bodies and replay ------------------------------------------------------------

EXACT_BOOK = (
    b'{"exchangeId":"36", "marketId":"26","asOf":null,"depth":5,\n'
    b' "bids":[{"price":0.53500000000000000001,"quantity":12.50},'
    b'{"price":1E-3,"quantity":300}],\n'
    b' "asks":[],"bestBid":0.53500000000000000001,"bestAsk":null,"spread":null}'
)


def test_bodies_are_stored_byte_for_byte(tmp_path: Path) -> None:
    router = sig_router(
        {EXCHANGE_BOOK: raw_response(200, EXACT_BOOK, {"content-type": "application/json"})}
    )
    result, manifest, _ = capture(tmp_path, router)
    body = step(manifest, "exchange-orderbook-36")["attempts"][0]["body"]
    stored = (result.run_dir / body["file"]).read_bytes()
    assert stored == EXACT_BOOK
    decoded = json.loads(stored, parse_float=Decimal)
    assert decoded["bids"][0]["price"] == Decimal("0.53500000000000000001")
    assert str(decoded["bids"][0]["quantity"]) == "12.50"
    assert decoded["asOf"] is None and decoded["bestAsk"] is None


def test_recorded_bodies_replay_through_the_client_unchanged(tmp_path: Path) -> None:
    router = sig_router(
        {EXCHANGE_BOOK: raw_response(200, EXACT_BOOK, {"content-type": "application/json"})}
    )
    result, _, _ = capture(tmp_path, router, capture_plan=plan(tmp_path, markets_max_pages=2))
    replay = rf.load_replayable(result.run_dir)
    assert {r.provenance for r in replay} == {"synthetic"}
    assert len(replay) == 7

    served = {(API + r.path, tuple(sorted(r.params.items()))): r for r in replay}

    def from_capture(request: httpx.Request) -> httpx.Response:
        recorded = served[(request.url.path, tuple(sorted(request.url.params.items())))]
        return raw_response(
            recorded.status, recorded.body, {"content-type": recorded.content_type or ""}
        )

    async def read_all(transport: httpx.MockTransport) -> list[Any]:
        clock = FakeClock()
        async with SigRestClient(
            config(),
            httpx.AsyncClient(transport=transport),
            owns_http=True,
            clock=clock,
            sleep=clock.sleep,
        ) as client:
            page1 = await client.list_tournament_markets_page(limit=10, status="any")
            return [
                await client.get_tournament(),
                page1,
                await client.list_tournament_markets_page(
                    limit=10, status="any", cursor=page1.pagination.nextCursor
                ),
                await client.get_exchange_prices(["36"]),
                await client.get_exchange_orderbook("36", depth=5),
                await client.get_market_orderbook("26", depth=5),
                await client.list_orders_page(status="open", limit=10),
            ]

    original = run(read_all(httpx.MockTransport(router)))
    replayed = run(read_all(httpx.MockTransport(from_capture)))
    assert replayed == original
    book = replayed[4]
    assert book.bids[0].price == Decimal("0.53500000000000000001")
    assert book.bestAsk is None and book.asOf is None


def test_replay_refuses_a_modified_body(tmp_path: Path) -> None:
    result, manifest, _ = capture(tmp_path)
    body = result.run_dir / step(manifest, "tournament")["attempts"][0]["body"]["file"]
    body.write_bytes(body.read_bytes().replace(b"847.5", b"847.6"))
    with pytest.raises(ValueError, match="SHA-256"):
        rf.load_replayable(result.run_dir)


# --- exclusions -------------------------------------------------------------------------


def with_hostile_headers(response: httpx.Response) -> httpx.Response:
    response.headers["set-cookie"] = f"session={COOKIE_SENTINEL}"
    response.headers["x-debug-echo"] = f"Bearer {SENTINEL_KEY} {ERROR_TEXT_SENTINEL}"
    response.headers["location"] = f"https://elsewhere.invalid/{COOKIE_SENTINEL}"
    return response


class HostileRouter(Router):
    """Adds cookie, echo and location headers holding sentinels to every response."""

    async def __call__(self, request: httpx.Request) -> httpx.Response:
        return with_hostile_headers(await super().__call__(request))


def test_no_key_cookie_header_or_error_text_reaches_any_file(
    tmp_path: Path, caplog: pytest.LogCaptureFixture, capsys: pytest.CaptureFixture[str]
) -> None:
    caplog.set_level(logging.DEBUG)
    error = {"error": {"code": "NOT_FOUND", "message": f"{ERROR_TEXT_SENTINEL} {SENTINEL_KEY}"}}
    router = HostileRouter(
        {
            PRICES: prices_route,
            MARKETS: markets_route,
            EXCHANGE_BOOK: raw_response(
                200, f"<html>{ERROR_TEXT_SENTINEL}</html>".encode(), {"content-type": "text/html"}
            ),
            MARKET_BOOK: json_response(404, error),
            ORDERS: json_response(
                400, {"error": {"code": ERROR_TEXT_SENTINEL, "message": ERROR_TEXT_SENTINEL}}
            ),
        }
    )
    result, manifest, _ = capture(tmp_path, router)
    written = all_bytes(result.run_dir)
    printed = capsys.readouterr()
    for secret in (SENTINEL_KEY, COOKIE_SENTINEL, ERROR_TEXT_SENTINEL, "Bearer", "elsewhere"):
        assert secret.encode() not in written, secret
        assert secret not in caplog.text
        assert secret not in printed.out + printed.err
    attempts = [a for s in manifest["steps"] for a in s["attempts"]]
    assert {key for a in attempts for key in a["headers"]} <= {"content-type", "retry-after"}
    assert all(set(a) == set(attempts[0]) for a in attempts)  # no request or header dumps
    assert "headers" in attempts[0] and "request" not in attempts[0]

    html = step(manifest, "exchange-orderbook-36")
    assert html["outcome"] == "validation_failed"
    assert html["attempts"][0]["body"]["recorded"] is False
    assert html["attempts"][0]["body"]["omitted"].startswith("not valid JSON")
    assert html["attempts"][0]["headers"] == {"content-type": "text/html"}

    missing = step(manifest, "market-orderbook-26")
    assert missing["outcome"] == "http_error"
    assert missing["attempts"][0]["error_body"] == {
        "shape": "error envelope",
        "code": "NOT_FOUND",
        "unrecognized_code": False,
    }
    assert missing["attempts"][0]["body"] is None
    orders = step(manifest, "orders-p1")
    assert orders["error"]["http"]["code"] is None
    assert orders["error"]["http"]["unrecognized_code"] is True
    assert result.status == "partial"


def test_unlisted_query_parameters_and_odd_headers_are_dropped() -> None:
    url = httpx.URL(
        "https://sig.invalid/api/v1/orders?status=open&apiKey=DUMMY&limit=10"
        "&status=closed&cursor=ok-cursor&ids=1%0A2"
    )
    assert rf._safe_params(url) == ({"status": "open", "limit": "10", "cursor": "ok-cursor"}, 3)
    headers = httpx.Headers(
        {"Content-Type": "Application/JSON; charset=utf-8", "Retry-After": "Wed, 21 Oct"}
    )
    assert rf._safe_headers(headers) == {
        "content-type": "application/json",
        "retry-after": "<omitted>",
    }


# --- the request guard ------------------------------------------------------------------


def observer_for(tmp_path: Path) -> rf._Observer:
    guard = rf._CredentialGuard(ApiKey(SENTINEL_KEY))
    return rf._Observer(config(), rf.planned_reads(plan(tmp_path)), fixed_now, guard)


@pytest.mark.parametrize(
    ("method", "url"),
    [
        ("POST", f"{BASE_URL}/orders?tournamentId={TID}"),
        ("DELETE", f"{BASE_URL}/orders/1?tournamentId={TID}"),
        ("POST", f"{BASE_URL}/realtime/token"),
        ("GET", f"{BASE_URL}/positions?tournamentId={TID}"),
        ("GET", f"{BASE_URL}/orders"),
        ("GET", f"{BASE_URL}/orders?tournamentId=11111111-1111-1111-1111-111111111111"),
        ("GET", f"{BASE_URL}/orders?tournamentId={TID}&tournamentId={TID}"),
        ("GET", f"{BASE_URL}/exchanges/99/orderbook?tournamentId={TID}"),
        ("GET", f"{BASE_URL}/tournaments/other-tournament"),
        ("GET", f"https://www.thesuper.market/api/v1/orders?tournamentId={TID}"),
        ("GET", f"https://sig.invalid/other/orders?tournamentId={TID}"),
    ],
)
def test_request_guard_refuses_anything_outside_the_plan(
    method: str, url: str, tmp_path: Path
) -> None:
    observer = observer_for(tmp_path)
    observer.current = rf._Step(1, "x", "r", "/p", {}, "t")
    with pytest.raises(rf.RecorderSafetyError):
        run(observer.on_request(httpx.Request(method, url)))
    assert observer.current.attempts == []
    assert observer.refused == 1


def test_request_guard_refuses_requests_outside_a_step(tmp_path: Path) -> None:
    observer = observer_for(tmp_path)
    with pytest.raises(rf.RecorderSafetyError):
        run(observer.on_request(httpx.Request("GET", f"{BASE_URL}/orders?tournamentId={TID}")))


# --- validation failures and partial runs -----------------------------------------------


def test_validation_failure_is_flagged_and_kept_only_as_local_evidence(tmp_path: Path) -> None:
    book = fixture("exchange_orderbook")
    book["bids"].reverse()  # ascending bids: the client rejects them
    router = sig_router({EXCHANGE_BOOK: json_response(200, book)})
    result, manifest, router = capture(tmp_path, router)
    failed = step(manifest, "exchange-orderbook-36")
    assert failed["outcome"] == "validation_failed"
    assert failed["client_validation_passed"] is False
    assert failed["error"] == {
        "class": "SigResponseError",
        "reason": "bids are not sorted descending",
    }
    assert failed["attempts"][0]["body"]["recorded"] is True  # evidence, kept locally
    assert router.count("/orders") == 1  # the run carried on
    assert result.status == "partial" and result.exit_code == 1
    assert "exchange-orderbook-36" not in {r.step for r in rf.load_replayable(result.run_dir)}


def test_schema_mismatch_reason_names_fields_not_values(tmp_path: Path) -> None:
    book = fixture("exchange_orderbook")
    book["bestBid"] = "DUMMY_VALUE_SENTINEL"
    _, manifest, _ = capture(tmp_path, sig_router({EXCHANGE_BOOK: json_response(200, book)}))
    failed = step(manifest, "exchange-orderbook-36")
    assert failed["outcome"] == "validation_failed"
    assert "bestBid" in failed["error"]["reason"]
    assert "DUMMY_VALUE_SENTINEL" not in json.dumps(manifest)


def test_auth_failure_aborts_with_an_accurate_partial_manifest(tmp_path: Path) -> None:
    router = sig_router({PRICES: json_response(401, fixture("error_envelopes")["INVALID_API_KEY"])})
    result, manifest, _ = capture(tmp_path, router)
    assert result.status == "aborted" and result.exit_code == 1
    assert manifest["run"]["status"] == "aborted"
    assert "HTTP 401" in manifest["run"]["stop_reason"]
    prices = step(manifest, "exchange-prices")
    assert prices["outcome"] == "http_error"
    assert prices["error"]["http"]["code"] == "INVALID_API_KEY"
    assert manifest["not_run"] == ["exchange-orderbook-36", "market-orderbook-26", "orders"]
    assert manifest["run"]["finished_at"] is not None


def test_a_page_that_stops_the_run_leaves_the_collection_incomplete(tmp_path: Path) -> None:
    denied = json_response(403, fixture("error_envelopes")["INSUFFICIENT_SCOPES"])
    result, manifest, _ = capture(tmp_path, sig_router({MARKETS: denied}))
    markets = manifest["collections"]["tournament_markets"]
    assert markets["complete"] is False and markets["truncated"] is False
    assert markets["note"] == "page 1 failed and the run stopped: incomplete"
    assert result.status == "aborted"


def test_a_selected_market_that_is_not_found_does_not_stop_the_run(tmp_path: Path) -> None:
    router = sig_router({MARKET_BOOK: json_response(404, fixture("error_envelopes")["NOT_FOUND"])})
    result, manifest, _ = capture(tmp_path, router)
    assert step(manifest, "market-orderbook-26")["outcome"] == "http_error"
    assert step(manifest, "orders-p1")["outcome"] == "passed"
    assert result.status == "partial"


def test_tournament_mismatch_stops_before_any_scoped_read(tmp_path: Path) -> None:
    other = fixture("tournament")
    other["slug"] = "beta-tournament"
    result, manifest, router = capture(
        tmp_path, sig_router({TOURNAMENT_PATH: json_response(200, other)})
    )
    assert router.paths() == ["/tournaments/alpha-tournament"]
    assert step(manifest, "tournament")["outcome"] == "context_mismatch"
    assert result.status == "aborted"
    assert len(manifest["not_run"]) == 5


def test_transport_failures_are_recorded_and_stop_the_run(tmp_path: Path) -> None:
    router = sig_router({PRICES: httpx.ConnectError("DUMMY_TRANSPORT_TEXT")})
    result, manifest, _ = capture(tmp_path, router)
    prices = step(manifest, "exchange-prices")
    assert prices["outcome"] == "retry_exhausted"
    assert prices["error"]["transport_error"] == "ConnectError"
    assert [a["outcome"] for a in prices["attempts"]] == ["no_response"] * 4
    assert b"DUMMY_TRANSPORT_TEXT" not in all_bytes(result.run_dir)
    assert result.status == "aborted"


def test_a_body_that_cannot_be_read_is_not_reported_as_a_response(tmp_path: Path) -> None:
    class BrokenStream(httpx.AsyncByteStream):
        async def __aiter__(self):  # type: ignore[override]
            raise httpx.ReadError("DUMMY_STREAM_TEXT")
            yield b""

    def broken(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, stream=BrokenStream())

    result, manifest, _ = capture(tmp_path, sig_router({PRICES: broken}))
    prices = step(manifest, "exchange-prices")
    assert [a["outcome"] for a in prices["attempts"]] == ["body_not_read"] * 4
    assert prices["attempts"][0]["http_status"] == 200
    assert prices["error"]["transport_error"] == "ReadError"
    assert result.status == "aborted"


# --- rate limiting ----------------------------------------------------------------------


def test_rate_limit_stops_recording(tmp_path: Path) -> None:
    limited = json_response(429, fixture("error_envelopes")["RATE_LIMITED"], {"Retry-After": "60"})
    result, manifest, router = capture(tmp_path, sig_router({PRICES: limited}))
    prices = step(manifest, "exchange-prices")
    assert prices["outcome"] == "rate_limited"
    assert prices["attempts"][0]["headers"]["retry-after"] == "60"
    assert router.count("/exchanges/prices") == 1
    assert result.status == "aborted"
    assert manifest["run"]["stop_reason"] == rf.RATE_LIMITED_REASON


def test_a_blocked_limiter_stops_recording_and_is_never_cleared(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def never(self: RateLimiter, kind: RequestKind) -> None:
        raise AssertionError("the recorder cleared a limiter block")

    monkeypatch.setattr(RateLimiter, "clear_block", never)
    clock = FakeClock()
    limiter = RateLimiter(clock=clock, sleep=clock.sleep)
    huge = json_response(
        429, fixture("error_envelopes")["RATE_LIMITED"], {"Retry-After": "99999999999"}
    )
    result, manifest, router = capture(
        tmp_path, sig_router({PRICES: huge}), clock=clock, limiter=limiter
    )
    assert limiter.is_blocked(RequestKind.READ)
    assert step(manifest, "exchange-prices")["outcome"] == "blocked"
    assert manifest["run"]["stop_reason"] == rf.BLOCKED_REASON
    assert router.paths()[-1] == "/exchanges/prices"
    assert result.status == "aborted" and result.exit_code == 1


def test_an_already_blocked_limiter_sends_nothing(tmp_path: Path) -> None:
    clock = FakeClock()
    limiter = RateLimiter(clock=clock, sleep=clock.sleep)
    limiter.block(RequestKind.READ)
    result, manifest, router = capture(tmp_path, clock=clock, limiter=limiter)
    assert router.requests == []
    assert manifest["steps"] == []
    assert result.status == "aborted"
    assert manifest["run"]["stop_reason"] == rf.BLOCKED_REASON
    assert limiter.is_blocked(RequestKind.READ)


# --- output directory, interruption, write failures -------------------------------------


def test_an_existing_run_directory_is_never_reused(tmp_path: Path) -> None:
    existing = tmp_path / "captures" / "run-1"
    existing.mkdir(parents=True)
    (existing / "manifest.json").write_text("previous", encoding="utf-8")
    router = sig_router()
    with pytest.raises(rf.RecorderOutputError, match="refusing to reuse"):
        capture(tmp_path, router)
    assert router.requests == []
    assert (existing / "manifest.json").read_text(encoding="utf-8") == "previous"
    assert sorted(p.name for p in existing.iterdir()) == ["manifest.json"]


def test_default_run_ids_are_unique(tmp_path: Path) -> None:
    first = rf._new_run_id(fixed_now)
    second = rf._new_run_id(fixed_now)
    assert first != second and first.startswith("20261009T120000Z-")


@pytest.mark.parametrize("run_id", ["../escape", "", "a/b", ".hidden", "x" * 65])
def test_unsafe_run_ids_are_rejected(run_id: str, tmp_path: Path) -> None:
    with pytest.raises(rf.RecorderArgumentError):
        capture(tmp_path, run_id=run_id)
    assert not (tmp_path / "captures").exists()


def test_plan_and_config_must_name_the_same_tournament(tmp_path: Path) -> None:
    other = TournamentRef(slug="beta", id="11111111-1111-1111-1111-111111111111")
    with pytest.raises(rf.RecorderArgumentError):
        capture(tmp_path, capture_plan=plan(tmp_path, tournament=other))


def test_cancellation_leaves_an_interrupted_manifest(tmp_path: Path) -> None:
    async def scenario() -> tuple[Router, Path]:
        started = asyncio.Event()

        async def hang(request: httpx.Request) -> httpx.Response:
            started.set()
            await asyncio.Event().wait()
            raise AssertionError("unreachable")

        router = sig_router({MARKET_BOOK: hang})
        clock = FakeClock()
        task = asyncio.create_task(
            rf.record(
                plan(tmp_path),
                config(),
                run_id="cancelled",
                transport=httpx.MockTransport(router),
                clock=clock,
                sleep=clock.sleep,
                now=fixed_now,
                build=BUILD,
            )
        )
        await started.wait()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        return router, tmp_path / "captures" / "cancelled"

    router, run_dir = asyncio.run(scenario())
    manifest = read_manifest(run_dir)
    assert manifest["run"]["status"] == "interrupted"
    assert step(manifest, "market-orderbook-26")["outcome"] == "interrupted"
    assert step(manifest, "exchange-orderbook-36")["outcome"] == "passed"
    assert manifest["not_run"] == ["orders"]
    assert "/orders" not in router.paths()


def test_a_body_write_failure_cleans_up_and_marks_the_run_failed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    real_replace = os.replace

    def failing_replace(src: Any, dst: Any) -> None:
        if Path(dst).name.startswith("04-exchange-orderbook-36"):
            raise OSError("DUMMY_DISK_TEXT")
        real_replace(src, dst)

    monkeypatch.setattr(rf.os, "replace", failing_replace)
    result, manifest, router = capture(tmp_path)
    assert result.status == "error" and result.exit_code == 1
    assert manifest["run"]["stop_reason"] == "recorder error: OSError"
    failed = step(manifest, "exchange-orderbook-36")
    assert failed["outcome"] == "recorder_error"
    assert failed["attempts"][0]["body"]["recorded"] is False
    assert "/markets/26/orderbook" not in router.paths()
    assert not list(result.run_dir.rglob("*.tmp"))
    assert b"DUMMY_DISK_TEXT" not in all_bytes(result.run_dir)
    assert "exchange-orderbook-36" not in {r.step for r in rf.load_replayable(result.run_dir)}


def test_describe_build_identifies_code_and_spec() -> None:
    build = rf.describe_build()
    assert build["code"]["recorder_version"] == rf.RECORDER_VERSION
    assert build["spec"]["path"] == "docs/api/openapi.json"
    assert build["spec"]["version"] == "1.0.0"
    assert len(build["spec"]["sha256"]) == 64
