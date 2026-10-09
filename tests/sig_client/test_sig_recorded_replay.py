"""Replay fixtures derived from a recorded SIG capture through SigRestClient (S1-T4-B).

The fixtures in tests/fixtures/sig/recorded/<run>/ come from one genuine, read-only SIG
capture. Two bodies are redacted (see fixtures.json and tests/fixtures/sig/README.md);
the other three are byte-identical to what SIG sent. Every test is offline: bodies are
served through httpx.MockTransport, and conftest.py blocks real network use.
"""

from __future__ import annotations

import hashlib
import json
from decimal import Decimal
from typing import Any

import httpx
from sig_testkit import API, BASE_URL, FIXTURES, SENTINEL_KEY, FakeClock, run

from engine.config import ApiKey, SigConfig, TournamentRef
from engine.sig_client.rate_limiter import RateLimiter
from engine.sig_client.rest import SigRestClient
from engine.sig_client.wire import TournamentMarket

RUN = FIXTURES / "recorded" / "20261009T122244Z-fa008a"
TOURNAMENT = TournamentRef(slug="midterm-elections", id="bda92870-621e-47b0-bc3c-3602c5c26f55")
REDACTED = "derived from recorded response, redacted"
UNMODIFIED = "recorded response, unmodified"


def effective_manifest() -> dict[str, Any]:
    return json.loads((RUN / "fixtures.json").read_bytes())


def fixtures_by_name() -> dict[str, dict[str, Any]]:
    return {f["name"]: f for f in effective_manifest()["fixtures"]}


def sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


class Replay:
    """Serves each fixture only for its recorded path and exact recorded query."""

    def __init__(self) -> None:
        self.requests: list[httpx.Request] = []
        self.unexpected: list[str] = []
        self.routes: dict[str, tuple[dict[str, str], dict[str, Any], bytes]] = {}
        for f in effective_manifest()["fixtures"]:
            body = (RUN / f["file"]).read_bytes()
            self.routes[API + f["request"]["path"]] = (f["request"]["params"], f, body)

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        route = self.routes.get(request.url.path)
        params = dict(request.url.params)
        if request.method != "GET" or route is None or params != route[0]:
            self.unexpected.append(f"{request.method} {request.url.path}")
            return httpx.Response(418, content=b"{}")
        _, f, body = route
        return httpx.Response(
            f["response"]["http_status"],
            content=body,
            headers={"content-type": f["response"]["content_type"]},
        )

    def paths(self) -> list[str]:
        return [r.url.path.removeprefix(API) for r in self.requests]


def make_replay_client() -> tuple[SigRestClient, Replay]:
    replay = Replay()
    clock = FakeClock()
    config = SigConfig(api_key=ApiKey(SENTINEL_KEY), tournament=TOURNAMENT, base_url=BASE_URL)
    client = SigRestClient(
        config,
        httpx.AsyncClient(transport=httpx.MockTransport(replay)),
        owns_http=True,
        clock=clock,
        sleep=clock.sleep,
        limiter=RateLimiter(clock=clock, sleep=clock.sleep),
    )
    return client, replay


def replay(call: str, *args: Any, **kwargs: Any) -> tuple[Any, Replay]:
    client, served = make_replay_client()

    async def scenario() -> Any:
        async with client:
            return await getattr(client, call)(*args, **kwargs)

    result = run(scenario())
    assert served.unexpected == []
    return result, served


def decimals(values: list[Decimal]) -> list[str]:
    assert all(type(v) is Decimal for v in values)
    return [str(v) for v in values]


# --- fixture integrity and provenance --------------------------------------------------


def test_fixture_bytes_match_the_effective_manifest() -> None:
    manifest = effective_manifest()
    assert manifest["format"] == "bellwether.sig-fixture/1"
    files = sorted(p.relative_to(RUN).as_posix() for p in (RUN / "bodies").iterdir())
    assert files == sorted(f["file"] for f in manifest["fixtures"])
    for f in manifest["fixtures"]:
        data = (RUN / f["file"]).read_bytes()
        assert sha256(data) == f["sha256"], f["name"]
        assert len(data) == f["bytes"], f["name"]


def test_source_capture_manifest_is_provenance_only() -> None:
    manifest = effective_manifest()
    source_bytes = (RUN / manifest["source_capture"]["manifest_file"]).read_bytes()
    assert sha256(source_bytes) == manifest["source_capture"]["manifest_sha256"]
    source = json.loads(source_bytes)
    assert (
        source["provenance"]["kind"] == "recorded" == manifest["source_capture"]["provenance_kind"]
    )
    assert source["run"]["id"] == manifest["source_capture"]["run_id"]
    assert source["run"]["status"] == "succeeded"
    assert source["code"]["git_dirty"] is False
    assert source["credential_detected"] is False and source["withheld_fields"] == 0
    assert source["target"]["tournament"] == {"slug": TOURNAMENT.slug, "id": TOURNAMENT.id}
    steps = {s["index"]: s for s in source["steps"]}
    for f in manifest["fixtures"]:
        step = steps[f["source"]["capture_step"]]
        (attempt,) = step["attempts"]
        # Each fixture names the recorded body it came from...
        assert step["name"] == f["name"] and step["outcome"] == "passed"
        assert attempt["body"]["file"] == f["source"]["body_file"]
        assert attempt["body"]["sha256"] == f["source"]["sha256"]
        assert attempt["path"] == f["request"]["path"]
        assert attempt["params"] == f["request"]["params"]
        # ...but only unmodified fixtures may share that body's hash.
        if f["label"] == UNMODIFIED:
            assert f["redactions"] == []
            assert f["sha256"] == f["source"]["sha256"]
        else:
            assert f["label"] == REDACTED
            assert f["redactions"]
            assert f["sha256"] != f["source"]["sha256"]


def test_redacted_paths_hold_their_documented_placeholders() -> None:
    labels = {name: f["label"] for name, f in fixtures_by_name().items()}
    assert labels == {
        "tournament": REDACTED,
        "tournament-markets-p1": REDACTED,
        "exchange-prices": UNMODIFIED,
        "exchange-orderbook-947": UNMODIFIED,
        "market-orderbook-258": UNMODIFIED,
    }
    for f in fixtures_by_name().values():
        body = json.loads((RUN / f["file"]).read_bytes(), parse_float=Decimal)
        for redaction in f["redactions"]:
            node: Any = body
            for part in redaction["path"].removeprefix("$.").replace("[", ".").split("."):
                node = node[int(part[:-1])] if part.endswith("]") else node[part]
            assert node == json.loads(redaction["replacement_json"], parse_float=Decimal)
    markets = fixtures_by_name()["tournament-markets-p1"]["redactions"]
    assert sorted(r["path"] for r in markets) == sorted(
        f"$.data[{i}].creator.{field}" for i in range(10) for field in ("id", "username")
    )


# --- endpoint replays -------------------------------------------------------------------


def test_tournament_replay_verifies_the_real_slug_and_uuid() -> None:
    tournament, served = replay("verify_tournament")
    assert served.paths() == ["/tournaments/midterm-elections"]
    assert (tournament.id, tournament.slug) == (TOURNAMENT.id, TOURNAMENT.slug)
    assert tournament.status == "active"
    assert tournament.startDate == "2026-10-01T16:00:00.000Z"
    assert tournament.endDate == "2026-11-04T17:00:00.000Z"
    assert decimals([tournament.initialBalance]) == ["100000"]
    assert decimals([tournament.myBalance]) == ["12345.67"]  # redacted test value
    assert (tournament.memberCount, tournament.marketCount) == (4352, 237)
    assert tournament.isPendingEnrolment is False


def test_markets_first_page_replay() -> None:
    page, served = replay("list_tournament_markets_page", limit=10, status="any")
    assert served.paths() == [
        "/tournaments/midterm-elections",
        "/tournaments/midterm-elections/markets",
    ]
    assert [m.id for m in page.data] == [str(i) for i in range(388, 378, -1)]
    assert [m.exchanges[0].id for m in page.data] == [str(i) for i in range(1077, 1067, -1)]
    assert decimals([m.exchanges[0].latestPrice for m in page.data]) == [
        "0.16",
        "0.85",
        "0.105",
        "0.09",
        "0.915",
        "0.105",
        "0.16",
        "0.865",
        "0.055",
        "0.94",
    ]
    assert {str(m.exchanges[0].initialPrice) for m in page.data} == {"0.5"}
    first = page.data[0]
    assert first.title == "Will the Republican Party win the Rhode Island Senate?"
    assert first.status == "open"
    assert first.createdAt == "2026-09-18T22:32:17.996Z"
    assert first.settlementDate == "2026-11-04T17:00:00.000Z"
    assert (first.settledWith, first.settledOn) == (None, None)
    assert first.categories == ["Election Outcome"]
    assert (first.isComposite, first.isMultiOutcome) == (False, False)
    assert first.exchanges[0].option == "YES"
    assert {(m.creator.id, m.creator.username) for m in page.data if m.creator} == {
        ("11111111-1111-4111-8111-111111111111", "redacted-creator-1")
    }
    # First-page pagination only: the cursor was not followed in the capture.
    assert page.pagination.total == 237
    assert page.pagination.limit == 10
    assert page.pagination.hasMore is True
    assert page.pagination.nextCursor == (
        "eyJ0eXBlIjoidGltZSIsImNyZWF0ZWRBdCI6IjIwMjYtMDktMTBUMDI6NTc6NTkuNjMzMDAwWiIsImlkIjoiMzc5In0"
    )


def test_thumbnail_url_is_an_observed_extra_field_the_model_ignores() -> None:
    raw = json.loads((RUN / fixtures_by_name()["tournament-markets-p1"]["file"]).read_bytes())
    assert all("thumbnailUrl" in market for market in raw["data"])
    assert "thumbnailUrl" not in TournamentMarket.model_fields
    page, _ = replay("list_tournament_markets_page", limit=10, status="any")
    assert all(not hasattr(m, "thumbnailUrl") and not m.model_extra for m in page.data)
    observed = effective_manifest()["observed_extra_fields"]
    assert [(o["fixture"], o["path"]) for o in observed] == [
        ("tournament-markets-p1", "$.data[*].thumbnailUrl")
    ]


def test_exchange_prices_replay_maps_947_to_258() -> None:
    prices, served = replay("get_exchange_prices", ["947"])
    assert dict(served.requests[-1].url.params) == {"ids": "947", "tournamentId": TOURNAMENT.id}
    (price,) = prices.data
    assert (price.exchangeId, price.marketId, price.option) == ("947", "258", "YES")
    assert decimals([price.latestPrice, price.bestBid, price.bestAsk, price.spread]) == [
        "0.95",
        "0.945",
        "0.95",
        "0.005",
    ]
    assert prices.missingIds == []


BOOK_BIDS = [
    ("0.945", "3"),
    ("0.94", "17487"),
    ("0.935", "12848"),
    ("0.93", "21309"),
    ("0.925", "35757"),
]
BOOK_ASKS = [
    ("0.95", "8159"),
    ("0.955", "3861"),
    ("0.96", "250282"),
    ("0.965", "149206"),
    ("0.97", "671809"),
]


def levels(side: list[Any]) -> list[tuple[str, str]]:
    return list(
        zip(
            decimals([lv.price for lv in side]), decimals([lv.quantity for lv in side]), strict=True
        )
    )


def test_exchange_orderbook_replay() -> None:
    book, served = replay("get_exchange_orderbook", "947", depth=5)
    assert dict(served.requests[-1].url.params) == {"depth": "5", "tournamentId": TOURNAMENT.id}
    assert (book.exchangeId, book.marketId, book.depth) == ("947", "258", 5)
    assert book.asOf is not None
    assert book.asOf.sequence == 109392279
    assert book.asOf.at == "2026-10-09T12:22:56.4607814+00:00"  # 7 digits, +00:00: kept as sent
    assert levels(book.bids) == BOOK_BIDS
    assert levels(book.asks) == BOOK_ASKS
    assert decimals([book.bestBid, book.bestAsk, book.spread]) == ["0.945", "0.95", "0.005"]


def test_market_orderbook_replay_is_scoped_to_the_tournament() -> None:
    book, served = replay("get_market_orderbook", "258", depth=5)
    assert dict(served.requests[-1].url.params) == {"depth": "5", "tournamentId": TOURNAMENT.id}
    (context,) = book.contexts
    assert context.type == "tournament" and context.tournament is not None
    assert (context.tournament.id, context.tournament.slug) == (TOURNAMENT.id, TOURNAMENT.slug)
    assert context.tournament.name == "Midterm Elections"
    assert context.tournament.currencyName == "SUSQies"
    assert context.tournament.isOngoingPlay is False
    for combined in (book, book.tournament_orderbook()):
        (exchange,) = combined.exchanges
        assert (exchange.exchangeId, exchange.option) == ("947", "YES")
        assert exchange.asOf is not None
        assert exchange.asOf.sequence == 109392681
        assert exchange.asOf.at == "2026-10-09T12:23:00.4798929+00:00"
        assert decimals([exchange.impliedProbability, combined.overround]) == ["0.95", "0.95"]
        assert combined.hasArbitrageOpportunity is False
        assert levels(exchange.bids) == BOOK_BIDS
        assert levels(exchange.asks) == BOOK_ASKS


def test_all_recorded_reads_in_one_session_send_the_recorded_requests() -> None:
    client, served = make_replay_client()

    async def scenario() -> tuple[Any, Any, Any]:
        async with client:
            await client.list_tournament_markets_page(limit=10, status="any")
            prices = await client.get_exchange_prices(["947"])
            book = await client.get_exchange_orderbook("947", depth=5)
            market_book = await client.get_market_orderbook("258", depth=5)
            return prices, book, market_book

    prices, book, market_book = run(scenario())
    assert served.unexpected == []
    assert [(r.url.path.removeprefix(API), dict(r.url.params)) for r in served.requests] == [
        (f["request"]["path"], f["request"]["params"]) for f in effective_manifest()["fixtures"]
    ]
    # 947 -> 258 holds across all three responses that can show it.
    assert prices.data[0].marketId == book.marketId == "258"
    assert [e.exchangeId for e in market_book.exchanges] == ["947"]


def test_fixture_directory_disables_line_ending_conversion() -> None:
    # Hashes cover exact bytes, so a CRLF checkout must not rewrite them.
    assert (RUN / ".gitattributes").read_bytes().splitlines()[-1] == b"* -text"
    for path in RUN.rglob("*.json"):
        assert b"\r" not in path.read_bytes(), path.name
