"""Wire models: SIG's documented JSON response shapes, kept apart from shared/schemas.py.

Field names match the API exactly. Nothing here converts to Bellwether's internal units
(ticks or milli) or to shared domain objects; that happens in later, separate code.

Parsing rules:
- Responses are decoded with `parse_float=Decimal`, so every JSON number keeps its exact
  decimal value (0.535 stays Decimal("0.535"), never a binary float). Numbers are never
  rounded. Booleans, strings and non-finite values are rejected where a number is
  documented.
- Models are strict: a missing required field, a wrong type, or a value outside a
  documented range is rejected. Unknown extra fields are ignored.
- Nullable fields stay None. Timestamps are kept as the strings SIG sent (the spec gives
  them as plain strings); a missing one is never replaced with the current time.
- Price bounds [0, 1] are applied only where the spec calls the price YES-normalized or
  [0, 1]. Order.priceLimit has no documented range or denomination, so it is unbounded.

Each model names the OpenAPI JSON pointer it mirrors (docs/api/openapi.json).
"""

from __future__ import annotations

import re
from decimal import Decimal
from typing import Annotated, Literal

from pydantic import (
    AfterValidator,
    BaseModel,
    BeforeValidator,
    ConfigDict,
    Field,
    field_validator,
)
from pydantic_core import PydanticCustomError


def _json_number(value: object) -> Decimal:
    # bool is a subclass of int, so test it first.
    if isinstance(value, bool):
        raise PydanticCustomError("json_number", "expected a JSON number, got a boolean")
    if isinstance(value, int):
        return Decimal(value)
    if isinstance(value, Decimal):
        if not value.is_finite():
            raise PydanticCustomError("json_number", "expected a finite JSON number")
        return value
    raise PydanticCustomError("json_number", "expected a JSON number")


WireNumber = Annotated[Decimal, BeforeValidator(_json_number)]
"""Any finite JSON number, exactly as sent."""

WirePrice = Annotated[Decimal, BeforeValidator(_json_number), Field(ge=0, le=1)]
"""A price the spec documents as YES-normalized / within [0, 1], exactly as sent."""

WirePositiveNumber = Annotated[Decimal, BeforeValidator(_json_number), Field(gt=0)]
"""A finite JSON number strictly above 0, exactly as sent (fractions allowed)."""


def _json_integer(value: object) -> Decimal:
    # JSON Schema's "integer" is any number with no fractional part (100 and 100.0).
    number = _json_number(value)
    if number != number.to_integral_value():
        raise PydanticCustomError("json_integer", "expected an integral JSON number")
    return number


WireNonNegativeInteger = Annotated[Decimal, BeforeValidator(_json_integer), Field(ge=0)]
"""An integral JSON number of at least 0, exactly as sent (kept as a Decimal)."""


def _json_true(value: object) -> bool:
    # Only the JSON literal `true` (Python True). Literal[True] alone would accept 1.
    if value is not True:
        raise PydanticCustomError("json_true", "expected the JSON boolean true")
    return value


JsonTrue = Annotated[bool, BeforeValidator(_json_true)]


class WireModel(BaseModel):
    model_config = ConfigDict(strict=True, frozen=True, extra="ignore")


# --- GET /tournaments/{slug} ------------------------------------------------------------
# #/paths/~1tournaments~1{slug}/get/responses/200/content/application~1json/schema


class Tournament(WireModel):
    id: str
    slug: str
    name: str
    description: str | None
    status: str
    startDate: str | None
    endDate: str | None
    initialBalance: WireNumber
    currencyName: str
    memberCount: int
    marketCount: int
    myBalance: WireNumber
    isPendingEnrolment: bool


# --- GET /tournaments/{slug}/markets ----------------------------------------------------
# #/paths/~1tournaments~1{slug}~1markets/get/responses/200/content/application~1json/schema
# Items: #/components/schemas/TournamentMarket


class TournamentMarketExchange(WireModel):
    id: str
    option: str | None
    latestPrice: WirePrice | None
    initialPrice: WirePrice | None


class MarketCreator(WireModel):
    id: str
    username: str | None


class TournamentMarket(WireModel):
    id: str
    title: str
    status: Literal["open", "closed", "settled"]
    createdAt: str | None
    settlementDate: str | None
    settledWith: str | None
    settledOn: str | None
    categories: list[str]
    isComposite: bool
    isMultiOutcome: bool
    exchanges: list[TournamentMarketExchange]
    creator: MarketCreator | None


class TournamentMarketsPagination(WireModel):
    total: int
    limit: int
    hasMore: bool
    nextCursor: str | None


class TournamentMarketsPage(WireModel):
    data: list[TournamentMarket]
    pagination: TournamentMarketsPagination


# --- GET /exchanges/prices --------------------------------------------------------------
# #/paths/~1exchanges~1prices/get/responses/200/content/application~1json/schema
# Items: #/components/schemas/BulkExchangePrice


class BulkExchangePrice(WireModel):
    exchangeId: str
    marketId: str
    option: str | None
    latestPrice: WirePrice | None
    bestBid: WirePrice | None
    bestAsk: WirePrice | None
    spread: WireNumber | None


class BulkExchangePrices(WireModel):
    data: list[BulkExchangePrice]
    missingIds: list[str]


# --- Orderbooks -------------------------------------------------------------------------
# #/components/schemas/BookVersion: nullable; null when read from the database.


class BookVersion(WireModel):
    sequence: int
    at: str


class OrderbookLevel(WireModel):
    """#/components/schemas/ExchangeOrderbookLevel and #/components/schemas/OrderbookLevel."""

    price: WirePrice
    quantity: WireNumber


# GET /exchanges/{id}/orderbook
# #/paths/~1exchanges~1{id}~1orderbook/get/responses/200/content/application~1json/schema


class ExchangeOrderbook(WireModel):
    exchangeId: str
    marketId: str
    asOf: BookVersion | None
    depth: int
    bids: list[OrderbookLevel]
    asks: list[OrderbookLevel]
    bestBid: WirePrice | None
    bestAsk: WirePrice | None
    spread: WireNumber | None


# GET /markets/{id}/orderbook
# #/paths/~1markets~1{id}~1orderbook/get/responses/200/content/application~1json/schema
# = #/components/schemas/CombinedMarketOrderbook + contexts[MarketOrderbookContext]


class CombinedOrderbookExchange(WireModel):
    """#/components/schemas/CombinedOrderbookExchange."""

    exchangeId: str
    option: str | None
    asOf: BookVersion | None
    impliedProbability: WirePrice | None
    bids: list[OrderbookLevel]
    asks: list[OrderbookLevel]


class CombinedMarketOrderbook(WireModel):
    """#/components/schemas/CombinedMarketOrderbook."""

    exchanges: list[CombinedOrderbookExchange]
    overround: WireNumber | None
    hasArbitrageOpportunity: bool


class MarketTournamentContext(WireModel):
    """#/components/schemas/MarketTournamentContext."""

    id: str
    slug: str
    name: str
    currencyName: str
    isOngoingPlay: bool


class MarketOrderbookContext(WireModel):
    """#/components/schemas/MarketOrderbookContext (with MarketReadContextDescriptor)."""

    type: Literal["public", "tournament"]
    tournament: MarketTournamentContext | None
    orderbook: CombinedMarketOrderbook


class MarketOrderbook(CombinedMarketOrderbook):
    """The client checks that `contexts` holds exactly the configured tournament.

    Read the book from `tournament_orderbook()`. The top-level fields are SIG's legacy
    mirror of one context; they are kept as sent but not relied on.
    """

    contexts: list[MarketOrderbookContext]

    def tournament_orderbook(self) -> CombinedMarketOrderbook:
        return self.contexts[0].orderbook


# --- GET /orders ------------------------------------------------------------------------
# #/paths/~1orders/get/responses/200/content/application~1json/schema
# Items: #/components/schemas/Order; pagination: #/components/schemas/OrderPagination


class Order(WireModel):
    id: int
    exchangeId: str
    side: Literal["yes", "no"]
    action: Literal["buy", "sell"]
    # "Always positive" (#/components/schemas/Order/properties/quantity); may be fractional.
    quantity: WirePositiveNumber
    priceLimit: WireNumber | None
    open: bool
    createdAt: str
    expirationDate: str | None


class OrderPagination(WireModel):
    limit: int
    hasMore: bool
    nextCursor: str | None


class OrderCoverage(WireModel):
    complete: JsonTrue  # documented as enum [true]
    projectedThroughSequence: int | None


class OrdersPage(WireModel):
    data: list[Order]
    pagination: OrderPagination
    # Documented as optional (not in `required`) but not nullable: None here means only
    # that SIG omitted it. An explicit null is rejected; the default is not validated.
    coverage: OrderCoverage | None = None

    @field_validator("coverage", mode="before")
    @classmethod
    def _coverage_not_null(cls, value: object) -> object:
        if value is None:
            raise PydanticCustomError("coverage_null", "coverage may be omitted but not null")
        return value


# --- GET /tournaments/{slug}/portfolio/positions ----------------------------------------
# #/paths/~1tournaments~1{slug}~1portfolio~1positions/get/responses/200/content/application~1json/schema
# Items: #/components/schemas/TournamentPortfolioPosition
#
# No price, cost or value field here documents its denomination, so none is bounded to
# [0, 1] and none is converted; every number is kept exactly as sent. The units and
# open questions of each field are in account_reads.NUMERIC_FIELD_STATUS.


class PositionLot(WireModel):
    """One FIFO lot. `side` is a free string in the spec (not an enum) and is kept as sent;
    `quantity` has no documented sign rule."""

    lotId: str
    side: str
    quantity: WireNumber
    entryPrice: WireNumber
    openedAt: str


class TournamentPosition(WireModel):
    """One position row. `quantity`: positive = YES shares, negative = NO shares.

    `currentPrice` is documented as null only for zero-quantity rows; a row that breaks
    that rule is kept as sent (a consistency check belongs to the account snapshot, A3).
    """

    exchangeId: str
    marketId: str
    marketTitle: str
    option: str | None
    settled: bool
    quantity: WireNumber
    avgCost: WireNumber
    currentPrice: WireNumber | None
    marketValue: WireNumber
    costBasis: WireNumber
    unrealizedPnl: WireNumber
    unrealizedPnlPct: WireNumber
    moneyEarned: WireNumber
    lots: list[PositionLot]


class PositionsSummary(WireModel):
    totalMarketValue: WireNumber
    totalCostBasis: WireNumber
    totalUnrealizedPnl: WireNumber


class TournamentPositions(WireModel):
    positions: list[TournamentPosition]
    summary: PositionsSummary


# The documented 409 body: #/components/schemas/MissingTournamentValuation.
# [documented] code is the enum ["CONFLICT"]; message, details and missingValuations are
# required; each item requires exchangeId, marketId, marketTitle and settlementOption.
# [client policy] the list must be non-empty and both ids must be ASCII digit strings,
# as the client requires of every id it sends. Titles and settlement labels are checked
# for type only and then dropped by the client.


_DIGITS = re.compile(r"[0-9]+")  # applied with fullmatch(), so a final "\n" is rejected


def _digit_string(value: str) -> str:
    if not _DIGITS.fullmatch(value):
        raise PydanticCustomError("digit_string", "expected a string of ASCII digits")
    return value


DigitString = Annotated[str, AfterValidator(_digit_string)]


class MissingValuationItem(WireModel):
    exchangeId: DigitString
    marketId: DigitString
    marketTitle: str
    settlementOption: str | None


class MissingValuationDetails(WireModel):
    missingValuations: list[MissingValuationItem] = Field(min_length=1)


class MissingValuationError(WireModel):
    code: Literal["CONFLICT"]
    message: str
    details: MissingValuationDetails


class MissingTournamentValuation(WireModel):
    error: MissingValuationError


# --- GET /markets/{id} ------------------------------------------------------------------
# #/paths/~1markets~1{id}/get/responses/200/content/application~1json/schema
# = #/components/schemas/Market


class MarketExchange(WireModel):
    """#/components/schemas/Exchange.

    `latestPrice` is documented as the latest YES trade price in the resolved context
    (null: no trade in that context; never the global price), so it is bounded to [0, 1].
    `initialPrice` has no documented range or denomination and is left unbounded.
    """

    id: str
    option: str | None
    latestPrice: WirePrice | None
    initialPrice: WireNumber | None


class MarketContextExchange(WireModel):
    """#/components/schemas/MarketContextExchange: the latest YES price in one context."""

    id: str
    latestPrice: WirePrice | None


class MarketPricingContext(WireModel):
    """#/components/schemas/MarketPricingContext (with MarketReadContextDescriptor)."""

    type: Literal["public", "tournament"]
    tournament: MarketTournamentContext | None
    status: Literal["open", "closed", "settled"]
    settledWith: str | None
    settledOn: str | None
    exchanges: list[MarketContextExchange]


class Market(WireModel):
    """The client checks that `contexts` holds exactly the configured tournament and that
    the top-level scope fields agree with it; read them from `tournament_context()`."""

    id: str
    title: str
    thumbnailUrl: str | None
    status: Literal["open", "closed", "settled"]
    createdAt: str | None
    settlementDate: str | None
    settledWith: str | None
    settledOn: str | None
    categories: list[str]
    isComposite: bool
    isMultiOutcome: bool
    exchanges: list[MarketExchange]
    contexts: list[MarketPricingContext]
    creator: MarketCreator | None

    def tournament_context(self) -> MarketPricingContext:
        return self.contexts[0]


# --- GET /exchanges/{id}/trades ---------------------------------------------------------
# #/paths/~1exchanges~1{id}~1trades/get/responses/200/content/application~1json/schema
# Items: #/components/schemas/ExchangeTrade


class ExchangeTrade(WireModel):
    """One fill. `price` is the YES-normalized fill price (null allowed), `size` the
    absolute share count (an integer), `volume` an undocumented-unit notional column."""

    id: str
    createdAt: str
    price: WirePrice | None
    size: WireNonNegativeInteger
    side: Literal["YES", "NO"]
    volume: WireNumber | None


class ExchangeTradesPagination(WireModel):
    limit: int
    hasMore: bool
    nextCursor: str | None


class TradeCoverage(WireModel):
    """Reporting projection coverage metadata, exactly as sent.

    `complete` is typed boolean and described as always true (here it is not an enum, as
    it is for orders), so the model keeps the value as sent; the client rejects false.
    `projectedThroughSequence` is the latest trading-engine sequence the reporting
    projection reflects, or null when no checkpoint exists. It says nothing about whether
    the projection has caught up with the engine.
    """

    complete: bool
    projectedThroughSequence: int | None


class ExchangeTradesPage(WireModel):
    exchangeId: str
    marketId: str
    from_: str = Field(alias="from")  # `from` is a Python keyword
    to: str
    data: list[ExchangeTrade]
    pagination: ExchangeTradesPagination
    # Documented as optional (not in `required`) but not nullable: None here means only
    # that SIG omitted it. An explicit null is rejected; the default is not validated.
    coverage: TradeCoverage | None = None

    @field_validator("coverage", mode="before")
    @classmethod
    def _coverage_not_null(cls, value: object) -> object:
        if value is None:
            raise PydanticCustomError("coverage_null", "coverage may be omitted but not null")
        return value


# --- A2-B: further tournament account reads ----------------------------------------------
# None of the price, cost, value or quantity fields below documents a denomination or a
# range, so none is bounded and none is converted; every number is kept exactly as sent.
# Their units and open questions are in account_reads.NUMERIC_FIELD_STATUS.


class TournamentPagination(WireModel):
    """#/components/schemas/TournamentPagination."""

    limit: int
    hasMore: bool
    nextCursor: str | None


# GET /tournaments/{slug}/portfolio/fills
# #/paths/~1tournaments~1{slug}~1portfolio~1fills/get/responses/200/content/application~1json/schema
# Items: #/components/schemas/Fill


class TournamentFill(WireModel):
    """One fill (#/components/schemas/Fill). `quantity` is outcome-signed (negative: NO)."""

    id: int
    orderId: int | None
    exchangeId: str
    marketId: str
    price: WireNumber | None
    quantity: WireNumber
    side: Literal["yes", "no"]
    filledAt: str


class FillsCoverage(WireModel):
    """Projection coverage on the fills read: `complete` is the enum [true]."""

    complete: JsonTrue
    projectedThroughSequence: int | None


class TournamentFillsPage(WireModel):
    data: list[TournamentFill]
    pagination: TournamentPagination
    # Documented as optional (not in `required`) but not nullable: None here means only
    # that SIG omitted it. An explicit null is rejected; the default is not validated.
    coverage: FillsCoverage | None = None

    @field_validator("coverage", mode="before")
    @classmethod
    def _coverage_not_null(cls, value: object) -> object:
        if value is None:
            raise PydanticCustomError("coverage_null", "coverage may be omitted but not null")
        return value


# GET /tournaments/{slug}/portfolio/pnl
# #/paths/~1tournaments~1{slug}~1portfolio~1pnl/get/responses/200/content/application~1json/schema
# Its 409 body is MissingTournamentValuation, as for positions.

PnlPeriod = Literal["day", "week", "month", "quarter", "year", "all"]


class TournamentPnl(WireModel):
    period: PnlPeriod
    periodStart: str | None
    periodEnd: str
    periodPnl: WireNumber | None
    unrealizedPnl: WireNumber
    totalAccountValue: WireNumber
    totalHoldingsValue: WireNumber
    totalCostBasis: WireNumber
    roi: WireNumber | None
    sharpe: WireNumber | None


# GET /tournaments/{slug}/portfolio/settlements
# #/paths/~1tournaments~1{slug}~1portfolio~1settlements/get/responses/200/content/application~1json/schema
# Items: #/components/schemas/TournamentSettlementEntry


class TournamentSettlementEntry(WireModel):
    id: str
    type: Literal["win", "loss", "refund", "correction"] | None
    createdAt: str | None
    marketId: str | None
    marketTitle: str | None
    exchangeId: str | None
    settlementOption: str | None
    settlementOutcome: str | None
    shares: WireNumber | None
    avgEntryPrice: WireNumber | None
    costBasis: WireNumber | None
    payout: WireNumber
    realizedPnl: WireNumber | None


class EconomicCommandCoverage(WireModel):
    """#/components/schemas/TournamentEconomicCommandCoverage: `complete` only.

    Documented as false when SIG's economic-command history walk hit its hard 20-page
    budget, so a page can legitimately be incomplete; the value is kept as sent. There is
    no projection checkpoint here: any other field (a `projectedThroughSequence`
    included) is ignored as unknown, never kept.
    """

    complete: bool


class TournamentSettlementsPage(WireModel):
    data: list[TournamentSettlementEntry]
    pagination: TournamentPagination
    coverage: EconomicCommandCoverage  # required, not nullable


# GET /orders/{id}/fills
# #/paths/~1orders~1{id}~1fills/get/responses/200/content/application~1json/schema


class OrderFill(WireModel):
    """One fill of one order. `quantity` is outcome-signed (negative: NO)."""

    id: int
    price: WireNumber | None
    quantity: WireNumber
    side: Literal["yes", "no"]
    filledAt: str


class OrderFillsCoverage(WireModel):
    complete: JsonTrue
    projectedThroughSequence: int | None


class OrderFillsPage(WireModel):
    """One page of one order's fills.

    `tournamentId` is the order's echoed context (null for global orders); the client
    checks it. `totalQuantityFilled` and `avgFillPrice` are documented as exact lifetime
    totals for the whole order, independent of the page.
    """

    orderId: int
    exchangeId: str
    tournamentId: str | None
    data: list[OrderFill]
    pagination: OrderPagination
    coverage: OrderFillsCoverage  # required here, not nullable
    totalQuantityFilled: WireNumber
    avgFillPrice: WireNumber | None
