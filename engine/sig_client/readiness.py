"""Per-market readiness result of the realtime feed (M2-A; design section 6).

`MarketReadiness` is engine-internal and immutable: it is built on request from the feed's
state and the recovery ledger, and nothing here is a shared/ schema (the PRD reserves
readiness schemas for Arnav's approval).

It is the evidence for one item of the engine's readiness check, "every market to be
traded is ready in the realtime feed with no outstanding blocker". It is necessary for a
new order in that market and never sufficient: it says nothing about account state,
orders, risk, configuration or reference data, and feed readiness alone never authorizes
trading. A READY result means no loss was detected in the revisioned stream since history
began, plus the initial-state evidence the bootstrap declaration requires. It does not
mean nothing was lost: delivery is best-effort, and a missed standalone `market_settled`
is undetectable. Protective cancellation is never gated on it.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum

from engine.sig_client.realtime_topic import BookSource
from engine.sig_client.recovery import (
    COVERAGE_NOTE,
    BlockerInstance,
    LedgerOrigin,
    LifecycleObservation,
    UnobservedWindow,
)
from engine.sig_client.wire import BookVersion

DOES_NOT_COVER = (
    "Feed readiness only. This result says nothing about account state (cash, positions, "
    "fills, settlements of our own), open orders or unresolved idempotency keys, risk, "
    "configuration, reference data or rate budget, and never authorizes trading on its own."
)


class ReadinessStatus(StrEnum):
    READY = "ready"
    NOT_READY = "not ready"
    QUARANTINED = "quarantined"  # local policy (design 4.1); still not ready


@dataclass(frozen=True)
class HistoryState:
    origin: LedgerOrigin
    history_unknown_open: bool
    bootstrap_declared: bool  # listed by a (test-only) bootstrap declaration
    history_began: bool
    initial_evidence: LifecycleObservation | None


@dataclass(frozen=True)
class TransportState:
    subscribed: bool
    token_valid: bool


@dataclass(frozen=True)
class BookState:
    exchange_id: str
    version: BookVersion | None
    source: BookSource
    complete: bool
    expiry_known: bool
    next_expiry_at: str | None  # as sent; on a REST book, an expiry its read did not cover
    expired: bool  # a known expiry has passed on the local clock


@dataclass(frozen=True)
class ContinuityState:
    last_accepted_revision: int | None
    windows: tuple[UnobservedWindow, ...]  # open first, then recent confirmed ones
    note: str = COVERAGE_NOTE


@dataclass(frozen=True)
class MarketReadiness:
    """One market's readiness, evaluated at `evaluated_mono` (local monotonic, for
    ordering) / `evaluated_utc` (local wall clock, for display only).

    `status` is READY only when the feed is not closing, the market was published ready
    after an orderbook resync that answered its latest trigger, it is subscribed with a
    valid token and no expired book, no blocker instance is open or quarantined, no
    unobserved window is open, and its history is known (a bootstrap market also needs
    its initial-state evidence). QUARANTINED when any instance is quarantined and the feed
    is not closing. NOT_READY otherwise, and always once closing has begun. `reasons`
    holds one human-readable line per failing condition, with the question or decision
    ids it is missing.
    """

    market_id: str
    status: ReadinessStatus
    generation: int
    evaluated_mono: float
    evaluated_utc: datetime
    reasons: tuple[str, ...]
    closing: bool
    history: HistoryState
    transport: TransportState
    books: tuple[BookState, ...]
    continuity: ContinuityState
    lifecycle: LifecycleObservation | None
    blockers: tuple[BlockerInstance, ...]
    escalated: bool
    consecutive_failures: int
    does_not_cover: str = DOES_NOT_COVER
