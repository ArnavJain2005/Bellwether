import asyncio
import json
from datetime import UTC, datetime, timedelta
from decimal import Decimal as D

import httpx

from bot.refs import (
    RefCollector,
    RefSettings,
    fair_value,
    make_quote,
    parse_kalshi_market,
    parse_polymarket_book,
)

T0 = datetime(2026, 11, 3, 12, 0, tzinfo=UTC)
S = RefSettings(D("0.04"), D(30), D("0.03"))


def q(venue, bid, ask, at=T0, status="active"):
    return make_quote(venue, "x", None if bid is None else D(bid),
                      None if ask is None else D(ask), at, status)


def test_kalshi_dollars_preferred_over_cents():
    m = {"ticker": "T", "status": "active", "yes_bid_dollars": "0.4300",
         "yes_ask_dollars": "0.4500", "yes_bid": 1, "yes_ask": 99}
    r = parse_kalshi_market(m, T0)
    assert (r.bid, r.ask, r.mid, r.spread, r.status) == (D("0.43"), D("0.45"), D("0.44"),
                                                          D("0.02"), "active")


def test_kalshi_cents_fallback_and_empty_sides():
    r = parse_kalshi_market({"ticker": "T", "status": "open", "yes_bid": 43, "yes_ask": 46}, T0)
    assert (r.bid, r.ask, r.mid) == (D("0.43"), D("0.46"), D("0.445"))
    r = parse_kalshi_market({"ticker": "T", "status": "active", "yes_bid_dollars": "0.0000",
                             "yes_ask_dollars": "0.5000"}, T0)
    assert r.status == "no_quote" and r.mid is None


def test_kalshi_closed_is_not_active():
    r = parse_kalshi_market({"ticker": "T", "status": "finalized", "yes_bid_dollars": "0.99",
                             "yes_ask_dollars": "1.00"}, T0)
    assert r.status == "finalized" and r.mid is None


def test_polymarket_book_best_prices_regardless_of_order():
    book = {"asset_id": "9",
            "bids": [{"price": "0.40", "size": "5"}, {"price": "0.42", "size": "1"}],
            "asks": [{"price": "0.47", "size": "2"}, {"price": "0.45", "size": "3"}]}
    r = parse_polymarket_book(book, T0)
    assert (r.bid, r.ask, r.mid) == (D("0.42"), D("0.45"), D("0.435"))
    assert parse_polymarket_book({"asset_id": "9", "bids": [], "asks": []}, T0).status == "no_quote"
    crossed = {"asset_id": "9", "bids": [{"price": "0.6"}], "asks": [{"price": "0.5"}]}
    assert parse_polymarket_book(crossed, T0).status == "crossed"


def test_fair_value_rules():
    now = T0 + timedelta(seconds=5)
    k = q("kalshi", "0.43", "0.45")
    assert fair_value(k, q("polymarket", "0.44", "0.46"), S, now) == (
        D("0.445"), "kalshi+polymarket average")
    assert fair_value(k, q("polymarket", "0.50", "0.52"), S, now) == (None, "venues disagree")
    f, why = fair_value(k, None, S, now)
    assert f == D("0.44") and why.startswith("kalshi only")
    f, why = fair_value(k, q("polymarket", "0.30", "0.50"), S, now)  # wide poly spread
    assert f == D("0.44") and "spread" in why
    f, why = fair_value(k, make_quote("polymarket", "x", None, None, T0, "missing"), S, now)
    assert f == D("0.44") and "missing" in why


def test_fair_value_kalshi_must_be_valid():
    now = T0 + timedelta(seconds=5)
    assert fair_value(None, None, S, now)[0] is None
    assert fair_value(q("kalshi", "0.40", "0.46"), None, S, now)[1].startswith("kalshi spread")
    assert fair_value(q("kalshi", "0.43", "0.45", status="closed"), None, S, now)[0] is None
    assert fair_value(q("kalshi", None, "0.45"), None, S, now)[0] is None


def test_staleness():
    k = q("kalshi", "0.43", "0.45")
    assert fair_value(k, None, S, T0 + timedelta(seconds=30))[0] == D("0.44")
    f, why = fair_value(k, None, S, T0 + timedelta(seconds=31))
    assert f is None and "stale" in why
    stale_poly = q("polymarket", "0.44", "0.46", at=T0 - timedelta(seconds=60))
    f, why = fair_value(k, stale_poly, S, T0 + timedelta(seconds=5))
    assert f == D("0.44") and "stale" in why


def test_settings_from_mapping():
    s = RefSettings.from_mapping({"max_ref_spread": 0.04, "max_ref_age_s": 30,
                                  "max_venue_disagreement": 0.03})
    assert s == S


def _collector(handler):
    sleeps = []

    async def sleep(s):
        sleeps.append(s)
    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    return RefCollector(client, sleep=sleep, clock=lambda: 0.0, now=lambda: T0), sleeps


def test_collector_chunks_tickers_and_parses():
    seen = []

    def handler(req: httpx.Request):
        tickers = req.url.params["tickers"].split(",")
        seen.append(len(tickers))
        body = {"markets": [{"ticker": t, "status": "active", "yes_bid_dollars": "0.40",
                             "yes_ask_dollars": "0.42"} for t in tickers if t != "T7"]}
        return httpx.Response(200, content=json.dumps(body))

    async def go():
        c, _ = _collector(handler)
        return await c.fetch_kalshi([f"T{i}" for i in range(120)])

    quotes, err = asyncio.run(go())
    assert seen == [50, 50, 20] and err is None
    assert quotes["T1"].mid == D("0.41") and quotes["T7"].status == "missing"


def test_kalshi_falls_back_to_second_host_and_backs_off():
    hosts = []

    def handler(req: httpx.Request):
        hosts.append(req.url.host)
        if req.url.host == "api.elections.kalshi.com":
            return httpx.Response(429)
        return httpx.Response(200, json={"markets": []})

    async def go():
        c, sleeps = _collector(handler)
        out = await c.fetch_kalshi(["A"])
        return out, sleeps

    (quotes, err), sleeps = asyncio.run(go())
    assert hosts == ["api.elections.kalshi.com"] * 4 + ["external-api.kalshi.com"]
    assert err is None and quotes["A"].status == "missing"
    assert [s for s in sleeps if s >= 0.5][:3] == [0.5, 1.0, 2.0]


def test_polymarket_books_and_midpoint_fallback():
    def handler(req: httpx.Request):
        if req.url.path == "/books":
            body = json.loads(req.content)
            assert body == [{"token_id": "1"}, {"token_id": "2"}, {"token_id": "3"}]
            return httpx.Response(200, json=[
                {"asset_id": "1", "bids": [{"price": "0.40", "size": "1"}],
                 "asks": [{"price": "0.44", "size": "1"}]},
                {"asset_id": "2", "bids": [], "asks": []}])
        return httpx.Response(404)

    async def go():
        c, _ = _collector(handler)
        return await c.fetch_polymarket(["1", "2", "3"])

    quotes, err = asyncio.run(go())
    assert err is None
    assert quotes["1"].mid == D("0.42") and quotes["1"].status == "active"
    assert quotes["2"].status == "no_quote"
    assert quotes["3"].status == "missing"


def test_midpoint_fallback_used_when_book_absent():
    def handler(req: httpx.Request):
        if req.url.path == "/books":
            return httpx.Response(200, json=[])
        return httpx.Response(200, json={"mid": "0.5"})

    async def go():
        c, _ = _collector(handler)
        return await c.fetch_polymarket(["1"])

    quotes, _ = asyncio.run(go())
    assert quotes["1"].status == "mid_only" and quotes["1"].mid == D("0.5")
    kalshi = q("kalshi", "0.49", "0.51")
    assert fair_value(kalshi, quotes["1"], S, T0)[1].startswith("kalshi only")


def test_one_failing_venue_does_not_block_the_other():
    def handler(req: httpx.Request):
        if req.url.host == "clob.polymarket.com":
            return httpx.Response(500)
        return httpx.Response(200, json={"markets": [{"ticker": "A", "status": "active",
                                                      "yes_bid_dollars": "0.4",
                                                      "yes_ask_dollars": "0.42"}]})

    async def go():
        c, _ = _collector(handler)
        return await c.fetch_all(["A"], ["1"])

    snap = asyncio.run(go())
    assert snap.kalshi["A"].status == "active"
    assert snap.polymarket["1"].status == "error" and "polymarket" in snap.errors
    assert "kalshi" not in snap.errors
