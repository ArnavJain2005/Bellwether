"""AccountSnapshot: one assembly of the A2 account reads, with its findings (A3).

Design: docs/design/account-state-contracts.md, section 5, acceptance tests T21 to T46.

What it is:
- An immutable, engine-internal record of one assembly for the configured tournament:
  a fresh opening balance read first, then positions and the full open-orders walk, then
  any optional components the caller asked for (fills, valuation, settlements, the fills
  of named orders), and a fresh closing balance read last. Only the merged A2 read
  methods of `SigRestClient` are called; no other endpoint is requested.
- Every requested component gets exactly one outcome, and the assembly continues after a
  failure. After a tournament-identity mismatch the client's latch makes every later read
  fail at once with no request; those components still get a SCOPE_MISMATCH outcome.
- The status is the most severe class present (section 5.5): SCOPE_MISMATCH, CONTRADICTED,
  UNAVAILABLE, INCOMPLETE, else OBSERVED_UNRESOLVED. There is no READY status. Precedence
  chooses only the label: every outcome, finding and reason is kept.

What it is not (it never claims any of these):
- atomicity: the reads are separate observations at different local times;
- freshness: no local time or age shows how current SIG's state was, and A3 sets no
  maximum age (that is S1 policy);
- membership, enrolment or seed-versus-current balance: always NOT_DETERMINED;
- established or spendable cash: `cash.value` is a candidate only (OQ-W11, OQ-C3);
- equity, price denomination or units: values are kept exactly as SIG sent them;
- restart, recovery, market or trading readiness.

Failed reads: A2 returns no provenance and no partial walk when a read fails, so no
`ReadProvenance` exists for a failed request and none is fabricated here. A failed
component instead keeps a `ComponentFailure` (error class, phase, HTTP status, documented
code, attempt count where the error carries them) and a `ComponentTiming` with source
CALL: local monotonic times taken by the assembler immediately before and after the A2
call. Those times bound every request the call made (including a one-time tournament
verification it triggered), but say nothing about how many requests or pages were sent.
`reads` therefore lists the requests of successful components only.

Cancellation: only `SigClientError` is classified as a component failure. Cancellation of
the assembling task (and any other exception, which would be a programming error)
propagates unchanged and produces no snapshot.

Immutability: a snapshot is immutable all the way down, so nothing a caller does can
change the data its findings, reasons and status were computed from. Every retained A2
result, wire model, provenance record and A3 record is checked when it is built: only
frozen dataclasses, frozen pydantic models, tuples, frozensets, read-only mappings and
immutable scalars are accepted (`_check_immutable`). Deliberately bypassing that (for
example with `object.__setattr__`) is outside what any Python object can prevent.

Redaction: reasons, findings and failures hold client-written text, component labels and
row positions only; never balances, quantities, market ids, titles or cursors. Result
objects hold response values for engine use only; never log them.
"""

from __future__ import annotations

import dataclasses
import math
import re
import time
from collections.abc import Iterable, Iterator, Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta
from decimal import Decimal
from enum import Enum, StrEnum
from types import MappingProxyType
from typing import Literal
from urllib.parse import quote

from pydantic import BaseModel

from engine.config import TournamentRef
from engine.sig_client.account_reads import (
    NUMERIC_FIELD_STATUS,
    FillsWalk,
    NumericFieldStatus,
    OrderFillsWalk,
    OrdersWalk,
    PnlRead,
    PositionsRead,
    ReadProvenance,
    ScopeEvidence,
    SettlementsWalk,
    TournamentBalanceRead,
    UnvaluedHoldingsRead,
    VerificationUse,
)
from engine.sig_client.errors import (
    SigApiError,
    SigClientError,
    SigContextMismatchError,
    SigDeadlineExceededError,
    SigRetryExhaustedError,
)
from engine.sig_client.rate_limiter import Clock
from engine.sig_client.rest import (
    FILLS_ROUTE,
    ORDER_FILLS_ROUTE,
    ORDERS_ROUTE,
    PNL_ROUTE,
    POSITIONS_ROUTE,
    SETTLEMENTS_ROUTE,
    TOURNAMENT_ROUTE,
    SigRestClient,
    WallClock,
    utc_now,
)
from engine.sig_client.wire import (
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

DOES_NOT_COVER = (
    "Account observations only. This snapshot is not a readiness result. It says nothing "
    "about market readiness, unresolved idempotency keys, risk, configuration, reference "
    "data or rate budget, and never authorizes trading on its own."
)

# --- components and outcomes (design 5.2, 5.3) --------------------------------------------


class ComponentKind(StrEnum):
    BALANCE_OPENING = "balance_opening"
    POSITIONS = "positions"
    OPEN_ORDERS = "open_orders"
    FILLS = "fills"
    VALUATION = "valuation"
    SETTLEMENTS = "settlements"
    ORDER_FILLS = "order_fills"
    BALANCE_CLOSING = "balance_closing"


REQUIRED_KINDS = frozenset(
    {
        ComponentKind.BALANCE_OPENING,
        ComponentKind.POSITIONS,
        ComponentKind.OPEN_ORDERS,
        ComponentKind.BALANCE_CLOSING,
    }
)
PORTFOLIO_KINDS = (
    ComponentKind.POSITIONS,
    ComponentKind.FILLS,
    ComponentKind.VALUATION,
    ComponentKind.SETTLEMENTS,
)
"""The portfolio reads whose access is recorded (design 5.6) and checked by K3 and K4."""


@dataclass(frozen=True)
class ComponentId:
    """One component of an assembly. `order_id` names the order of an ORDER_FILLS component.

    `ComponentId(ORDER_FILLS)` without an order id stands for "order fills" as a whole and
    is used only for the NOT_REQUESTED outcome when no order was named.
    """

    kind: ComponentKind
    order_id: int | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.kind, ComponentKind):
            raise TypeError("kind must be a ComponentKind")
        if self.order_id is not None:
            if self.kind is not ComponentKind.ORDER_FILLS:
                raise ValueError("only an order_fills component names an order")
            _check_order_id(self.order_id)

    @property
    def label(self) -> str:
        if self.order_id is not None:
            return f"order_fills[{self.order_id}]"
        return self.kind.value


OPENING = ComponentId(ComponentKind.BALANCE_OPENING)
CLOSING = ComponentId(ComponentKind.BALANCE_CLOSING)
POSITIONS = ComponentId(ComponentKind.POSITIONS)
OPEN_ORDERS = ComponentId(ComponentKind.OPEN_ORDERS)
FILLS = ComponentId(ComponentKind.FILLS)
VALUATION = ComponentId(ComponentKind.VALUATION)
SETTLEMENTS = ComponentId(ComponentKind.SETTLEMENTS)
ORDER_FILLS_ANY = ComponentId(ComponentKind.ORDER_FILLS)


def _check_order_id(order_id: object) -> int:
    # Mirrors the read client: order ids are non-negative wire integers.
    if isinstance(order_id, bool) or not isinstance(order_id, int) or order_id < 0:
        raise ValueError("order ids must be non-negative integers")
    return order_id


@dataclass(frozen=True)
class SnapshotRequest:
    """Which optional components one assembly requests. Required ones are always read."""

    fills: bool = False
    valuation: bool = False
    settlements: bool = False
    order_fill_ids: tuple[int, ...] = ()

    def __post_init__(self) -> None:
        for name in ("fills", "valuation", "settlements"):
            if not isinstance(getattr(self, name), bool):
                raise TypeError(f"{name} must be a bool")
        if not isinstance(self.order_fill_ids, tuple):
            raise TypeError("order_fill_ids must be a tuple")
        for order_id in self.order_fill_ids:
            _check_order_id(order_id)
        if len(set(self.order_fill_ids)) != len(self.order_fill_ids):
            raise ValueError("order_fill_ids must not repeat an order")

    def components(self) -> tuple[ComponentId, ...]:
        """The requested components, in assembly order (opening first, closing last)."""
        chosen = [OPENING, POSITIONS, OPEN_ORDERS]
        if self.fills:
            chosen.append(FILLS)
        if self.valuation:
            chosen.append(VALUATION)
        if self.settlements:
            chosen.append(SETTLEMENTS)
        chosen.extend(ComponentId(ComponentKind.ORDER_FILLS, i) for i in self.order_fill_ids)
        chosen.append(CLOSING)
        return tuple(chosen)

    def not_requested(self) -> tuple[ComponentId, ...]:
        """The optional components this request leaves out."""
        left = [
            component
            for component, asked in ((FILLS, self.fills), (VALUATION, self.valuation))
            if not asked
        ]
        if not self.settlements:
            left.append(SETTLEMENTS)
        if not self.order_fill_ids:
            left.append(ORDER_FILLS_ANY)
        return tuple(left)


class Outcome(StrEnum):
    OBSERVED = "OBSERVED"
    OBSERVED_INCOMPLETE = "OBSERVED_INCOMPLETE"
    UNVALUED = "UNVALUED"
    HTTP_ERROR = "HTTP_ERROR"
    FAILED = "FAILED"
    SCOPE_MISMATCH = "SCOPE_MISMATCH"
    NOT_REQUESTED = "NOT_REQUESTED"


_READ_OUTCOMES = frozenset({Outcome.OBSERVED, Outcome.OBSERVED_INCOMPLETE, Outcome.UNVALUED})
_FAILURE_OUTCOMES = frozenset({Outcome.HTTP_ERROR, Outcome.FAILED, Outcome.SCOPE_MISMATCH})
_HTTP_200_OUTCOMES = frozenset({Outcome.OBSERVED, Outcome.OBSERVED_INCOMPLETE})

ComponentRead = (
    TournamentBalanceRead
    | PositionsRead
    | UnvaluedHoldingsRead
    | OrdersWalk
    | FillsWalk
    | PnlRead
    | SettlementsWalk
    | OrderFillsWalk
)

_READ_TYPES: Mapping[ComponentKind, tuple[type, ...]] = MappingProxyType(
    {
        ComponentKind.BALANCE_OPENING: (TournamentBalanceRead,),
        ComponentKind.POSITIONS: (PositionsRead, UnvaluedHoldingsRead),
        ComponentKind.OPEN_ORDERS: (OrdersWalk,),
        ComponentKind.FILLS: (FillsWalk,),
        ComponentKind.VALUATION: (PnlRead, UnvaluedHoldingsRead),
        ComponentKind.SETTLEMENTS: (SettlementsWalk,),
        ComponentKind.ORDER_FILLS: (OrderFillsWalk,),
        ComponentKind.BALANCE_CLOSING: (TournamentBalanceRead,),
    }
)


def _read_outcome(read: ComponentRead) -> Outcome:
    if isinstance(read, UnvaluedHoldingsRead):
        return Outcome.UNVALUED
    if isinstance(read, SettlementsWalk) and not read.complete:
        return Outcome.OBSERVED_INCOMPLETE
    return Outcome.OBSERVED


def _expand(provenance: ReadProvenance) -> Iterator[ReadProvenance]:
    # A verification read made by this call happened before the request it verified.
    if provenance.verification_read is not None:
        yield provenance.verification_read
    yield provenance


def _requests(read: ComponentRead) -> tuple[ReadProvenance, ...]:
    """Every request a successful A2 read made, in order (verification reads included)."""
    if isinstance(read, (OrdersWalk, FillsWalk, SettlementsWalk, OrderFillsWalk)):
        return tuple(p for page in read.pages for p in _expand(page.provenance))
    return tuple(_expand(read.provenance))


# --- timing (design 5.8) ------------------------------------------------------------------


class TimingSource(StrEnum):
    REQUESTS = "requests"
    """From the ReadProvenance of every request a successful component made."""

    CALL = "call boundary"
    """A failed component: assembler-local times immediately around the A2 call. A2
    reports no per-request provenance for a failed read, so none is invented."""


def _check_time(name: str, value: object) -> None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise TypeError(f"{name} must be a number")
    if not math.isfinite(value):
        raise ValueError(f"{name} must be finite")


def _check_utc(name: str, value: object) -> None:
    if not isinstance(value, datetime) or value.utcoffset() != timedelta(0):
        raise ValueError(f"{name} must be a timezone-aware UTC datetime")


@dataclass(frozen=True)
class ComponentTiming:
    """Local times of one component's requests. Never a server time.

    - `started_mono`: start of the earliest request (REQUESTS) or of the A2 call (CALL).
      A component's age is measured from it.
    - `finished_mono`: finish of the latest request, or the end of the A2 call.
    - `first_finished_mono`: when the earliest request's response had been read
      (REQUESTS only; None for CALL, where no response was returned).
    - `last_started_mono`: start of the latest request (REQUESTS only; None for CALL).
    - `finished_utc`: local wall clock at `finished_mono`, for display only.
    """

    source: TimingSource
    started_mono: float
    finished_mono: float
    first_finished_mono: float | None
    last_started_mono: float | None
    finished_utc: datetime

    def __post_init__(self) -> None:
        if not isinstance(self.source, TimingSource):
            raise TypeError("source must be a TimingSource")
        _check_time("started_mono", self.started_mono)
        _check_time("finished_mono", self.finished_mono)
        _check_utc("finished_utc", self.finished_utc)
        if self.finished_mono < self.started_mono:
            raise ValueError("finished_mono must not precede started_mono")
        inner = (self.first_finished_mono, self.last_started_mono)
        if self.source is TimingSource.CALL:
            if inner != (None, None):
                raise ValueError("a call-boundary timing records no per-request times")
            return
        for name, value in zip(("first_finished_mono", "last_started_mono"), inner, strict=True):
            _check_time(name, value)
            assert value is not None
            if not self.started_mono <= value <= self.finished_mono:
                raise ValueError(f"{name} must lie within the component's span")

    @classmethod
    def from_requests(cls, requests: Sequence[ReadProvenance]) -> ComponentTiming:
        if not requests:
            raise ValueError("a successful component made at least one request")
        earliest = min(requests, key=lambda p: p.started_mono)
        latest = max(requests, key=lambda p: p.finished_mono)
        return cls(
            source=TimingSource.REQUESTS,
            started_mono=earliest.started_mono,
            finished_mono=latest.finished_mono,
            first_finished_mono=earliest.finished_mono,
            last_started_mono=latest.started_mono,
            finished_utc=latest.finished_utc,
        )


@dataclass(frozen=True)
class ComponentAge:
    """One component's local age at monotonic time `at_mono` (design 5.8).

    `age` is `at_mono - timing.started_mono`: time since we first asked for the component.
    `since_first_response` is time since its earliest response had been read, or None for
    a failed component. Neither bounds the age of SIG's state.
    """

    component: ComponentId
    outcome: Outcome
    at_mono: float
    age: float
    since_first_response: float | None
    timing_source: TimingSource


# --- failures -----------------------------------------------------------------------------


@dataclass(frozen=True)
class ComponentFailure:
    """How a requested component's A2 read failed. Client-written facts only.

    - `error_class`: the SigClientError subclass raised.
    - `http_status`, `code`, `unrecognized_code`: for an HTTP error (including the last
      error of exhausted retries); `code` is a documented SIG code or None, never text.
    - `phase`: where a read deadline ran out, else None.
    - `attempts`: HTTP attempts of the failing logical read, when the error reports them
      (deadline or exhausted retries); None otherwise. It does not count earlier pages.
    - `last_error_class`: the previous attempt's error class, when the error carries one.

    A failed walk returns no pages from A2, so the failing page index is not known here.
    """

    error_class: str
    http_status: int | None
    code: str | None
    unrecognized_code: bool
    phase: str | None
    attempts: int | None
    last_error_class: str | None

    @classmethod
    def from_error(cls, error: SigClientError) -> tuple[Outcome, ComponentFailure]:
        if not isinstance(error, SigClientError):
            raise TypeError("only SIG client errors are classified as component failures")
        name = type(error).__name__
        if isinstance(error, SigContextMismatchError):
            return Outcome.SCOPE_MISMATCH, cls(name, None, None, False, None, None, None)
        if isinstance(error, SigApiError):
            failure = cls(name, error.status, error.code, error.unrecognized_code, None, None, None)
            return Outcome.HTTP_ERROR, failure
        if isinstance(error, SigRetryExhaustedError):
            last = error.last_error
            last_name = type(last).__name__
            if isinstance(last, SigApiError):
                failure = cls(
                    name,
                    last.status,
                    last.code,
                    last.unrecognized_code,
                    None,
                    error.attempts,
                    last_name,
                )
                return Outcome.HTTP_ERROR, failure
            return Outcome.FAILED, cls(name, None, None, False, None, error.attempts, last_name)
        if isinstance(error, SigDeadlineExceededError):
            last_name = None if error.last_error is None else type(error.last_error).__name__
            failure = cls(name, None, None, False, error.phase, error.attempts, last_name)
            return Outcome.FAILED, failure
        return Outcome.FAILED, cls(name, None, None, False, None, None, None)

    def describe(self) -> str:
        parts = [self.error_class]
        if self.http_status is not None:
            shown = self.code or ("unrecognized code" if self.unrecognized_code else "no code")
            parts.append(f"HTTP {self.http_status} {shown}")
        if self.phase is not None:
            parts.append(f"phase {self.phase}")
        if self.attempts is not None:
            parts.append(f"{self.attempts} attempt(s)")
        if self.last_error_class is not None:
            parts.append(f"last {self.last_error_class}")
        return ", ".join(parts)


# --- immutability ----------------------------------------------------------------------------

_IMMUTABLE_SCALARS = (str, int, float, Decimal, datetime, Enum, type(None))


def _check_immutable(value: object, where: str) -> None:
    """Raise TypeError unless `value` cannot be changed through any reachable reference.

    Accepted: immutable scalars (bool is an int), tuples and frozensets of accepted values,
    read-only mappings (MappingProxyType) of them, frozen dataclasses and frozen pydantic
    models whose fields are accepted and which hold no extra fields. Anything else, such
    as a list or dict reachable from a retained observation, is refused.
    """
    if isinstance(value, _IMMUTABLE_SCALARS):
        return
    if isinstance(value, (tuple, frozenset)):
        for index, item in enumerate(value):
            _check_immutable(item, f"{where}[{index}]")
        return
    if isinstance(value, MappingProxyType):
        for key, item in value.items():
            _check_immutable(key, f"{where}<key>")
            _check_immutable(item, f"{where}[{key!r}]")
        return
    if isinstance(value, BaseModel):
        if value.model_config.get("frozen") is not True or value.model_extra:
            raise TypeError(f"{where}: {type(value).__name__} is not a frozen wire model")
        for name in type(value).model_fields:
            _check_immutable(getattr(value, name), f"{where}.{name}")
        return
    if dataclasses.is_dataclass(value) and not isinstance(value, type):
        if not type(value).__dataclass_params__.frozen:  # type: ignore[attr-defined]
            raise TypeError(f"{where}: {type(value).__name__} is not a frozen dataclass")
        for field in dataclasses.fields(value):
            _check_immutable(getattr(value, field.name), f"{where}.{field.name}")
        return
    raise TypeError(f"{where}: {type(value).__name__} is mutable or not known to be immutable")


# --- component results ---------------------------------------------------------------------


@dataclass(frozen=True)
class ComponentResult:
    """Exactly one outcome for one component, with what that outcome keeps.

    - OBSERVED, OBSERVED_INCOMPLETE, UNVALUED: `read` is the A2 result exactly as
      returned (rows as sent, an empty list included); timing from its requests.
    - HTTP_ERROR, FAILED, SCOPE_MISMATCH: `failure` and call-boundary timing; no read.
    - NOT_REQUESTED: nothing.
    """

    component: ComponentId
    outcome: Outcome
    read: ComponentRead | None
    failure: ComponentFailure | None
    timing: ComponentTiming | None

    def __post_init__(self) -> None:
        if not isinstance(self.component, ComponentId):
            raise TypeError("component must be a ComponentId")
        if not isinstance(self.outcome, Outcome):
            raise TypeError("outcome must be an Outcome")
        kind = self.component.kind
        # Findings, reasons and the status are computed from what is kept here.
        _check_immutable(self.read, f"{self.component.label}.read")
        _check_immutable(self.failure, f"{self.component.label}.failure")
        if self.outcome is Outcome.NOT_REQUESTED:
            if kind in REQUIRED_KINDS:
                raise ValueError("a required component is always requested")
            if (self.read, self.failure, self.timing) != (None, None, None):
                raise ValueError("a component that was not requested keeps nothing")
            return
        if kind is ComponentKind.ORDER_FILLS and self.component.order_id is None:
            raise ValueError("a requested order_fills component names its order")
        if self.timing is None:
            raise ValueError("a requested component has timing")
        if self.outcome in _READ_OUTCOMES:
            if self.failure is not None or not isinstance(self.read, _READ_TYPES[kind]):
                raise TypeError(f"{self.component.label}: not an A2 result for this component")
            if _read_outcome(self.read) is not self.outcome:
                raise ValueError(f"{self.component.label}: outcome does not match the read")
            if self.timing != ComponentTiming.from_requests(_requests(self.read)):
                raise ValueError(f"{self.component.label}: timing must come from the requests")
            if isinstance(self.read, OrderFillsWalk) and (
                self.read.order_id != self.component.order_id
            ):
                raise ValueError("order fills are for a different order than the component")
        else:
            if self.read is not None or not isinstance(self.failure, ComponentFailure):
                raise TypeError(f"{self.component.label}: a failed read keeps its failure only")
            if self.timing.source is not TimingSource.CALL:
                raise ValueError("a failed read has call-boundary timing")

    @classmethod
    def observed(cls, component: ComponentId, read: ComponentRead) -> ComponentResult:
        if not isinstance(read, _READ_TYPES.get(component.kind, ())):
            raise TypeError(f"{component.label}: not an A2 result for this component")
        timing = ComponentTiming.from_requests(_requests(read))
        return cls(component, _read_outcome(read), read, None, timing)

    @classmethod
    def failed(
        cls,
        component: ComponentId,
        error: SigClientError,
        *,
        started_mono: float,
        finished_mono: float,
        finished_utc: datetime,
    ) -> ComponentResult:
        outcome, failure = ComponentFailure.from_error(error)
        timing = ComponentTiming(
            TimingSource.CALL, started_mono, finished_mono, None, None, finished_utc
        )
        return cls(component, outcome, None, failure, timing)

    @classmethod
    def not_requested(cls, component: ComponentId) -> ComponentResult:
        return cls(component, Outcome.NOT_REQUESTED, None, None, None)

    @property
    def requests(self) -> tuple[ReadProvenance, ...]:
        """The requests this component reported (none for a failed or unrequested one)."""
        return () if self.read is None else _requests(self.read)


# --- identity and balance observations (design 5.5, 5.7) ------------------------------------


class Identity(StrEnum):
    MATCH = "MATCH"
    MISMATCH = "MISMATCH"
    NOT_OBSERVED = "NOT_OBSERVED"


_IDENTITY_BY_OUTCOME = MappingProxyType(
    {
        Outcome.OBSERVED: Identity.MATCH,
        Outcome.SCOPE_MISMATCH: Identity.MISMATCH,
        Outcome.HTTP_ERROR: Identity.NOT_OBSERVED,
        Outcome.FAILED: Identity.NOT_OBSERVED,
    }
)


@dataclass(frozen=True)
class IdentityState:
    """Each balance read's identity, classified separately; never from a cache."""

    opening: Identity
    closing: Identity

    @property
    def overall(self) -> Identity:
        if Identity.MISMATCH in (self.opening, self.closing):
            return Identity.MISMATCH
        if Identity.NOT_OBSERVED in (self.opening, self.closing):
            return Identity.NOT_OBSERVED
        return Identity.MATCH


class BalanceRole(StrEnum):
    OPENING = "OPENING"
    CLOSING = "CLOSING"


BALANCE_FIELDS = ("myBalance", "isPendingEnrolment", "initialBalance", "currencyName")


@dataclass(frozen=True)
class BalanceObservation:
    """One of the two fresh balance reads, kept in full whatever its outcome.

    Values are those of that response exactly as sent, or None when it was not observed.
    `provenance` is the read's own record when it succeeded, else None (a failed read has
    no provenance); `failure` and `timing` describe a failed read.
    """

    role: BalanceRole
    outcome: Outcome
    identity: Identity
    my_balance: Decimal | None
    is_pending_enrolment: bool | None
    initial_balance: Decimal | None
    currency_name: str | None
    provenance: ReadProvenance | None
    failure: ComponentFailure | None
    timing: ComponentTiming

    @classmethod
    def from_result(cls, result: ComponentResult) -> BalanceObservation:
        roles = {OPENING: BalanceRole.OPENING, CLOSING: BalanceRole.CLOSING}
        if not isinstance(result, ComponentResult) or result.component not in roles:
            raise TypeError("a balance observation comes only from a balance component result")
        identity = _IDENTITY_BY_OUTCOME[result.outcome]
        assert result.timing is not None  # required components always have timing
        if result.read is None:
            return cls(
                roles[result.component],
                result.outcome,
                identity,
                None,
                None,
                None,
                None,
                None,
                result.failure,
                result.timing,
            )
        read = result.read
        assert isinstance(read, TournamentBalanceRead)
        provenance = read.provenance
        # Both balance observations are fresh reads made by this assembly (design 3.3).
        if (
            provenance.route != TOURNAMENT_ROUTE
            or provenance.scope is not ScopeEvidence.ECHO
            or provenance.verification is not VerificationUse.NOT_APPLICABLE
            or provenance.page_index is not None
        ):
            raise ValueError("a balance observation must be a fresh GET /tournaments/{slug} read")
        tournament: Tournament = read.tournament
        return cls(
            roles[result.component],
            result.outcome,
            identity,
            tournament.myBalance,
            tournament.isPendingEnrolment,
            tournament.initialBalance,
            tournament.currencyName,
            provenance,
            None,
            result.timing,
        )

    def values(self) -> tuple[object, ...]:
        return (
            self.my_balance,
            self.is_pending_enrolment,
            self.initial_balance,
            self.currency_name,
        )


def _same_exactly(left: object, right: object) -> bool:
    """Agreement of two balance-field values as A2 decoded them (K13 and cash).

    Numbers agree when they are equal Decimals with the same (sign, digits, exponent),
    as decoded by A2 (`parse_float=Decimal`; JSON integers via `Decimal(int)`). So
    "847.5" and "847.50" differ, and "100" and "1e2" differ, but "100" and "1.00e2"
    agree, as do "1e2" and "1E+2": A2 keeps the decoded Decimal, not the JSON text,
    so lexemes that decode to the same triple cannot be told apart here. This is not a
    byte-exact comparison of the response. Other values agree when they have the same
    type and are equal.
    """
    if isinstance(left, Decimal) or isinstance(right, Decimal):
        return (
            isinstance(left, Decimal)
            and isinstance(right, Decimal)
            and left == right
            and left.as_tuple() == right.as_tuple()
        )
    return type(left) is type(right) and left == right


def balance_differences(
    opening: BalanceObservation, closing: BalanceObservation
) -> tuple[str, ...]:
    """Wire names of the fields on which two observed balance reads differ (K13)."""
    if opening.outcome is not Outcome.OBSERVED or closing.outcome is not Outcome.OBSERVED:
        return ()
    return tuple(
        name
        for name, left, right in zip(
            BALANCE_FIELDS, opening.values(), closing.values(), strict=True
        )
        if not _same_exactly(left, right)
    )


# --- cash, conclusions and access (design 5.6, 5.7) ---------------------------------------


class CashSource(StrEnum):
    CLOSING = "CLOSING"
    NONE = "NONE"


class ReservationKnowledge(StrEnum):
    UNKNOWN = "UNKNOWN"  # OQ-C3: whether resting buys are deducted is unanswered


@dataclass(frozen=True)
class CashCandidate:
    """A candidate tournament cash value, never established and never available for orders.

    `value` is the closing read's `myBalance` only when both balance reads were OBSERVED
    with identity MATCH and agreed on myBalance, isPendingEnrolment, initialBalance and
    currencyName under `_same_exactly` (equal decoded Decimals with the same sign, digits
    and exponent; not the JSON text); otherwise None with source NONE. `currency` and
    `reported_pending_at_source` come from the same closing read under the same rule.
    `source_established` is always False (OQ-W11) and `excludes_resting_orders` always
    UNKNOWN (OQ-C3) until Arnav records evidence.
    """

    value: Decimal | None
    source: CashSource
    currency: str | None
    reported_pending_at_source: bool | None
    source_established: Literal[False] = False
    excludes_resting_orders: ReservationKnowledge = ReservationKnowledge.UNKNOWN

    def __post_init__(self) -> None:
        if self.source_established is not False:
            raise ValueError("the cash source is not established (OQ-W11)")
        if self.excludes_resting_orders is not ReservationKnowledge.UNKNOWN:
            raise ValueError("whether resting buys are excluded is unknown (OQ-C3)")
        if self.source is CashSource.NONE:
            if (self.value, self.currency, self.reported_pending_at_source) != (None, None, None):
                raise ValueError("no source, no candidate value")
        elif self.value is None or self.currency is None or self.reported_pending_at_source is None:
            raise ValueError("a candidate from the closing read keeps value, currency and flag")


class Membership(StrEnum):
    NOT_DETERMINED = "NOT_DETERMINED"


class SeedOrCurrent(StrEnum):
    NOT_DETERMINED = "NOT_DETERMINED"


@dataclass(frozen=True)
class Conclusions:
    """A3 draws no conclusion about membership or seed versus current balance (5.6)."""

    membership: Membership = Membership.NOT_DETERMINED
    seed_or_current: SeedOrCurrent = SeedOrCurrent.NOT_DETERMINED


@dataclass(frozen=True)
class PortfolioAccess:
    """What one requested portfolio read returned: outcome, HTTP status, documented code."""

    component: ComponentId
    outcome: Outcome
    http_status: int | None
    code: str | None


CANDIDATE_EQUITY_LABEL = "candidate, OQ-D1; unused"


@dataclass(frozen=True)
class CandidateEquity:
    """`totalAccountValue` from the valuation read as sent. Not equity, and not used."""

    value: Decimal
    label: str = CANDIDATE_EQUITY_LABEL


# --- findings (design 5.10) ---------------------------------------------------------------


class FindingKind(StrEnum):
    SCOPE = "SCOPE"
    """A tournament-scope failure (K1, K12): status class 1."""

    CONTRADICTION = "CONTRADICTION"
    """Inside one response, against a documented rule for that response: class 2."""

    UNEXPLAINED = "UNEXPLAINED"
    """A difference across separate reads, or undocumented semantics: no status class."""


@dataclass(frozen=True)
class Finding:
    """One consistency check that fired. `locations` names rows by position only."""

    check_id: str
    kind: FindingKind
    components: tuple[ComponentId, ...]
    question_ids: tuple[str, ...]
    description: str
    locations: tuple[str, ...] = ()

    def reason(self) -> str:
        where = f" at {', '.join(self.locations)}" if self.locations else ""
        ids = f" [{', '.join(self.question_ids)}]" if self.question_ids else ""
        labels = ", ".join(c.label for c in self.components)
        return f"{self.check_id} {self.kind.value} ({labels}): {self.description}{where}{ids}"


_EXPECTED_SCOPE: Mapping[str, ScopeEvidence] = MappingProxyType(
    {
        TOURNAMENT_ROUTE: ScopeEvidence.ECHO,
        POSITIONS_ROUTE: ScopeEvidence.VERIFIED_SLUG_PATH,
        FILLS_ROUTE: ScopeEvidence.VERIFIED_SLUG_PATH,
        PNL_ROUTE: ScopeEvidence.VERIFIED_SLUG_PATH,
        SETTLEMENTS_ROUTE: ScopeEvidence.VERIFIED_SLUG_PATH,
        ORDERS_ROUTE: ScopeEvidence.TOURNAMENT_ID_SENT,
        ORDER_FILLS_ROUTE: ScopeEvidence.ECHO,
    }
)
_ORDER_FILLS_PATH = re.compile(r"/orders/[0-9]+/fills")


def _scope_problem(provenance: ReadProvenance, tournament: TournamentRef) -> str | None:
    """Why a recorded request is not bound to the configured tournament, or None (K1)."""
    expected = _EXPECTED_SCOPE.get(provenance.route)
    if expected is None:
        return "the route is not a tournament-bound account read"
    if provenance.scope is not expected:
        return "the scope evidence does not match the route"
    if "{slug}" in provenance.route:
        if provenance.path != provenance.route.replace("{slug}", quote(tournament.slug, safe="")):
            return "the path does not name the configured tournament slug"
    elif provenance.route == ORDERS_ROUTE:
        if provenance.path != ORDERS_ROUTE or dict(provenance.params).get("tournamentId") != (
            tournament.id
        ):
            return "the configured tournamentId was not sent"
    elif not _ORDER_FILLS_PATH.fullmatch(provenance.path):
        return "the path is not an order-fills read"
    return None


def _parse_instant(text: str) -> datetime | None:
    try:
        moment = datetime.fromisoformat(text)
    except ValueError:
        return None
    return moment if moment.utcoffset() is not None else None


def _lot_disagrees(row: TournamentPosition) -> bool:
    # [A3 policy] lot semantics are undocumented (OQ-W5, OQ-W8). A lot side is compared
    # without case with the side the row's sign implies, and the lots' absolute sizes
    # with the row's absolute size. Any difference is reported, never resolved.
    implied = "yes" if row.quantity > 0 else "no" if row.quantity < 0 else None
    if any(lot.side.casefold() != implied for lot in row.lots):
        return True
    return sum((abs(lot.quantity) for lot in row.lots), Decimal(0)) != abs(row.quantity)


def _fill_sign_disagrees(side: str, quantity: Decimal) -> bool:
    return (side == "yes" and quantity < 0) or (side == "no" and quantity > 0)


def _row_findings(
    results: Mapping[ComponentId, ComponentResult], evaluated_utc: datetime
) -> list[Finding]:
    found: list[Finding] = []
    positions = results[POSITIONS].read
    if isinstance(positions, PositionsRead):
        k6 = tuple(
            f"row {i}"
            for i, row in enumerate(positions.rows)
            if row.currentPrice is None and row.quantity != 0
        )
        if k6:
            found.append(
                Finding(
                    "K6",
                    FindingKind.CONTRADICTION,
                    (POSITIONS,),
                    ("OQ-D3",),
                    "a nonzero position has a null currentPrice in the same response "
                    "(documented null only for zero-quantity rows)",
                    k6,
                )
            )
        k7 = tuple(f"row {i}" for i, row in enumerate(positions.rows) if _lot_disagrees(row))
        if k7:
            found.append(
                Finding(
                    "K7",
                    FindingKind.UNEXPLAINED,
                    (POSITIONS,),
                    ("OQ-W5", "OQ-W8"),
                    "lot sides or sizes differ from the row quantity's sign or size "
                    "(lot semantics undocumented)",
                    k7,
                )
            )
    for component, result in results.items():
        read = result.read
        if isinstance(read, FillsWalk):
            rows: Sequence[TournamentFill | OrderFill] = read.fills
        elif isinstance(read, OrderFillsWalk):
            rows = read.fills
        else:
            continue
        k8 = tuple(
            f"fill {i}"
            for i, fill in enumerate(rows)
            if _fill_sign_disagrees(fill.side, fill.quantity)
        )
        if k8:
            found.append(
                Finding(
                    "K8",
                    FindingKind.CONTRADICTION,
                    (component,),
                    ("OQ-W4",),
                    "a fill's quantity sign disagrees with its side in the same row",
                    k8,
                )
            )
    orders = results[OPEN_ORDERS].read
    if isinstance(orders, OrdersWalk):
        k9 = tuple(f"order {i}" for i, order in enumerate(orders.orders) if order.open is not True)
        if k9:
            found.append(
                Finding(
                    "K9",
                    FindingKind.CONTRADICTION,
                    (OPEN_ORDERS,),
                    ("A1-N5",),
                    "a row of a status=open response reports open=false",
                    k9,
                )
            )
        k10 = []
        for i, order in enumerate(orders.orders):
            if order.expirationDate is None:
                continue
            moment = _parse_instant(order.expirationDate)
            if moment is None or moment <= evaluated_utc:
                k10.append(f"order {i}")
        if k10:
            found.append(
                Finding(
                    "K10",
                    FindingKind.UNEXPLAINED,
                    (OPEN_ORDERS,),
                    ("A1-N5",),
                    "an open order's expirationDate is at or before the local clock, or could "
                    "not be compared with it (local-clock skew unknown)",
                    tuple(k10),
                )
            )
    return found


def _cross_read_findings(
    results: Mapping[ComponentId, ComponentResult],
    balances: Sequence[BalanceObservation],
    differences: tuple[str, ...],
) -> list[Finding]:
    found: list[Finding] = []
    components = {BalanceRole.OPENING: OPENING, BalanceRole.CLOSING: CLOSING}
    portfolio = [results[c] for c in results if c.kind in PORTFOLIO_KINDS]
    for balance in balances:
        if balance.outcome is not Outcome.OBSERVED:
            continue
        source = components[balance.role]
        for result in portfolio:
            if balance.is_pending_enrolment is True and result.outcome in _HTTP_200_OUTCOMES:
                found.append(
                    Finding(
                        "K3",
                        FindingKind.UNEXPLAINED,
                        (source, result.component),
                        ("A1-N4",),
                        "pending enrolment was reported, and a separate portfolio read "
                        "returned 200; no documented guarantee ties the two reads",
                    )
                )
            failure = result.failure
            if (
                balance.is_pending_enrolment is False
                and failure is not None
                and result.outcome is Outcome.HTTP_ERROR
                and failure.http_status == 403
                and failure.code == "FORBIDDEN"
            ):
                found.append(
                    Finding(
                        "K4",
                        FindingKind.UNEXPLAINED,
                        (source, result.component),
                        ("A1-N4",),
                        "enrolment was reported as not pending, and a separate portfolio read "
                        "returned 403 FORBIDDEN (a generic code)",
                    )
                )
        if balance.is_pending_enrolment is True and balance.my_balance != balance.initial_balance:
            found.append(
                Finding(
                    "K5",
                    FindingKind.UNEXPLAINED,
                    (source,),
                    ("OQ-W11", "A1-N4"),
                    "pending enrolment was reported with myBalance different from "
                    "initialBalance (a seed is not documented to equal initialBalance)",
                )
            )
    positions = results[POSITIONS].outcome
    valuation = results[VALUATION].outcome
    if {positions, valuation} == {Outcome.UNVALUED, Outcome.OBSERVED}:
        found.append(
            Finding(
                "K11",
                FindingKind.UNEXPLAINED,
                (POSITIONS, VALUATION),
                ("OQ-D3", "A1-N1"),
                "one of positions and valuation returned the missing-valuation 409 and the "
                "other returned 200; separate reads, reported and not reconciled",
            )
        )
    if differences:
        found.append(
            Finding(
                "K13",
                FindingKind.UNEXPLAINED,
                (OPENING, CLOSING),
                ("A1-N1", "A1-N3"),
                f"the opening and closing balance reads differ in {', '.join(differences)}",
            )
        )
    return found


# --- status (design 5.5) ------------------------------------------------------------------


class AccountStatus(StrEnum):
    """Most severe first. There is deliberately no READY member."""

    SCOPE_MISMATCH = "SCOPE_MISMATCH"
    CONTRADICTED = "CONTRADICTED"
    UNAVAILABLE = "UNAVAILABLE"
    INCOMPLETE = "INCOMPLETE"
    OBSERVED_UNRESOLVED = "OBSERVED_UNRESOLVED"


_SEVERITY = (
    AccountStatus.SCOPE_MISMATCH,
    AccountStatus.CONTRADICTED,
    AccountStatus.UNAVAILABLE,
    AccountStatus.INCOMPLETE,
)
_OUTCOME_CLASS: Mapping[Outcome, AccountStatus | None] = MappingProxyType(
    {
        Outcome.OBSERVED: None,
        Outcome.NOT_REQUESTED: None,
        Outcome.SCOPE_MISMATCH: AccountStatus.SCOPE_MISMATCH,
        Outcome.HTTP_ERROR: AccountStatus.UNAVAILABLE,
        Outcome.FAILED: AccountStatus.UNAVAILABLE,
        Outcome.UNVALUED: AccountStatus.INCOMPLETE,
        Outcome.OBSERVED_INCOMPLETE: AccountStatus.INCOMPLETE,
    }
)
_IDENTITY_CLASS: Mapping[Identity, AccountStatus | None] = MappingProxyType(
    {
        Identity.MATCH: None,
        Identity.MISMATCH: AccountStatus.SCOPE_MISMATCH,
        Identity.NOT_OBSERVED: AccountStatus.UNAVAILABLE,
    }
)
_FINDING_CLASS: Mapping[FindingKind, AccountStatus | None] = MappingProxyType(
    {
        FindingKind.SCOPE: AccountStatus.SCOPE_MISMATCH,
        FindingKind.CONTRADICTION: AccountStatus.CONTRADICTED,
        FindingKind.UNEXPLAINED: None,
    }
)


def account_status(
    *,
    opening: Identity,
    closing: Identity,
    outcomes: Iterable[Outcome],
    finding_kinds: Iterable[FindingKind],
    balance_differs: bool,
) -> AccountStatus:
    """The most severe class among the inputs (design 5.5), else OBSERVED_UNRESOLVED.

    Every input maps to exactly one class or to none; an input outside the enums raises.
    """
    if not isinstance(balance_differs, bool):
        raise TypeError("balance_differs must be a bool")
    present: set[AccountStatus | None] = set()
    for identity in (opening, closing):
        if not isinstance(identity, Identity):
            raise TypeError("identity must be an Identity")
        present.add(_IDENTITY_CLASS[identity])
    for outcome in outcomes:
        if not isinstance(outcome, Outcome):
            raise TypeError("outcome must be an Outcome")
        present.add(_OUTCOME_CLASS[outcome])
    for kind in finding_kinds:
        if not isinstance(kind, FindingKind):
            raise TypeError("finding kind must be a FindingKind")
        present.add(_FINDING_CLASS[kind])
    if balance_differs:
        present.add(AccountStatus.INCOMPLETE)
    for status in _SEVERITY:
        if status in present:
            return status
    return AccountStatus.OBSERVED_UNRESOLVED


# --- vouching (design 5.11) ---------------------------------------------------------------

CANNOT_VOUCH_FOR: tuple[str, ...] = (
    "that myBalance is cash available for new orders (OQ-W11, OQ-C3)",
    (
        "whether myBalance is a seed or a current balance, and whether the account is "
        "enrolled (design 5.6; A1-N4)"
    ),
    "that an empty response means SIG holds nothing of that kind for the account (5.3; A1-N1)",
    "that the reads describe one instant (no atomicity)",
    "that any read reflects every engine event up to its time (projection freshness, A1-N1)",
    "that any local age bounds the age of SIG's state (5.8)",
    "the units or denomination of any price, cost or value field (OQ-W4 to OQ-W7)",
    "that positions, fills, orders and cash agree with one another",
    "that no order or fill exists that the engine did not place (OQ-E1, OQ-E2; needs O2)",
    "anything that happened before this process started (restart recovery, G-3)",
    "market readiness of any market",
)
NOT_CHECKED: tuple[str, ...] = (
    "not checked: positions against fills (OQ-W4, OQ-W5, OQ-W8)",
    "not checked: fills against orders (OQ-W4, OQ-W8)",
    "not checked: cash against settlements or fills (OQ-W4, OQ-W5, A1-N3)",
    "not checked: summary totals against row sums (OQ-W5)",
    "not compared: any projectedThroughSequence with another page, endpoint or sequence (A1-N1)",
)


def _vouches(results: Mapping[ComponentId, ComponentResult], cash: CashCandidate) -> list[str]:
    said: list[str] = []
    if all(results[c].outcome is Outcome.OBSERVED for c in (OPENING, CLOSING)):
        said.append("both balance reads echoed the configured tournament identity (slug and UUID)")
    if results[POSITIONS].outcome is Outcome.OBSERVED:
        said.append("the positions response held no valuation conflict")
    for component, result in results.items():
        read = result.read
        if isinstance(read, OrdersWalk):
            said.append(
                f"every page of the open-orders walk was read ({len(read.pages)} page(s)), "
                "with no repeated cursor or duplicate id"
            )
        elif isinstance(read, FillsWalk):
            said.append(
                f"every page of the fills walk was read ({len(read.pages)} page(s)), with no "
                "repeated cursor or duplicate id, newest first"
            )
        elif isinstance(read, PnlRead):
            said.append("the valuation response was for period=all and held no valuation conflict")
        elif isinstance(read, SettlementsWalk):
            if read.complete:
                said.append(
                    f"every page of the settlements walk was read ({len(read.pages)} page(s)), "
                    "each reporting coverage.complete true"
                )
            else:
                said.append(
                    f"every page SIG offered in the settlements walk was read "
                    f"({len(read.pages)} page(s)); some reported coverage.complete false"
                )
        elif isinstance(read, OrderFillsWalk):
            said.append(
                f"{component.label}: every page was read and echoed the configured tournament "
                "and the same lifetime totals"
            )
    if cash.source is CashSource.CLOSING:
        said.append(
            "the opening and closing balance responses agreed on myBalance, "
            "isPendingEnrolment, initialBalance and currencyName as decoded (equal Decimals "
            "with the same sign, digits and exponent; not a comparison of the JSON text)"
        )
    return said


# --- unresolved fields (design 5.9) -------------------------------------------------------

_COMPONENT_MODELS: Mapping[ComponentKind, tuple[type, ...]] = MappingProxyType(
    {
        ComponentKind.BALANCE_OPENING: (Tournament,),
        ComponentKind.BALANCE_CLOSING: (Tournament,),
        ComponentKind.POSITIONS: (TournamentPosition, PositionLot, PositionsSummary),
        ComponentKind.OPEN_ORDERS: (Order, OrderCoverage),
        ComponentKind.FILLS: (TournamentFill, FillsCoverage),
        ComponentKind.VALUATION: (TournamentPnl,),
        ComponentKind.SETTLEMENTS: (TournamentSettlementEntry,),
        ComponentKind.ORDER_FILLS: (OrderFillsPage, OrderFill, OrderFillsCoverage),
    }
)


def _unresolved_fields(
    results: Iterable[ComponentResult],
) -> Mapping[tuple[str, str], NumericFieldStatus]:
    models: set[str] = set()
    for result in results:
        if result.outcome in _HTTP_200_OUTCOMES:
            models.update(model.__name__ for model in _COMPONENT_MODELS[result.component.kind])
    return MappingProxyType(
        {key: status for key, status in NUMERIC_FIELD_STATUS.items() if key[0] in models}
    )


# --- the snapshot ---------------------------------------------------------------------------


@dataclass(frozen=True)
class AccountSnapshot:
    """One assembly's account observations (design 5.4). Not a readiness result.

    `components` holds one result per requested component in assembly order, followed by
    one NOT_REQUESTED result per optional component left out. Times are local; ages are
    derived with `component_ages`, `oldest_component` and `completion_age`.
    """

    tournament: TournamentRef
    status: AccountStatus
    requested: tuple[ComponentId, ...]
    components: tuple[ComponentResult, ...]
    reasons: tuple[str, ...]
    findings: tuple[Finding, ...]
    assembled_from_mono: float
    assembled_to_mono: float
    evaluated_mono: float
    evaluated_utc: datetime
    reads: tuple[ReadProvenance, ...]
    identity: IdentityState
    balance_opening: BalanceObservation
    balance_closing: BalanceObservation
    balance_differences: tuple[str, ...]
    portfolio_access: tuple[PortfolioAccess, ...]
    conclusions: Conclusions
    cash: CashCandidate
    candidate_equity: CandidateEquity | None
    unresolved_fields: Mapping[tuple[str, str], NumericFieldStatus]
    vouches_for: tuple[str, ...]
    cannot_vouch_for: tuple[str, ...]
    does_not_cover: str = DOES_NOT_COVER

    @property
    def component_outcomes(self) -> Mapping[ComponentId, Outcome]:
        return MappingProxyType({r.component: r.outcome for r in self.components})

    def component(self, component: ComponentId) -> ComponentResult:
        for result in self.components:
            if result.component == component:
                return result
        raise KeyError(component.label)

    @property
    def positions(self) -> ComponentResult:
        return self.component(POSITIONS)

    @property
    def open_orders(self) -> ComponentResult:
        return self.component(OPEN_ORDERS)

    @property
    def fills(self) -> ComponentResult:
        return self.component(FILLS)

    @property
    def valuation(self) -> ComponentResult:
        return self.component(VALUATION)

    @property
    def settlements(self) -> ComponentResult:
        return self.component(SETTLEMENTS)

    @property
    def order_fills(self) -> Mapping[int, ComponentResult]:
        """Requested order id -> its result; empty when no order was named."""
        return MappingProxyType(
            {
                r.component.order_id: r
                for r in self.components
                if r.component.kind is ComponentKind.ORDER_FILLS
                and r.component.order_id is not None
            }
        )

    # Ages (design 5.8). Every one is local and measures our own requests only.

    @property
    def assembly_span(self) -> float:
        return self.assembled_to_mono - self.assembled_from_mono

    def completion_age(self, at_mono: float) -> float:
        """Time since the last request finished. Not the age of any data."""
        return at_mono - self.assembled_to_mono

    def component_ages(self, at_mono: float) -> tuple[ComponentAge, ...]:
        """The age of every requested component (opening balance included), in order."""
        _check_time("at_mono", at_mono)
        ages = []
        for result in self.components:
            timing = result.timing
            if timing is None:
                continue  # NOT_REQUESTED: never asked for, so it has no age
            first = timing.first_finished_mono
            ages.append(
                ComponentAge(
                    component=result.component,
                    outcome=result.outcome,
                    at_mono=at_mono,
                    age=at_mono - timing.started_mono,
                    since_first_response=None if first is None else at_mono - first,
                    timing_source=timing.source,
                )
            )
        return tuple(ages)

    def oldest_component(self, at_mono: float) -> ComponentAge:
        """The component with the largest age (the earliest in assembly order on a tie)."""
        ages = self.component_ages(at_mono)
        oldest = ages[0]
        for age in ages[1:]:
            if age.age > oldest.age:
                oldest = age
        return oldest


def _outcome_reason(result: ComponentResult) -> str | None:
    label = result.component.label
    outcome = result.outcome
    read = result.read
    if outcome is Outcome.OBSERVED:
        return None
    if outcome is Outcome.NOT_REQUESTED:
        return f"{label}: NOT_REQUESTED (excluded from the status; not vouched for)"
    if outcome is Outcome.OBSERVED_INCOMPLETE:
        assert isinstance(read, SettlementsWalk)
        pages = ", ".join(str(i) for i in read.incomplete_pages)
        return (
            f"{label}: OBSERVED_INCOMPLETE (pages reporting coverage.complete false: {pages}; "
            "never presented as complete) [A1-N2]"
        )
    if outcome is Outcome.UNVALUED:
        assert isinstance(read, UnvaluedHoldingsRead)
        return (
            f"{label}: UNVALUED ({len(read.missing)} holding(s) reported without a valuation "
            "price; none valued at a default) [OQ-D3]"
        )
    failure = result.failure
    assert failure is not None
    text = f"{label}: {outcome.value} ({failure.describe()}"
    if outcome is Outcome.FAILED and label != OPENING.label and label != CLOSING.label:
        text += "; a failed walk returns no pages, so the failing page is not reported"
    text += ")"
    if failure.http_status == 403:
        text += " [A1-N4: a 403 is not read as membership or non-membership]"
    return text


def _identity_reason(role: str, observation: BalanceObservation) -> str | None:
    if observation.identity is Identity.MATCH:
        return None
    if observation.identity is Identity.MISMATCH:
        return (
            f"{role} identity MISMATCH: the read reported another tournament, or the client's "
            "mismatch latch raised"
        )
    return (
        f"{role} identity NOT_OBSERVED: the read ended {observation.outcome.value} before any "
        "identity check"
    )


def _cash(opening: BalanceObservation, closing: BalanceObservation) -> tuple[CashCandidate, str]:
    missing = [
        o.role.value.lower()
        for o in (opening, closing)
        if o.outcome is not Outcome.OBSERVED or o.identity is not Identity.MATCH
    ]
    if missing:
        reason = (
            f"cash: no candidate value; the {' and '.join(missing)} balance read(s) were not "
            "observed with identity MATCH [OQ-W11]"
        )
        return CashCandidate(None, CashSource.NONE, None, None), reason
    differences = balance_differences(opening, closing)
    if differences:
        reason = (
            "cash: no candidate value; the balance reads differ in "
            f"{', '.join(differences)} [OQ-W11, A1-N3]"
        )
        return CashCandidate(None, CashSource.NONE, None, None), reason
    assert closing.my_balance is not None and closing.currency_name is not None
    assert closing.is_pending_enrolment is not None
    candidate = CashCandidate(
        closing.my_balance, CashSource.CLOSING, closing.currency_name, closing.is_pending_enrolment
    )
    reason = (
        "cash: candidate value from the closing read only; not established (OQ-W11), "
        "resting-order reservation unknown (OQ-C3), not available for orders"
    )
    return candidate, reason


def _access(result: ComponentResult) -> PortfolioAccess:
    if isinstance(result.read, UnvaluedHoldingsRead):
        # Accepted by A2 only for a valid MissingTournamentValuation body (code CONFLICT).
        return PortfolioAccess(result.component, result.outcome, 409, "CONFLICT")
    if result.read is not None:
        status = result.requests[-1].http_status
        return PortfolioAccess(result.component, result.outcome, status, None)
    failure = result.failure
    assert failure is not None
    return PortfolioAccess(result.component, result.outcome, failure.http_status, failure.code)


def build_account_snapshot(
    tournament: TournamentRef,
    request: SnapshotRequest,
    results: Sequence[ComponentResult],
    *,
    evaluated_mono: float,
    evaluated_utc: datetime,
) -> AccountSnapshot:
    """Evaluate one assembly's component results. Pure: makes no request.

    `results` must hold exactly the requested components, in assembly order, each
    starting no earlier than the previous one finished (so both balance observations are
    reads made inside this assembly: opening first, closing last). Raises ValueError or
    TypeError otherwise.
    """
    if not isinstance(tournament, TournamentRef):
        raise TypeError("tournament must be the configured TournamentRef")
    if not isinstance(request, SnapshotRequest):
        raise TypeError("request must be a SnapshotRequest")
    requested = request.components()
    if tuple(getattr(r, "component", None) for r in results) != requested:
        raise ValueError("results must hold exactly the requested components, in order")
    _check_time("evaluated_mono", evaluated_mono)
    _check_utc("evaluated_utc", evaluated_utc)
    previous_finish: float | None = None
    for result in results:
        if not isinstance(result, ComponentResult) or result.outcome is Outcome.NOT_REQUESTED:
            raise ValueError("every requested component has a requested outcome")
        assert result.timing is not None
        if previous_finish is not None and result.timing.started_mono < previous_finish:
            raise ValueError("components must be read one after another, in assembly order")
        previous_finish = result.timing.finished_mono
        read = result.read
        if isinstance(read, TournamentBalanceRead) and (
            read.tournament.slug != tournament.slug or read.tournament.id.lower() != tournament.id
        ):
            raise ValueError("a balance read is for a different tournament than configured")
        if isinstance(read, OrderFillsWalk) and read.echoed_tournament_id.lower() != tournament.id:
            raise ValueError("an order-fills read is for a different tournament than configured")
    assert previous_finish is not None
    if evaluated_mono < previous_finish:
        raise ValueError("evaluation must follow the last request")

    everything = tuple(results) + tuple(
        ComponentResult.not_requested(c) for c in request.not_requested()
    )
    by_component: dict[ComponentId, ComponentResult] = {r.component: r for r in everything}
    opening = BalanceObservation.from_result(by_component[OPENING])
    closing = BalanceObservation.from_result(by_component[CLOSING])
    identity = IdentityState(opening.identity, closing.identity)
    differences = balance_differences(opening, closing)
    reads = tuple(p for r in results for p in r.requests)

    findings: list[Finding] = []
    k1 = []
    for index, provenance in enumerate(reads):
        problem = _scope_problem(provenance, tournament)
        if problem is not None:
            k1.append(f"reads[{index}] ({problem})")
    if k1:
        findings.append(
            Finding(
                "K1",
                FindingKind.SCOPE,
                tuple(r.component for r in results),
                (),
                "a recorded request is not bound to the configured tournament "
                "(a programming error)",
                tuple(k1),
            )
        )
    findings.extend(_row_findings(by_component, evaluated_utc))
    latched_earlier = False
    for result in results:
        if result.outcome is not Outcome.SCOPE_MISMATCH:
            continue
        if result.component.kind is not ComponentKind.ORDER_FILLS:
            latched_earlier = True  # only the client's identity latch raises here
            continue
        cause = (
            "after the client's mismatch latch was set earlier in this assembly"
            if latched_earlier
            else "because it echoed a tournamentId other than the configured one, or null "
            "(or the client's mismatch latch raised during its verification)"
        )
        findings.append(
            Finding(
                "K12",
                FindingKind.SCOPE,
                (result.component,),
                (),
                f"the order-fills read failed its tournament scope check {cause}",
            )
        )
    findings.extend(_cross_read_findings(by_component, (opening, closing), differences))

    status = account_status(
        opening=identity.opening,
        closing=identity.closing,
        outcomes=(r.outcome for r in everything),
        finding_kinds=(f.kind for f in findings),
        balance_differs=bool(differences),
    )
    cash, cash_reason = _cash(opening, closing)

    reasons: list[str] = []
    for role, observation in (("balance_opening", opening), ("balance_closing", closing)):
        line = _identity_reason(role, observation)
        if line is not None:
            reasons.append(line)
    reasons.extend(line for r in everything if (line := _outcome_reason(r)) is not None)
    reasons.extend(f.reason() for f in findings)
    reasons.extend(
        f"balance difference: {name} differs between the opening and closing reads [A1-N1, A1-N3]"
        for name in differences
    )
    reasons.append(cash_reason)

    valuation = by_component[VALUATION].read
    candidate_equity = (
        CandidateEquity(valuation.pnl.totalAccountValue) if isinstance(valuation, PnlRead) else None
    )
    cannot = list(CANNOT_VOUCH_FOR) + list(NOT_CHECKED)
    cannot.extend(
        f"{c.label}: not requested in this assembly, so nothing about it is observed"
        for c in request.not_requested()
    )
    timings = [r.timing for r in results if r.timing is not None]
    snapshot = AccountSnapshot(
        tournament=tournament,
        status=status,
        requested=requested,
        components=everything,
        reasons=tuple(reasons),
        findings=tuple(findings),
        assembled_from_mono=min(t.started_mono for t in timings),
        assembled_to_mono=max(t.finished_mono for t in timings),
        evaluated_mono=evaluated_mono,
        evaluated_utc=evaluated_utc,
        reads=reads,
        identity=identity,
        balance_opening=opening,
        balance_closing=closing,
        balance_differences=differences,
        portfolio_access=tuple(
            _access(by_component[c]) for c in requested if c.kind in PORTFOLIO_KINDS
        ),
        conclusions=Conclusions(),
        cash=cash,
        candidate_equity=candidate_equity,
        unresolved_fields=_unresolved_fields(everything),
        vouches_for=tuple(_vouches(by_component, cash)),
        cannot_vouch_for=tuple(cannot),
    )
    _check_immutable(snapshot, "snapshot")
    return snapshot


# --- assembly -------------------------------------------------------------------------------


async def _read(client: SigRestClient, component: ComponentId) -> ComponentRead:
    # Only the merged A2 read methods; nothing else is requested.
    kind = component.kind
    if kind in (ComponentKind.BALANCE_OPENING, ComponentKind.BALANCE_CLOSING):
        return await client.read_tournament_balance()  # always fresh, never the cache
    if kind is ComponentKind.POSITIONS:
        return await client.read_tournament_positions()
    if kind is ComponentKind.OPEN_ORDERS:
        return await client.read_open_orders()
    if kind is ComponentKind.FILLS:
        return await client.read_tournament_fills()
    if kind is ComponentKind.VALUATION:
        return await client.read_tournament_pnl(period="all")
    if kind is ComponentKind.SETTLEMENTS:
        return await client.read_tournament_settlements()
    assert component.order_id is not None
    return await client.read_order_fills(component.order_id)


async def assemble_account_snapshot(
    client: SigRestClient,
    request: SnapshotRequest | None = None,
    *,
    clock: Clock = time.monotonic,
    wall_clock: WallClock = utc_now,
) -> AccountSnapshot:
    """Read every requested component in order and evaluate the snapshot.

    `clock` and `wall_clock` must be the client's own clocks: call-boundary times of a
    failed component are compared with the provenance times of the others. A component
    whose read raises a SigClientError gets an outcome and the assembly continues; after
    an identity mismatch the client's latch fails every later read with no request.
    Cancellation, and any exception that is not a SigClientError, propagates unchanged.
    """
    if not isinstance(client, SigRestClient):
        raise TypeError("client must be a SigRestClient")
    if request is None:
        request = SnapshotRequest()
    if not isinstance(request, SnapshotRequest):
        raise TypeError("request must be a SnapshotRequest")
    results: list[ComponentResult] = []
    for component in request.components():
        started = clock()
        try:
            read = await _read(client, component)
        except SigClientError as error:
            results.append(
                ComponentResult.failed(
                    component,
                    error,
                    started_mono=started,
                    finished_mono=clock(),
                    finished_utc=wall_clock(),
                )
            )
        else:
            results.append(ComponentResult.observed(component, read))
    return build_account_snapshot(
        client.tournament,
        request,
        results,
        evaluated_mono=clock(),
        evaluated_utc=wall_clock(),
    )
