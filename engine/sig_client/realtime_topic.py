"""State of one tournament market topic: revisions, versioned books, trades (S1-T3-A).

Pure and synchronous: no I/O, no clock. The feed (realtime.py) passes in every message
and REST snapshot and acts on the outcome. Rules, from #/info/description ("Realtime")
and POST /realtime/token in docs/api/openapi.json, applied to each `market_batch` in
this order:

1. [documented] Check the whole message first. [client policy] A message that is not
   valid JSON, does not match the documented shape, names another market or tournament,
   or carries an unparseable book clock is malformed. Nothing in it is applied. If its
   `delivery` parsed, its revision is still accepted when newer, and a resync is asked
   for (we cannot know what it carried).
2. [documented] Apply `books` by version, even when the batch is a duplicate: keep the
   higher `asOf`, comparing `sequence`, then `at` as an instant. A held book with a null
   `asOf` (a REST book read without an engine version) is replaced by any pushed book.
3. [documented] `resyncRequired` is checked before any array: accept the revision if it
   is newer, then resync. Its partial `trades` are not emitted.
4. [documented] A batch whose `revision` is at or below the last accepted one is a
   duplicate: its arrays are ignored (its books were already applied in step 2).
5. [documented] A batch was missed only when `previousRevision` is greater than the last
   accepted revision: accept this revision, emit nothing from its arrays, and resync.
   A batch that overlaps revisions already accepted (previousRevision below the last
   accepted, revision above it) is accepted; trades are de-duplicated by trade `id`.
6. [documented] Every exchange named by `trades` or `bookDirty` comes with its book.
   [client policy] A named exchange without one is a protocol break: resync.
7. [documented] A trade with a null `sequence` cannot be ordered, and "the client must
   resynchronize that exchange's price and trade tape over REST". None of that batch's
   trades are emitted (as in the spec's first example).
8. [documented] `marketSettled` is an invalidation hint: emit it, then "refetch the
   authoritative market state".

Steps 7 and 8 need REST reads other than the orderbook (the trade tape and the market
itself), which this slice does not make. They are reported as `blockers`: the feed keeps
the market not ready until they are reconciled, and an orderbook resync cannot clear
them.

Delivery revisions are the only gap counter. Trade and book `sequence` values are
engine event numbers ("provenance, not the topic gap counter") and are never compared
with revisions.

REST snapshots ([client policy] where the spec is silent):
- A versioned REST book replaces the held book only when strictly newer, by the same
  rule as pushes.
- A REST book with a null `asOf` cannot be ordered. It replaces the held book (it is the
  authoritative read after a gap) unless a pushed book was applied to that exchange
  after the resync started: a pushed book is read at publication, so it is at least as
  fresh as the gap recovery and carries a version.
- A side holding exactly `depth` levels may be truncated, so the book is not `complete`.
- REST books carry no `nextExpiryAt`, so their order-expiry time is unknown.
- A held book whose known `nextExpiryAt` has passed is out of date even though its
  version is not: order expiry emits no engine event, so the engine version does not
  move. A REST book replaces it when its version is equal or newer (or either version is
  null); an older REST read is ignored, and the market stays not ready.
- Exchanges absent from the snapshot are dropped: REST is authoritative on the set.
"""

from __future__ import annotations

import json
from collections import deque
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from decimal import Decimal
from enum import StrEnum

from pydantic import ValidationError

from engine.sig_client.realtime_wire import (
    BatchTrade,
    BookDirty,
    Delivery,
    MarketBatch,
    MarketSettled,
    PushedBook,
    id_text,
    parse_instant,
)
from engine.sig_client.wire import BookVersion, CombinedOrderbookExchange, OrderbookLevel

DEFAULT_TRADE_ID_MEMORY = 4096

VersionKey = tuple[int, Decimal]


class StaleReason(StrEnum):
    """Why a market's data must be refetched over REST before it is used again."""

    SUBSCRIBED = "subscribed"  # initial subscription or re-subscription after reconnect
    CHANNEL_ERROR = "channel error"
    CHANNEL_TIMED_OUT = "channel timed out"
    CHANNEL_CLOSED = "channel closed"
    SOCKET_ERROR = "socket error"
    SOCKET_CLOSED = "socket closed"
    TOKEN_REFRESH = "token refresh"
    TOKEN_EXPIRED = "token expired"
    REVISION_GAP = "revision gap"
    RESYNC_REQUIRED = "batch flagged resyncRequired"
    NULL_TRADE_SEQUENCE = "trade without a sequence"
    MISSING_BOOK = "named exchange without a book"
    MALFORMED = "malformed message"
    BOOK_DIRTY_EVENT = "standalone book_dirty"
    MARKET_SETTLED = "market settled"
    BOOK_EXPIRY = "resting order expiry"
    STOPPED = "feed stopped"


# Recovery that an orderbook read cannot provide (see steps 7 and 8).
BLOCKING_REASONS = frozenset({StaleReason.NULL_TRADE_SEQUENCE, StaleReason.MARKET_SETTLED})


class BookSource(StrEnum):
    PUSH = "push"
    REST = "rest"


class SnapshotError(ValueError):
    """A REST snapshot could not be applied (bad id, duplicate exchange, bad clock)."""


@dataclass(frozen=True)
class HeldBook:
    """The book held for one exchange. Prices and quantities are exactly as SIG sent.

    `complete` is False for a REST book with a side that may be cut off at `depth`.
    `expiry_known` is False for REST books, which carry no `nextExpiryAt`.
    """

    exchange_id: str
    as_of: BookVersion | None
    bids: tuple[OrderbookLevel, ...]
    asks: tuple[OrderbookLevel, ...]
    source: BookSource
    complete: bool
    next_expiry_at: str | None
    expiry_known: bool
    stamp: int = field(repr=False)
    version_key: VersionKey | None = field(repr=False)
    expiry_epoch: Decimal | None = field(repr=False)


@dataclass
class TopicOutcome:
    """What one message changed.

    `resync` is set when the market must be refetched. `blockers` holds the reasons that
    need more than an orderbook read (BLOCKING_REASONS); they are also in `resync`.
    """

    books: list[HeldBook] = field(default_factory=list)
    trades: list[BatchTrade] = field(default_factory=list)
    settlements: list[MarketSettled] = field(default_factory=list)
    resync: StaleReason | None = None
    blockers: set[StaleReason] = field(default_factory=set)
    duplicate: bool = False


class _Malformed(Exception):
    pass


def version_key(version: BookVersion | None) -> VersionKey | None:
    """(sequence, exact instant of `at`); raises ValueError for an unparseable `at`."""
    if version is None:
        return None
    return (version.sequence, parse_instant(version.at))


def _reject_constant(name: str) -> None:
    raise ValueError(f"non-finite JSON constant {name}")


def decode_payload(payload: object) -> object:
    """Raw JSON text is decoded with exact Decimals; an already-decoded object is kept.

    A decoded object must have been decoded the same way: any binary float in it fails
    validation later, because the wire models accept only ints and Decimals as numbers.
    """
    if isinstance(payload, (bytes, bytearray, str)):
        try:
            return json.loads(payload, parse_float=Decimal, parse_constant=_reject_constant)
        except ValueError:
            raise _Malformed from None
    return payload


class MarketTopic:
    """One `tournament:{tournament_id}:market:{market_id}` topic."""

    def __init__(
        self,
        tournament_id: str,
        market_id: str,
        *,
        trade_id_memory: int = DEFAULT_TRADE_ID_MEMORY,
    ) -> None:
        self.tournament_id = tournament_id
        self.market_id = market_id
        self.topic = f"tournament:{tournament_id}:market:{market_id}"
        self._last_revision: int | None = None
        self._books: dict[str, HeldBook] = {}
        self._stamp = 0
        self._expiry_handled: set[int] = set()
        self._seen_trade_ids: set[str] = set()
        self._seen_order: deque[str] = deque()
        self._trade_id_memory = trade_id_memory

    @property
    def last_revision(self) -> int | None:
        return self._last_revision

    @property
    def books(self) -> Mapping[str, HeldBook]:
        return dict(self._books)

    def stamp(self) -> int:
        """A counter that rises with every book applied; marks when a resync started."""
        return self._stamp

    # --- realtime messages --------------------------------------------------------------

    def on_message(self, event: str, payload: object) -> TopicOutcome:
        if event == "market_batch":
            return self._on_batch(payload)
        if event == "book_dirty":
            # [documented] same shape as a bookDirty item, but it carries no book.
            item = self._standalone(BookDirty, payload)
            if item is None:
                return TopicOutcome(resync=StaleReason.MALFORMED)
            return TopicOutcome(resync=StaleReason.BOOK_DIRTY_EVENT)
        if event == "market_settled":
            item = self._standalone(MarketSettled, payload)
            if item is None:
                return TopicOutcome(resync=StaleReason.MALFORMED)
            return TopicOutcome(
                settlements=[item],
                resync=StaleReason.MARKET_SETTLED,
                blockers={StaleReason.MARKET_SETTLED},
            )
        return TopicOutcome()  # an event this topic does not carry: ignored

    def _standalone(
        self, model: type[BookDirty | MarketSettled], payload: object
    ) -> BookDirty | MarketSettled | None:
        try:
            item = model.model_validate(decode_payload(payload))
        except (_Malformed, ValidationError):
            return None
        return item if self._in_scope([item]) else None

    def _on_batch(self, payload: object) -> TopicOutcome:
        try:
            data = decode_payload(payload)
        except _Malformed:
            return TopicOutcome(resync=StaleReason.MALFORMED)
        if not isinstance(data, dict):
            return TopicOutcome(resync=StaleReason.MALFORMED)
        try:
            delivery = Delivery.model_validate(data.get("delivery"))
        except ValidationError:
            return TopicOutcome(resync=StaleReason.MALFORMED)  # no revision to accept
        try:
            batch = MarketBatch.model_validate(data)
            pushed = [(book, *self._book_clocks(book)) for book in batch.books or ()]
        except (ValidationError, ValueError):
            batch = None
        if batch is None or not self._in_scope(
            [*(batch.trades or ()), *(batch.bookDirty or ()), *(batch.marketSettled or ())]
        ):
            self._accept_if_newer(delivery.revision)
            return TopicOutcome(resync=StaleReason.MALFORMED)

        outcome = TopicOutcome()
        for book, key, expiry in pushed:
            held = self._apply_push(book, key, expiry)
            if held is not None:
                outcome.books.append(held)

        revision, previous = delivery.revision, delivery.previousRevision
        last = self._last_revision
        if batch.resyncRequired:
            self._accept_if_newer(revision)
            outcome.resync = StaleReason.RESYNC_REQUIRED
            return outcome
        if last is not None and revision <= last:
            outcome.duplicate = True
            return outcome
        missed = last is not None and previous > last
        self._last_revision = revision
        if missed:
            outcome.resync = StaleReason.REVISION_GAP
            return outcome

        trades = batch.trades or []
        dirty = batch.bookDirty or []
        settled = batch.marketSettled or []
        # Blockers first, so that no later early return can drop them.
        if settled:
            outcome.settlements = list(settled)
            outcome.blockers.add(StaleReason.MARKET_SETTLED)
            outcome.resync = StaleReason.MARKET_SETTLED
        if any(trade.sequence is None for trade in trades):
            outcome.blockers.add(StaleReason.NULL_TRADE_SEQUENCE)
            outcome.resync = StaleReason.NULL_TRADE_SEQUENCE
        named = {id_text(t.exchangeId) for t in trades} | {id_text(d.exchangeId) for d in dirty}
        if not named <= {str(book.exchangeId) for book in batch.books or ()}:
            outcome.resync = StaleReason.MISSING_BOOK
            return outcome  # no trades from a batch that broke the protocol
        if StaleReason.NULL_TRADE_SEQUENCE not in outcome.blockers:
            outcome.trades = self._new_trades(trades)
        return outcome

    def _accept_if_newer(self, revision: int) -> None:
        if self._last_revision is None or revision > self._last_revision:
            self._last_revision = revision

    def _in_scope(self, items: Iterable[BatchTrade | BookDirty | MarketSettled]) -> bool:
        # [client policy] every item names this topic's market, and its tournament when
        # it names one (trades may omit it). UUIDs compare case-insensitively.
        for item in items:
            if id_text(item.marketId) != self.market_id:
                return False
            if item.tournamentId is not None and item.tournamentId.lower() != self.tournament_id:
                return False
        return True

    @staticmethod
    def _book_clocks(book: PushedBook) -> tuple[VersionKey, Decimal | None]:
        key = version_key(book.asOf)
        assert key is not None  # a pushed book always carries asOf
        expiry = None if book.nextExpiryAt is None else parse_instant(book.nextExpiryAt)
        return key, expiry

    def _apply_push(
        self, book: PushedBook, key: VersionKey, expiry: Decimal | None
    ) -> HeldBook | None:
        exchange_id = str(book.exchangeId)
        held = self._books.get(exchange_id)
        if held is not None and held.version_key is not None and key <= held.version_key:
            return None  # not newer: versions never go backwards
        self._stamp += 1
        new = HeldBook(
            exchange_id=exchange_id,
            as_of=book.asOf,
            bids=tuple(book.bids),
            asks=tuple(book.asks),
            source=BookSource.PUSH,
            complete=True,  # [documented] pushed books have no depth cap
            next_expiry_at=book.nextExpiryAt,
            expiry_known=True,
            stamp=self._stamp,
            version_key=key,
            expiry_epoch=expiry,
        )
        self._books[exchange_id] = new
        return new

    def _new_trades(self, trades: Sequence[BatchTrade]) -> list[BatchTrade]:
        fresh = []
        for trade in trades:
            assert trade.id is not None  # null ids were handled before this point
            if trade.id in self._seen_trade_ids:
                continue
            self._seen_trade_ids.add(trade.id)
            self._seen_order.append(trade.id)
            if len(self._seen_order) > self._trade_id_memory:
                self._seen_trade_ids.discard(self._seen_order.popleft())
            fresh.append(trade)
        return fresh

    # --- REST snapshots -----------------------------------------------------------------

    def apply_snapshot(
        self,
        exchanges: Sequence[CombinedOrderbookExchange],
        *,
        depth: int,
        started_at: int,
        now_epoch: Decimal | None = None,
    ) -> list[HeldBook]:
        """Apply one REST market orderbook. `started_at` is `stamp()` when it was requested.

        `now_epoch` is the wall-clock instant the result is applied at; held books whose
        known order expiry is at or before it are treated as out of date.

        Returns the books that changed. Raises SnapshotError, applying nothing, for a
        snapshot that cannot be applied.
        """
        candidates: list[tuple[CombinedOrderbookExchange, VersionKey | None]] = []
        seen: set[str] = set()
        for exchange in exchanges:
            if not exchange.exchangeId.isascii() or not exchange.exchangeId.isdigit():
                raise SnapshotError("snapshot exchange id is not a digit string")
            if exchange.exchangeId in seen:
                raise SnapshotError("snapshot lists one exchange twice")
            seen.add(exchange.exchangeId)
            try:
                candidates.append((exchange, version_key(exchange.asOf)))
            except ValueError:
                raise SnapshotError("snapshot book clock is not a timestamp") from None

        books: dict[str, HeldBook] = {}
        changed: list[HeldBook] = []
        for exchange, key in candidates:
            held = self._books.get(exchange.exchangeId)
            if not self._snapshot_wins(held, key, started_at, now_epoch):
                assert held is not None
                books[exchange.exchangeId] = held
                continue
            self._stamp += 1
            new = HeldBook(
                exchange_id=exchange.exchangeId,
                as_of=exchange.asOf,
                bids=tuple(exchange.bids),
                asks=tuple(exchange.asks),
                source=BookSource.REST,
                complete=len(exchange.bids) < depth and len(exchange.asks) < depth,
                next_expiry_at=None,
                expiry_known=False,
                stamp=self._stamp,
                version_key=key,
                expiry_epoch=None,
            )
            books[exchange.exchangeId] = new
            changed.append(new)
        self._books = books
        return changed

    @staticmethod
    def _snapshot_wins(
        held: HeldBook | None,
        key: VersionKey | None,
        started_at: int,
        now_epoch: Decimal | None,
    ) -> bool:
        if held is None:
            return True
        if _expired(held, now_epoch):
            # Expiry changed the book without moving its version: an equal version wins.
            return key is None or held.version_key is None or key >= held.version_key
        if key is not None:
            return held.version_key is None or key > held.version_key
        # An unversioned REST book: authoritative, unless a push landed after the request.
        return not (held.source is BookSource.PUSH and held.stamp > started_at)

    # --- order expiry -------------------------------------------------------------------

    def has_expired_book(self, now_epoch: Decimal) -> bool:
        """True while any held book's known `nextExpiryAt` is at or before `now_epoch`."""
        return any(_expired(book, now_epoch) for book in self._books.values())

    def take_due_expiry(self, now_epoch: Decimal) -> bool:
        """True once per held book whose `nextExpiryAt` has passed (refetch it then)."""
        self._expiry_handled &= {book.stamp for book in self._books.values()}
        due = False
        for book in self._books.values():
            if (
                book.expiry_epoch is not None
                and book.expiry_epoch <= now_epoch
                and book.stamp not in self._expiry_handled
            ):
                self._expiry_handled.add(book.stamp)
                due = True
        return due


def _expired(book: HeldBook, now_epoch: Decimal | None) -> bool:
    return (
        now_epoch is not None and book.expiry_epoch is not None and book.expiry_epoch <= now_epoch
    )
