"""Shared contracts between the engine and the dashboard.

Defined here once (Pydantic v2) and exported to `shared/schemas.json` for the
dashboard via `python -m shared.export_schema`. Changes need Arnav's approval.

Units:
- Prices on SIG's grid are integer ticks: price x 200, so 0.005 -> 1 and 0.995 -> 199.
  Convert only at the API boundary with `price_to_ticks` / `ticks_to_price`.
- Quotes (from every venue) are integer milli: price x 1000, so 0.537 -> 537.
  Polymarket moves in 0.001 steps, finer than SIG's tick, so quotes are not rounded.
  Fair values and overrides are also in milli (as floats), so everything compared
  against SIG shares one unit. A SIG tick is exactly 5 milli.
- Every internal price is YES-denominated, whatever the side: quotes, fair values,
  overrides, OrderIntent.price and Fill.price. A NO share is worth `1000 - milli` or
  `200 - ticks`.
- These are Bellwether's internal representations, not SIG's wire format. Translating
  to and from API requests and responses (including any NO-side price complement
  the API needs) belongs at the API boundary; it is not implemented here.
- Typed float fields, and the float values of typed float dicts, reject NaN and
  +/-infinity. Untyped contents (EngineMessage.data) are not checked.
- All timestamps are timezone-aware and normalised to UTC.

Serialization:
- `model_dump()` / `model_dump_json()` include computed fields such as Quote.mid.
  Use this for the dashboard. It does not validate back into the model.
- `model_dump(round_trip=True)` / `model_dump_json(round_trip=True)` omit computed
  fields. Use this for storage and replay; it validates back into an equal model.
"""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal
from enum import StrEnum
from typing import Annotated, Any, Literal

from pydantic import AfterValidator, AwareDatetime, BaseModel, ConfigDict, Field, computed_field

TICKS_PER_UNIT = 200
MILLI_PER_UNIT = 1000
MIN_ORDER_TICK = 1  # 0.005
MAX_ORDER_TICK = 199  # 0.995

# From docs/api/openapi.json, components.schemas.OrderInput / SingleOrderInput.
MAX_ORDER_QTY = 2_147_483_647  # quantity: integer, exclusiveMinimum 0, maximum 2147483647
MAX_IDEMPOTENCY_KEY_LEN = 255  # idempotencyKey: minLength 1, maxLength 255
# exchangeId: pattern ^\d+$. Spelled [0-9] because the spec's \d is ASCII-only
# (ECMAScript), whereas Pydantic's \d also matches other Unicode digits.
EXCHANGE_ID_PATTERN = r"^[0-9]+$"


def _to_utc(value: datetime) -> datetime:
    return value.astimezone(UTC)


UtcDatetime = Annotated[AwareDatetime, AfterValidator(_to_utc)]
"""A timezone-aware datetime, stored in UTC. Naive datetimes are rejected."""

Tick = Annotated[int, Field(ge=0, le=TICKS_PER_UNIT, strict=True)]
"""A YES price on the 0.005 grid, in ticks (0-200)."""

OrderTick = Annotated[int, Field(ge=MIN_ORDER_TICK, le=MAX_ORDER_TICK, strict=True)]
"""A limit price we may send: 1-199 ticks. 0 and 200 would encode market orders."""

Milli = Annotated[int, Field(ge=0, le=MILLI_PER_UNIT, strict=True)]
"""A YES quote price on the 0.001 grid, in milli (0-1000)."""

PositiveInt = Annotated[int, Field(gt=0, strict=True)]
"""A whole number above zero. Strict: rejects bools, numeric strings and floats."""

TICKS_NOTE = "Integer ticks: price x 200, so 0.005 -> 1, 0.5 -> 100, 0.995 -> 199."
MILLI_NOTE = "Milli: price x 1000, so 0.537 -> 537."


def _scale(price: Decimal | str | float, units: int, step: str) -> int:
    scaled = Decimal(str(price)) * units
    if not scaled.is_finite():
        raise ValueError(f"price {price} is not finite")
    if scaled != scaled.to_integral_value():
        raise ValueError(f"price {price} is not on the {step} step")
    value = int(scaled)
    if not 0 <= value <= units:
        raise ValueError(f"price {price} is outside [0, 1]")
    return value


def price_to_ticks(price: Decimal | str | float) -> int:
    """Convert a decimal price to ticks, refusing anything off the 0.005 grid."""
    return _scale(price, TICKS_PER_UNIT, "0.005")


def ticks_to_price(ticks: int) -> Decimal:
    """Convert ticks back to an exact decimal price for the API."""
    return Decimal(ticks) / TICKS_PER_UNIT


def price_to_milli(price: Decimal | str | float) -> int:
    """Convert a decimal quote price to milli, refusing anything off the 0.001 grid."""
    return _scale(price, MILLI_PER_UNIT, "0.001")


class _Model(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid", allow_inf_nan=False)


class Side(StrEnum):
    YES = "yes"
    NO = "no"


class Action(StrEnum):
    BUY = "buy"
    SELL = "sell"


class Lean(StrEnum):
    D = "D"
    R = "R"
    NONE = "none"


class QuoteSource(StrEnum):
    SIG = "sig"
    POLYMARKET = "polymarket"
    KALSHI = "kalshi"


class Mode(StrEnum):
    MANUAL = "manual"
    ELECTION = "election"


class Market(_Model):
    """One SIG market (one candidate's YES/NO contract within a race)."""

    sig_market_id: str
    title: str
    exchanges: list[str] = Field(description="SIG exchange ids belonging to this market")
    is_composite: bool
    resolution_text: str | None = Field(None, description="Copied from the market's Info tab")
    settlement_date: UtcDatetime | None = None
    lean: Lean = Lean.NONE


class MarketMapping(_Model):
    """Links one SIG exchange to the matching Polymarket and Kalshi contracts."""

    sig_exchange_id: str
    polymarket_token_id: str | None = None
    kalshi_ticker: str | None = None
    match_confidence: float = Field(ge=0.0, le=1.0)
    mismatch_notes: str = ""


class Quote(_Model):
    """Top of book from one venue, YES-denominated, in milli (price x 1000).

    `mid` is computed from bid and ask, never accepted as input. A plain dump
    (for the dashboard) includes it; a `round_trip=True` dump (for storage and
    replay) omits it so the record validates back into a Quote.
    """

    source: QuoteSource
    exchange_id: str
    bid: Milli | None = Field(
        description=f"Best bid, YES-denominated. {MILLI_NOTE} Integer 0-1000."
    )
    ask: Milli | None = Field(
        description=f"Best ask, YES-denominated. {MILLI_NOTE} Integer 0-1000."
    )
    ts: UtcDatetime

    @computed_field(  # type: ignore[prop-decorator]
        description=(
            f"Computed (bid + ask) / 2, YES-denominated. {MILLI_NOTE} May fall on a half "
            "milli. Output only: omitted by round_trip dumps and rejected as input."
        )
    )
    @property
    def mid(self) -> float | None:
        if self.bid is None or self.ask is None:
            return None
        return (self.bid + self.ask) / 2


class FairValue(_Model):
    """Our estimate of one contract's YES value."""

    exchange_id: str
    value: float = Field(
        ge=0,
        le=MILLI_PER_UNIT,
        description=f"Fair value, YES-denominated. {MILLI_NOTE} Float 0-1000; may be off-grid.",
    )
    inputs: dict[str, float] = Field(description="Weight given to each source; finite floats")
    model_version: str
    ts: UtcDatetime


class Signal(_Model):
    """A strategy's suggestion. Logged whether or not it is traded."""

    exchange_id: str
    strategy: str
    side: Side
    edge_cents: float
    size: PositiveInt
    reason: str = Field(min_length=1, description="Human-readable; shown on the dashboard")
    ts: UtcDatetime


class OrderIntent(_Model):
    """An order we want to place. Must pass risk.check() before it is sent.

    `price` is the internal YES-denominated limit price, for YES and NO orders alike.
    It is not the API request's `price`: the future SIG client must translate it at
    the boundary, including any NO-side complement the API requires (unresolved).

    `price` is restricted to 1-199 ticks, so this model cannot carry a market-order
    price (0, 200 or none). That alone does not guarantee limit-only execution; the
    order-sending code must still send a limit price derived from it.

    qty, idempotency_key and exchange_id follow the API's OrderInput constraints.
    """

    idempotency_key: str = Field(
        min_length=1,
        max_length=MAX_IDEMPOTENCY_KEY_LEN,
        description="Saved before sending; reused on retry. 1-255 characters.",
    )
    exchange_id: str = Field(
        pattern=EXCHANGE_ID_PATTERN, description="SIG exchange id: ASCII digits only."
    )
    side: Side
    action: Action
    qty: PositiveInt = Field(le=MAX_ORDER_QTY, description="Whole shares, 1-2147483647.")
    price: OrderTick = Field(
        description=(
            f"Internal limit price, YES-denominated for both sides. {TICKS_NOTE} "
            "Integer 1-199. Not the API wire price; converted at the API boundary."
        )
    )
    reason: str = Field(min_length=1)


class Fill(_Model):
    """Mirrors the API's Fill. `quantity` is signed: negative means NO shares.

    `price` is Bellwether's internal YES-denominated tick price. How REST and
    realtime fill prices map onto it is decided by the future SIG client.
    """

    id: int
    order_id: int | None
    exchange_id: str
    market_id: str
    price: Tick | None = Field(
        description=(
            f"Internal execution price, YES-denominated for both sides. {TICKS_NOTE} "
            "Integer 0-200. Not the API wire price; normalised at the API boundary."
        )
    )
    quantity: float
    side: Side
    filled_at: UtcDatetime


class Position(_Model):
    """Mirrors the API's Position. `quantity` is signed: positive YES, negative NO."""

    exchange_id: str
    market_id: str
    market_title: str
    settled: bool
    quantity: float
    avg_cost: float
    current_price: float | None
    unrealized_pnl: float


class RiskLimits(_Model):
    """Limits for one mode. Values come from config/risk.yaml, never from code."""

    max_race_pct: float = Field(gt=0, le=100)
    max_order_susqies: float = Field(gt=0)
    max_scenario_loss_pct: float = Field(gt=0, le=100)
    fair_value_max_age_s: float = Field(gt=0)
    min_cash_pct: float = Field(ge=0, le=100)


class RiskState(_Model):
    mode: Mode
    limits: RiskLimits
    killed: bool
    kill_reason: str | None = None
    ts: UtcDatetime


MessageType = Literal[
    "quote_update", "fair_value", "signal", "order", "fill", "position", "risk", "log", "health"
]


class EngineMessage(_Model):
    """Envelope for every engine -> dashboard WebSocket message."""

    type: MessageType
    ts: UtcDatetime
    data: dict[str, Any]


class ModeCommand(_Model):
    """Body of POST /mode."""

    mode: Mode


class OverrideCommand(_Model):
    """Body of POST /override. Expires after 15 minutes; still passes risk.check()."""

    exchange_id: str
    value: float = Field(
        ge=0,
        le=MILLI_PER_UNIT,
        description=f"Fair value set by hand, YES-denominated. {MILLI_NOTE} Float 0-1000.",
    )


EXPORTED_MODELS: tuple[type[BaseModel], ...] = (
    Market,
    MarketMapping,
    Quote,
    FairValue,
    Signal,
    OrderIntent,
    Fill,
    Position,
    RiskLimits,
    RiskState,
    EngineMessage,
    ModeCommand,
    OverrideCommand,
)
