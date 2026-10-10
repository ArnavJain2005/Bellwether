"""Replay capture 20261009T190027Z-fb88c9 through SigRestClient (S1-T4-C).

Eight reads: tournament, all three markets pages (100 + 100 + 37, cursor chain ending
with hasMore false), prices, exchange and market orderbooks, and one orders page.
Four bodies are redacted (see fixtures.json and tests/fixtures/sig/README.md); the other
four, including the orders page, are byte-identical to what SIG sent.

The orders page is empty. It covers the /orders envelope only: no order item and no
orders cursor-following were observed, so neither is tested here.
"""

from __future__ import annotations

from typing import Any

import pytest
from recorded_kit import (
    CREATOR,
    RECORDED,
    TOURNAMENT,
    decimals,
    effective_manifest,
    levels,
    make_replay_client,
    replay,
    source_provenance,
)
from sig_testkit import run

from engine.sig_client.errors import SigPaginationError

RUN = RECORDED / "20261009T190027Z-fb88c9"
MARKETS = "/tournaments/midterm-elections/markets"
PAGE2_CURSOR = (
    "eyJ0eXBlIjoidGltZSIsImNyZWF0ZWRBdCI6IjIwMjYtMDgtMjRUMjA6NDI6MDkuMTU2MDAwWiIsImlkIjoiMjg4In0"
)
PAGE3_CURSOR = (
    "eyJ0eXBlIjoidGltZSIsImNyZWF0ZWRBdCI6IjIwMjYtMDgtMjRUMjA6NDA6MDguMTgyMDAwWiIsImlkIjoiMTg4In0"
)


def test_capture_provenance() -> None:
    _, source = source_provenance(RUN)
    assert source["code"]["git_commit"] == "36aebfe71c18e9985ef847ec12f505a96c989b61"
    assert source["selection"]["include_orders"] is True
    assert source["selection"]["orders_status"] == "all"
    markets = source["collections"]["tournament_markets"]
    assert (markets["pages_fetched"], markets["complete"], markets["truncated"]) == (3, True, False)
    orders = source["collections"]["orders"]
    assert (orders["pages_fetched"], orders["complete"], orders["truncated"]) == (1, True, False)


def test_tournament_replay() -> None:
    tournament, served = replay(RUN, "verify_tournament")
    assert served.paths() == ["/tournaments/midterm-elections"]
    assert (tournament.id, tournament.slug) == (TOURNAMENT.id, TOURNAMENT.slug)
    assert decimals([tournament.initialBalance]) == ["100000"]
    assert decimals([tournament.myBalance]) == ["12345.67"]  # redacted test value
    assert tournament.isPendingEnrolment is True  # redacted test value
    assert (tournament.memberCount, tournament.marketCount) == (4376, 237)


def test_list_all_markets_follows_the_recorded_cursor_chain() -> None:
    markets, served = replay(RUN, "list_all_tournament_markets", page_limit=100, max_pages=3)
    assert served.sent() == [
        ("/tournaments/midterm-elections", {}),
        (MARKETS, {"limit": "100", "status": "any"}),
        (MARKETS, {"limit": "100", "status": "any", "cursor": PAGE2_CURSOR}),
        (MARKETS, {"limit": "100", "status": "any", "cursor": PAGE3_CURSOR}),
    ]
    ids = [m.id for m in markets]
    assert len(ids) == len(set(ids)) == 237
    assert (ids[0], ids[99], ids[100], ids[199], ids[200], ids[-1]) == (
        "388",
        "288",
        "287",
        "188",
        "187",
        "151",
    )
    by_id = {m.id: m for m in markets}
    georgia = by_id["258"]
    assert georgia.title == "Will the Democratic Party win the Georgia Senate?"
    assert [(e.id, e.option) for e in georgia.exchanges] == [("947", "YES")]
    independents = sorted((m.id, m.exchanges[0].id) for m in markets if "Independent" in m.title)
    assert independents == [("191", "880"), ("276", "965"), ("280", "969")]
    assert sorted(m.id for m in markets if "Freeform" in m.categories) == [
        "151",
        "152",
        "153",
        "154",
    ]
    # Variants this capture holds: every market open, one YES exchange, prices present.
    assert {m.status for m in markets} == {"open"}
    assert {len(m.exchanges) for m in markets} == {1}
    assert all(m.exchanges[0].latestPrice is not None for m in markets)
    assert {(m.creator.id, m.creator.username) for m in markets if m.creator} == {CREATOR}


def test_markets_pages_report_consistent_pagination() -> None:
    first, _ = replay(RUN, "list_tournament_markets_page", limit=100, status="any")
    assert (len(first.data), first.pagination.total, first.pagination.limit) == (100, 237, 100)
    assert first.pagination.hasMore is True
    assert first.pagination.nextCursor == PAGE2_CURSOR
    last, _ = replay(
        RUN, "list_tournament_markets_page", limit=100, status="any", cursor=PAGE3_CURSOR
    )
    assert (len(last.data), last.pagination.total) == (37, 237)
    assert (last.pagination.hasMore, last.pagination.nextCursor) == (False, None)


def test_list_all_markets_never_returns_a_partial_list_at_the_page_cap() -> None:
    client, served = make_replay_client(RUN)

    async def scenario() -> None:
        async with client:
            await client.list_all_tournament_markets(page_limit=100, max_pages=2)

    with pytest.raises(SigPaginationError):
        run(scenario())
    assert served.unexpected == []
    assert served.paths() == ["/tournaments/midterm-elections", MARKETS, MARKETS]


def test_exchange_prices_replay_maps_947_to_258() -> None:
    prices, _ = replay(RUN, "get_exchange_prices", ["947"])
    (price,) = prices.data
    assert (price.exchangeId, price.marketId, price.option) == ("947", "258", "YES")
    assert decimals([price.latestPrice, price.bestBid, price.bestAsk, price.spread]) == [
        "0.94",
        "0.94",
        "0.945",
        "0.005",
    ]
    assert prices.missingIds == []


BOOK_BIDS = [
    ("0.94", "16314"),
    ("0.935", "6348"),
    ("0.93", "17947"),
    ("0.925", "12735"),
    ("0.92", "53302"),
]
BOOK_ASKS = [
    ("0.945", "21315"),
    ("0.95", "9137"),
    ("0.955", "87751"),
    ("0.96", "491541"),
    ("0.965", "200017"),
]


def test_exchange_orderbook_replay() -> None:
    book, _ = replay(RUN, "get_exchange_orderbook", "947", depth=5)
    assert (book.exchangeId, book.marketId, book.depth) == ("947", "258", 5)
    assert book.asOf is not None
    assert book.asOf.sequence == 112607913
    assert book.asOf.at == "2026-10-09T19:00:47.0625388+00:00"
    assert levels(book.bids) == BOOK_BIDS
    assert levels(book.asks) == BOOK_ASKS
    assert decimals([book.bestBid, book.bestAsk, book.spread]) == ["0.94", "0.945", "0.005"]


def test_market_orderbook_replay_is_scoped_to_the_tournament() -> None:
    book, _ = replay(RUN, "get_market_orderbook", "258", depth=5)
    (context,) = book.contexts
    assert context.type == "tournament" and context.tournament is not None
    assert (context.tournament.id, context.tournament.slug) == (TOURNAMENT.id, TOURNAMENT.slug)
    for combined in (book, book.tournament_orderbook()):
        (exchange,) = combined.exchanges
        assert (exchange.exchangeId, exchange.option) == ("947", "YES")
        assert exchange.asOf is not None
        assert exchange.asOf.sequence == 112608401
        assert exchange.asOf.at == "2026-10-09T19:00:51.1493955+00:00"
        assert decimals([exchange.impliedProbability, combined.overround]) == ["0.945", "0.945"]
        assert combined.hasArbitrageOpportunity is False
        assert levels(exchange.bids) == BOOK_BIDS
        assert levels(exchange.asks) == BOOK_ASKS


def test_orders_page_replay_covers_the_empty_envelope() -> None:
    page, served = replay(RUN, "list_orders_page", status="all", limit=50)
    assert served.sent()[-1] == (
        "/orders",
        {"status": "all", "limit": "50", "tournamentId": TOURNAMENT.id},
    )
    assert page.data == []
    assert (page.pagination.limit, page.pagination.hasMore) == (50, False)
    assert page.pagination.nextCursor is None
    # The optional coverage object was present. projectedThroughSequence is checked only
    # as the integer SIG sent; its meaning is not inferred.
    assert page.coverage is not None
    assert page.coverage.complete is True
    assert type(page.coverage.projectedThroughSequence) is int
    assert page.coverage.projectedThroughSequence == 112608652


def test_list_all_orders_stops_on_the_single_final_page() -> None:
    orders, served = replay(RUN, "list_all_orders", status="all", page_limit=50)
    assert orders == []
    assert served.paths() == ["/tournaments/midterm-elections", "/orders"]


def test_all_recorded_reads_in_one_session_send_the_recorded_requests() -> None:
    client, served = make_replay_client(RUN)

    async def scenario() -> tuple[Any, ...]:
        async with client:
            markets = await client.list_all_tournament_markets(page_limit=100, max_pages=3)
            prices = await client.get_exchange_prices(["947"])
            book = await client.get_exchange_orderbook("947", depth=5)
            market_book = await client.get_market_orderbook("258", depth=5)
            orders = await client.list_all_orders(status="all", page_limit=50, max_pages=3)
            return markets, prices, book, market_book, orders

    markets, prices, book, market_book, orders = run(scenario())
    assert served.unexpected == []
    assert served.sent() == [
        (f["request"]["path"], f["request"]["params"]) for f in effective_manifest(RUN)["fixtures"]
    ]
    assert len(markets) == 237 and orders == []
    assert prices.data[0].marketId == book.marketId == "258"
    assert [e.exchangeId for e in market_book.exchanges] == ["947"]
