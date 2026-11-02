"""Test kit for bot/: fake time, a fake httpx transport, a fake reader. No network."""

from __future__ import annotations

import json
import re
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import httpx

from bot import db
from bot.limits import Limits
from bot.orders import OrderPath
from bot.risk import Intent
from bot.sig_writer import SigWriter
from engine.config import MIDTERM_ELECTIONS, ApiKey, SigConfig
from engine.sig_client.errors import SigTransportError
from engine.sig_client.rate_limiter import RateLimiter

KEY = "sk-test-FAKEKEY-0123456789abcdef"
NOW = datetime(2026, 11, 3, 23, 30, 0, tzinfo=UTC)

BASE_LIMITS: dict[str, Any] = {
    "mode": "live",
    "tournament_id": MIDTERM_ELECTIONS.id,
    "scale": Decimal("0.10"),
    "caps": {
        "per_order": 500,
        "per_race": 2000,
        "total_committed": 10000,
        "min_cash_reserve": 90000,
        "loss_stop": 2000,
    },
    "start_balance": None,
    "preexisting_committed": 0,
    "max_writes_per_min": 20,
    "order_expiry_seconds": 120,
    "no_orders_enabled": False,
    "no_price_convention": "unknown",
    "loop_seconds": 10,
    "strategies": {
        "complement": {"enabled": True, "max_sum_ask": "0.97", "min_sum_bid": "1.03"},
        "reference": {
            "enabled": True,
            "edge": "0.04",
            "edge_election_night": "0.06",
            "election_night_from_utc": "2026-11-03T23:00:00Z",
            "max_ref_spread": "0.04",
            "max_ref_age_s": 30,
            "max_venue_disagreement": "0.03",
        },
    },
}


def make_limits(**overrides: Any) -> Limits:
    raw = json.loads(json.dumps(BASE_LIMITS, default=str))
    for name, value in overrides.items():
        if name in raw["caps"]:
            raw["caps"][name] = value
        else:
            raw[name] = value
    return Limits.model_validate(raw)


def make_intent(**overrides: Any) -> Intent:
    values: dict[str, Any] = {
        "race_key": "senate:PA",
        "sig_market_id": "10",
        "sig_exchange_id": "36",
        "side": "yes",
        "price_side": Decimal("0.435"),
        "quantity": 10,
        "strategy": "reference",
    }
    values.update(overrides)
    return Intent(**values)


class FakeTime:
    def __init__(self) -> None:
        self.t = 1000.0
        self.sleeps: list[float] = []

    def clock(self) -> float:
        return self.t

    async def sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)
        self.t += seconds


@dataclass
class Seen:
    method: str
    path: str
    content: bytes
    headers: dict[str, str]
    at: float


def ok_body(**overrides: Any) -> dict[str, Any]:
    body: dict[str, Any] = {
        "orderId": 1001,
        "exchangeId": "36",
        "open": True,
        "remainingQuantity": 10,
        "action": "buy",
        "side": "yes",
        "price": Decimal("0.435"),
        "quantity": 10,
        "terminalReasonCode": None,
        "quantityTraded": 0,
        "totalCost": 0,
        "fillPrice": None,
        "all": None,
    }
    body.update(overrides)
    return body


def _encode(body: Any) -> str:
    """JSON with every Decimal written as an exact JSON number literal."""

    def mark(value: Any) -> Any:
        if isinstance(value, Decimal):
            return f"__DEC__{value}__"
        if isinstance(value, dict):
            return {k: mark(v) for k, v in value.items()}
        if isinstance(value, list):
            return [mark(v) for v in value]
        return value

    return re.sub(r'"__DEC__([-0-9.]+)__"', lambda m: m.group(1), json.dumps(mark(body)))


def reply(status: int, body: Any = None, headers: dict[str, str] | None = None) -> httpx.Response:
    text = _encode(body) if body is not None else ""
    return httpx.Response(status, content=text.encode(), headers=headers or {})


def error(status: int, code: str, headers: dict[str, str] | None = None) -> httpx.Response:
    return reply(status, {"error": {"code": code, "message": "x"}}, headers)


Step = httpx.Response | Exception | Callable[[httpx.Request], httpx.Response]


class FakeSig:
    """A scripted httpx transport. Each request consumes the next step."""

    def __init__(self, time: FakeTime, steps: list[Step] | None = None) -> None:
        self.time = time
        self.steps = list(steps or [])
        self.seen: list[Seen] = []

    def add(self, *steps: Step) -> None:
        self.steps.extend(steps)

    def handler(self, request: httpx.Request) -> httpx.Response:
        self.seen.append(
            Seen(
                request.method,
                request.url.path.removeprefix("/api/v1"),
                request.content,
                dict(request.headers),
                self.time.t,
            )
        )
        if not self.steps:
            raise AssertionError(f"unexpected request {request.method} {request.url.path}")
        step = self.steps.pop(0)
        if isinstance(step, Exception):
            raise step
        if callable(step) and not isinstance(step, httpx.Response):
            return step(request)
        return step

    def transport(self) -> httpx.MockTransport:
        return httpx.MockTransport(self.handler)

    def posts(self) -> list[Seen]:
        return [s for s in self.seen if s.method == "POST" and s.path == "/orders"]


@dataclass
class FakeReader:
    open_ids: list[int] = field(default_factory=list)
    fills: dict[int, tuple[Decimal, Decimal | None]] = field(default_factory=dict)
    balance: Decimal | None = Decimal(100000)
    fail_balance: bool = False

    async def read_open_orders(self) -> Any:
        return SimpleNamespace(orders=[SimpleNamespace(id=i) for i in self.open_ids])

    async def read_order_fills(self, order_id: int) -> Any:
        qty, avg = self.fills.get(order_id, (Decimal(0), None))
        return SimpleNamespace(total_quantity_filled=qty, avg_fill_price=avg)

    async def read_tournament_balance(self) -> Any:
        if self.fail_balance:
            raise SigTransportError(method="GET", path="/tournaments/x", kind="ConnectError")
        return SimpleNamespace(tournament=SimpleNamespace(myBalance=self.balance))


def sig_config() -> SigConfig:
    return SigConfig(api_key=ApiKey(KEY), tournament=MIDTERM_ELECTIONS)


@dataclass
class Rig:
    path: OrderPath
    sig: FakeSig
    reader: FakeReader
    time: FakeTime
    conn: Any
    state_dir: Path
    writer: SigWriter
    limiter: RateLimiter

    def rows(self) -> list[Any]:
        return self.conn.execute("SELECT * FROM orders ORDER BY created_utc, rowid").fetchall()

    def events(self, kind: str | None = None) -> list[Any]:
        rows = self.conn.execute("SELECT * FROM events ORDER BY id").fetchall()
        return [r for r in rows if kind is None or r["kind"] == kind]

    @property
    def paused(self) -> bool:
        return (self.state_dir / "PAUSED").exists()


def make_rig(
    tmp_path: Path,
    steps: list[Step] | None = None,
    *,
    limits: Limits | None = None,
    live: bool = True,
    reader: FakeReader | None = None,
) -> Rig:
    time = FakeTime()
    sig = FakeSig(time, steps)
    state_dir = tmp_path / "state"
    conn = db.connect(state_dir / "bellwether.db")
    limiter = RateLimiter(clock=time.clock, sleep=time.sleep)
    writer = SigWriter(
        sig_config(), limiter, transport=sig.transport(), clock=time.clock, sleep=time.sleep
    )
    reader = reader or FakeReader()
    keys = iter(f"key-{n}" for n in range(1, 1000))
    path = OrderPath(
        limits=limits or make_limits(),
        conn=conn,
        writer=writer,
        reader=reader,
        limiter=limiter,
        live=live,
        state_dir=state_dir,
        clock=time.clock,
        sleep=time.sleep,
        now=lambda: NOW,
        new_key=lambda: next(keys),
    )
    return Rig(path, sig, reader, time, conn, state_dir, writer, limiter)


def body_of(seen: Seen) -> dict[str, Any]:
    return json.loads(seen.content, parse_float=Decimal)
