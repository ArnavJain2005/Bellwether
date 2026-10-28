"""M2-B1 acceptance tests: fail-closed detectors for duplicate batches, offline.

Design: docs/design/market-data-recovery.md, section 4.7 ("Revision resets") and AT-20 to
AT-22. A `market_batch` whose `revision` is at or below the last accepted one is a
duplicate: its books still apply by version, its arrays are never emitted, and three
detectors open blockers from them:

- a `marketSettled` item opens MARKET_SETTLED (AT-20);
- a trade with a null `sequence` opens NULL_TRADE_SEQUENCE (AT-21);
- a sequenced trade `id` the topic does not hold in its trade-id memory (4,096 ids by
  default) opens REVISION_ANOMALY (AT-22). Two possible false positives are accepted:
  both block and neither is ever cleared. (1) An id accepted earlier and then evicted from
  that memory. (2) A genuine retry reaching a replacement feed that shares the ledger:
  its topic is seeded with the ledger's revision but its trade-id memory starts empty,
  because the memory is not in the ledger.

Controls: a genuinely retried duplicate of an accepted batch with sequenced trades opens
nothing, costs no REST read and re-emits nothing **on the feed whose topic accepted it**
(not on a replacement feed, case 2 above); a retried settlement opens a second, redundant
MARKET_SETTLED instance and emits no second signal; an id still in memory opens nothing. Realtime payloads are synthetic (realtime_kit.py); REST goes through
httpx.MockTransport and time through ManualClock. Unless a test says otherwise the feed
has a TEST-ONLY bootstrap ledger (realtime_kit.test_bootstrap).

REVISION_ANOMALY is looked up inside each test body, so that against a baseline without
it every other test in this module still runs.
"""

from __future__ import annotations

import asyncio
from typing import Any

import httpx
import pytest
from realtime_kit import (
    MARKET,
    FakeTransport,
    Harness,
    batch,
    book,
    dumps,
    market_detail,
    settle,
    settled,
    trade,
)
from sig_testkit import TOURNAMENT, json_response

from engine.sig_client.readiness import ReadinessStatus
from engine.sig_client.realtime import (
    BookUpdated,
    BroadcastEvent,
    MarketReady,
    MarketStale,
    RealtimeFeedPolicy,
    SettlementSignal,
    SocketEvent,
    SocketState,
    TradeReceived,
)
from engine.sig_client.realtime_topic import DEFAULT_TRADE_ID_MEMORY, MarketTopic, StaleReason
from engine.sig_client.recovery import MISSING, InstanceState, WindowCause, WindowState

QUIET = RealtimeFeedPolicy(tick_s=1e6)  # the timer never fires within these tests
ORDERBOOK = f"/markets/{MARKET}/orderbook"
DETAIL = f"/markets/{MARKET}"
DETAIL_ROUTE = f"/api/v1{DETAIL}"

# The batch every feed test accepts first (revision 1), and duplicates of its revision.
ORIGINAL = batch(1, 0, trades=[trade("t1", 11)], books=[book(11)])
DUPLICATES: dict[str, Any] = {
    "MARKET_SETTLED": batch(1, 0, trades=[trade("t1", 11)], books=[book(11)],
                            market_settled=[settled()]),
    "NULL_TRADE_SEQUENCE": batch(1, 0, trades=[trade(None, None)], books=[book(11)]),
    "REVISION_ANOMALY": batch(1, 0, trades=[trade("t-unseen", 12)], books=[book(12)]),
}
DETECTORS = list(DUPLICATES)


def anomaly() -> StaleReason:
    return StaleReason.REVISION_ANOMALY


def reason_of(name: str) -> StaleReason:
    return getattr(StaleReason, name)


# --- helpers ---------------------------------------------------------------------------------


def run(scenario: Any, **kwargs: Any) -> Harness:
    async def main() -> Harness:
        h = Harness(**kwargs)
        try:
            await scenario(h)
        finally:
            if h.task is not None and not h.task.done():
                await h.feed.aclose()
                await asyncio.gather(h.task, return_exceptions=True)
        return h

    return asyncio.run(main())


def detail(status: str = "open", settled_with: str | None = None) -> httpx.Response:
    settled_on = "2026-10-10T12:00:01.000Z" if status == "settled" else None
    body = market_detail(MARKET, status=status, settled_with=settled_with, settled_on=settled_on)
    return json_response(200, body)


class Held:
    """A market-detail route: `immediate` responses at once, then each request waits until
    the test releases it."""

    def __init__(self, *immediate: httpx.Response) -> None:
        self.immediate = list(immediate)
        self.waiting: list[asyncio.Future[httpx.Response]] = []

    async def __call__(self, request: httpx.Request) -> httpx.Response:
        if self.immediate:
            return self.immediate.pop(0)
        future: asyncio.Future[httpx.Response] = asyncio.get_running_loop().create_future()
        self.waiting.append(future)
        return await future

    def pending(self) -> int:
        return sum(not f.done() for f in self.waiting)

    def release(self, response: httpx.Response) -> bool:
        for future in self.waiting:
            if not future.done():
                future.set_result(response)
                return True
        return False


class SlowClose(FakeTransport):
    """A transport whose close() does not return until the test allows it."""

    def __init__(self) -> None:
        super().__init__()
        self.allow_close = asyncio.Event()

    async def close(self) -> None:
        if not self.closed:
            self.closed = True
            self.queue.put_nowait(None)
        await self.allow_close.wait()


async def accept_original(h: Harness) -> None:
    await h.start()
    h.transport.batch(h.topic(), ORIGINAL)
    await settle()
    assert h.feed.is_ready(MARKET) and h.feed.last_revision(MARKET) == 1


async def book_trigger(h: Harness) -> None:
    """A standalone book_dirty and the resync it starts (a book-only trigger)."""
    h.transport.batch(
        h.topic(),
        {"exchangeId": 36, "marketId": MARKET, "tournamentId": TOURNAMENT.id,
         "at": "2026-10-10T12:00:00.123Z"},
        event="book_dirty",
    )
    await settle()
    await h.clock.advance(2.0)


def found(h: Harness, reason: StaleReason) -> list[Any]:
    return [i for i in h.ledger.instances(MARKET) if i.reason is reason]


def emitted(h: Harness, kind: type) -> list[Any]:
    return [e for e in h.events if isinstance(e, kind)]


def readies(h: Harness) -> int:
    return len(emitted(h, MarketReady))


def ledger_state(h: Harness) -> tuple[Any, ...]:
    ledger = h.ledger
    return (
        ledger.instances(MARKET),
        ledger.generation(MARKET),
        ledger.windows(MARKET),
        ledger.lifecycle(MARKET),
        ledger.last_revision(MARKET),
    )


def topic() -> MarketTopic:
    return MarketTopic(TOURNAMENT.id, MARKET)


def send(t: MarketTopic, payload: Any) -> Any:
    return t.on_message("market_batch", dumps(payload))


def ids(outcome: Any) -> list[str]:
    return [tr.id for tr in outcome.trades]


# --- AT-20 to AT-22 through the feed ---------------------------------------------------------


@pytest.mark.parametrize("name", DETECTORS)
def test_at20_at22_duplicate_opens_its_blocker_and_withdraws_readiness_first(name: str) -> None:
    """A duplicate carrying a settlement, a null-sequence trade or an unseen trade id opens
    the matching blocker in the same step: readiness is withdrawn before any callback from
    the message, the generation rises once, nothing from its arrays is emitted, and a
    recovery attempt starts."""
    reason = reason_of(name)

    async def scenario(h: Harness) -> None:
        await accept_original(h)
        generation = h.ledger.generation(MARKET)
        trades, settlements = len(emitted(h, TradeReceived)), len(emitted(h, SettlementSignal))
        details = h.count(DETAIL)
        mark = len(h.observed)
        h.transport.batch(h.topic(), DUPLICATES[name])
        await settle()
        (blocker,) = found(h, reason)
        assert blocker.state is InstanceState.OPEN
        assert blocker.revisions == (0, 1)  # the duplicate's own delivery window
        assert h.ledger.generation(MARKET) == generation + 1
        assert h.feed.last_revision(MARKET) == 1  # a duplicate advances nothing
        # Readiness withdrawn first; every callback from the message saw it not ready.
        assert h.observed[mark] == ("MarketStale", False)
        assert all(not ready for _, ready in h.observed[mark:])
        assert emitted(h, MarketStale)[-1].reason is reason
        # The duplicate's arrays are never emitted as new events.
        assert len(emitted(h, TradeReceived)) == trades
        assert len(emitted(h, SettlementSignal)) == settlements
        # Recovery targets the new instance (a lifecycle read), and nothing clears it.
        assert h.count(DETAIL) == details + 1
        await book_trigger(h)
        result = h.feed.readiness(MARKET)
        assert result.status is ReadinessStatus.NOT_READY and not h.feed.is_ready(MARKET)
        assert reason in h.feed.blockers(MARKET)
        assert any(line.startswith(f"blocker: {reason.value}") for line in result.reasons)
        assert readies(h) == 1
        await h.stop()

    run(scenario, policy=QUIET)


def test_at20_duplicate_settlement_records_its_signal_without_emitting_it() -> None:
    async def scenario(h: Harness) -> None:
        await accept_original(h)
        h.transport.batch(h.topic(), batch(1, 0, market_settled=[settled(value="REFUND")]))
        await settle()
        (blocker,) = found(h, StaleReason.MARKET_SETTLED)
        assert blocker.item is not None and blocker.item.settledWith == "REFUND"
        assert emitted(h, SettlementSignal) == []
        await h.stop()

    run(scenario, policy=QUIET)


def test_at20_duplicate_settlement_is_confirmable_like_any_other() -> None:
    """The instance a duplicate raised is targeted by the next attempt and confirmed by a
    current read showing the same settlement: quarantined, never cleared."""

    async def scenario(h: Harness) -> None:
        h.router.routes[DETAIL_ROUTE] = [detail(), detail("settled", "YES")]
        await accept_original(h)
        h.transport.batch(h.topic(), DUPLICATES["MARKET_SETTLED"])
        await settle()
        (blocker,) = found(h, StaleReason.MARKET_SETTLED)
        assert blocker.state is InstanceState.QUARANTINED
        assert h.feed.readiness(MARKET).status is ReadinessStatus.QUARANTINED
        await book_trigger(h)
        assert not h.feed.is_ready(MARKET) and readies(h) == 1
        await h.stop()

    run(scenario, policy=QUIET)


def test_at21_duplicate_null_sequence_trade_reads_no_tape() -> None:
    async def scenario(h: Harness) -> None:
        await accept_original(h)
        h.transport.batch(h.topic(), DUPLICATES["NULL_TRADE_SEQUENCE"])
        await settle()
        await h.clock.advance(600.0)
        assert found(h, StaleReason.NULL_TRADE_SEQUENCE)[0].state is InstanceState.OPEN
        assert h.count("/exchanges/36/trades") == 0  # no tape read in this slice
        assert not h.feed.is_ready(MARKET)
        await h.stop()

    run(scenario, policy=QUIET)


def test_at22_reason_names_its_missing_guarantees_and_the_false_positive() -> None:
    async def scenario(h: Harness) -> None:
        await accept_original(h)
        h.transport.batch(h.topic(), DUPLICATES["REVISION_ANOMALY"])
        await settle()
        reason = anomaly()
        (blocker,) = found(h, reason)
        assert blocker.missing == MISSING[reason] and "Q7" in blocker.missing
        (line,) = [r for r in h.feed.readiness(MARKET).reasons if r.startswith("blocker:")]
        assert "Q7" in line and "false positive" in line
        assert "evicted" in line and "replacement feed" in line  # both false positives
        await h.stop()

    run(scenario, policy=QUIET)


def test_at22_missing_guarantees_include_the_unknown_lost_span() -> None:
    """Q8 (the time or sequence span of a missed revision range) is missing: an anomaly
    means the original batch was discarded or the revisions went backwards, so which tape
    window holds the lost content is unknown, as for a revision gap."""
    reason = anomaly()
    assert "Q8" in MISSING[reason] and "Q8" in MISSING[StaleReason.REVISION_GAP]
    assert MISSING[reason] == ("Q3", "Q4", "Q6", "Q7", "Q8", "Q14")


def test_at22_genuine_retry_reaching_a_replacement_feed_is_a_false_positive() -> None:
    """Feed A accepts a sequenced trade and the ledger retains its revision. Feed B shares
    the ledger but builds a fresh topic: seeded with that revision, with an empty trade-id
    memory (the memory is not in the ledger and is not persisted). A genuine retry of A's
    batch is therefore a duplicate to B whose id B never saw: REVISION_ANOMALY opens.
    Accepted fail-closed for this slice: it blocks, nothing re-emits the trade, and
    nothing clears it."""

    async def main() -> None:
        a = Harness(policy=QUIET)
        await accept_original(a)
        assert [e.trade.id for e in emitted(a, TradeReceived)] == ["t1"]
        assert a.feed.blockers(MARKET) == frozenset()
        await a.stop()
        b = Harness(policy=QUIET, ledger=a.ledger, clock=a.clock)
        try:
            await b.start()
            assert b.feed.last_revision(MARKET) == 1  # seeded from the ledger
            before = {i.instance_id for i in b.ledger.instances(MARKET)}
            generation = b.ledger.generation(MARKET)
            b.transport.batch(b.topic(), ORIGINAL)  # the same send, retried
            await settle()
            new = [i for i in b.ledger.instances(MARKET) if i.instance_id not in before]
            assert [(i.reason, i.revisions, i.state) for i in new] == [
                (anomaly(), (0, 1), InstanceState.OPEN)
            ]
            assert b.ledger.generation(MARKET) == generation + 1
            assert emitted(b, TradeReceived) == []  # nothing re-emitted
            assert b.feed.last_revision(MARKET) == 1
            for _ in range(2):
                await book_trigger(b)
            assert anomaly() in b.feed.blockers(MARKET) and not b.feed.is_ready(MARKET)
            assert readies(b) == 0
        finally:
            await b.stop()

    asyncio.run(main())


def test_at22_evicted_id_opens_revision_anomaly_with_the_default_memory() -> None:
    """The default memory holds 4,096 ids. After 4,097 accepted trades the oldest id has
    left it: a duplicate repeating that id opens REVISION_ANOMALY (a false positive that
    blocks), while one repeating an id still held opens nothing."""
    assert DEFAULT_TRADE_ID_MEMORY == 4096
    many = [trade(f"t{n}", 1000 + n) for n in range(DEFAULT_TRADE_ID_MEMORY + 1)]

    async def scenario(h: Harness) -> None:
        await h.start()
        h.transport.batch(h.topic(), batch(1, 0, trades=many, books=[book(10_000)]))
        await settle()
        assert len(emitted(h, TradeReceived)) == len(many) and h.feed.is_ready(MARKET)
        generation = h.ledger.generation(MARKET)
        # Retained ids (the second-oldest and the newest): a genuine retry, nothing opens.
        retained = [many[1], many[-1]]
        h.transport.batch(h.topic(), batch(1, 0, trades=retained, books=[book(10_000)]))
        await settle()
        assert h.ledger.generation(MARKET) == generation and h.feed.is_ready(MARKET)
        # The evicted id: the topic cannot tell it from an unseen one.
        h.transport.batch(h.topic(), batch(1, 0, trades=[many[0]], books=[book(10_000)]))
        await settle()
        (blocker,) = found(h, anomaly())
        assert blocker.state is InstanceState.OPEN and not h.feed.is_ready(MARKET)
        assert len(emitted(h, TradeReceived)) == len(many)  # nothing re-emitted
        await h.stop()

    run(scenario, policy=QUIET)


# --- controls: a genuinely retried duplicate -------------------------------------------------


def test_control_genuine_retry_on_the_same_feed_opens_nothing() -> None:
    async def scenario(h: Harness) -> None:
        await h.start()
        accepted = batch(2, 0, trades=[trade("a", 1), trade("b", 2)], books=[book(2)])
        h.transport.batch(h.topic(), accepted)
        await settle()
        before = (ledger_state(h), h.count(ORDERBOOK), h.count(DETAIL), len(h.events))
        h.transport.batch(h.topic(), accepted)  # the same send, retried
        h.transport.batch(h.topic(), batch(1, 0, trades=[trade("a", 1)], books=[book(1)]))
        await settle()
        assert (ledger_state(h), h.count(ORDERBOOK), h.count(DETAIL), len(h.events)) == before
        assert h.feed.is_ready(MARKET) and h.feed.blockers(MARKET) == frozenset()
        assert [e.trade.id for e in emitted(h, TradeReceived)] == ["a", "b"]
        await h.stop()

    run(scenario, policy=QUIET)


def test_control_genuine_retry_of_a_settlement_is_redundant_and_emits_once() -> None:
    async def scenario(h: Harness) -> None:
        await h.start()
        original = batch(1, 0, market_settled=[settled()])
        h.transport.batch(h.topic(), original)
        await settle()
        generation = h.ledger.generation(MARKET)
        h.transport.batch(h.topic(), original)
        await settle()
        first, second = found(h, StaleReason.MARKET_SETTLED)
        assert first.instance_id != second.instance_id
        assert first.state is second.state is InstanceState.OPEN
        assert h.ledger.generation(MARKET) == generation + 1
        assert len(emitted(h, SettlementSignal)) == 1
        await h.stop()

    run(scenario, policy=QUIET)


def test_control_duplicate_without_arrays_still_applies_a_newer_book() -> None:
    async def scenario(h: Harness) -> None:
        await accept_original(h)
        generation = h.ledger.generation(MARKET)
        h.transport.batch(h.topic(), batch(1, 0, books=[book(20, bids=[("0.42", 5)])]))
        await settle()
        assert emitted(h, BookUpdated)[-1].book.as_of.sequence == 20
        assert h.ledger.generation(MARKET) == generation and h.feed.is_ready(MARKET)
        await h.stop()

    run(scenario, policy=QUIET)


# --- preserved rules: resyncs, replacement, fences, supersession, closing --------------------


@pytest.mark.parametrize("name", DETECTORS)
def test_duplicate_blocker_survives_resyncs_and_a_replacement_feed(name: str) -> None:
    reason = reason_of(name)

    async def main() -> None:
        first = Harness(policy=QUIET)
        await accept_original(first)
        first.transport.batch(first.topic(), DUPLICATES[name])
        await settle()
        for _ in range(3):
            await book_trigger(first)
        assert reason in first.feed.blockers(MARKET) and not first.feed.is_ready(MARKET)
        await first.stop()
        h = Harness(policy=QUIET, ledger=first.ledger, clock=first.clock)
        try:
            await h.start()
            await book_trigger(h)
            assert found(h, reason)[0].state is InstanceState.OPEN
            assert not h.feed.is_ready(MARKET) and readies(h) == 0
        finally:
            await h.stop()

    asyncio.run(main())


def test_duplicate_anomaly_after_a_reconnect_neither_confirms_nor_breaks_the_window() -> None:
    """Fence 40; a retried (39, 40] carrying an unseen id opens REVISION_ANOMALY but
    advances nothing, so the window stays open and confirmable. A clean (40, 41] then
    confirms the window, and the anomaly still blocks."""

    async def scenario(h: Harness) -> None:
        await h.start()
        h.transport.batch(h.topic(), batch(40, 39))
        await settle()
        h.transport.push(SocketEvent(SocketState.CLOSED))
        await settle()
        h.transport.subscribed(h.topic())
        await h.clock.advance(2.0)
        (window,) = h.ledger.open_windows(MARKET)
        assert window.fence == 40
        assert window.causes == (WindowCause.SOCKET_CLOSED, WindowCause.REJOIN)
        h.transport.batch(h.topic(), batch(40, 39, trades=[trade("new", 7)], books=[book(7)]))
        await settle()
        assert found(h, anomaly())[0].revisions == (39, 40)
        (window,) = h.ledger.open_windows(MARKET)
        assert window.state is WindowState.OPEN and window.unconfirmable is None
        h.transport.batch(h.topic(), batch(41, 40))
        await h.clock.advance(2.0)
        assert h.ledger.open_windows(MARKET) == ()
        assert anomaly() in h.feed.blockers(MARKET) and not h.feed.is_ready(MARKET)
        await h.stop()

    run(scenario, policy=QUIET)


def test_duplicate_blocker_supersedes_an_attempt_in_flight() -> None:
    """A duplicate-borne blocker raises the generation while a lifecycle read is in flight:
    the response is superseded, so a matching `settled` confirms nothing and only
    restricts (design 4.4, 4.6)."""
    held = Held(detail())

    async def scenario(h: Harness) -> None:
        h.router.routes[DETAIL_ROUTE] = held
        await h.start()
        h.transport.batch(h.topic(), batch(1, 0, market_settled=[settled()]))
        await settle()
        assert held.pending() == 1  # the attempt for the settlement is reading
        h.transport.batch(h.topic(), batch(1, 0, trades=[trade("zz", 5)], books=[book(5)]))
        await settle()
        assert found(h, anomaly())
        generation = h.ledger.generation(MARKET)
        assert held.release(detail("settled", "YES"))
        await settle()
        lifecycle = h.feed.readiness(MARKET).lifecycle
        assert lifecycle is not None and not lifecycle.current
        (settlement,) = found(h, StaleReason.MARKET_SETTLED)
        assert settlement.state is InstanceState.OPEN and not settlement.evidence
        (not_open,) = found(h, StaleReason.LIFECYCLE_NOT_OPEN)
        assert not_open.state is InstanceState.QUARANTINED
        assert h.ledger.generation(MARKET) == generation + 1
        await h.stop()

    run(scenario, policy=QUIET)


def test_duplicate_handled_after_closing_begins_changes_nothing() -> None:
    """White-box: the receive loop has stopped, so the duplicate is handed to _handle."""
    transport = SlowClose()

    async def scenario(h: Harness) -> None:
        try:
            await accept_original(h)
            before = ledger_state(h)
            events = len(h.events)
            closer = asyncio.create_task(h.feed.aclose())
            await settle()
            for payload in DUPLICATES.values():
                h.feed._handle(BroadcastEvent(h.topic(), "market_batch", dumps(payload)))
            assert ledger_state(h) == before
            # The existing teardown may withdraw readiness with STOPPED (design 4.5); nothing
            # from the duplicates is emitted.
            later = h.events[events:]
            assert not any(isinstance(e, (TradeReceived, SettlementSignal)) for e in later)
            assert all(e.reason is StaleReason.STOPPED
                       for e in later if isinstance(e, MarketStale))
        finally:
            transport.allow_close.set()  # a failure above must not hang the cleanup
        await closer
        assert h.task is not None
        await h.task
        assert ledger_state(h) == before
        await h.rest.aclose()
        await h.tokens.aclose()

    run(scenario, policy=QUIET, transport=transport)


# --- the topic on its own ----------------------------------------------------------------------


def test_topic_duplicate_detectors_report_blockers_and_emit_nothing() -> None:
    t = topic()
    send(t, ORIGINAL)
    out = send(
        t,
        batch(1, 0, trades=[trade("t-new", 12), trade(None, None)], books=[book(12)],
              market_settled=[settled()]),
    )
    assert out.duplicate is True and out.clean is False
    assert out.blockers == {StaleReason.MARKET_SETTLED, StaleReason.NULL_TRADE_SEQUENCE,
                            anomaly()}
    assert out.resync in out.blockers
    assert out.trades == [] and out.settlements == []
    assert [s.settledWith for s in out.duplicate_settlements] == ["YES"]
    assert t.last_revision == 1


def test_topic_unseen_id_in_a_duplicate_is_not_remembered() -> None:
    """A duplicate's trades are not applied: its unseen id is not added to the memory, so
    a later duplicate repeating it is flagged again and a later new batch emits it."""
    t = topic()
    send(t, ORIGINAL)
    assert anomaly() in send(t, DUPLICATES["REVISION_ANOMALY"]).blockers
    assert anomaly() in send(t, DUPLICATES["REVISION_ANOMALY"]).blockers
    out = send(t, batch(2, 1, trades=[trade("t-unseen", 12)], books=[book(12)]))
    assert out.blockers == set() and ids(out) == ["t-unseen"]


def test_topic_eviction_with_a_small_memory() -> None:
    t = MarketTopic(TOURNAMENT.id, MARKET, trade_id_memory=2)
    send(t, batch(1, 0, trades=[trade("a", 1), trade("b", 2), trade("c", 3)], books=[book(3)]))
    held = send(t, batch(1, 0, trades=[trade("b", 2), trade("c", 3)], books=[book(3)]))
    assert held.blockers == set() and held.resync is None  # both still remembered
    evicted = send(t, batch(1, 0, trades=[trade("a", 1)], books=[book(3)]))
    assert evicted.blockers == {anomaly()} and evicted.trades == []


def test_topic_retry_of_a_discarded_gap_batch_is_an_anomaly() -> None:
    """The design's other source of an unseen id: the original batch's trades were
    discarded (here by a gap, which already blocks), so its retry repeats ids never seen."""
    t = topic()
    send(t, batch(1, 0))
    gap = send(t, batch(5, 4, trades=[trade("g1", 50)], books=[book(50)]))
    assert gap.resync is StaleReason.REVISION_GAP and gap.trades == []
    retry = send(t, batch(5, 4, trades=[trade("g1", 50)], books=[book(50)]))
    assert retry.duplicate and retry.blockers == {anomaly()}


def test_revision_anomaly_is_a_blocking_reason_with_missing_guarantees() -> None:
    from engine.sig_client.realtime_topic import BLOCKING_REASONS

    assert anomaly() in BLOCKING_REASONS
    assert set(MISSING) == set(BLOCKING_REASONS)
