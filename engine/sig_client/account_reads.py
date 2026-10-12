"""Raw tournament account reads (A2-A): results, provenance and numeric-field status.

Engine-internal types for `SigRestClient.read_tournament_balance`,
`read_tournament_positions` and `read_open_orders` (docs/design/account-state-contracts.md,
sections 4 and 8). Nothing here is a `shared/` schema, and nothing converts a wire value
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
from enum import Enum
from types import MappingProxyType

from engine.sig_client.wire import (
    Order,
    OrderCoverage,
    PositionLot,
    PositionsSummary,
    Tournament,
    TournamentPosition,
)


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
    """Whether a response carried projection coverage."""

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
    coverage: OrderCoverage | None
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
    """The documented 409 from the positions read: holdings exist without valuations.

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

_POSITION_VALUE = "units and sign for a NO holding are undocumented"


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
"""(wire model name, field name) -> status, for every numeric field the A2-A reads expose."""


def numeric_field_status(model: type | str, field: str) -> NumericFieldStatus:
    """The status of one numeric wire field. Raises KeyError for a field not in the table."""
    name = model if isinstance(model, str) else model.__name__
    return NUMERIC_FIELD_STATUS[(name, field)]
