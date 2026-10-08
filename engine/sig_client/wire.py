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

from decimal import Decimal
from typing import Annotated, Literal

from pydantic import BaseModel, BeforeValidator, ConfigDict, Field
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
    quantity: WireNumber
    priceLimit: WireNumber | None
    open: bool
    createdAt: str
    expirationDate: str | None


class OrderPagination(WireModel):
    limit: int
    hasMore: bool
    nextCursor: str | None


class OrderCoverage(WireModel):
    complete: Literal[True]
    projectedThroughSequence: int | None


class OrdersPage(WireModel):
    data: list[Order]
    pagination: OrderPagination
    coverage: OrderCoverage | None = None  # documented as optional (not in `required`)
