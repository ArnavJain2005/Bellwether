"""Replay capture 20261011T211201Z-12ab20 through SigRestClient (S1-T3-B-D).

Five of the capture's nine reads are committed: the tournament and market 258 (both
redacted; see fixtures.json and tests/fixtures/sig/README.md) and three pages of
exchange 947's trade tape, byte-identical to what SIG sent. The other four reads
(markets page, prices, both books) were captured but not promoted.

The tape is a truncated sample: every recorded page says hasMore true, and the cursor
for the next page was never followed. Nothing here shows the full history for the
window, or that SIG's reporting projection had caught up with the trading engine.
"""

from __future__ import annotations

from datetime import UTC, datetime
from itertools import pairwise
from typing import Any

import pytest
from recorded_kit import (
    CREATOR,
    RECORDED,
    THUMBNAIL_SEGMENT,
    TOURNAMENT,
    decimals,
    effective_manifest,
    make_replay_client,
    replay,
    source_provenance,
)
from sig_testkit import run

from engine.sig_client.errors import SigPaginationError, SigUnexpectedStatusError

RUN = RECORDED / "20261011T211201Z-12ab20"
TRADES = "/exchanges/947/trades"
SINCE = datetime(2026, 10, 1, tzinfo=UTC)
UNTIL = datetime(2026, 10, 10, tzinfo=UTC)
WINDOW = {"from": "2026-10-01T00:00:00.000Z", "to": "2026-10-10T00:00:00.000Z"}
TRADES_QUERY = {"tournamentId": TOURNAMENT.id, "limit": "50", **WINDOW}
# Opaque cursors, exactly as SIG sent them. Tests never decode them.
PAGE2_CURSOR = (
    "eyJ0eXBlIjoidGltZSIsInNvdXJjZSI6ImVuZ2luZSIsImNyZWF0ZWRBdCI6IjIwMjYtMTAtMDlUMjE6NDA6"
    "MTcuOTA1WiIsImlkIjoiMTEzOTIwMTg3In0"
)
PAGE3_CURSOR = (
    "eyJ0eXBlIjoidGltZSIsInNvdXJjZSI6ImVuZ2luZSIsImNyZWF0ZWRBdCI6IjIwMjYtMTAtMDlUMTU6MDE6"
    "MDUuNDU5WiIsImlkIjoiMTEwNjc5OTcyIn0"
)
UNFOLLOWED_CURSOR = (
    "eyJ0eXBlIjoidGltZSIsInNvdXJjZSI6ImVuZ2luZSIsImNyZWF0ZWRBdCI6IjIwMjYtMTAtMDlUMDk6NDM6"
    "MTEuNzY1WiIsImlkIjoiMTA4MTA3OTk4In0"
)
CURSORS = [None, PAGE2_CURSOR, PAGE3_CURSOR]
CHECKPOINTS = [133949521, 133950141, 133950511]


def trades_query(cursor: str | None) -> dict[str, str]:
    return TRADES_QUERY if cursor is None else {**TRADES_QUERY, "cursor": cursor}


def read_three_pages() -> tuple[list[Any], Any]:
    """The three recorded pages in one client session, each following the last nextCursor."""
    client, served = make_replay_client(RUN)

    async def scenario() -> list[Any]:
        pages: list[Any] = []
        cursor: str | None = None
        async with client:
            for _ in range(3):
                page = await client.list_exchange_trades_page(
                    "947", since=SINCE, until=UNTIL, limit=50, cursor=cursor
                )
                pages.append(page)
                cursor = page.pagination.nextCursor
        return pages

    pages = run(scenario())
    assert served.unexpected == []
    return pages, served


def test_capture_provenance_keeps_the_truncated_tape() -> None:
    _, source = source_provenance(RUN)
    assert source["code"]["git_commit"] == "35f455217ad3ab786c3ab3a8cf267e6ee7bfa436"
    selection = source["selection"]
    assert selection["include_market_detail"] is True and selection["include_trades"] is True
    assert selection["include_orders"] is False
    assert (selection["trades_from"], selection["trades_to"]) == (WINDOW["from"], WINDOW["to"])
    assert (selection["trades_page_limit"], selection["trades_max_pages"]) == (50, 3)
    tape = source["collections"]["exchange_trades_947"]
    assert (tape["pages_fetched"], tape["complete"], tape["truncated"]) == (3, False, True)
    assert tape["cursor_chain"] == CURSORS
    assert tape["last_next_cursor"] == UNFOLLOWED_CURSOR
    assert tape["coverage_per_page"] == [
        {"present": True, "complete": True, "projectedThroughSequence": checkpoint}
        for checkpoint in CHECKPOINTS
    ]
    assert tape["projection_freshness"] == "unknown"
    markets = source["collections"]["tournament_markets"]
    assert (markets["complete"], markets["truncated"]) == (False, True)
    assert [n["name"] for n in effective_manifest(RUN)["not_promoted"]] == [
        "tournament-markets-p1",
        "exchange-prices",
        "exchange-orderbook-947",
        "market-orderbook-258",
    ]


def test_tournament_replay() -> None:
    tournament, served = replay(RUN, "verify_tournament")
    assert served.paths() == ["/tournaments/midterm-elections"]
    assert (tournament.id, tournament.slug) == (TOURNAMENT.id, TOURNAMENT.slug)
    assert decimals([tournament.myBalance]) == ["12345.67"]  # redacted test value
    assert tournament.isPendingEnrolment is True  # redacted test value
    assert (tournament.memberCount, tournament.marketCount) == (4462, 237)


def test_market_detail_replay_is_market_258_in_the_tournament() -> None:
    market, served = replay(RUN, "get_market", "258")
    assert served.sent() == [
        ("/tournaments/midterm-elections", {}),
        ("/markets/258", {"tournamentId": TOURNAMENT.id}),
    ]
    assert market.id == "258"
    assert market.title == "Will the Democratic Party win the Georgia Senate?"
    assert (market.status, market.settledWith, market.settledOn) == ("open", None, None)
    assert market.createdAt == "2026-08-24T20:41:36.184Z"
    assert market.settlementDate == "2026-11-04T17:00:00.000Z"
    assert market.categories == ["Election Outcome"]
    assert (market.isComposite, market.isMultiOutcome) == (False, False)
    (exchange,) = market.exchanges
    assert (exchange.id, exchange.option) == ("947", "YES")
    assert decimals([exchange.latestPrice, exchange.initialPrice]) == ["0.945", "0.5"]
    context = market.tournament_context()
    assert context.type == "tournament" and context.tournament is not None
    assert (context.tournament.id, context.tournament.slug) == (TOURNAMENT.id, TOURNAMENT.slug)
    assert (context.status, context.settledWith, context.settledOn) == ("open", None, None)
    assert [(e.id, str(e.latestPrice)) for e in context.exchanges] == [("947", "0.945")]
    # Redaction placeholders, not the recorded values.
    assert market.creator is not None
    assert (market.creator.id, market.creator.username) == CREATOR
    assert market.thumbnailUrl is not None and THUMBNAIL_SEGMENT in market.thumbnailUrl


def test_trade_pages_follow_the_recorded_cursor_chain() -> None:
    pages, served = read_three_pages()
    assert served.sent() == [("/tournaments/midterm-elections", {})] + [
        (TRADES, trades_query(cursor)) for cursor in CURSORS
    ]
    assert [p.pagination.nextCursor for p in pages] == [*CURSORS[1:], UNFOLLOWED_CURSOR]
    # Every recorded page says there is more: this is not the whole tape.
    assert [p.pagination.hasMore for p in pages] == [True, True, True]
    assert [p.pagination.limit for p in pages] == [50, 50, 50]
    assert [len(p.data) for p in pages] == [50, 50, 50]
    for page in pages:
        assert (page.exchangeId, page.marketId) == ("947", "258")
        assert (page.from_, page.to) == (WINDOW["from"], WINDOW["to"])


def test_trade_pages_keep_coverage_exactly_as_sent() -> None:
    # Checkpoints are kept as the integers SIG sent. That they differ between pages is
    # an observation; nothing here shows the projection had caught up with the engine.
    pages, _ = read_three_pages()
    for page, checkpoint in zip(pages, CHECKPOINTS, strict=True):
        assert page.coverage is not None
        assert page.coverage.complete is True
        assert type(page.coverage.projectedThroughSequence) is int
        assert page.coverage.projectedThroughSequence == checkpoint


def test_trade_items_are_150_unique_newest_first_inside_the_window() -> None:
    pages, _ = read_three_pages()
    trades = [t for p in pages for t in p.data]
    ids = [t.id for t in trades]
    assert len(ids) == len(set(ids)) == 150
    edges = [
        (p.data[0].id, p.data[0].createdAt, p.data[-1].id, p.data[-1].createdAt) for p in pages
    ]
    assert edges == [
        ("114373466", "2026-10-09T22:33:16.692Z", "113920187", "2026-10-09T21:40:17.905Z"),
        ("113910857", "2026-10-09T21:39:14.797Z", "110679972", "2026-10-09T15:01:05.459Z"),
        ("110667364", "2026-10-09T14:59:43.477Z", "108107998", "2026-10-09T09:43:11.765Z"),
    ]
    times = [datetime.fromisoformat(t.createdAt) for t in trades]
    assert all(SINCE <= moment < UNTIL for moment in times)
    # Newest first within and across pages. Equal times occur only inside a page, and
    # there the recorded ids still decrease.
    assert all(newer >= older for newer, older in pairwise(times))
    ties = [(a.id, b.id) for a, b in pairwise(trades) if a.createdAt == b.createdAt]
    assert ties == [
        ("111081929", "111081923"),
        ("111081923", "111081917"),
        ("109936663", "109936658"),
    ]
    for page, following in pairwise(pages):
        assert page.data[-1].createdAt > following.data[0].createdAt


def test_trade_items_keep_exact_decimal_values() -> None:
    pages, _ = read_three_pages()
    by_id = {t.id: t for p in pages for t in p.data}
    assert all(type(t.size) is type(t.price) is type(t.volume) for t in by_id.values())
    exact = {
        "114373466": ("YES", "0.945", "2", "1.89"),
        "114372379": ("NO", "0.94", "13", "0.7800000000000007"),
        "114174091": ("YES", "0.945", "14", "13.229999999999999"),
        "113920187": ("NO", "0.94", "3", "0.18000000000000016"),
        "113874151": ("YES", "0.94", "150", "141"),
        "110679972": ("YES", "0.95", "6", "5.699999999999999"),
        "110657133": ("YES", "0.95", "3", "2.8499999999999996"),
        "108107998": ("YES", "0.945", "1", "0.945"),
    }
    for trade_id, (side, price, size, volume) in exact.items():
        trade = by_id[trade_id]
        assert trade.side == side
        assert decimals([trade.price, trade.size, trade.volume]) == [price, size, volume]
    trades = list(by_id.values())
    assert sorted({str(t.price) for t in trades}) == ["0.94", "0.945", "0.95"]
    assert [sum(t.side == "YES" for t in p.data) for p in pages] == [25, 26, 27]
    assert [str(sum(t.size for t in p.data)) for p in pages] == ["240", "4872", "1936"]


def test_list_all_trades_raises_at_the_page_cap_without_a_fourth_request() -> None:
    client, served = make_replay_client(RUN)

    async def scenario() -> None:
        async with client:
            await client.list_all_exchange_trades(
                "947", since=SINCE, until=UNTIL, page_limit=50, max_pages=3
            )

    with pytest.raises(SigPaginationError):
        run(scenario())
    assert served.unexpected == []
    assert served.sent() == [("/tournaments/midterm-elections", {})] + [
        (TRADES, trades_query(cursor)) for cursor in CURSORS
    ]


def test_list_all_trades_never_invents_the_unrecorded_final_page() -> None:
    # With room for a fourth page the walk asks for it, the replay refuses it, and the
    # client raises instead of returning the 150 trades it has.
    client, served = make_replay_client(RUN)

    async def scenario() -> None:
        async with client:
            await client.list_all_exchange_trades(
                "947", since=SINCE, until=UNTIL, page_limit=50, max_pages=4
            )

    with pytest.raises(SigUnexpectedStatusError):
        run(scenario())
    assert served.unexpected == [f"GET /api/v1{TRADES}"]
    assert served.sent()[-1] == (TRADES, trades_query(UNFOLLOWED_CURSOR))


@pytest.mark.parametrize(
    ("label", "kwargs"),
    [
        ("other limit", {"limit": 49}),
        ("other window", {"until": datetime(2026, 10, 9, tzinfo=UTC)}),
        ("unfollowed cursor", {"cursor": UNFOLLOWED_CURSOR}),
    ],
)
def test_unrecorded_trade_requests_are_refused(label: str, kwargs: dict[str, Any]) -> None:
    client, served = make_replay_client(RUN)
    arguments: dict[str, Any] = {"since": SINCE, "until": UNTIL, "limit": 50, **kwargs}

    async def scenario() -> None:
        async with client:
            await client.list_exchange_trades_page("947", **arguments)

    with pytest.raises(SigUnexpectedStatusError):
        run(scenario())
    assert served.unexpected == [f"GET /api/v1{TRADES}"], label


def test_all_committed_reads_in_one_session_send_the_recorded_requests() -> None:
    client, served = make_replay_client(RUN)

    async def scenario() -> tuple[Any, list[Any]]:
        async with client:
            market = await client.get_market("258")
            pages: list[Any] = []
            for cursor in CURSORS:
                pages.append(
                    await client.list_exchange_trades_page(
                        "947", since=SINCE, until=UNTIL, limit=50, cursor=cursor
                    )
                )
            return market, pages

    market, pages = run(scenario())
    assert served.unexpected == []
    assert served.sent() == [
        (f["request"]["path"], f["request"]["params"]) for f in effective_manifest(RUN)["fixtures"]
    ]
    assert [e.id for e in market.exchanges] == ["947"]
    assert {(p.exchangeId, p.marketId) for p in pages} == {("947", "258")}
