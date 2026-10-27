"""Raw tournament account reads (A2-A, A2-B): results, provenance and numeric-field status.

Engine-internal types for `SigRestClient.read_tournament_balance`,
`read_tournament_positions` and `read_open_orders` (A2-A), and `read_tournament_fills`,
`read_tournament_pnl`, `read_tournament_settlements` and `read_order_fills` (A2-B)
(docs/design/account-state-contracts.md, sections 4 and 8). Nothing here is a `shared/` schema, and nothing converts a wire value
into Bellwether's domain units: every number stays the exact `Decimal` SIG sent.

What a result shows, and what it does not:
- Each result holds the wire models of one completed read, exactly as validated, plus a
  `ReadProvenance` describing our own request(s).
- Provenance times are local. `started_mono` and `finished_mono` come from the client's
  injected monotonic clock and order our own requests; `finished_utc` is the local wall
  clock, for display only. None of them is a server time, and none shows that a value
  was current when served. No read here carries a server-side as-of time.
- Separate results are separate observations. Nothing ties two reads to one instant or
  one engine state, and provenance is not evidence of freshness, atomicity or readiness.
- Coverage is kept exactly as SIG sent it, per page, or recorded as omitted. A
  `projectedThroughSequence` is provenance, never compared with anything here.

Result objects hold response values for engine use only. Never log them: errors and log
lines carry no response values (see errors.py).
"""

from __future__ import annotations

import math
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime, timedelta
from decimal import Decimal
from enum import Enum
from types import MappingProxyType

from engine.sig_client.wire import (
    EconomicCommandCoverage,
    FillsCoverage,
    Order,
    OrderCoverage,
    OrderFill,
    OrderFillsCoverage,
    OrderFillsPage,
    PositionLot,
    PositionsSummary,
    Tournament,
    TournamentFill,
    TournamentPnl,
    TournamentPosition,
    TournamentSettlementEntry,
)

ProjectionCoverage = OrderCoverage | FillsCoverage | OrderFillsCoverage
"""Projection coverage (`complete` always true, plus a checkpoint), as sent per page."""

PageCoverage = ProjectionCoverage | EconomicCommandCoverage
"""Any coverage object a page may carry, kept exactly as sent."""


class ScopeEvidence(Enum):
    """How a read is bound to the configured tournament (design 1.2).

    There is deliberately no default-context member: no A2 method can make a read that
    resolves to the key's default or the global context, so such a read cannot be
    represented here.
    """

    ECHO = "echo"
    """The response names its scope (slug and UUID), and the client compared it."""

    VERIFIED_SLUG_PATH = "verified slug in path"
    """The path names the configured slug, verified once against the UUID; no echo."""

    TOURNAMENT_ID_SENT = "tournamentId sent, no echo"
    """Scope rests on the configured `tournamentId` sent; the response does not echo it."""


class CoverageState(Enum):
    """Whether a response carried coverage (projection or economic-command)."""

    NOT_APPLICABLE = "not applicable"  # the endpoint documents no coverage
    OMITTED = "omitted"  # documented as optional; SIG did not send it
    PRESENT = "present"  # sent, and kept exactly as sent in `coverage`


class VerificationUse(Enum):
    """The one-time tournament verification, as seen by one call."""

    NOT_APPLICABLE = "not applicable"
    """The read is itself an identity-checked GET /tournaments/{slug}."""

    CACHED = "cached"
    """Identity was verified earlier in the client's life; this call sent no request."""

    PERFORMED = "performed"
    """This call made the one-time verification read; see `verification_read`."""


@dataclass(frozen=True)
class ReadProvenance:
    """Our own record of one logical read (one request object or one page).

    - `route` is the endpoint template; `path` is the request path built from
      configuration, without the query.
    - `params` holds only allowlisted query parameters, as sent and sorted by name.
      A cursor's value is response data and is never kept: `cursor_sent` says only
      whether one was sent.
    - `attempts` counts the HTTP attempts of this read alone (retries included). It
      never includes the tournament verification, which has its own record.
    - `http_status` is the status of the final attempt (the one that produced this
      result).
    - `started_mono` is the local monotonic time at which this logical read began,
      before rate admission; `finished_mono` is when its final response had been read.
      `finished_utc` is the local wall clock at that point, for display only.
    - `page_index` is the 0-based page number within a walk, or None for a single read.
    - `coverage` is the page's coverage exactly as sent when `coverage_state` is
      PRESENT, and None otherwise.
    - `verification_read` is the verification read's own provenance when this call
      performed it (`verification` PERFORMED), and None otherwise.

    This is not evidence that any value was current, that two reads saw one state, or
    that the account is ready for anything.
    """

    route: str
    path: str
    params: tuple[tuple[str, str], ...]
    cursor_sent: bool
    scope: ScopeEvidence
    attempts: int
    http_status: int
    started_mono: float
    finished_mono: float
    finished_utc: datetime
    page_index: int | None
    coverage_state: CoverageState
    coverage: PageCoverage | None
    verification: VerificationUse
    verification_read: ReadProvenance | None

    def __post_init__(self) -> None:
        if isinstance(self.attempts, bool) or not isinstance(self.attempts, int):
            raise TypeError("attempts must be an integer")
        if self.attempts < 1:
            raise ValueError("a completed read made at least one attempt")
        for name in ("started_mono", "finished_mono"):
            value = getattr(self, name)
            if isinstance(value, bool) or not isinstance(value, (int, float)):
                raise TypeError(f"{name} must be a number")
            if not math.isfinite(value):
                raise ValueError(f"{name} must be finite")
        if self.finished_mono < self.started_mono:
            raise ValueError("finished_mono must not precede started_mono")
        if not isinstance(self.finished_utc, datetime) or self.finished_utc.utcoffset() != (
            timedelta(0)
        ):
            raise ValueError("finished_utc must be a timezone-aware UTC datetime")
        if self.page_index is not None and (
            isinstance(self.page_index, bool)
            or not isinstance(self.page_index, int)
            or self.page_index < 0
        ):
            raise ValueError("page_index must be None or a non-negative integer")
        if (self.coverage_state is CoverageState.PRESENT) != (self.coverage is not None):
            raise ValueError("coverage is kept exactly when coverage_state is PRESENT")
        if (self.verification is VerificationUse.PERFORMED) != (self.verification_read is not None):
            raise ValueError("verification_read is kept exactly when verification is PERFORMED")


@dataclass(frozen=True)
class TournamentBalanceRead:
    """A fresh GET /tournaments/{slug}, identity-checked, never served from a cache.

    `tournament.myBalance` is a candidate, unestablished tournament cash value
    (NUMERIC_FIELD_STATUS): whether it excludes resting buys, is the balance orders are
    admitted against, or is a seed while enrolment is pending is not documented.
    """

    tournament: Tournament
    provenance: ReadProvenance


@dataclass(frozen=True)
class PositionsRead:
    """A 200 from GET /tournaments/{slug}/portfolio/positions, rows exactly as sent.

    An empty `rows` shows only what this response held. It does not show that SIG holds
    no position for the account. No consistency check is applied across rows, lots or
    the summary; units and denominations are unresolved (NUMERIC_FIELD_STATUS).
    """

    rows: tuple[TournamentPosition, ...]
    summary: PositionsSummary
    provenance: ReadProvenance


@dataclass(frozen=True)
class MissingValuation:
    """One holding SIG reported as lacking a valuation price (digit-string ids only)."""

    exchange_id: str
    market_id: str


@dataclass(frozen=True)
class UnvaluedHoldingsRead:
    """The documented 409 from the positions or P&L read: holdings lack valuations.

    `provenance.route` says which read returned it.

    `missing` holds the validated (exchange id, market id) pairs in the order SIG sent
    them, duplicates included. Market titles and settlement labels are dropped. Nothing
    is valued at a default.
    """

    missing: tuple[MissingValuation, ...]
    provenance: ReadProvenance


@dataclass(frozen=True)
class PageRecord:
    """One page of a walk: its provenance (with coverage) and how many items it held."""

    provenance: ReadProvenance
    item_count: int


@dataclass(frozen=True)
class OrdersWalk:
    """Every page of GET /orders?status=open in the configured tournament, or nothing.

    `orders` is every order across `pages`, in page order. A walk is returned only when
    the last page reported `hasMore: false`; a repeated cursor, a duplicate order id, a
    cursor on the last page or the page cap raises instead. Completing the walk shows
    that every page was read, not that SIG's reporting projection had caught up.
    """

    orders: tuple[Order, ...]
    pages: tuple[PageRecord, ...]


# --- A2-B results ----------------------------------------------------------------------------


@dataclass(frozen=True)
class FillsWalk:
    """Every page of GET /tournaments/{slug}/portfolio/fills, newest first, or nothing.

    Returned only when the last page reported `hasMore: false`, with no repeated cursor,
    duplicate fill id or page-cap overrun, and no fill newer than the one before it,
    across all pages. Each page's projection coverage is kept as sent, or recorded as
    omitted. Units and denominations are unresolved (NUMERIC_FIELD_STATUS).
    """

    fills: tuple[TournamentFill, ...]
    pages: tuple[PageRecord, ...]


@dataclass(frozen=True)
class PnlRead:
    """A 200 from GET /tournaments/{slug}/portfolio/pnl, fields exactly as sent.

    `pnl.totalAccountValue` is a candidate equity figure only (OQ-D1); no cash figure is
    derived from it and no field is reconciled with any other read.
    """

    pnl: TournamentPnl
    provenance: ReadProvenance


@dataclass(frozen=True)
class SettlementsWalk:
    """Every page of GET /tournaments/{slug}/portfolio/settlements, or nothing.

    Economic-command coverage can legitimately be incomplete: SIG documents
    `coverage.complete` as false when its own history walk hit a 20-page budget. The walk
    still reads every page SIG offers, keeps each page's `complete` as sent, and lists the
    0-based indexes of the pages that reported false in `incomplete_pages`. `complete` is
    True only when every page reported true, and never otherwise.
    """

    entries: tuple[TournamentSettlementEntry, ...]
    pages: tuple[PageRecord, ...]
    complete: bool
    incomplete_pages: tuple[int, ...]

    def __post_init__(self) -> None:
        reported = []
        for record in self.pages:
            coverage = record.provenance.coverage
            if not isinstance(coverage, EconomicCommandCoverage):
                raise TypeError("every settlements page keeps its economic-command coverage")
            if coverage.complete is not True:
                reported.append(record.provenance.page_index)
        if tuple(reported) != self.incomplete_pages:
            raise ValueError("incomplete_pages must list exactly the pages reporting false")
        if self.complete != (not self.incomplete_pages):
            raise ValueError("complete is True exactly when no page reported false")


@dataclass(frozen=True)
class OrderFillsWalk:
    """Every page of GET /orders/{id}/fills for one order, or nothing.

    `echoed_tournament_id` is the order's tournament as SIG echoed it on every page; the
    client checked that it is the configured tournament (a null or other value raises).
    `total_quantity_filled` and `avg_fill_price` are SIG's lifetime totals for the whole
    order, exactly as sent and identical on every page; they are not recomputed from
    `fills`. An order with no fills is one page with no fills.
    """

    order_id: int
    exchange_id: str
    echoed_tournament_id: str
    fills: tuple[OrderFill, ...]
    total_quantity_filled: Decimal
    avg_fill_price: Decimal | None
    pages: tuple[PageRecord, ...]


# --- numeric-field status (design 4.1 R5 and 5.9) ----------------------------------------


class FieldState(Enum):
    DOCUMENTED = "documented"
    """The spec documents what the value means; nothing open is attached to it."""

    UNRESOLVED = "unresolved"
    """Some meaning, unit, denomination or relation is open; see `questions`."""


@dataclass(frozen=True)
class NumericFieldStatus:
    """What the spec documents about one numeric wire field, and what stays open.

    `pointer` is the JSON pointer into docs/api/openapi.json for the field.
    `questions` names the PRD open questions (OQ-*) and design candidates (A1-N*) that
    must be answered before the value can be interpreted beyond what is documented.
    """

    model: str
    field: str
    pointer: str
    state: FieldState
    documented: str
    unresolved: str
    questions: tuple[str, ...]

    def __post_init__(self) -> None:
        if self.state is FieldState.UNRESOLVED and not (self.unresolved and self.questions):
            raise ValueError(f"{self.model}.{self.field}: an unresolved field names its questions")
        if self.state is FieldState.DOCUMENTED and (self.unresolved or self.questions):
            raise ValueError(f"{self.model}.{self.field}: a documented field has nothing open")


_TOURNAMENT = "#/paths/~1tournaments~1{slug}/get/responses/200/content/application~1json/schema"
_POSITION = "#/components/schemas/TournamentPortfolioPosition/properties"
_LOT = f"{_POSITION}/lots/items/properties"
_SUMMARY = (
    "#/paths/~1tournaments~1{slug}~1portfolio~1positions/get/responses/200/content/"
    "application~1json/schema/properties/summary/properties"
)
_ORDER = "#/components/schemas/Order/properties"
_ORDER_COVERAGE = (
    "#/paths/~1orders/get/responses/200/content/application~1json/schema/properties/"
    "coverage/properties"
)

_FILL = "#/components/schemas/Fill/properties"
_FILLS_COVERAGE = (
    "#/paths/~1tournaments~1{slug}~1portfolio~1fills/get/responses/200/content/"
    "application~1json/schema/properties/coverage/properties"
)
_PNL = (
    "#/paths/~1tournaments~1{slug}~1portfolio~1pnl/get/responses/200/content/"
    "application~1json/schema/properties"
)
_SETTLEMENT = "#/components/schemas/TournamentSettlementEntry/properties"
_ORDER_FILLS = (
    "#/paths/~1orders~1{id}~1fills/get/responses/200/content/application~1json/schema/properties"
)

_POSITION_VALUE = "units and sign for a NO holding are undocumented"
_PNL_VALUE = "meaning as equity or P&L for Bellwether; behaviour with stale or missing marks"
_PNL_QUESTIONS = ("OQ-D1", "OQ-D3")
_FILL_PRICE = "denomination (YES or held-side price) on REST; whether on the 0.005 grid; null"
_FILL_QUANTITY = "unit and granularity"


def _entry(
    model: type,
    field: str,
    pointer: str,
    documented: str,
    unresolved: str = "",
    questions: tuple[str, ...] = (),
) -> NumericFieldStatus:
    state = FieldState.UNRESOLVED if questions else FieldState.DOCUMENTED
    return NumericFieldStatus(
        model=model.__name__,
        field=field,
        pointer=pointer,
        state=state,
        documented=documented,
        unresolved=unresolved,
        questions=questions,
    )


_ENTRIES: tuple[NumericFieldStatus, ...] = (
    # GET /tournaments/{slug}
    _entry(
        Tournament,
        "myBalance",
        f"{_TOURNAMENT}/properties/myBalance",
        "the caller's current or seed virtual cash balance in this tournament",
        "whether resting buys are deducted; whether it is the balance orders are admitted "
        "against; whether it is a seed or current while enrolment is pending",
        ("OQ-C3", "OQ-W11", "A1-N4"),
    ),
    _entry(
        Tournament,
        "initialBalance",
        f"{_TOURNAMENT}/properties/initialBalance",
        "a number; no description",
        "whether it equals the seed balance of a member pending enrolment",
        ("OQ-W11", "A1-N4"),
    ),
    _entry(Tournament, "memberCount", f"{_TOURNAMENT}/properties/memberCount", "an integer count"),
    _entry(Tournament, "marketCount", f"{_TOURNAMENT}/properties/marketCount", "an integer count"),
    # GET /tournaments/{slug}/portfolio/positions
    _entry(
        TournamentPosition,
        "quantity",
        f"{_POSITION}/quantity",
        "positive = YES shares, negative = NO shares",
        "granularity; how lots relate to it after netting",
        ("OQ-W5", "OQ-W8"),
    ),
    _entry(
        TournamentPosition,
        "avgCost",
        f"{_POSITION}/avgCost",
        "a number; no description",
        "denomination (YES or held-side price); mark source",
        ("OQ-W5", "OQ-D1"),
    ),
    _entry(
        TournamentPosition,
        "currentPrice",
        f"{_POSITION}/currentPrice",
        "the tournament valuation price; null only for zero-quantity rows",
        "denomination (YES or held-side price); mark source",
        ("OQ-W5", "OQ-D1"),
    ),
    _entry(
        TournamentPosition,
        "marketValue",
        f"{_POSITION}/marketValue",
        "a number; no description",
        _POSITION_VALUE,
        ("OQ-W5",),
    ),
    _entry(
        TournamentPosition,
        "costBasis",
        f"{_POSITION}/costBasis",
        "a number; no description",
        _POSITION_VALUE,
        ("OQ-W5",),
    ),
    _entry(
        TournamentPosition,
        "unrealizedPnl",
        f"{_POSITION}/unrealizedPnl",
        "a number; no description",
        _POSITION_VALUE,
        ("OQ-W5",),
    ),
    _entry(
        TournamentPosition,
        "unrealizedPnlPct",
        f"{_POSITION}/unrealizedPnlPct",
        "a number; no description",
        "scale (fraction or percent) and sign for a NO holding are undocumented",
        ("OQ-W5",),
    ),
    _entry(
        TournamentPosition,
        "moneyEarned",
        f"{_POSITION}/moneyEarned",
        "a number; no description",
        _POSITION_VALUE,
        ("OQ-W5",),
    ),
    _entry(
        PositionLot,
        "quantity",
        f"{_LOT}/quantity",
        "a number; no sign rule",
        "sign, unit and relation to the row quantity and lot side",
        ("OQ-W5", "OQ-W8"),
    ),
    _entry(
        PositionLot,
        "entryPrice",
        f"{_LOT}/entryPrice",
        "a number; no description",
        "denomination (YES or held-side price)",
        ("OQ-W5", "OQ-D1"),
    ),
    _entry(
        PositionsSummary,
        "totalMarketValue",
        f"{_SUMMARY}/totalMarketValue",
        "a number; no description",
        "units, and how NO holdings enter the total",
        ("OQ-W5",),
    ),
    _entry(
        PositionsSummary,
        "totalCostBasis",
        f"{_SUMMARY}/totalCostBasis",
        "a number; no description",
        "units, and how NO holdings enter the total",
        ("OQ-W5",),
    ),
    _entry(
        PositionsSummary,
        "totalUnrealizedPnl",
        f"{_SUMMARY}/totalUnrealizedPnl",
        "a number; no description",
        "units, and how NO holdings enter the total",
        ("OQ-W5",),
    ),
    # GET /orders
    _entry(Order, "id", f"{_ORDER}/id", "the order id (an identifier, not a quantity)"),
    _entry(
        Order,
        "quantity",
        f"{_ORDER}/quantity",
        "always positive; while open, the executable remainder; side gives YES or NO",
    ),
    _entry(
        Order,
        "priceLimit",
        f"{_ORDER}/priceLimit",
        "a number or null; no description or range",
        "denomination for NO orders (YES or NO price)",
        ("OQ-W6",),
    ),
    _entry(
        OrderCoverage,
        "projectedThroughSequence",
        f"{_ORDER_COVERAGE}/projectedThroughSequence",
        "latest trading-engine sequence reflected by the reporting projection, or null",
        "what it guarantees relative to balance, positions and other reads",
        ("A1-N1",),
    ),
    # GET /tournaments/{slug}/portfolio/fills
    _entry(TournamentFill, "id", f"{_FILL}/id", "the fill record id (an identifier)"),
    _entry(TournamentFill, "orderId", f"{_FILL}/orderId", "the filled order's id, or null"),
    _entry(
        TournamentFill,
        "price",
        f"{_FILL}/price",
        "a number or null; no description (YES-denominated is documented for realtime only)",
        _FILL_PRICE,
        ("OQ-W4", "OQ-W7"),
    ),
    _entry(
        TournamentFill,
        "quantity",
        f"{_FILL}/quantity",
        "outcome-signed fill quantity; negative values indicate the NO side",
        _FILL_QUANTITY,
        ("OQ-W4",),
    ),
    _entry(
        FillsCoverage,
        "projectedThroughSequence",
        f"{_FILLS_COVERAGE}/projectedThroughSequence",
        "latest trading-engine sequence reflected by the reporting projection, or null",
        "what it guarantees relative to balance, positions and other reads",
        ("A1-N1",),
    ),
    # GET /tournaments/{slug}/portfolio/pnl
    _entry(
        TournamentPnl,
        "periodPnl",
        f"{_PNL}/periodPnl",
        "tournament-scoped; no description",
        _PNL_VALUE,
        _PNL_QUESTIONS,
    ),
    _entry(
        TournamentPnl,
        "unrealizedPnl",
        f"{_PNL}/unrealizedPnl",
        "tournament-scoped; no description",
        _PNL_VALUE,
        _PNL_QUESTIONS,
    ),
    _entry(
        TournamentPnl,
        "totalAccountValue",
        f"{_PNL}/totalAccountValue",
        "tournament-scoped; no description",
        _PNL_VALUE,
        _PNL_QUESTIONS,
    ),
    _entry(
        TournamentPnl,
        "totalHoldingsValue",
        f"{_PNL}/totalHoldingsValue",
        "tournament-scoped; no description",
        _PNL_VALUE,
        _PNL_QUESTIONS,
    ),
    _entry(
        TournamentPnl,
        "totalCostBasis",
        f"{_PNL}/totalCostBasis",
        "tournament-scoped; no description",
        _PNL_VALUE,
        _PNL_QUESTIONS,
    ),
    _entry(
        TournamentPnl,
        "roi",
        f"{_PNL}/roi",
        "return vs initial balance (period=all) or vs snapshot baseline",
        f"scale (fraction or percent); {_PNL_VALUE}",
        _PNL_QUESTIONS,
    ),
    _entry(
        TournamentPnl,
        "sharpe",
        f"{_PNL}/sharpe",
        "tournament-scoped; no description",
        _PNL_VALUE,
        _PNL_QUESTIONS,
    ),
    # GET /tournaments/{slug}/portfolio/settlements
    _entry(
        TournamentSettlementEntry,
        "shares",
        f"{_SETTLEMENT}/shares",
        "a number or null; no description on the tournament row",
        "sign and unit (the default-scope row's sign rule is not carried over)",
        ("OQ-W5",),
    ),
    _entry(
        TournamentSettlementEntry,
        "avgEntryPrice",
        f"{_SETTLEMENT}/avgEntryPrice",
        "a number or null; no description",
        "denomination (YES or held-side price)",
        ("OQ-W5",),
    ),
    _entry(
        TournamentSettlementEntry,
        "costBasis",
        f"{_SETTLEMENT}/costBasis",
        "a number or null; no description",
        _POSITION_VALUE,
        ("OQ-W5",),
    ),
    _entry(
        TournamentSettlementEntry,
        "payout",
        f"{_SETTLEMENT}/payout",
        "cash credited to the tournament virtual balance (or 0 for losses)",
        "when it is reflected in myBalance relative to positions",
        ("A1-N3",),
    ),
    _entry(
        TournamentSettlementEntry,
        "realizedPnl",
        f"{_SETTLEMENT}/realizedPnl",
        "a number or null; no description",
        _POSITION_VALUE,
        ("OQ-W5",),
    ),
    # GET /orders/{id}/fills
    _entry(OrderFillsPage, "orderId", f"{_ORDER_FILLS}/orderId", "the order id (an identifier)"),
    _entry(
        OrderFillsPage,
        "totalQuantityFilled",
        f"{_ORDER_FILLS}/totalQuantityFilled",
        "exact outcome-signed filled quantity for the whole order; negative indicates NO",
        _FILL_QUANTITY,
        ("OQ-W4",),
    ),
    _entry(
        OrderFillsPage,
        "avgFillPrice",
        f"{_ORDER_FILLS}/avgFillPrice",
        "exact average fill price for the whole order, or null before the first fill",
        _FILL_PRICE,
        ("OQ-W4", "OQ-W7"),
    ),
    _entry(OrderFill, "id", f"{_ORDER_FILLS}/data/items/properties/id", "the fill record id"),
    _entry(
        OrderFill,
        "price",
        f"{_ORDER_FILLS}/data/items/properties/price",
        "a number or null; no description",
        _FILL_PRICE,
        ("OQ-W4", "OQ-W7"),
    ),
    _entry(
        OrderFill,
        "quantity",
        f"{_ORDER_FILLS}/data/items/properties/quantity",
        "outcome-signed fill quantity; negative values indicate the NO side",
        _FILL_QUANTITY,
        ("OQ-W4",),
    ),
    _entry(
        OrderFillsCoverage,
        "projectedThroughSequence",
        f"{_ORDER_FILLS}/coverage/properties/projectedThroughSequence",
        "latest trading-engine sequence reflected by the reporting projection, or null",
        "what it guarantees relative to balance, positions and other reads",
        ("A1-N1",),
    ),
)


def _index(entries: tuple[NumericFieldStatus, ...]) -> Mapping[tuple[str, str], NumericFieldStatus]:
    table: dict[tuple[str, str], NumericFieldStatus] = {}
    for entry in entries:
        key = (entry.model, entry.field)
        if key in table:
            raise ValueError(f"duplicate numeric-field status for {entry.model}.{entry.field}")
        table[key] = entry
    return MappingProxyType(table)


NUMERIC_FIELD_STATUS: Mapping[tuple[str, str], NumericFieldStatus] = _index(_ENTRIES)
"""(wire model name, field name) -> status, for every numeric field the A2 reads expose."""


def numeric_field_status(model: type | str, field: str) -> NumericFieldStatus:
    """The status of one numeric wire field. Raises KeyError for a field not in the table."""
    name = model if isinstance(model, str) else model.__name__
    return NUMERIC_FIELD_STATUS[(name, field)]
