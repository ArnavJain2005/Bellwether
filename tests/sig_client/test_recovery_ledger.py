"""M2-A unit tests: RecoveryLedger, the observe/commit decision, policy bounds, topic flags.

Design: docs/design/market-data-recovery.md, sections 4 to 6 and 9.1. Pure and synchronous:
no feed, no I/O. The feed-level acceptance tests are in test_recovery_feed.py.
"""

from __future__ import annotations

import ast
import math
from pathlib import Path
from typing import Any

import pytest
from realtime_kit import MARKET, batch, book, dirty, dumps, settled, trade
from sig_testkit import TOURNAMENT

from engine.sig_client.realtime_topic import BLOCKING_REASONS, MarketTopic, StaleReason
from engine.sig_client.realtime_wire import MarketSettled
from engine.sig_client.recovery import (
    MAX_CONFIRMED_WINDOWS,
    MAX_EVIDENCE_PER_INSTANCE,
    MAX_INSTANCES_PER_MARKET,
    MISSING,
    BootstrapDeclaration,
    InstanceSpec,
    InstanceState,
    LedgerOrigin,
    RecoveryAttempt,
    RecoveryLedger,
    RecoveryPolicy,
    WindowCause,
    WindowState,
    bounded_jitter,
)
from engine.sig_client.wire import MarketPricingContext

ENGINE = Path(__file__).resolve().parents[2] / "engine"


class Owner:
    def __init__(self) -> None:
        self.closing = False


def declaration(*market_ids: str) -> BootstrapDeclaration:
    return BootstrapDeclaration(
        market_ids=frozenset(market_ids or (MARKET,)), declared_by="tests", note="unit test"
    )


def attached(ledger: RecoveryLedger, *market_ids: str) -> Owner:
    owner = Owner()
    ledger.attach(owner, market_ids or (MARKET,), 0.0)
    return owner


def context(status: str = "open", settled_with: str | None = None) -> MarketPricingContext:
    return MarketPricingContext.model_validate(
        {
            "type": "tournament",
            "tournament": {
                "id": TOURNAMENT.id,
                "slug": TOURNAMENT.slug,
                "name": "Alpha",
                "currencyName": "credits",
                "isOngoingPlay": False,
            },
            "status": status,
            "settledWith": settled_with,
            "settledOn": None if status != "settled" else "2026-10-10T12:00:01.000Z",
            "exchanges": [],
        }
    )


def signal(value: str = "YES") -> MarketSettled:
    return MarketSettled.model_validate(settled(value=value))


def attempt(ledger: RecoveryLedger, market_id: str = MARKET, **changes: Any) -> RecoveryAttempt:
    fields: dict[str, Any] = {
        "attempt_id": "A1",
        "market_id": market_id,
        "generation": ledger.generation(market_id),
        "targets": ledger.open_instance_ids(market_id),
        "started_mono": 10.0,
        "deadline_mono": 40.0,
        "started_seq": ledger.next_seq(),
    }
    fields.update(changes)
    return RecoveryAttempt(**fields)


def apply(ledger: RecoveryLedger, run: RecoveryAttempt, ctx: MarketPricingContext) -> Any:
    return ledger.apply_lifecycle(
        run, ctx, read_seq=ledger.next_seq(), read_started_mono=11.0, read_finished_mono=12.0
    )


def settled_ledger(value: str = "YES") -> RecoveryLedger:
    ledger = RecoveryLedger.bootstrap(declaration())
    attached(ledger)
    ledger.raise_instances(
        MARKET, [InstanceSpec(StaleReason.MARKET_SETTLED, (0, 1), signal(value))], 1.0
    )
    return ledger


# --- origins and history ----------------------------------------------------------------


def test_default_ledger_has_unknown_history_on_every_market() -> None:
    ledger = RecoveryLedger()
    attached(ledger, "26", "27")
    assert ledger.origin is LedgerOrigin.UNKNOWN and ledger.declaration is None
    for market_id in ("26", "27"):
        (only,) = ledger.instances(market_id)
        assert (only.reason, only.state) == (StaleReason.HISTORY_UNKNOWN, InstanceState.OPEN)
        assert only.missing == ("D-3", "D-4")
        assert ledger.reasons(market_id) == {StaleReason.HISTORY_UNKNOWN}
        assert not ledger.begin_subscription(market_id)  # never a bootstrap start


def test_bootstrap_declares_only_its_listed_markets() -> None:
    ledger = RecoveryLedger.bootstrap(declaration("26"))
    attached(ledger, "26", "27")
    assert ledger.origin is LedgerOrigin.BOOTSTRAP
    assert ledger.instances("26") == ()
    assert ledger.reasons("27") == {StaleReason.HISTORY_UNKNOWN}
    assert ledger.begin_subscription("26") is True  # history begins here
    assert ledger.begin_subscription("26") is False  # every later one is a re-join
    assert ledger.evidence_pending("26") and ledger.needs_recovery("26")


@pytest.mark.parametrize(
    "kwargs",
    [
        {"market_ids": frozenset()},
        {"market_ids": {"26"}},
        {"market_ids": "26"},
        {"market_ids": frozenset({"x6"})},
        {"declared_by": " "},
        {"note": ""},
    ],
)
def test_bootstrap_declaration_is_validated(kwargs: dict[str, Any]) -> None:
    fields: dict[str, Any] = {"market_ids": frozenset({"26"}), "declared_by": "t", "note": "n"}
    fields.update(kwargs)
    with pytest.raises(ValueError):
        BootstrapDeclaration(**fields)


def test_bootstrap_requires_a_declaration() -> None:
    with pytest.raises(TypeError):
        RecoveryLedger.bootstrap(object())  # type: ignore[arg-type]


def test_unregistered_market_is_an_error() -> None:
    with pytest.raises(KeyError):
        RecoveryLedger().generation(MARKET)


# --- feed replacement ---------------------------------------------------------------------


def test_attach_is_refused_while_another_feed_is_live() -> None:
    ledger = RecoveryLedger.bootstrap(declaration())
    first = attached(ledger)
    with pytest.raises(RuntimeError):
        ledger.attach(Owner(), (MARKET,), 1.0)
    first.closing = True
    second = Owner()
    ledger.attach(second, (MARKET,), 1.0)  # allowed once the first has begun closing


def test_replacement_opens_a_window_with_the_retained_fence() -> None:
    ledger = RecoveryLedger.bootstrap(declaration())
    first = attached(ledger)
    ledger.note_revision(MARKET, None, 7, clean=True, why="")
    first.closing = True
    generation = ledger.generation(MARKET)
    ledger.attach(Owner(), (MARKET,), 5.0)
    (window,) = ledger.open_windows(MARKET)
    assert (window.causes, window.fence, window.state) == (
        (WindowCause.FEED_REPLACED,),
        7,
        WindowState.OPEN,
    )
    assert ledger.generation(MARKET) == generation + 1
    assert ledger.last_revision(MARKET) == 7


# --- windows and the continuity predicate ----------------------------------------------------


def test_window_captures_the_fence_and_raises_the_generation() -> None:
    ledger = RecoveryLedger.bootstrap(declaration())
    attached(ledger)
    ledger.note_revision(MARKET, None, 40, clean=True, why="")
    ledger.open_window(MARKET, WindowCause.SOCKET_CLOSED, 1.0)
    ledger.open_window(MARKET, WindowCause.REJOIN, 2.0)  # same fence: one record
    (window,) = ledger.open_windows(MARKET)
    assert window.fence == 40 and window.causes == (WindowCause.SOCKET_CLOSED, WindowCause.REJOIN)
    assert ledger.generation(MARKET) == 2


def test_duplicate_confirms_nothing_and_clean_advance_confirms() -> None:
    ledger = RecoveryLedger.bootstrap(declaration())
    attached(ledger)
    ledger.note_revision(MARKET, None, 40, clean=True, why="")
    ledger.open_window(MARKET, WindowCause.SOCKET_CLOSED, 1.0)
    assert ledger.note_revision(MARKET, 40, 40, clean=False, why="") == []  # duplicate
    assert ledger.open_windows(MARKET)[0].unconfirmable is None
    assert len(ledger.note_revision(MARKET, 40, 41, clean=True, why="")) == 1
    assert ledger.open_windows(MARKET) == ()
    (window,) = ledger.windows(MARKET)
    assert (window.state, window.confirmed_through) == (WindowState.CONFIRMED, 41)


def test_unclean_advance_makes_the_window_unconfirmable_forever() -> None:
    ledger = RecoveryLedger.bootstrap(declaration())
    attached(ledger)
    ledger.note_revision(MARKET, None, 40, clean=True, why="")
    ledger.open_window(MARKET, WindowCause.TOKEN_REFRESH, 1.0)
    ledger.note_revision(MARKET, 40, 43, clean=False, why="a gap")
    assert ledger.note_revision(MARKET, 43, 44, clean=True, why="") == []
    (window,) = ledger.open_windows(MARKET)
    assert window.unconfirmable is not None and "a gap" in window.unconfirmable


def test_window_without_a_fence_is_never_confirmed() -> None:
    ledger = RecoveryLedger.bootstrap(declaration())
    attached(ledger)
    ledger.open_window(MARKET, WindowCause.CHANNEL_ERROR, 1.0)
    ledger.open_window(MARKET, WindowCause.REJOIN, 1.0)
    for revision in range(1, 5):
        assert ledger.note_revision(MARKET, revision - 1 or None, revision, clean=True, why="") == []
    (window,) = ledger.open_windows(MARKET)
    assert window.fence is None and len(window.causes) == 2


def test_confirmed_window_history_is_bounded() -> None:
    ledger = RecoveryLedger.bootstrap(declaration())
    attached(ledger)
    ledger.note_revision(MARKET, None, 1, clean=True, why="")
    for revision in range(2, MAX_CONFIRMED_WINDOWS + 12):
        ledger.open_window(MARKET, WindowCause.REJOIN, 1.0)
        ledger.note_revision(MARKET, revision - 1, revision, clean=True, why="")
    assert len(ledger.windows(MARKET)) == MAX_CONFIRMED_WINDOWS
    assert ledger.open_windows(MARKET) == ()


# --- instances ---------------------------------------------------------------------------------


def test_instances_raise_the_generation_once_per_event() -> None:
    ledger = RecoveryLedger.bootstrap(declaration())
    attached(ledger)
    ids = ledger.raise_instances(
        MARKET,
        [
            InstanceSpec(StaleReason.MARKET_SETTLED, (0, 1), signal()),
            InstanceSpec(StaleReason.MARKET_SETTLED, (0, 1), signal("NO")),
        ],
        1.0,
    )
    assert len(ids) == 2 and ledger.generation(MARKET) == 1  # two instances, one event
    assert [i.item.settledWith for i in ledger.instances(MARKET)] == ["YES", "NO"]


def test_instance_cap_merges_detail_but_never_unblocks() -> None:
    ledger = RecoveryLedger.bootstrap(declaration())
    attached(ledger)
    for _ in range(MAX_INSTANCES_PER_MARKET + 5):
        ledger.raise_instances(MARKET, [InstanceSpec(StaleReason.REVISION_GAP)], 1.0)
    instances = ledger.instances(MARKET)
    assert len(instances) == MAX_INSTANCES_PER_MARKET
    assert instances[-1].repeats == 5 and instances[-1].state is InstanceState.OPEN
    assert ledger.generation(MARKET) == MAX_INSTANCES_PER_MARKET + 5


def test_no_instance_state_means_cleared() -> None:
    assert {s.value for s in InstanceState} == {"open", "quarantined"}


def test_every_blocking_reason_names_its_missing_guarantees() -> None:
    assert set(MISSING) == set(BLOCKING_REASONS)
    assert {StaleReason.HISTORY_UNKNOWN, StaleReason.LIFECYCLE_NOT_OPEN} <= BLOCKING_REASONS


# --- the observe/commit step, steps 2 to 5 ---------------------------------------------------


def test_current_matching_settlement_confirms_and_quarantines_in_one_step() -> None:
    ledger = settled_ledger()
    run = attempt(ledger)
    generation = ledger.generation(MARKET)
    result = apply(ledger, run, context("settled", "YES"))
    assert result.current and result.changed and result.restricted
    assert len(result.confirmed) == 1
    assert ledger.generation(MARKET) == result.generation == generation + 1  # once, after
    states = {i.reason: i.state for i in ledger.instances(MARKET)}
    assert states == {
        StaleReason.MARKET_SETTLED: InstanceState.QUARANTINED,
        StaleReason.LIFECYCLE_NOT_OPEN: InstanceState.QUARANTINED,
    }
    assert ledger.quarantined(MARKET) and not ledger.needs_recovery(MARKET)


def test_superseded_response_may_only_restrict() -> None:
    ledger = settled_ledger()
    run = attempt(ledger)
    ledger.raise_instances(MARKET, [InstanceSpec(StaleReason.REVISION_GAP, (1, 3))], 2.0)
    result = apply(ledger, run, context("settled", "YES"))
    assert not result.current and result.restricted and result.confirmed == ()
    settlement = ledger.instances(MARKET)[0]
    assert settlement.state is InstanceState.OPEN and settlement.evidence == ()
    observation = ledger.lifecycle(MARKET)
    assert observation is not None and not observation.current


def test_superseded_open_changes_nothing() -> None:
    ledger = settled_ledger()
    run = attempt(ledger)
    ledger.open_window(MARKET, WindowCause.REJOIN, 2.0)
    before = (ledger.instances(MARKET), ledger.generation(MARKET))
    result = apply(ledger, run, context("open"))
    assert not result.changed
    assert (ledger.instances(MARKET), ledger.generation(MARKET)) == before


@pytest.mark.parametrize(
    ("ctx", "finding", "restricted"),
    [
        (context("open"), "(Q1)", False),
        (context("closed"), "closed, not settled (Q1)", True),
        (context("settled", "NO"), "(Q2)", True),
        (context("settled", None), "(Q2)", True),
    ],
)
def test_current_inconclusive_reads_leave_the_settlement_open(
    ctx: MarketPricingContext, finding: str, restricted: bool
) -> None:
    ledger = settled_ledger()
    generation = ledger.generation(MARKET)
    result = apply(ledger, attempt(ledger), ctx)
    settlement = ledger.instances(MARKET)[0]
    assert settlement.state is InstanceState.OPEN
    assert finding in settlement.evidence[-1].finding  # evidence is not a state change
    assert result.restricted is restricted
    assert ledger.generation(MARKET) == generation + (1 if restricted else 0)


def test_read_not_started_after_the_signal_cannot_confirm() -> None:
    ledger = settled_ledger()
    run = attempt(ledger)
    result = ledger.apply_lifecycle(
        run, context("settled", "YES"), read_seq=0, read_started_mono=0.0, read_finished_mono=1.0
    )
    assert result.confirmed == ()
    assert "not started after" in ledger.instances(MARKET)[0].evidence[-1].finding


def test_untargeted_instance_is_not_confirmed() -> None:
    ledger = settled_ledger()
    result = apply(ledger, attempt(ledger, targets=frozenset()), context("settled", "YES"))
    assert result.confirmed == () and result.restricted


def test_market_is_quarantined_at_most_once() -> None:
    ledger = settled_ledger()
    apply(ledger, attempt(ledger), context("closed"))
    result = apply(ledger, attempt(ledger), context("closed"))
    assert not result.restricted
    reasons = [i.reason for i in ledger.instances(MARKET)]
    assert reasons.count(StaleReason.LIFECYCLE_NOT_OPEN) == 1


def test_evidence_per_instance_is_bounded() -> None:
    ledger = settled_ledger()
    for _ in range(MAX_EVIDENCE_PER_INSTANCE + 3):
        apply(ledger, attempt(ledger), context("open"))
    assert len(ledger.instances(MARKET)[0].evidence) == MAX_EVIDENCE_PER_INSTANCE


def test_initial_evidence_needs_a_current_open_read_after_the_first_subscription() -> None:
    ledger = RecoveryLedger.bootstrap(declaration())
    attached(ledger)
    early = attempt(ledger)
    assert not apply(ledger, early, context("open")).initial_evidence  # history not begun
    ledger.begin_subscription(MARKET)
    stale = attempt(ledger)
    ledger.open_window(MARKET, WindowCause.REJOIN, 1.0)
    assert not apply(ledger, stale, context("open")).initial_evidence  # superseded
    result = apply(ledger, attempt(ledger), context("open"))
    assert result.initial_evidence and ledger.initial_evidence(MARKET) is not None
    assert not ledger.evidence_pending(MARKET)


def test_undeclared_market_never_gets_initial_evidence() -> None:
    ledger = RecoveryLedger()
    attached(ledger)
    ledger.begin_subscription(MARKET)
    assert not apply(ledger, attempt(ledger), context("open")).initial_evidence
    assert ledger.reasons(MARKET) == {StaleReason.HISTORY_UNKNOWN}


# --- policy bounds -----------------------------------------------------------------------------


def test_policy_defaults_are_the_approved_bounds() -> None:
    policy = RecoveryPolicy()
    assert (
        policy.attempts_per_window,
        policy.attempt_window_s,
        policy.attempt_deadline_s,
        policy.backoff_base_s,
        policy.backoff_cap_s,
        policy.escalate_after,
    ) == (10, 60.0, 30.0, 5.0, 120.0, 5)
    assert [policy.backoff(n) for n in range(1, 9)] == [5, 10, 20, 40, 80, 120, 120, 120]
    assert policy.backoff(10_000) == 120


@pytest.mark.parametrize(
    "kwargs",
    [
        {"attempts_per_window": 0},
        {"attempts_per_window": True},
        {"attempt_deadline_s": math.inf},
        {"attempt_window_s": 0},
        {"backoff_base_s": -1},
        {"backoff_base_s": 200.0},
        {"escalate_after": 1.5},
    ],
)
def test_policy_rejects_bad_bounds(kwargs: dict[str, Any]) -> None:
    with pytest.raises(ValueError):
        RecoveryPolicy(**kwargs)


@pytest.mark.parametrize("value", [2.5, 5.0, 3])
def test_bounded_jitter_accepts_half_to_all(value: float) -> None:
    assert bounded_jitter(5.0, value) == float(value)


@pytest.mark.parametrize("value", [2.49, 5.01, math.nan, True, "5"])
def test_bounded_jitter_rejects_everything_else(value: object) -> None:
    with pytest.raises(ValueError):
        bounded_jitter(5.0, value)


# --- topic flags used by the continuity predicate -----------------------------------------------


@pytest.mark.parametrize(
    ("payloads", "clean", "window"),
    [
        ([batch(1, 0)], True, (0, 1)),
        ([batch(1, 0, market_settled=[settled()])], True, (0, 1)),  # content still raises
        ([batch(1, 0, trades=[trade(None, None)], books=[book(5)])], True, (0, 1)),
        ([batch(2, 1), batch(2, 1)], False, (1, 2)),  # duplicate
        ([batch(2, 1), batch(5, 4)], False, (4, 5)),  # gap
        ([{"resyncRequired": True, "delivery": {"revision": 1, "previousRevision": 0}}],
         False, (0, 1)),
        ([batch(1, 0, book_dirty=[dirty()])], False, (0, 1)),  # missing book
        (['{"delivery": {"revision": 1, "previousRevision": 0}, "trades": "x"}'], False, (0, 1)),
        (['{"delivery": "broken"}'], False, None),
    ],
)
def test_topic_reports_window_and_cleanliness(payloads: list[Any], clean: bool, window: Any) -> None:
    topic = MarketTopic(TOURNAMENT.id, MARKET)
    outcome = None
    for payload in payloads:
        text = payload if isinstance(payload, str) else dumps(payload)
        outcome = topic.on_message("market_batch", text)
    assert outcome is not None
    assert (outcome.clean, outcome.window) == (clean, window)


def test_topic_seeded_with_a_retained_revision_checks_gaps_against_it() -> None:
    topic = MarketTopic(TOURNAMENT.id, MARKET, last_revision=40)
    assert topic.last_revision == 40
    assert topic.on_message("market_batch", dumps(batch(40, 39))).duplicate
    assert topic.on_message("market_batch", dumps(batch(43, 42))).resync is StaleReason.REVISION_GAP


@pytest.mark.parametrize("value", [-1, True, 1.0, "4"])
def test_topic_rejects_a_bad_seed(value: object) -> None:
    with pytest.raises(ValueError):
        MarketTopic(TOURNAMENT.id, MARKET, last_revision=value)  # type: ignore[arg-type]


# --- AT-39: no production path declares bootstrap ---------------------------------------------


def bootstrap_creations(source: str) -> list[int]:
    """Lines that construct a BootstrapDeclaration, call a `bootstrap` constructor or set a
    ledger origin to BOOTSTRAP."""
    lines = []
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.Call):
            func = node.func
            name = func.id if isinstance(func, ast.Name) else getattr(func, "attr", None)
            if name in {"BootstrapDeclaration", "bootstrap"}:
                lines.append(node.lineno)
        elif isinstance(node, ast.Attribute) and node.attr == "BOOTSTRAP":
            if isinstance(node.ctx, ast.Load):
                lines.append(node.lineno)
    return lines


def test_at39_scanner_finds_a_bootstrap_declaration() -> None:
    """Positive control for the scan below."""
    source = (
        "from engine.sig_client.recovery import BootstrapDeclaration, RecoveryLedger\n"
        "d = BootstrapDeclaration(market_ids=frozenset({'1'}), declared_by='x', note='y')\n"
        "ledger = RecoveryLedger.bootstrap(d)\n"
    )
    assert bootstrap_creations(source) == [2, 3]


def test_at39_no_module_in_engine_creates_a_bootstrap_declaration() -> None:
    offenders = {}
    for path in sorted(ENGINE.rglob("*.py")):
        source = path.read_text(encoding="utf-8")
        lines = bootstrap_creations(source)
        if path.name == "recovery.py":
            # The definition: `bootstrap()` itself sets the origin; nothing calls it.
            tree = ast.parse(source)
            (method,) = [
                n for n in ast.walk(tree) if isinstance(n, ast.FunctionDef) and n.name == "bootstrap"
            ]
            inside = range(method.lineno, (method.end_lineno or method.lineno) + 1)
            lines = [line for line in lines if line not in inside]
        if lines:
            offenders[str(path.relative_to(ENGINE))] = lines
    assert offenders == {}
    assert (ENGINE / "sig_client" / "recovery.py").is_file()  # the scan saw the module
