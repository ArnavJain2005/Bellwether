"""Loop test kit: a fake SIG server behind the real SigRestClient and SigWriter, fake
Kalshi behind the real RefCollector, and one fake clock for all of them. No network."""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs

import httpx

from bot import db
from bot.ladder import LoadedLadder
from bot.limits import Limits
from bot.main import Bot
from bot.mapping import MappedContract
from bot.refs import RefCollector
from bot.sig_writer import SigWriter
from bot.tests.kit import NOW, FakeTime, _encode, make_limits, sig_config
from engine.config import MIDTERM_ELECTIONS
from engine.sig_client.rate_limiter import RateLimiter
from engine.sig_client.rest import SigRestClient

SLUG = MIDTERM_ELECTIONS.slug
D = Decimal


def contracts(races: int) -> list[MappedContract]:
    """`races` races with a D and an R contract each (exchanges 1000.., markets 500..)."""
    out = []
    for n in range(races):
        for i, party in enumerate("DR"):
            k = 2 * n + i
            out.append(
                MappedContract(
                    race_key=f"senate:S{n}",
                    party=party,
                    sig_market_id=str(500 + k),
                    sig_exchange_id=str(1000 + k),
                    sig_title=f"race {n} {party}",
                    kalshi_ticker=f"K{n}-{party}",
                    polymarket_token_id=None,
                )
            )
    return out


@dataclass
class Seen:
    method: str
    path: str
    query: dict[str, list[str]]
    content: bytes
    at: float


@dataclass
class FakeSig:
    """SIG's documented routes for one tournament, with mutable state."""

    time: FakeTime
    contracts: list[MappedContract]
    balance: Decimal = field(default_factory=lambda: D(100000))
    quotes: dict[str, tuple[Decimal | None, Decimal | None]] = field(default_factory=dict)
    books: dict[str, tuple[list, list]] = field(default_factory=dict)
    market_status: dict[str, str] = field(default_factory=dict)
    open_orders: list[dict[str, Any]] = field(default_factory=list)
    fills: dict[int, Decimal] = field(default_factory=dict)
    seen: list[Seen] = field(default_factory=list)
    next_order_id: int = 7001
    order_reply: Any = None  # a callable(body) -> httpx.Response, or None for "resting"

    def quote(self, exchange_id: str) -> tuple[Decimal | None, Decimal | None]:
        return self.quotes.get(exchange_id, (D("0.50"), D("0.60")))  # no edge, no complement

    def reads(self) -> list[Seen]:
        return [s for s in self.seen if s.method == "GET"]

    def writes(self) -> list[Seen]:
        return [s for s in self.seen if s.method != "GET"]

    def handler(self, request: httpx.Request) -> httpx.Response:
        path = request.url.path.removeprefix("/api/v1")
        query = parse_qs(request.url.query.decode())
        self.seen.append(Seen(request.method, path, query, request.content, self.time.t))
        method = request.method
        if method == "GET" and path == f"/tournaments/{SLUG}":
            return self._ok(self._tournament())
        if method == "GET" and path == "/exchanges/prices":
            return self._ok(self._prices(query["ids"][0].split(",")))
        if method == "GET" and path.startswith("/exchanges/") and path.endswith("/orderbook"):
            return self._ok(self._book(path.split("/")[2]))
        if method == "GET" and path == "/orders":
            data = [o for o in self.open_orders if o["open"]]
            return self._ok(
                {"data": data, "pagination": {"limit": 200, "hasMore": False, "nextCursor": None}}
            )
        if method == "GET" and path == f"/tournaments/{SLUG}/portfolio/positions":
            return self._ok(
                {
                    "positions": [],
                    "summary": {
                        "totalMarketValue": 0,
                        "totalCostBasis": 0,
                        "totalUnrealizedPnl": 0,
                    },
                }
            )
        if method == "GET" and path == f"/tournaments/{SLUG}/markets":
            return self._ok(self._markets(query))
        if method == "GET" and path.startswith("/orders/") and path.endswith("/fills"):
            return self._ok(self._fills(int(path.split("/")[2])))
        if method == "POST" and path == "/orders":
            return self._place(json.loads(request.content, parse_float=Decimal))
        if method == "DELETE" and path.startswith("/orders/"):
            order_id = int(path.split("/")[2])
            for order in self.open_orders:
                if order["id"] == order_id:
                    order["open"] = False
            return self._ok(
                {"orderId": order_id, "tournamentId": MIDTERM_ELECTIONS.id, "message": "ok"}
            )
        if method == "POST" and path == "/orders/cancel-all":
            count = sum(1 for o in self.open_orders if o["open"])
            for order in self.open_orders:
                order["open"] = False
            return self._ok({"cancelled": count})
        raise AssertionError(f"unexpected {method} {path}")

    @staticmethod
    def _ok(body: Any) -> httpx.Response:
        return httpx.Response(200, content=_encode(body).encode())

    def _tournament(self) -> dict[str, Any]:
        return {
            "id": MIDTERM_ELECTIONS.id,
            "slug": SLUG,
            "name": "Midterms",
            "description": None,
            "status": "active",
            "startDate": None,
            "endDate": None,
            "initialBalance": 100000,
            "currencyName": "SUSQies",
            "memberCount": 10,
            "marketCount": 237,
            "myBalance": self.balance,
            "isPendingEnrolment": False,
        }

    def _market_of(self, exchange_id: str) -> str:
        return next(c.sig_market_id for c in self.contracts if c.sig_exchange_id == exchange_id)

    def _prices(self, ids: list[str]) -> dict[str, Any]:
        data = []
        for i in ids:
            bid, ask = self.quote(i)
            data.append(
                {
                    "exchangeId": i,
                    "marketId": self._market_of(i),
                    "option": "YES",
                    "latestPrice": bid,
                    "bestBid": bid,
                    "bestAsk": ask,
                    "spread": None if bid is None or ask is None else ask - bid,
                }
            )
        return {"data": data, "missingIds": []}

    def _book(self, exchange_id: str) -> dict[str, Any]:
        bid, ask = self.quote(exchange_id)
        bids, asks = self.books.get(
            exchange_id,
            (
                [(bid, D(1000))] if bid is not None else [],
                [(ask, D(1000))] if ask is not None else [],
            ),
        )
        return {
            "exchangeId": exchange_id,
            "marketId": self._market_of(exchange_id),
            "asOf": None,
            "depth": 20,
            "bids": [{"price": p, "quantity": q} for p, q in bids],
            "asks": [{"price": p, "quantity": q} for p, q in asks],
            "bestBid": bids[0][0] if bids else None,
            "bestAsk": asks[0][0] if asks else None,
            "spread": None,
        }

    def _markets(self, query: dict[str, list[str]]) -> dict[str, Any]:
        markets = sorted({c.sig_market_id for c in self.contracts}, key=int)
        limit = int(query["limit"][0])
        start = int(query.get("cursor", ["0"])[0])
        page = markets[start : start + limit]
        more = start + limit < len(markets)
        data = [
            {
                "id": m,
                "title": f"market {m}",
                "status": self.market_status.get(m, "open"),
                "createdAt": None,
                "settlementDate": None,
                "settledWith": None,
                "settledOn": None,
                "categories": [],
                "isComposite": False,
                "isMultiOutcome": False,
                "exchanges": [],
                "creator": None,
            }
            for m in page
        ]
        return {
            "data": data,
            "pagination": {
                "total": len(markets),
                "limit": limit,
                "hasMore": more,
                "nextCursor": str(start + limit) if more else None,
            },
        }

    def _fills(self, order_id: int) -> dict[str, Any]:
        order = next(o for o in self.open_orders if o["id"] == order_id)
        return {
            "orderId": order_id,
            "exchangeId": order["exchangeId"],
            "tournamentId": MIDTERM_ELECTIONS.id,
            "data": [],
            "pagination": {"limit": 200, "hasMore": False, "nextCursor": None},
            "coverage": {"complete": True, "projectedThroughSequence": None},
            "totalQuantityFilled": self.fills.get(order_id, D(0)),
            "avgFillPrice": None,
        }

    def _place(self, body: dict[str, Any]) -> httpx.Response:
        if self.order_reply is not None:
            return self.order_reply(body)
        return self.resting(body)

    def resting(self, body: dict[str, Any]) -> httpx.Response:
        """Accept the order: it rests on the book and stays open."""
        order_id = self.next_order_id
        self.next_order_id += 1
        self.open_orders.append(
            {
                "id": order_id,
                "exchangeId": body["exchangeId"],
                "side": body["side"],
                "action": "buy",
                "quantity": body["quantity"],
                "priceLimit": body["price"],
                "open": True,
                "createdAt": "2026-11-03T23:30:00.000Z",
                "expirationDate": body["expirationDate"],
            }
        )
        return self._ok(
            {
                "orderId": order_id,
                "exchangeId": body["exchangeId"],
                "open": True,
                "remainingQuantity": body["quantity"],
                "action": "buy",
                "side": body["side"],
                "price": body["price"],
                "quantity": body["quantity"],
                "terminalReasonCode": None,
                "quantityTraded": 0,
                "totalCost": 0,
                "fillPrice": None,
                "all": None,
            }
        )


@dataclass
class FakeKalshi:
    """Kalshi quotes per ticker as (bid, ask); default 0.47/0.49 (fair 0.48)."""

    quotes: dict[str, tuple[str, str]] = field(default_factory=dict)
    calls: int = 0

    def handler(self, request: httpx.Request) -> httpx.Response:
        self.calls += 1
        tickers = parse_qs(request.url.query.decode())["tickers"][0].split(",")
        markets = []
        for t in tickers:
            bid, ask = self.quotes.get(t, ("0.47", "0.49"))
            markets.append(
                {"ticker": t, "status": "active", "yes_bid_dollars": bid, "yes_ask_dollars": ask}
            )
        return httpx.Response(200, json={"markets": markets})


@dataclass
class LoopRig:
    bot: Bot
    sig: FakeSig
    kalshi: FakeKalshi
    time: FakeTime
    conn: Any
    state_dir: Path

    def events(self, kind: str | None = None) -> list[Any]:
        rows = self.conn.execute("SELECT * FROM events ORDER BY id").fetchall()
        return [r for r in rows if kind is None or r["kind"] == kind]

    def decisions(self, verdict: str | None = None) -> list[Any]:
        rows = self.conn.execute("SELECT * FROM decisions ORDER BY id").fetchall()
        return [r for r in rows if verdict is None or r["verdict"] == verdict]

    def heartbeat(self) -> Any:
        return self.conn.execute("SELECT * FROM heartbeat WHERE id = 1").fetchone()

    def orders(self) -> list[Any]:
        return self.conn.execute("SELECT * FROM orders ORDER BY created_utc").fetchall()

    @property
    def paused(self) -> bool:
        return (self.state_dir / "PAUSED").exists()


def make_loop(
    tmp_path: Path,
    *,
    races: int = 2,
    limits: Limits | None = None,
    live: bool = False,
    sig_contracts: list[MappedContract] | None = None,
    ladder: LoadedLadder | None = None,
    ladder_path: Path | None = None,
) -> LoopRig:
    time = FakeTime()
    start = time.t

    def now() -> datetime:
        return NOW + timedelta(seconds=time.t - start)

    mapped = contracts(races)
    sig = FakeSig(time, sig_contracts or mapped)
    kalshi = FakeKalshi()
    limiter = RateLimiter(clock=time.clock, sleep=time.sleep)
    config = sig_config()
    rest = SigRestClient.create(
        config,
        transport=httpx.MockTransport(sig.handler),
        limiter=limiter,
        clock=time.clock,
        sleep=time.sleep,
        wall_clock=now,
    )
    writer = SigWriter(
        config,
        limiter,
        transport=httpx.MockTransport(sig.handler),
        clock=time.clock,
        sleep=time.sleep,
    )
    refs = RefCollector(
        httpx.AsyncClient(transport=httpx.MockTransport(kalshi.handler)),
        clock=time.clock,
        sleep=time.sleep,
        now=now,
    )
    state_dir = tmp_path / "state"
    conn = db.connect(state_dir / "bellwether.db")
    limits = limits or make_limits(
        mode="live" if live else "dry_run",
        start_balance=100000 if live else None,
    )
    bot = Bot(
        limits=limits,
        contracts=mapped,
        conn=conn,
        rest=rest,
        writer=writer,
        refs=refs,
        limiter=limiter,
        live=live,
        state_dir=state_dir,
        clock=time.clock,
        sleep=time.sleep,
        now=now,
        ladder=ladder,
        **({} if ladder_path is None else {"ladder_path": ladder_path}),
    )
    return LoopRig(bot, sig, kalshi, time, conn, state_dir)
