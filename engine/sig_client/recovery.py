"""Market-data recovery ledger (M2-A; design: docs/design/market-data-recovery.md, 4-9).

The ledger is the engine's record of what the realtime feed may have missed for each
market. The engine owns it and passes it to RealtimeMarketFeed; a replacement feed in the
same process is given the same ledger and inherits everything in it. It is in memory only
(persistence is a later slice, M2-C) and engine-internal: nothing here is a shared/ schema.

What it holds, per market:
- **Blocker instances** (design 4.1). One record per blocking event, not a set member: two
  settlements are two instances. The state is `open` or `quarantined`, and **both block**.
  There is no cleared state: nothing in M2-A clears an instance. Quarantine is local
  policy (this engine stops treating the market as tradable); it is not evidence that SIG
  cannot correct, reverse or reopen the market (Q9). Nothing lifts it (D-5).
- **History** (design 4.2). A ledger's origin is `unknown` unless it was created from an
  explicit BootstrapDeclaration. With unknown history every market carries an open
  HISTORY_UNKNOWN instance, which nothing in M2-A resolves, so the market is never ready.
  **BootstrapDeclaration exists for tests only.** No module in engine/ creates one until
  Arnav decides D-3 and D-4 (a test enforces this), so in production every ledger is
  `unknown` and no market is ever ready. Using one in tests is not an adoption of D-3.
- **Unobserved windows and fences** (design 4.3). Each socket or channel break, re-join,
  token refresh or expiry, and feed replacement opens a window whose fence `F` is the
  topic's last accepted revision at that instant (none if no batch was ever accepted). A
  window is confirmed through `R` only when the last accepted revision advanced from `F`
  to `R > F` through clean advancing batches alone, all processed after the fence. A
  duplicate never advances anything; a gap, a `resyncRequired` batch, a malformed batch
  with a parsed `delivery` or a batch missing a named book advances the revision without
  being clean, and then the window can never be confirmed. A window with no fence can
  never be confirmed either. Confirmation covers the revisioned `market_batch` stream of
  that topic only, under the assumption that a topic's revisions never reset (Q7): it
  says nothing about standalone `market_settled` or `book_dirty` events (Q10), about the
  account topic, or about content of batches that were not clean. While D-1 is undecided
  an unconfirmed window blocks readiness.
- **Generation** (design 4.4): raised synchronously by a new blocker instance, by a window
  opening, and by an observe/commit step that adds or changes an instance (once, after
  its own changes). Book-only events never raise it.
- **Lifecycle observations** (design 4.6): the last market-detail read applied.

The observe/commit step (design 4.6) is split in two. The feed runs step 1 (the lifetime
guard: not closing, not cancelled, before the deadline) and step 6 (at most a MarketStale
withdrawal; never MarketReady). `RecoveryLedger.apply_lifecycle` runs steps 2-5 in one
synchronous call: classify the response as current or superseded, decide, apply every
decision together, and raise the generation once if an instance was added or changed. A
lifecycle read can only restrict (a `settled` or `closed` market gets a quarantined
LIFECYCLE_NOT_OPEN, whether the response is current or superseded) or confirm (a current
read showing `settled` with the same `settledWith` as an open MARKET_SETTLED instance
moves that instance to quarantined). It never clears anything, and `open` restricts
nothing.

Labels are kept exactly as sent and are never logged (they are response data).
"""

from __future__ import annotations

import math
import weakref
from collections import deque
from collections.abc import Iterable
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Protocol

from engine.sig_client.rate_limiter import check_positive_int, check_positive_seconds
from engine.sig_client.realtime_topic import StaleReason
from engine.sig_client.realtime_wire import MarketSettled
from engine.sig_client.wire import MarketPricingContext

# Bounds that keep a pathological market from growing the ledger without limit. Reaching
# one only ever merges or drops observability detail; it never removes a blocking state.
MAX_INSTANCES_PER_MARKET = 1024
MAX_EVIDENCE_PER_INSTANCE = 8
MAX_CONFIRMED_WINDOWS = 16

# The questions for SIG (design section 10) and the decisions for Arnav (section 11)
# whose answers each blocker is missing (design 2.4).
MISSING: dict[StaleReason, tuple[str, ...]] = {
    StaleReason.MARKET_SETTLED: ("Q1", "Q2", "Q9"),
    StaleReason.NULL_TRADE_SEQUENCE: ("Q3", "Q4", "Q5", "Q6", "Q11"),
    StaleReason.REVISION_GAP: ("Q1", "Q3", "Q4", "Q6", "Q7", "Q8"),
    StaleReason.RESYNC_REQUIRED: ("Q1", "Q3", "Q4", "Q6", "Q7", "Q8", "Q12"),
    StaleReason.MALFORMED: ("Q1", "Q3", "Q4", "Q6", "Q7", "Q8"),
    StaleReason.MISSING_BOOK: ("Q3", "Q4", "Q6", "Q7"),
    StaleReason.HISTORY_UNKNOWN: ("D-3", "D-4"),
    StaleReason.LIFECYCLE_NOT_OPEN: ("Q9", "D-5"),
}
WINDOW_MISSING: tuple[str, ...] = ("D-1", "Q7", "Q10", "Q14")

COVERAGE_NOTE = (
    "A confirmation covers only the revisioned market_batch stream of this topic, for the "
    "revisions after the fence through the confirmed revision, assuming the topic's "
    "revisions never reset (Q7). It does not show that standalone market_settled or "
    "book_dirty events sent during the window were delivered (Q10), and says nothing about "
    "later revisions, batches that were not clean, or the account topic."
)
LIFECYCLE_NOTE = (
    "unversioned: market detail carries no version or read time, so its freshness "
    "relative to any realtime signal is unproven (Q1)"
)


class LedgerOrigin(StrEnum):
    UNKNOWN = "unknown"
    BOOTSTRAP = "bootstrap"
    # "loaded" (a durable ledger reloaded at start) belongs to M2-C; nothing creates it.


class InstanceState(StrEnum):
    """Both states block. There is deliberately no cleared state in M2-A."""

    OPEN = "open"
    QUARANTINED = "quarantined"


class WindowCause(StrEnum):
    SOCKET_ERROR = "socket error"
    SOCKET_CLOSED = "socket closed"
    CHANNEL_ERROR = "channel error"
    CHANNEL_TIMED_OUT = "channel timed out"
    CHANNEL_CLOSED = "channel closed"
    REJOIN = "re-join (SUBSCRIBED)"
    TOKEN_REFRESH = "token refresh"
    TOKEN_EXPIRED = "token expired"
    FEED_REPLACED = "replacement feed sharing the ledger"


class WindowState(StrEnum):
    OPEN = "open"
    CONFIRMED = "confirmed"


@dataclass(frozen=True)
class BootstrapDeclaration:
    """A declaration that history begins at each listed market's first subscription and
    that no earlier history exists.

    **Tests only in M2-A.** D-3 (who may declare bootstrap, and with what initial-state
    evidence) is undecided, and no module in engine/ may create one. The initial-state
    evidence a test declaration requires is the D-3 *candidate*: a successful, current
    lifecycle read showing `open`, started after the market's first subscription.
    Markets the declaration does not list have unknown history.
    """

    market_ids: frozenset[str]
    declared_by: str
    note: str

    def __post_init__(self) -> None:
        ids = self.market_ids
        if isinstance(ids, str) or not isinstance(ids, frozenset) or not ids:
            raise ValueError("market_ids must be a non-empty frozenset of id strings")
        if any(not isinstance(i, str) or not i.isascii() or not i.isdigit() for i in ids):
            raise ValueError("every market id must be a string of ASCII digits")
        for name in ("declared_by", "note"):
            value = getattr(self, name)
            if not isinstance(value, str) or not value.strip():
                raise ValueError(f"{name} must be a non-empty string")


@dataclass(frozen=True)
class LifecycleObservation:
    """One market-detail read that passed the lifetime guard, exactly as sent.

    `read_started_mono` and `read_finished_mono` are our own request times on the local
    monotonic clock. They order our requests; they say nothing about SIG's clock.
    """

    attempt_id: str
    generation: int  # the attempt's starting generation
    current: bool
    status: str
    settled_with: str | None
    settled_on: str | None
    read_started_mono: float
    read_finished_mono: float
    note: str = LIFECYCLE_NOTE


@dataclass(frozen=True)
class Evidence:
    """What one lifecycle read showed about one instance, and the finding."""

    observation: LifecycleObservation
    finding: str


@dataclass(frozen=True)
class BlockerInstance:
    """A snapshot of one blocker instance.

    `revisions` is the interval (after, through] of topic revisions it concerns, when
    known. `item` is the triggering `marketSettled` payload for a settlement. `repeats`
    counts later events of the same reason merged into this record once the market
    reached MAX_INSTANCES_PER_MARKET.
    """

    instance_id: str
    reason: StaleReason
    state: InstanceState
    raised_mono: float
    generation: int  # the market's generation when it was raised
    revisions: tuple[int, int] | None
    item: MarketSettled | None
    evidence: tuple[Evidence, ...]
    missing: tuple[str, ...]
    repeats: int = 0


@dataclass(frozen=True)
class UnobservedWindow:
    """A snapshot of one unobserved window. Open windows block readiness (D-1)."""

    window_id: str
    causes: tuple[WindowCause, ...]
    opened_mono: float
    generation: int  # the generation the window's opening raised the market to
    fence: int | None
    state: WindowState
    confirmed_through: int | None
    unconfirmable: str | None  # why no batch can ever confirm it, if so
    missing: tuple[str, ...] = WINDOW_MISSING


@dataclass(frozen=True)
class LifecycleResult:
    """What one apply_lifecycle call did."""

    current: bool
    changed: bool  # an instance was added or changed; the generation was raised once
    restricted: bool  # a quarantined LIFECYCLE_NOT_OPEN was added
    confirmed: tuple[str, ...]  # MARKET_SETTLED instances moved to quarantined
    initial_evidence: bool  # recorded as the bootstrap's initial-state evidence
    generation: int


@dataclass
class RecoveryAttempt:
    """One recovery attempt for one market (design 4.1, 4.5).

    It records the generation, its targets (the open instances) and its own deadline
    when it starts, after admission through the feed's recovery gate. `started_seq` and
    every read's start sequence order our own actions on the ledger's local counter.
    """

    attempt_id: str
    market_id: str
    generation: int
    targets: frozenset[str]
    started_mono: float
    deadline_mono: float
    started_seq: int
    cancelled: bool = False


@dataclass(frozen=True)
class RecoveryPolicy:
    """[client policy] Recovery bounds set by Arnav for M2-A.

    - `attempts_per_window` logical attempt *starts* per rolling `attempt_window_s`,
      feed-wide (10 per 60 s). One start is one admission through the feed's recovery
      gate; the attempt then makes one market-detail read, which may make several HTTP
      attempts under the REST client's ReadRetryPolicy, each on the shared account
      RateLimiter. The gate therefore bounds recovery's demand but reserves no account
      budget for anyone: the shared limiter has no priorities.
    - `attempt_deadline_s` (30 s): one overall deadline per attempt from its start,
      covering shared rate admission, the request and every retry wait.
    - Failed attempts back off exponentially from `backoff_base_s` (5 s) up to
      `backoff_cap_s` (120 s), with bounded jitter (between half and all of the delay).
      SIG's Retry-After is a floor: the cap never shortens it.
    - `escalate_after` (5) consecutive failed attempts mark the market `escalated`: still
      not ready, still retried at the capped interval, visible to the operator.
    """

    attempts_per_window: int = 10
    attempt_window_s: float = 60.0
    attempt_deadline_s: float = 30.0
    backoff_base_s: float = 5.0
    backoff_cap_s: float = 120.0
    escalate_after: int = 5

    def __post_init__(self) -> None:
        check_positive_int("attempts_per_window", self.attempts_per_window)
        check_positive_int("escalate_after", self.escalate_after)
        for name in ("attempt_window_s", "attempt_deadline_s", "backoff_base_s", "backoff_cap_s"):
            check_positive_seconds(name, getattr(self, name))
        if self.backoff_base_s > self.backoff_cap_s:
            raise ValueError("backoff_base_s must not exceed backoff_cap_s")

    def backoff(self, failures: int) -> float:
        """The un-jittered delay after `failures` consecutive failed attempts."""
        return min(self.backoff_cap_s, self.backoff_base_s * 2 ** min(failures - 1, 60))


def bounded_jitter(delay: float, jittered: object) -> float:
    """Check that a jitter function kept its result within [delay / 2, delay]."""
    if (
        isinstance(jittered, bool)
        or not isinstance(jittered, (int, float))
        or not math.isfinite(jittered)
        or not delay / 2 <= jittered <= delay
    ):
        raise ValueError("recovery jitter must return a delay between half and all of its input")
    return float(jittered)


class LedgerOwner(Protocol):
    """A feed using the ledger. Only a feed that has begun closing can be replaced."""

    @property
    def closing(self) -> bool: ...


@dataclass
class _Instance:
    instance_id: str
    reason: StaleReason
    state: InstanceState
    raised_mono: float
    raised_seq: int
    generation: int
    revisions: tuple[int, int] | None
    item: MarketSettled | None
    evidence: deque[Evidence] = field(
        default_factory=lambda: deque(maxlen=MAX_EVIDENCE_PER_INSTANCE)
    )
    repeats: int = 0

    def snapshot(self) -> BlockerInstance:
        return BlockerInstance(
            instance_id=self.instance_id,
            reason=self.reason,
            state=self.state,
            raised_mono=self.raised_mono,
            generation=self.generation,
            revisions=self.revisions,
            item=self.item,
            evidence=tuple(self.evidence),
            missing=MISSING[self.reason],
            repeats=self.repeats,
        )


@dataclass
class _Window:
    window_id: str
    causes: list[WindowCause]
    opened_mono: float
    generation: int
    fence: int | None
    state: WindowState = WindowState.OPEN
    confirmed_through: int | None = None
    unconfirmable: str | None = None

    def snapshot(self) -> UnobservedWindow:
        return UnobservedWindow(
            window_id=self.window_id,
            causes=tuple(self.causes),
            opened_mono=self.opened_mono,
            generation=self.generation,
            fence=self.fence,
            state=self.state,
            confirmed_through=self.confirmed_through,
            unconfirmable=self.unconfirmable,
        )


@dataclass
class _Record:
    market_id: str
    declared: bool  # listed by the ledger's bootstrap declaration
    generation: int = 0
    last_revision: int | None = None
    instances: list[_Instance] = field(default_factory=list)
    windows: list[_Window] = field(default_factory=list)  # open windows
    confirmed: deque[_Window] = field(default_factory=lambda: deque(maxlen=MAX_CONFIRMED_WINDOWS))
    history_began_seq: int | None = None  # bootstrap: the market's first subscription
    initial_evidence: LifecycleObservation | None = None
    lifecycle: LifecycleObservation | None = None
    feeds: int = 0  # feeds that have registered this market


@dataclass(frozen=True)
class InstanceSpec:
    """A blocker instance to raise: its reason, revision interval and triggering item."""

    reason: StaleReason
    revisions: tuple[int, int] | None = None
    item: MarketSettled | None = None


class RecoveryLedger:
    """Engine-owned, in-memory recovery state for the markets of one tournament.

    Create it with `RecoveryLedger()` (origin `unknown`, the only production path in
    M2-A). Every mutating method is called by the feed synchronously, in the same step as
    the event it records, and never after the feed has begun closing.
    """

    def __init__(self) -> None:
        self._origin = LedgerOrigin.UNKNOWN
        self._declaration: BootstrapDeclaration | None = None
        self._records: dict[str, _Record] = {}
        self._owner: weakref.ref[LedgerOwner] | None = None
        self._seq = 0
        self._ids = 0

    @classmethod
    def bootstrap(cls, declaration: BootstrapDeclaration) -> RecoveryLedger:
        """A ledger whose history begins now (TESTS ONLY in M2-A; see the class docs)."""
        if not isinstance(declaration, BootstrapDeclaration):
            raise TypeError("declaration must be a BootstrapDeclaration")
        ledger = cls()
        ledger._origin = LedgerOrigin.BOOTSTRAP
        ledger._declaration = declaration
        return ledger

    @property
    def origin(self) -> LedgerOrigin:
        return self._origin

    @property
    def declaration(self) -> BootstrapDeclaration | None:
        return self._declaration

    def next_seq(self) -> int:
        """The next value of the ledger's local event counter (orders our own actions)."""
        self._seq += 1
        return self._seq

    def _next_id(self, prefix: str) -> str:
        self._ids += 1
        return f"{prefix}{self._ids}"

    def _record(self, market_id: str) -> _Record:
        try:
            return self._records[market_id]
        except KeyError:
            raise KeyError(f"market {market_id} is not registered with this ledger") from None

    # --- registration -------------------------------------------------------------------

    def attach(self, owner: LedgerOwner, market_ids: Iterable[str], now_mono: float) -> None:
        """Register a feed and its markets.

        Refused while another feed using the ledger has not begun closing. A market new
        to the ledger gets HISTORY_UNKNOWN unless the bootstrap declaration lists it. A
        market an earlier feed registered gets an unobserved window (FEED_REPLACED) with
        the retained revision as its fence.
        """
        current = self._owner() if self._owner is not None else None
        if current is not None and current is not owner and not current.closing:
            raise RuntimeError("the recovery ledger is in use by a feed that is not closing")
        self._owner = weakref.ref(owner)
        for market_id in market_ids:
            record = self._records.get(market_id)
            if record is None:
                declared = self._declaration is not None and market_id in (
                    self._declaration.market_ids
                )
                record = self._records[market_id] = _Record(market_id, declared=declared)
                if not declared:
                    self._add(record, [InstanceSpec(StaleReason.HISTORY_UNKNOWN)], now_mono)
            elif record.feeds:
                self.open_window(market_id, WindowCause.FEED_REPLACED, now_mono)
            record.feeds += 1

    # --- realtime events ----------------------------------------------------------------

    def begin_subscription(self, market_id: str) -> bool:
        """A SUBSCRIBED status. True if it began a declared market's history (bootstrap's
        first subscription, which is not an unobserved window); False otherwise, and the
        caller opens a REJOIN window."""
        record = self._record(market_id)
        if record.declared and record.history_began_seq is None:
            record.history_began_seq = self.next_seq()
            return True
        return False

    def open_window(self, market_id: str, cause: WindowCause, now_mono: float) -> None:
        """Capture a fence at this instant and raise the generation."""
        record = self._record(market_id)
        record.generation += 1
        fence = record.last_revision
        for window in record.windows:
            # Same fence, both still confirmable: exactly the same future batches confirm
            # (or break) both, so one record carries every cause.
            if window.fence == fence and window.unconfirmable is None:
                window.causes.append(cause)
                return
            if fence is None and window.fence is None:
                window.causes.append(cause)
                return
        record.windows.append(
            _Window(
                window_id=self._next_id("W"),
                causes=[cause],
                opened_mono=now_mono,
                generation=record.generation,
                fence=fence,
                unconfirmable=None if fence is not None else "no batch was accepted before it",
            )
        )

    def raise_instances(
        self, market_id: str, specs: Iterable[InstanceSpec], now_mono: float
    ) -> list[str]:
        """Add one instance per spec; raise the generation once if any was added."""
        return self._add(self._record(market_id), list(specs), now_mono)

    def _add(self, record: _Record, specs: list[InstanceSpec], now_mono: float) -> list[str]:
        if not specs:
            return []
        record.generation += 1
        added = []
        for spec in specs:
            if len(record.instances) >= MAX_INSTANCES_PER_MARKET:
                same = [
                    i for i in record.instances
                    if i.reason is spec.reason and i.state is InstanceState.OPEN
                ]
                if same:
                    same[-1].repeats += 1  # still open, still blocking; detail merged
                    continue
            instance = _Instance(
                instance_id=self._next_id("B"),
                reason=spec.reason,
                state=InstanceState.OPEN,
                raised_mono=now_mono,
                raised_seq=self.next_seq(),
                generation=record.generation,
                revisions=spec.revisions,
                item=spec.item,
            )
            record.instances.append(instance)
            added.append(instance.instance_id)
        return added

    def note_revision(
        self, market_id: str, prior: int | None, after: int | None, *, clean: bool, why: str
    ) -> list[str]:
        """Record one processed batch's effect on the topic's revision.

        `prior` and `after` are the last accepted revision before and after it. Returns
        the windows the batch confirmed. Does not raise the generation.
        """
        record = self._record(market_id)
        record.last_revision = after
        if after is None or (prior is not None and after <= prior):
            return []  # nothing advanced (a duplicate, or no delivery)
        confirmed = []
        still_open = []
        for window in record.windows:
            if window.unconfirmable is not None:
                still_open.append(window)
            elif not clean:
                window.unconfirmable = f"the revision advanced past its fence through {why}"
                still_open.append(window)
            else:
                assert window.fence is not None and after > window.fence
                window.state = WindowState.CONFIRMED
                window.confirmed_through = after
                record.confirmed.append(window)
                confirmed.append(window.window_id)
        record.windows = still_open
        return confirmed

    # --- the observe/commit step, steps 2-5 ---------------------------------------------

    def apply_lifecycle(
        self,
        attempt: RecoveryAttempt,
        context: MarketPricingContext,
        *,
        read_seq: int,
        read_started_mono: float,
        read_finished_mono: float,
    ) -> LifecycleResult:
        """Apply one market-detail response that passed the feed's lifetime guard."""
        record = self._record(attempt.market_id)
        # 2. Classify.
        current = record.generation == attempt.generation
        observation = LifecycleObservation(
            attempt_id=attempt.attempt_id,
            generation=attempt.generation,
            current=current,
            status=context.status,
            settled_with=context.settledWith,
            settled_on=context.settledOn,
            read_started_mono=read_started_mono,
            read_finished_mono=read_finished_mono,
        )
        # 3. Decide, changing nothing yet.
        not_open = context.status in ("settled", "closed")
        restrict = not_open and not any(
            i.reason is StaleReason.LIFECYCLE_NOT_OPEN and i.state is InstanceState.QUARANTINED
            for i in record.instances
        )
        confirm: list[_Instance] = []
        findings: list[tuple[_Instance, str]] = []
        if current:  # a superseded response may only restrict
            for instance in record.instances:
                if (
                    instance.instance_id not in attempt.targets
                    or instance.state is not InstanceState.OPEN
                    or instance.reason is not StaleReason.MARKET_SETTLED
                ):
                    continue
                verdict = _settlement_verdict(instance, attempt, context, read_seq)
                if verdict is None:
                    confirm.append(instance)
                else:
                    findings.append((instance, verdict))
        initial = (
            current
            and record.declared
            and record.initial_evidence is None
            and record.history_began_seq is not None
            and read_seq > record.history_began_seq
            and read_seq > attempt.started_seq
            and context.status == "open"
        )
        # 4. Apply every decision together.
        changed = False
        if restrict:
            self._add_quarantined_not_open(record, observation, read_finished_mono)
            changed = True
        for instance in confirm:
            instance.state = InstanceState.QUARANTINED
            instance.evidence.append(
                Evidence(
                    observation,
                    "confirmed: market detail shows settled with the signal's settledWith; "
                    "quarantined under local policy, not proof SIG cannot reopen it (Q9)",
                )
            )
            changed = True
        for instance, finding in findings:
            instance.evidence.append(Evidence(observation, finding))  # not a state change
        record.lifecycle = observation
        if initial:
            record.initial_evidence = observation
        # 5. Raise the generation once, after this step's own changes.
        if changed:
            record.generation += 1
        return LifecycleResult(
            current=current,
            changed=changed,
            restricted=restrict,
            confirmed=tuple(i.instance_id for i in confirm),
            initial_evidence=initial,
            generation=record.generation,
        )

    def _add_quarantined_not_open(
        self, record: _Record, observation: LifecycleObservation, now_mono: float
    ) -> None:
        instance = _Instance(
            instance_id=self._next_id("B"),
            reason=StaleReason.LIFECYCLE_NOT_OPEN,
            state=InstanceState.QUARANTINED,
            raised_mono=now_mono,
            raised_seq=self.next_seq(),
            generation=record.generation,
            revisions=None,
            item=None,
        )
        instance.evidence.append(
            Evidence(
                observation,
                f"market detail shows {observation.status}; quarantined under local policy, "
                "not proof SIG cannot reopen or correct it (Q9, D-5)",
            )
        )
        record.instances.append(instance)

    # --- queries ------------------------------------------------------------------------

    def market_ids(self) -> frozenset[str]:
        return frozenset(self._records)

    def generation(self, market_id: str) -> int:
        return self._record(market_id).generation

    def last_revision(self, market_id: str) -> int | None:
        return self._record(market_id).last_revision

    def instances(self, market_id: str) -> tuple[BlockerInstance, ...]:
        """Every instance, open and quarantined (none is ever cleared in M2-A)."""
        return tuple(i.snapshot() for i in self._record(market_id).instances)

    def reasons(self, market_id: str) -> frozenset[StaleReason]:
        """The reason of every open and every quarantined instance."""
        return frozenset(i.reason for i in self._record(market_id).instances)

    def open_instance_ids(self, market_id: str) -> frozenset[str]:
        return frozenset(
            i.instance_id
            for i in self._record(market_id).instances
            if i.state is InstanceState.OPEN
        )

    def quarantined(self, market_id: str) -> bool:
        return any(
            i.state is InstanceState.QUARANTINED for i in self._record(market_id).instances
        )

    def open_windows(self, market_id: str) -> tuple[UnobservedWindow, ...]:
        return tuple(w.snapshot() for w in self._record(market_id).windows)

    def windows(self, market_id: str) -> tuple[UnobservedWindow, ...]:
        """Open windows, then the most recent confirmed ones (at most 16)."""
        record = self._record(market_id)
        return tuple(w.snapshot() for w in [*record.windows, *record.confirmed])

    def lifecycle(self, market_id: str) -> LifecycleObservation | None:
        return self._record(market_id).lifecycle

    def declared(self, market_id: str) -> bool:
        return self._record(market_id).declared

    def history_began(self, market_id: str) -> bool:
        return self._record(market_id).history_began_seq is not None

    def initial_evidence(self, market_id: str) -> LifecycleObservation | None:
        return self._record(market_id).initial_evidence

    def evidence_pending(self, market_id: str) -> bool:
        """A declared market whose history began but has no initial-state evidence."""
        record = self._record(market_id)
        return (
            record.declared
            and record.history_began_seq is not None
            and record.initial_evidence is None
        )

    def needs_recovery(self, market_id: str) -> bool:
        """Whether a lifecycle read could still restrict, confirm or provide evidence."""
        record = self._record(market_id)
        return bool(
            any(i.state is InstanceState.OPEN for i in record.instances)
            or record.windows
            or self.evidence_pending(market_id)
        )


def _settlement_verdict(
    instance: _Instance,
    attempt: RecoveryAttempt,
    context: MarketPricingContext,
    read_seq: int,
) -> str | None:
    """None if a current, live response confirms the settlement (design 4.6, conditions
    3 to 6); otherwise the finding that explains why it does not."""
    if not (read_seq > instance.raised_seq and read_seq > attempt.started_seq):
        return "inconclusive: the read was not started after the signal and the attempt"
    if context.status == "open":
        return "inconclusive: market detail shows open; its freshness is unproven (Q1)"
    if context.status != "settled":
        return f"inconclusive: market detail shows {context.status}, not settled (Q1)"
    signal = instance.item.settledWith if instance.item is not None else None
    if signal is None or context.settledWith is None or context.settledWith != signal:
        return (
            "inconclusive: market detail's settledWith differs from the realtime signal's, "
            "or one is absent (Q2)"
        )
    return None
