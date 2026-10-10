"""Replay capture 20261009T122244Z-fa008a through SigRestClient (S1-T4-B).

Five reads (no orders): tournament, markets page 1 of 10, prices, exchange and market
orderbooks. Two bodies are redacted (see fixtures.json and tests/fixtures/sig/README.md);
the other three are byte-identical to what SIG sent. Integrity and provenance checks
for every capture are in test_sig_recorded_integrity.py.
"""

from __future__ import annotations

from typing import Any

from recorded_kit import (
    CREATOR,
    RECORDED,
    TOURNAMENT,
    decimals,
    effective_manifest,
    levels,
    make_replay_client,
    raw_body,
    replay,
    source_provenance,
)
from sig_testkit import run

from engine.sig_client.wire import TournamentMarket

RUN = RECORDED / "20261009T122244Z-fa008a"


def test_capture_provenance() -> None:
    _, source = source_provenance(RUN)
    assert source["code"]["git_commit"] == "a42cfb46afaa9a78debef1e6470c2dec17148355"
    assert source["selection"]["include_orders"] is False
    assert source["collections"]["tournament_markets"]["truncated"] is True
    assert "orders" not in source["collections"]


def test_tournament_replay_verifies_the_real_slug_and_uuid() -> None:
    tournament, served = replay(RUN, "verify_tournament")
    assert served.paths() == ["/tournaments/midterm-elections"]
    assert (tournament.id, tournament.slug) == (TOURNAMENT.id, TOURNAMENT.slug)
    assert tournament.status == "active"
    assert tournament.startDate == "2026-10-01T16:00:00.000Z"
    assert tournament.endDate == "2026-11-04T17:00:00.000Z"
    assert decimals([tournament.initialBalance]) == ["100000"]
    assert decimals([tournament.myBalance]) == ["12345.67"]  # redacted test value
    assert (tournament.memberCount, tournament.marketCount) == (4352, 237)
    assert tournament.isPendingEnrolment is True  # redacted test value


def test_markets_first_page_replay() -> None:
    page, served = replay(RUN, "list_tournament_markets_page", limit=10, status="any")
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
    assert {(m.creator.id, m.creator.username) for m in page.data if m.creator} == {CREATOR}
    # First-page pagination only: the cursor was not followed in this capture.
    assert page.pagination.total == 237
    assert page.pagination.limit == 10
    assert page.pagination.hasMore is True
    assert page.pagination.nextCursor == (
        "eyJ0eXBlIjoidGltZSIsImNyZWF0ZWRBdCI6IjIwMjYtMDktMTBUMDI6NTc6NTkuNjMzMDAwWiIsImlkIjoiMzc5In0"
    )


def test_thumbnail_url_is_an_observed_extra_field_the_model_ignores() -> None:
    raw = raw_body(RUN, "tournament-markets-p1")
    assert all("thumbnailUrl" in market for market in raw["data"])
    assert "thumbnailUrl" not in TournamentMarket.model_fields
    page, _ = replay(RUN, "list_tournament_markets_page", limit=10, status="any")
    assert all(not hasattr(m, "thumbnailUrl") and not m.model_extra for m in page.data)


def test_exchange_prices_replay_maps_947_to_258() -> None:
    prices, served = replay(RUN, "get_exchange_prices", ["947"])
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


def test_exchange_orderbook_replay() -> None:
    book, served = replay(RUN, "get_exchange_orderbook", "947", depth=5)
    assert dict(served.requests[-1].url.params) == {"depth": "5", "tournamentId": TOURNAMENT.id}
    assert (book.exchangeId, book.marketId, book.depth) == ("947", "258", 5)
    assert book.asOf is not None
    assert book.asOf.sequence == 109392279
    assert book.asOf.at == "2026-10-09T12:22:56.4607814+00:00"  # 7 digits, +00:00: kept as sent
    assert levels(book.bids) == BOOK_BIDS
    assert levels(book.asks) == BOOK_ASKS
    assert decimals([book.bestBid, book.bestAsk, book.spread]) == ["0.945", "0.95", "0.005"]


def test_market_orderbook_replay_is_scoped_to_the_tournament() -> None:
    book, served = replay(RUN, "get_market_orderbook", "258", depth=5)
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
    client, served = make_replay_client(RUN)

    async def scenario() -> tuple[Any, Any, Any]:
        async with client:
            await client.list_tournament_markets_page(limit=10, status="any")
            prices = await client.get_exchange_prices(["947"])
            book = await client.get_exchange_orderbook("947", depth=5)
            market_book = await client.get_market_orderbook("258", depth=5)
            return prices, book, market_book

    prices, book, market_book = run(scenario())
    assert served.unexpected == []
    assert served.sent() == [
        (f["request"]["path"], f["request"]["params"]) for f in effective_manifest(RUN)["fixtures"]
    ]
    # 947 -> 258 holds across all three responses that can show it.
    assert prices.data[0].marketId == book.marketId == "258"
    assert [e.exchangeId for e in market_book.exchanges] == ["947"]
