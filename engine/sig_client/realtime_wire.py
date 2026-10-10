"""Wire models for SIG Realtime tournament market topics (S1-T3-A).

Source: the "Realtime (WebSocket) feeds" section of #/info/description and the
description of POST /realtime/token in docs/api/openapi.json. The spec gives these
payloads in prose, not as component schemas, so each field below cites what the prose
states. Anything the prose leaves open is accepted only in the forms it allows and is
marked [client policy].

The same parsing rules as wire.py apply: JSON numbers stay exact Decimals (a binary float
anywhere a number is expected is rejected, because it means the payload was decoded
lossily), timestamps stay the strings SIG sent, and unknown extra fields are ignored.
"""

from __future__ import annotations

import calendar
import itertools
import re
from datetime import UTC, datetime
from decimal import Decimal
from typing import Annotated

from pydantic import BeforeValidator, Field, model_validator
from pydantic_core import PydanticCustomError

from engine.sig_client.wire import BookVersion, OrderbookLevel, WireModel, WireNumber, WirePrice

# Every pattern is applied with fullmatch(). ASCII digits only: \d also matches other
# Unicode digits.
_DIGITS = re.compile(r"[0-9]+")
_TIMESTAMP = re.compile(
    r"([0-9]{4})-([0-9]{2})-([0-9]{2})T([0-9]{2}):([0-9]{2}):([0-9]{2})"
    r"(?:\.([0-9]{1,12}))?(Z|[+-][0-9]{2}:[0-9]{2})"
)


def parse_instant(text: str) -> Decimal:
    """An ISO-8601 timestamp with an explicit offset, as exact seconds since the epoch.

    SIG's book clocks carry seven fractional digits (recorded: "...51.1493955+00:00"),
    more than `datetime` holds, so the fraction is kept as a Decimal and two instants
    compare exactly. Raises ValueError for anything else, including a missing offset.
    """
    match = _TIMESTAMP.fullmatch(text) if isinstance(text, str) else None
    if match is None:
        raise ValueError("not an ISO-8601 timestamp with an offset")
    year, month, day, hour, minute, second = (int(match.group(i)) for i in range(1, 7))
    fraction, offset = match.group(7), match.group(8)
    # Validates every field's range; the offset is applied below.
    moment = datetime(year, month, day, hour, minute, second, tzinfo=UTC)
    offset_s = 0
    if offset != "Z":
        hours, minutes = int(offset[1:3]), int(offset[4:6])
        if hours > 23 or minutes > 59:
            raise ValueError("timestamp offset out of range")
        offset_s = (hours * 3600 + minutes * 60) * (1 if offset[0] == "+" else -1)
    whole = calendar.timegm(moment.timetuple()) - offset_s
    return Decimal(whole) + (Decimal(f"0.{fraction}") if fraction else Decimal(0))


def epoch_seconds(moment: datetime) -> Decimal:
    """An aware datetime as exact seconds since the epoch."""
    if moment.tzinfo is None or moment.utcoffset() is None:
        raise ValueError("wall clock must return a timezone-aware datetime")
    whole = calendar.timegm(moment.utctimetuple())
    return Decimal(whole) + Decimal(moment.microsecond) / Decimal(1_000_000)


def _wire_id(value: object) -> object:
    # [client policy] The prose types `exchangeId` as a number in pushed books but gives
    # no type for ids in trades, bookDirty or marketSettled, while REST sends them as
    # digit strings. Both forms are accepted as sent; nothing else is.
    if isinstance(value, bool):
        raise PydanticCustomError("wire_id", "expected an id, got a boolean")
    if isinstance(value, int):
        if value < 0:
            raise PydanticCustomError("wire_id", "expected a non-negative id")
        return value
    if isinstance(value, str):
        if not _DIGITS.fullmatch(value):
            raise PydanticCustomError("wire_id", "expected a string of ASCII digits")
        return value
    raise PydanticCustomError("wire_id", "expected a digit string or an integer id")


WireId = Annotated[str | int, BeforeValidator(_wire_id)]
"""An id exactly as sent: a digit string or a non-negative integer."""

NonNegativeInt = Annotated[int, Field(ge=0)]


def id_text(value: str | int) -> str:
    """The id as REST spells it (decimal digits), for comparison only."""
    return value if isinstance(value, str) else str(value)


def _sorted_levels(bids: list[OrderbookLevel], asks: list[OrderbookLevel]) -> None:
    # [documented] bids best (highest) first, asks best (lowest) first.
    if any(a.price < b.price for a, b in itertools.pairwise(bids)):
        raise PydanticCustomError("book_order", "bids are not sorted descending")
    if any(a.price > b.price for a, b in itertools.pairwise(asks)):
        raise PydanticCustomError("book_order", "asks are not sorted ascending")


class Delivery(WireModel):
    """`delivery` on every market_batch: the topic's revision window.

    The batch covers the topic's revisions after `previousRevision` through `revision`.
    `sourceSequenceFrom`/`sourceSequenceThrough` are engine provenance, "not a
    topic-local gap counter", so they are deliberately not modelled or used.
    `correlationId` is for support diagnostics; it is kept but never logged.
    """

    revision: NonNegativeInt
    previousRevision: NonNegativeInt
    correlationId: str | None = None

    @model_validator(mode="after")
    def _window(self) -> Delivery:
        # [client policy] a window "after previousRevision through revision" that holds
        # no revision at all is not a valid batch.
        if self.previousRevision >= self.revision:
            raise PydanticCustomError("delivery", "previousRevision must be below revision")
        return self


class PushedBook(WireModel):
    """One entry of `books`: a complete order book, "with exchangeId as a number"."""

    exchangeId: Annotated[int, Field(ge=0)]
    asOf: BookVersion
    nextExpiryAt: str | None
    bids: list[OrderbookLevel]
    asks: list[OrderbookLevel]

    @model_validator(mode="after")
    def _levels(self) -> PushedBook:
        _sorted_levels(self.bids, self.asks)
        return self


class BatchTrade(WireModel):
    """`{ id, sequence, exchangeId, marketId, price, quantity, executedAt, tournamentId? }`.

    `price` is the YES-denominated execution price in [0, 1], exactly as sent. `id` and
    `sequence` are both null when the engine recorded a fill without a match event.
    """

    id: str | None
    sequence: NonNegativeInt | None
    exchangeId: WireId
    marketId: WireId
    price: WirePrice
    quantity: WireNumber
    executedAt: str
    tournamentId: str | None = None

    @model_validator(mode="after")
    def _identity(self) -> BatchTrade:
        # [client policy] the prose nulls `id` and `sequence` together.
        if (self.id is None) != (self.sequence is None):
            raise PydanticCustomError("trade_id", "id and sequence must both be null or set")
        return self


class BookDirty(WireModel):
    """`{ exchangeId, marketId, tournamentId, at }`; also the standalone `book_dirty`."""

    exchangeId: WireId
    marketId: WireId
    tournamentId: str
    at: str


class MarketSettled(WireModel):
    """`{ marketId, tournamentId, settledWith, at }`; also the standalone `market_settled`.

    `settledWith` is the stamped settlement value (an outcome label, or "REFUND").
    """

    marketId: WireId
    tournamentId: str
    settledWith: str
    at: str


class MarketBatch(WireModel):
    """A `market_batch` on `tournament:{tournament_id}:market:{market_id}`.

    A normal batch carries `trades`, `bookDirty` and `marketSettled` (required here, as
    documented). A batch flagged `resyncRequired` keeps only what fits: its `books`, then
    some of its `trades`, and no `bookDirty` or `marketSettled`; the compact form has no
    arrays at all. `books` is absent when the batch names no exchange.
    """

    delivery: Delivery
    resyncRequired: bool = False
    books: list[PushedBook] | None = None
    trades: list[BatchTrade] | None = None
    bookDirty: list[BookDirty] | None = None
    marketSettled: list[MarketSettled] | None = None

    @model_validator(mode="after")
    def _shape(self) -> MarketBatch:
        if not self.resyncRequired and (
            self.trades is None or self.bookDirty is None or self.marketSettled is None
        ):
            raise PydanticCustomError(
                "batch_shape", "trades, bookDirty and marketSettled are required"
            )
        ids = [book.exchangeId for book in self.books or ()]
        if len(set(ids)) != len(ids):
            raise PydanticCustomError("batch_books", "two books for one exchange")
        return self
