"""The order path: the ONLY module that calls SigWriter.post_order (bypass test).

place(intent):
  a. risk.check; a denial writes a 'blocked' decision and sends nothing.
  b. In dry run (anything but live=True with mode 'live'; a probe needs only live=True)
     a 'would_order' decision is written and nothing is sent.
  c. The orders row is inserted as 'journaled' and committed before anything is sent.
     The request bytes are rebuilt from that row, so every retry is byte-identical
     and carries the same idempotency key.
  d. Answers are handled as in the module constants below; every outcome that SIG did
     not confirm ends as 'unknown' with bot/state/PAUSED created.
Placements are serialised by one lock, so no intent is sent while another order's
outcome is being resolved, and none is sent while any order is 'unknown'.

cancel(order_key) and cancel_all(reason) are protective: no risk check, they work while
paused or stopped, they never place or enlarge anything, they write an event and then
refresh statuses from the open-orders read.
"""

from __future__ import annotations

import asyncio
import json
import logging
import sqlite3
import time
import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any, Protocol

from bot import db, risk
from bot.db import STATE_DIR
from bot.limits import Limits
from bot.risk import Intent
from bot.sig_writer import CancelIncomplete, SigWriter, WriteNoResponse, WriteResponse
from engine.sig_client.errors import SigClientError, SigRateBudgetError
from engine.sig_client.rate_limiter import RateLimiter, RequestKind

logger = logging.getLogger(__name__)

STATUS_BACKOFF_S = (2.0, 4.0, 8.0, 16.0)  # 502 ORDER_STATUS_UNKNOWN / 503: 5 tries
MAX_STATUS_TRIES = 5
IN_FLIGHT_WAIT_S = 90.0  # documented REQUEST_IN_FLIGHT lease
MAX_IN_FLIGHT_WAITS = 3
MAX_RATE_LIMITED = 3
DEFAULT_RATE_LIMIT_WAIT_S = 60.0
NO_RESPONSE_RETRIES = 3  # Arnav's decision (b): at most 3 retries within 5 minutes
NO_RESPONSE_WINDOW_S = 300.0
NO_RESPONSE_BACKOFF_S = (10.0, 30.0, 60.0)


class OrderReader(Protocol):
    """The SigRestClient reads the order path uses (a fake in tests)."""

    async def read_open_orders(self) -> Any: ...

    async def read_order_fills(self, order_id: int) -> Any: ...

    async def read_tournament_balance(self) -> Any: ...


@dataclass(frozen=True)
class OrderRecord:
    """The outcome of place(). `key` is None when nothing was journaled."""

    key: str | None
    status: str  # an orders.status, or 'blocked' / 'would_order' when not journaled
    reason: str
    wire_price: str | None = None
    sig_order_id: str | None = None
    quantity_traded: str | None = None
    total_cost: str | None = None
    http_status: int | None = None
    error: str | None = None
    response_json: str | None = None

    @classmethod
    def from_row(cls, row: sqlite3.Row) -> OrderRecord:
        return cls(
            key=row["key"],
            status=row["status"],
            reason=row["reason"] or "",
            wire_price=row["wire_price"],
            sig_order_id=row["sig_order_id"],
            quantity_traded=row["quantity_traded"],
            total_cost=row["total_cost"],
            http_status=row["http_status"],
            error=row["error"],
            response_json=row["response_json"],
        )


class OrderRefused(Exception):
    """An intent that must never reach the wire (raised before journaling)."""


def format_price(price: Decimal) -> str:
    """A grid price as a JSON number literal with at most 3 decimals ("0.435", "0.01")."""
    if not risk.on_grid(price):
        raise OrderRefused("price is off the 0.005 grid or outside 0.005-0.995")
    text = format(price.quantize(Decimal("0.001")).normalize(), "f")
    if Decimal(text) != price or len(text.partition(".")[2]) > 3:
        raise OrderRefused("price cannot be written exactly with 3 decimals")
    return text


def wire_price(intent: Intent, limits: Limits, *, probe: bool) -> Decimal:
    """The request price. YES: price_side. NO: by no_price_convention; probe: raw."""
    if intent.side == "yes" or probe:
        return intent.price_side
    convention = limits.no_price_convention
    if convention == "no_price":
        return intent.price_side
    if convention == "yes_price":
        return 1 - intent.price_side
    raise OrderRefused("NO order refused: no_price_convention is unknown (OQ-W3)")


def request_bytes(row: sqlite3.Row, tournament_id: str) -> bytes:
    """The exact POST /orders body for a journaled row. Same row, same bytes."""
    price = format_price(Decimal(row["wire_price"]))
    parts = [
        ("exchangeId", json.dumps(row["sig_exchange_id"])),
        ("side", json.dumps(row["side"])),
        ("action", json.dumps(row["action"])),
        ("quantity", json.dumps(int(row["quantity"]))),
        ("price", price),
        ("tournamentId", json.dumps(tournament_id)),
        ("expirationDate", json.dumps(row["expires_utc"])),
        ("idempotencyKey", json.dumps(row["key"])),
    ]
    return ("{" + ",".join(f'"{name}":{value}' for name, value in parts) + "}").encode()


def _number(value: object) -> Decimal | None:
    if isinstance(value, bool) or not isinstance(value, (int, Decimal)):
        return None
    out = Decimal(value)
    return out if out.is_finite() else None


class OrderPath:
    def __init__(
        self,
        *,
        limits: Limits,
        conn: sqlite3.Connection,
        writer: SigWriter,
        reader: OrderReader,
        limiter: RateLimiter,
        live: bool = False,
        state_dir: Path = STATE_DIR,
        balance: Callable[[], Awaitable[Decimal | None]] | None = None,
        clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
        now: Callable[[], datetime] = lambda: datetime.now(UTC),
        new_key: Callable[[], str] = lambda: str(uuid.uuid4()),
    ) -> None:
        self._limits = limits
        self._conn = conn
        self._writer = writer
        self._reader = reader
        self._limiter = limiter
        self._live = live
        self._state_dir = Path(state_dir)
        self._balance = balance or self._read_balance
        self._clock = clock
        self._sleep = sleep
        self._now = now
        self._new_key = new_key
        self._lock = asyncio.Lock()

    def __repr__(self) -> str:
        return f"OrderPath(mode={self._limits.mode!r}, live={self._live})"

    # --- pauses and events ----------------------------------------------------------

    def pause(self, reason: str, kind: str = "pause") -> None:
        """Create bot/state/PAUSED (no new orders until Arnav deletes it)."""
        self._state_dir.mkdir(parents=True, exist_ok=True)
        (self._state_dir / "PAUSED").write_text(f"{db.utc_stamp()} {reason}\n", encoding="utf-8")
        db.insert_event(self._conn, "error", kind, f"PAUSED: {reason}")
        logger.error("paused: %s", reason)

    async def _read_balance(self) -> Decimal | None:
        # myBalance is a candidate tournament cash value (OQ-W11); see the PR notes.
        read = await self._reader.read_tournament_balance()
        return read.tournament.myBalance

    # --- placement ------------------------------------------------------------------

    async def place(
        self, intent: Intent, *, probe: bool = False, expiry_seconds: int | None = None
    ) -> OrderRecord:
        async with self._lock:
            return await self._place(intent, probe=probe, expiry_seconds=expiry_seconds)

    def _decision(self, intent: Intent, verdict: str, reason: str, key: str | None = None) -> None:
        db.insert_decision(
            self._conn,
            race_key=intent.race_key,
            sig_exchange_id=intent.sig_exchange_id,
            strategy=intent.strategy,
            verdict=verdict,
            side=intent.side,
            price_side=str(intent.price_side),
            quantity=intent.quantity if isinstance(intent.quantity, int) else None,
            reason=reason,
            order_key=key,
        )

    async def _place(
        self, intent: Intent, *, probe: bool, expiry_seconds: int | None
    ) -> OrderRecord:
        try:
            balance = await self._balance()
        except SigClientError as exc:
            logger.warning("balance read failed: %s", type(exc).__name__)
            balance = None
        state = risk.build_state(
            self._conn,
            self._limits,
            race_key=intent.race_key,
            balance=balance,
            writes_last_min=self._limiter.used(RequestKind.WRITE),
            state_dir=self._state_dir,
        )
        result = risk.check(intent, state, probe=probe)
        if not result.allowed:
            self._decision(intent, "blocked", result.reason)
            return OrderRecord(key=None, status="blocked", reason=result.reason)
        try:
            price = wire_price(intent, self._limits, probe=probe)
            price_text = format_price(price)
        except OrderRefused as exc:
            self._decision(intent, "blocked", str(exc))
            return OrderRecord(key=None, status="blocked", reason=str(exc))
        assert intent.side == "yes" or probe or self._limits.no_price_convention != "unknown"
        if not (self._live and (self._limits.mode == "live" or probe)):
            reason = f"dry run: {result.reason}"
            self._decision(intent, "would_order", reason)
            return OrderRecord(key=None, status="would_order", reason=reason)

        key = self._new_key()
        now = self._now()
        expiry = expiry_seconds if expiry_seconds is not None else self._limits.order_expiry_seconds
        reason = intent.reason or result.reason
        db.insert_order(
            self._conn,
            {
                "key": key,
                "created_utc": db.utc_stamp(now),
                "updated_utc": db.utc_stamp(now),
                "race_key": intent.race_key,
                "sig_market_id": intent.sig_market_id,
                "sig_exchange_id": intent.sig_exchange_id,
                "side": intent.side,
                "action": "buy",
                "price_side": str(intent.price_side),
                "wire_price": price_text,
                "quantity": intent.quantity,
                "expires_utc": db.utc_stamp(now + timedelta(seconds=expiry)),
                "strategy": intent.strategy,
                "reason": reason,
                "status": "journaled",
            },
        )
        self._decision(intent, "order", reason, key)
        if db.any_unknown(self._conn):  # second guard, right before the first send
            db.update_order(self._conn, key, status="rejected", error="not sent: unknown order")
            return OrderRecord.from_row(db.get_order(self._conn, key))
        db.update_order(self._conn, key, status="sent")
        row = db.get_order(self._conn, key)
        await self._send(row, request_bytes(row, self._writer.tournament_id), intent, probe)
        return OrderRecord.from_row(db.get_order(self._conn, key))

    def _unknown(self, key: str, why: str, **fields: Any) -> None:
        db.update_order(self._conn, key, status="unknown", error=why, **fields)
        db.insert_event(self._conn, "error", "order_unknown", f"order {key}: {why}")
        self.pause(f"order {key} outcome unknown: {why}")

    def _rejected(self, key: str, response: WriteResponse, *, pause: str | None = None) -> None:
        error = response.code or f"HTTP {response.status}"
        db.update_order(
            self._conn, key, status="rejected", http_status=response.status, error=error
        )
        db.insert_event(self._conn, "warn", "order_rejected", f"order {key}: {error}")
        if pause:
            self.pause(f"order {key}: {pause}")

    async def _send(self, row: sqlite3.Row, body: bytes, intent: Intent, probe: bool) -> None:
        key = row["key"]
        status_tries = in_flight = rate_limited = no_response = 0
        first_no_response: float | None = None
        sent_once = False
        while True:
            try:
                response = await self._writer.post_order(body)
            except SigRateBudgetError:
                # Admission refused before sending; earlier attempts may have landed.
                if sent_once:
                    self._unknown(key, "write admission blocked during retries")
                else:
                    db.update_order(self._conn, key, status="rejected", error="rate budget")
                    self.pause(f"order {key}: write admission blocked")
                return
            except WriteNoResponse as exc:
                sent_once = True
                no_response += 1
                if first_no_response is None:
                    first_no_response = self._clock()
                if no_response > NO_RESPONSE_RETRIES:
                    self._unknown(key, f"no response after {no_response} tries ({exc.kind})")
                    return
                wait = NO_RESPONSE_BACKOFF_S[no_response - 1]
                if self._clock() + wait - first_no_response >= NO_RESPONSE_WINDOW_S:
                    self._unknown(key, f"no response within 5 minutes ({exc.kind})")
                    return
                await self._sleep(wait)
                continue
            sent_once = True
            db.update_order(self._conn, key, http_status=response.status)
            status, code = response.status, response.code
            if status == 200:
                self._apply_success(row, response, intent, probe)
                return
            if status in (400, 401, 404):
                self._rejected(key, response)
                return
            if status == 403:
                pause = "terms not acknowledged" if code == "TERMS_NOT_ACKNOWLEDGED" else None
                self._rejected(key, response, pause=pause)
                return
            if status == 409:
                if code != "REQUEST_IN_FLIGHT":
                    # Key reuse or a duplicate command: the earlier request may have been
                    # placed (OQ-W10 W10-d), so the outcome is unknown, never retried.
                    self._unknown(
                        key,
                        f"HTTP 409 {code or '-'}: the earlier request may have been placed",
                        http_status=status,
                    )
                    return
                in_flight += 1
                if in_flight > MAX_IN_FLIGHT_WAITS:
                    self._unknown(key, "still REQUEST_IN_FLIGHT", http_status=status)
                    return
                await self._sleep(IN_FLIGHT_WAIT_S)
                continue
            if status == 429:
                rate_limited += 1
                db.insert_event(self._conn, "warn", "rate_limited", f"order {key}: HTTP 429")
                if response.retry_after_oversized or rate_limited > MAX_RATE_LIMITED:
                    self._unknown(key, "rate limited without a usable retry", http_status=status)
                    return
                self._limiter.impose_cooldown(
                    RequestKind.WRITE, response.retry_after or DEFAULT_RATE_LIMIT_WAIT_S
                )
                continue
            if (status == 502 and code == "ORDER_STATUS_UNKNOWN") or status == 503:
                status_tries += 1
                if status_tries >= MAX_STATUS_TRIES:
                    self._unknown(key, f"HTTP {status} after {status_tries} tries")
                    return
                await self._sleep(STATUS_BACKOFF_S[status_tries - 1])
                continue
            self._unknown(key, f"HTTP {status} {code or '-'}", http_status=status)
            return

    def _apply_success(
        self, row: sqlite3.Row, response: WriteResponse, intent: Intent, probe: bool
    ) -> None:
        key = row["key"]
        body = response.body
        if not isinstance(body, dict):
            self._unknown(key, "200 body unreadable", response_json=response.text)
            return
        is_open = body.get("open")
        traded = _number(body.get("quantityTraded"))
        cost = _number(body.get("totalCost"))
        order_id = body.get("orderId")
        if (
            not isinstance(is_open, bool)
            or traded is None
            or cost is None
            or not (
                order_id is None or (isinstance(order_id, int) and not isinstance(order_id, bool))
            )
        ):
            self._unknown(key, "200 body missing required fields", response_json=response.text)
            return
        quantity = int(row["quantity"])
        if is_open:
            status = "resting"
        elif traded == quantity:
            status = "filled"
        elif traded > 0:
            status = "partial"
        else:
            status = "cancelled"
        terminal = body.get("terminalReasonCode")
        db.update_order(
            self._conn,
            key,
            status=status,
            sig_order_id=None if order_id is None else str(order_id),
            quantity_traded=str(traded),
            total_cost=str(cost),
            response_json=response.text,
            error=terminal if isinstance(terminal, str) else None,
        )
        mismatches = self._mismatches(row, body, traded, intent, probe)
        if mismatches:
            text = "; ".join(mismatches)
            db.insert_event(self._conn, "error", "mismatch", f"order {key}: {text}")
            self.pause(f"mismatch on order {key}: {text}", kind="mismatch")

    @staticmethod
    def _mismatches(
        row: sqlite3.Row, body: dict, traded: Decimal, intent: Intent, probe: bool
    ) -> list[str]:
        out = []
        quantity = int(row["quantity"])
        if traded < 0 or traded > quantity or traded != traded.to_integral_value():
            out.append("quantityTraded is not a whole number within the order")
        if body.get("exchangeId") not in (None, row["sig_exchange_id"]):
            out.append("exchangeId differs")
        if "side" in body and body["side"] != row["side"]:
            out.append("canonical side differs")
        if "action" in body and body["action"] != "buy":
            out.append("canonical action differs")
        if "quantity" in body and _number(body["quantity"]) != quantity:
            out.append("canonical quantity differs")
        if "price" in body:
            canonical = _number(body["price"])
            # The canonical price is side-relative, so it must equal price_side. The
            # probe's NO order is exactly where this is unknown, so it is only recorded.
            if canonical != Decimal(row["price_side"]) and not (probe and row["side"] == "no"):
                out.append("canonical price differs")
        return out

    # --- protective cancellation ----------------------------------------------------

    async def cancel(self, order_key: str) -> None:
        """Cancel one journaled order by its key. No risk check; works while paused."""
        row = db.get_order(self._conn, order_key)
        if row is None or row["sig_order_id"] is None:
            db.insert_event(
                self._conn,
                "warn",
                "reconcile",
                f"cancel {order_key}: no SIG order id known; use cancel_all",
            )
            return
        try:
            response = await self._writer.cancel_order(row["sig_order_id"])
            outcome = f"HTTP {response.status} {response.code or '-'}"
            level = "info" if response.status in (200, 409) else "warn"
        except (CancelIncomplete, WriteNoResponse, SigRateBudgetError) as exc:
            outcome, level = f"not confirmed ({type(exc).__name__})", "error"
        db.insert_event(self._conn, level, "reconcile", f"cancel {order_key}: {outcome}")
        await self.refresh_statuses()

    async def cancel_all(self, reason: str) -> None:
        """Cancel every open order in the configured tournament. Works while paused."""
        try:
            response = await self._writer.cancel_all()
            outcome = f"HTTP {response.status} {response.code or '-'}"
            level = "info" if response.status == 200 else "error"
        except (CancelIncomplete, WriteNoResponse, SigRateBudgetError) as exc:
            outcome, level = f"not confirmed ({type(exc).__name__})", "error"
        db.insert_event(self._conn, level, "stop", f"cancel all ({reason}): {outcome}")
        await self.refresh_statuses()

    async def refresh_statuses(self) -> None:
        """Close journaled resting orders that SIG no longer lists as open.

        Each one's fills are read; on any read failure its status is left unchanged
        (it keeps counting as committed).
        """
        try:
            walk = await self._reader.read_open_orders()
        except SigClientError as exc:
            db.insert_event(
                self._conn, "warn", "reconcile", f"open-orders read failed: {type(exc).__name__}"
            )
            return
        open_ids = {str(order.id) for order in walk.orders}
        closed = 0
        for row in db.orders_with_status(self._conn, "resting"):
            if row["sig_order_id"] is None or row["sig_order_id"] in open_ids:
                continue
            try:
                fills = await self._reader.read_order_fills(int(row["sig_order_id"]))
            except SigClientError as exc:
                db.insert_event(
                    self._conn,
                    "warn",
                    "reconcile",
                    f"fills read for {row['key']} failed: {type(exc).__name__}",
                )
                continue
            traded = Decimal(fills.total_quantity_filled)
            quantity = int(row["quantity"])
            if traded >= quantity:
                status = "filled"
            elif traded > 0:
                status = "partial"
            elif self._now() >= datetime.fromisoformat(row["expires_utc"]):
                status = "expired"
            else:
                status = "cancelled"
            fields: dict[str, Any] = {"status": status, "quantity_traded": str(traded)}
            if fills.avg_fill_price is not None:
                fields["total_cost"] = str(Decimal(fills.avg_fill_price) * traded)
            db.update_order(self._conn, row["key"], **fields)
            closed += 1
        db.insert_event(
            self._conn,
            "info",
            "reconcile",
            f"{len(open_ids)} open order(s) on SIG; {closed} journaled order(s) closed",
        )
