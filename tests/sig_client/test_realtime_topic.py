"""MarketTopic: the documented market_batch rules, applied without any I/O (S1-T3-A).

Every payload here is synthetic (see realtime_kit.py).
"""

from __future__ import annotations

import json
from decimal import Decimal
from typing import Any

import pytest
from realtime_kit import (
    EXCHANGE,
    MARKET,
    RawNumber,
    batch,
    book,
    delivery,
    dirty,
    dumps,
    settled,
    trade,
)
from sig_testkit import TOURNAMENT

from engine.sig_client.realtime_topic import (
    BookSource,
    MarketTopic,
    SnapshotError,
    StaleReason,
)
from engine.sig_client.realtime_wire import parse_instant
from engine.sig_client.wire import CombinedOrderbookExchange

OTHER_TOURNAMENT = "11111111-2222-4333-8444-555555555555"


def topic() -> MarketTopic:
    return MarketTopic(TOURNAMENT.id, MARKET)


def send(t: MarketTopic, payload: Any, event: str = "market_batch") -> Any:
    return t.on_message(event, dumps(payload))


def ids(outcome: Any) -> list[str]:
    return [tr.id for tr in outcome.trades]


def at_rev(t: MarketTopic, revision: int) -> None:
    """Bring the topic to an accepted revision with one plain batch."""
    send(t, batch(revision, revision - 1))
    assert t.last_revision == revision


def rest_exchange(
    sequence: int | None,
    at: str = "2026-10-10T12:00:00.0000001+00:00",
    *,
    bids: int = 1,
    asks: int = 1,
    exchange: str = str(EXCHANGE),
) -> CombinedOrderbookExchange:
    return CombinedOrderbookExchange.model_validate(
        {
            "exchangeId": exchange,
            "option": "YES",
            "asOf": None if sequence is None else {"sequence": sequence, "at": at},
            "impliedProbability": None,
            "bids": [{"price": Decimal(f"0.{40 - i:02d}"), "quantity": 1} for i in range(bids)],
            "asks": [{"price": Decimal(f"0.{60 + i:02d}"), "quantity": 1} for i in range(asks)],
        }
    )


# --- revisions ------------------------------------------------------------------------------


def test_first_batch_is_accepted_and_emits_trades() -> None:
    t = topic()
    out = send(t, batch(10, 7, trades=[trade("t1", 501)], books=[book(501)]))
    # [documented] a gap needs a previously accepted revision; there is none yet.
    assert out.resync is None and not out.duplicate
    assert t.last_revision == 10
    assert ids(out) == ["t1"]


def test_contiguous_batch_is_accepted() -> None:
    t = topic()
    at_rev(t, 5)
    out = send(t, batch(6, 5, trades=[trade("t1", 501)], books=[book(501)]))
    assert (out.resync, t.last_revision, ids(out)) == (None, 6, ["t1"])


@pytest.mark.parametrize("revision", [4, 5])
def test_duplicate_ignores_arrays(revision: int) -> None:
    t = topic()
    at_rev(t, 5)
    out = send(
        t,
        batch(
            revision,
            revision - 1,
            trades=[trade("t-dup", 77)],
            book_dirty=[dirty()],
            market_settled=[settled()],
            books=[book(77)],
        ),
    )
    assert out.duplicate is True
    assert out.trades == [] and out.settlements == []
    # M2-B1 (design 4.7): never emitted, but its settlement and its unseen trade id
    # open blockers (test_recovery_duplicates.py).
    assert out.blockers == {StaleReason.MARKET_SETTLED, StaleReason.REVISION_ANOMALY}
    assert t.last_revision == 5


def test_duplicate_still_applies_a_newer_book() -> None:
    t = topic()
    send(t, batch(5, 4, books=[book(100)], book_dirty=[dirty()]))
    out = send(t, batch(5, 4, books=[book(140, bids=[("0.42", 50)])], book_dirty=[dirty()]))
    # [documented] "Apply a batch's books even when its revision is a duplicate".
    assert out.duplicate is True
    assert [b.as_of.sequence for b in out.books] == [140]
    assert t.books[str(EXCHANGE)].bids[0].price == Decimal("0.42")


def test_gap_resyncs_and_emits_nothing_from_arrays() -> None:
    t = topic()
    at_rev(t, 5)
    out = send(
        t,
        batch(9, 7, trades=[trade("t9", 909)], books=[book(909)], market_settled=[settled()]),
    )
    assert out.resync is StaleReason.REVISION_GAP
    assert out.trades == [] and out.settlements == []
    assert t.last_revision == 9  # the revision is still accepted
    assert [b.as_of.sequence for b in out.books] == [909]  # books apply by version


def test_overlapping_batch_is_accepted_and_trades_deduplicated() -> None:
    t = topic()
    send(t, batch(5, 3, trades=[trade("t4", 401), trade("t5", 501)], books=[book(501)]))
    # Covers revisions 4..7: overlaps 4..5 already accepted. Not a duplicate, not a gap.
    out = send(
        t,
        batch(7, 3, trades=[trade("t5", 501), trade("t6", 601), trade("t7", 701)], books=[book(701)]),
    )
    assert (out.resync, out.duplicate, t.last_revision) == (None, False, 7)
    assert ids(out) == ["t6", "t7"]


def test_source_sequences_are_not_the_gap_counter() -> None:
    t = topic()
    first = batch(1, 0, trades=[trade("a", 10)], books=[book(10)])
    second = batch(2, 1, trades=[trade("b", 99_999)], books=[book(99_999)])
    second["delivery"]["sourceSequenceFrom"] = 5_000_000  # a huge provenance jump
    send(t, first)
    out = send(t, second)
    assert out.resync is None and ids(out) == ["b"]


def test_trade_sequence_jumps_are_not_gaps() -> None:
    t = topic()
    at_rev(t, 1)
    out = send(t, batch(2, 1, trades=[trade("x", 10), trade("y", 1_000_000)], books=[book(10)]))
    assert out.resync is None and ids(out) == ["x", "y"]


# --- resyncRequired ---------------------------------------------------------------------------


def test_resync_required_applies_books_advances_and_emits_no_trades() -> None:
    t = topic()
    at_rev(t, 5)
    payload = {
        "resyncRequired": True,
        "delivery": delivery(8, 5),
        "books": [book(800)],
        "trades": [trade("partial", 801)],
    }
    out = send(t, payload)
    assert out.resync is StaleReason.RESYNC_REQUIRED
    assert out.trades == []
    assert t.last_revision == 8
    assert [b.as_of.sequence for b in out.books] == [800]


def test_compact_resync_required_has_no_arrays() -> None:
    t = topic()
    at_rev(t, 5)
    out = send(t, {"resyncRequired": True, "delivery": delivery(6, 5)})
    assert (out.resync, t.last_revision, out.books) == (StaleReason.RESYNC_REQUIRED, 6, [])


def test_resync_required_checked_before_the_duplicate_rule() -> None:
    t = topic()
    at_rev(t, 9)
    out = send(t, {"resyncRequired": True, "delivery": delivery(8, 7)})
    # [documented] the spec's example resyncs first and advances only when newer.
    assert out.resync is StaleReason.RESYNC_REQUIRED
    assert t.last_revision == 9


def test_resync_required_on_first_batch_sets_the_revision() -> None:
    t = topic()
    out = send(t, {"resyncRequired": True, "delivery": delivery(3, 2)})
    assert (out.resync, t.last_revision) == (StaleReason.RESYNC_REQUIRED, 3)


# --- trades, books named, settlements --------------------------------------------------------


def test_null_sequence_trade_resyncs_and_emits_none() -> None:
    t = topic()
    at_rev(t, 1)
    out = send(t, batch(2, 1, trades=[trade("ok", 10), trade(None, None)], books=[book(10)]))
    assert out.resync is StaleReason.NULL_TRADE_SEQUENCE
    assert out.trades == []
    assert t.last_revision == 2


def test_half_null_trade_identity_is_malformed() -> None:
    t = topic()
    out = send(t, batch(1, 0, trades=[trade("x", None)], books=[book(1)]))
    assert out.resync is StaleReason.MALFORMED


@pytest.mark.parametrize(
    ("kind", "payload"),
    [
        ("trade", lambda: batch(2, 1, trades=[trade("t", 5, exchange=37)], books=[book(5)])),
        ("bookDirty", lambda: batch(2, 1, book_dirty=[dirty()])),
    ],
)
def test_named_exchange_without_a_book_resyncs(kind: str, payload: Any) -> None:
    t = topic()
    at_rev(t, 1)
    out = send(t, payload())
    assert out.resync is StaleReason.MISSING_BOOK, kind
    assert out.trades == []


def test_string_and_integer_ids_both_name_the_book() -> None:
    t = topic()
    out = send(
        t,
        batch(1, 0, trades=[trade("s", 5, exchange="36", market="26")], book_dirty=[dirty(36, 26)],
              books=[book(5)]),
    )
    assert out.resync is None and ids(out) == ["s"]


def test_market_settled_is_emitted_and_resyncs() -> None:
    t = topic()
    out = send(t, batch(1, 0, market_settled=[settled(value="REFUND")]))
    assert out.resync is StaleReason.MARKET_SETTLED
    assert [s.settledWith for s in out.settlements] == ["REFUND"]


def test_trade_id_memory_is_bounded() -> None:
    t = MarketTopic(TOURNAMENT.id, MARKET, trade_id_memory=2)
    send(t, batch(1, 0, trades=[trade("a", 1), trade("b", 2), trade("c", 3)], books=[book(3)]))
    out = send(t, batch(2, 0, trades=[trade("a", 1), trade("c", 3)], books=[book(3)]))
    assert ids(out) == ["a"]  # "a" was evicted, "c" is still remembered


def test_trade_values_stay_exact() -> None:
    t = topic()
    out = send(t, batch(1, 0, trades=[trade("p", 1, price="0.5450")], books=[book(1)]))
    price = out.trades[0].price
    assert type(price) is Decimal and str(price) == "0.5450"


# --- book versions ------------------------------------------------------------------------------


def apply(t: MarketTopic, revision: int, *books: dict[str, Any]) -> list[int]:
    out = send(t, batch(revision, revision - 1, books=list(books)))
    return [b.as_of.sequence for b in out.books]


def test_older_and_equal_pushed_books_are_ignored() -> None:
    t = topic()
    assert apply(t, 1, book(100)) == [100]
    assert apply(t, 2, book(99)) == []
    assert apply(t, 3, book(100)) == []
    assert t.books[str(EXCHANGE)].as_of.sequence == 100


def test_same_sequence_compares_at_exactly_beyond_microseconds() -> None:
    t = topic()
    assert apply(t, 1, book(100, "2026-10-10T12:00:00.1234567+00:00")) == [100]
    # Differs only in the seventh fractional digit: newer, so applied.
    assert apply(t, 2, book(100, "2026-10-10T12:00:00.1234568+00:00")) == [100]
    assert t.books[str(EXCHANGE)].as_of.at.endswith("568+00:00")
    # Same instant written with Z: not newer.
    assert apply(t, 3, book(100, "2026-10-10T12:00:00.1234568Z")) == []


def test_sequence_wins_over_at() -> None:
    t = topic()
    apply(t, 1, book(100, "2026-10-10T12:00:05Z"))
    assert apply(t, 2, book(101, "2026-10-10T12:00:01Z")) == [101]


def test_pushed_levels_stay_exact() -> None:
    t = topic()
    apply(t, 1, book(5, bids=[("0.9450", 16314)], asks=[("0.95", 9137)]))
    held = t.books[str(EXCHANGE)]
    assert [str(level.price) for level in held.bids] == ["0.9450"]
    assert held.source is BookSource.PUSH and held.complete and held.expiry_known


# --- malformed messages ----------------------------------------------------------------------


@pytest.mark.parametrize(
    "raw",
    [
        "not json",
        "[1, 2]",
        '{"trades": [], "bookDirty": [], "marketSettled": []}',  # no delivery
        '{"delivery": {"revision": true, "previousRevision": 0}}',
        '{"delivery": {"revision": "7", "previousRevision": "6"}}',
        '{"delivery": {"revision": 6, "previousRevision": 6}}',
        '{"delivery": {"revision": NaN, "previousRevision": 1}}',
    ],
)
def test_message_without_a_valid_delivery_keeps_the_revision(raw: str) -> None:
    t = topic()
    at_rev(t, 5)
    out = t.on_message("market_batch", raw)
    assert out.resync is StaleReason.MALFORMED
    assert t.last_revision == 5


@pytest.mark.parametrize(
    "mutate",
    [
        lambda p: p.update(trades=[{**trade("t", 1), "price": RawNumber("1.5")}]),
        lambda p: p.update(trades=[trade("t", 1, market="27")]),
        lambda p: p.update(trades=[trade("t", 1, tournament=OTHER_TOURNAMENT)]),
        lambda p: p.update(bookDirty=[{**dirty(), "tournamentId": OTHER_TOURNAMENT}]),
        lambda p: p.update(books=[book(9, at="yesterday")]),
        lambda p: p.update(books=[book(9, bids=[("0.40", 1), ("0.41", 1)])]),
        lambda p: p.update(books=[book(9), book(10)]),
        lambda p: p.pop("bookDirty"),
        lambda p: p.update(resyncRequired=None),
    ],
)
def test_malformed_body_with_valid_delivery_applies_nothing(mutate: Any) -> None:
    t = topic()
    apply(t, 5, book(100))
    payload = batch(6, 5, trades=[trade("t", 1)], books=[book(200)])
    mutate(payload)
    out = send(t, payload)
    assert out.resync is StaleReason.MALFORMED
    assert out.books == [] and out.trades == []
    assert t.books[str(EXCHANGE)].as_of.sequence == 100
    assert t.last_revision == 6  # its delivery was valid, so the revision is accepted


def test_lossily_decoded_payload_is_rejected() -> None:
    t = topic()
    payload = json.loads(dumps(batch(1, 0, trades=[trade("f", 1)], books=[book(1)])))
    # json.loads without parse_float=Decimal produced binary floats.
    assert isinstance(payload["trades"][0]["price"], float)
    assert t.on_message("market_batch", payload).resync is StaleReason.MALFORMED


def test_decimal_decoded_payload_is_accepted() -> None:
    t = topic()
    payload = json.loads(
        dumps(batch(1, 0, trades=[trade("d", 1)], books=[book(1)])), parse_float=Decimal
    )
    out = t.on_message("market_batch", payload)
    assert out.resync is None and ids(out) == ["d"]


# --- standalone events --------------------------------------------------------------------------


def test_standalone_book_dirty_requires_a_refetch() -> None:
    t = topic()
    assert send(t, dirty(), "book_dirty").resync is StaleReason.BOOK_DIRTY_EVENT
    assert send(t, dirty(market=99), "book_dirty").resync is StaleReason.MALFORMED
    assert t.last_revision is None  # standalone events carry no delivery


def test_standalone_market_settled() -> None:
    t = topic()
    out = send(t, settled(), "market_settled")
    assert out.resync is StaleReason.MARKET_SETTLED
    assert [s.settledWith for s in out.settlements] == ["YES"]


def test_unknown_event_is_ignored() -> None:
    t = topic()
    out = send(t, {"anything": 1}, "presence_state")
    assert out.resync is None and t.last_revision is None


# --- REST snapshots ------------------------------------------------------------------------------


def test_snapshot_replaces_an_older_book_but_not_a_newer_one() -> None:
    t = topic()
    apply(t, 1, book(100))
    stamp = t.stamp()
    assert t.apply_snapshot([rest_exchange(90)], depth=200, started_at=stamp) == []
    assert t.books[str(EXCHANGE)].source is BookSource.PUSH  # stale snapshot ignored
    changed = t.apply_snapshot([rest_exchange(150)], depth=200, started_at=stamp)
    assert [b.as_of.sequence for b in changed] == [150]
    assert t.books[str(EXCHANGE)].source is BookSource.REST


def test_unversioned_snapshot_yields_to_a_push_made_after_the_request() -> None:
    t = topic()
    apply(t, 1, book(100))
    started = t.stamp()
    apply(t, 2, book(120))  # pushed while the REST read was in flight
    assert t.apply_snapshot([rest_exchange(None)], depth=200, started_at=started) == []
    assert t.books[str(EXCHANGE)].as_of.sequence == 120


def test_unversioned_snapshot_replaces_a_push_from_before_the_request() -> None:
    t = topic()
    apply(t, 1, book(100))
    changed = t.apply_snapshot([rest_exchange(None)], depth=200, started_at=t.stamp())
    assert [b.as_of for b in changed] == [None]
    # [documented] any pushed book replaces a held book with a null asOf.
    assert apply(t, 2, book(1)) == [1]


def test_snapshot_at_depth_is_marked_incomplete_and_expiry_unknown() -> None:
    t = topic()
    t.apply_snapshot([rest_exchange(5, bids=3, asks=2)], depth=3, started_at=0)
    held = t.books[str(EXCHANGE)]
    assert held.complete is False  # three bids at depth 3: more may exist
    assert held.expiry_known is False and held.next_expiry_at is None


def test_snapshot_drops_exchanges_it_does_not_list() -> None:
    t = topic()
    apply(t, 1, book(5, exchange=37))
    t.apply_snapshot([rest_exchange(6)], depth=200, started_at=t.stamp())
    assert set(t.books) == {str(EXCHANGE)}


@pytest.mark.parametrize(
    "exchanges",
    [
        lambda: [rest_exchange(5), rest_exchange(6)],
        lambda: [rest_exchange(5, at="2026-10-10 12:00")],
        lambda: [rest_exchange(5, exchange="3x")],
    ],
)
def test_bad_snapshot_applies_nothing(exchanges: Any) -> None:
    t = topic()
    apply(t, 1, book(100))
    with pytest.raises(SnapshotError):
        t.apply_snapshot(exchanges(), depth=200, started_at=0)
    assert t.books[str(EXCHANGE)].as_of.sequence == 100


# --- order expiry ---------------------------------------------------------------------------------


def test_next_expiry_is_reported_once() -> None:
    t = topic()
    apply(t, 1, book(5, next_expiry="2026-10-10T12:05:00Z"))
    before = parse_instant("2026-10-10T12:04:59.999Z")
    after = parse_instant("2026-10-10T12:05:00Z")
    assert t.take_due_expiry(before) is False
    assert t.take_due_expiry(after) is True
    assert t.take_due_expiry(after) is False


# --- timestamps ------------------------------------------------------------------------------------


def test_parse_instant_is_exact() -> None:
    a = parse_instant("2026-10-09T19:00:51.1493955+00:00")  # recorded book clock format
    assert a - parse_instant("2026-10-09T19:00:51Z") == Decimal("0.1493955")
    assert parse_instant("2026-10-09T21:00:00+02:00") == parse_instant("2026-10-09T19:00:00Z")
    assert parse_instant("2026-10-09T17:30:00-01:30") == parse_instant("2026-10-09T19:00:00Z")


@pytest.mark.parametrize(
    "text",
    [
        "2026-10-09T19:00:51",  # no offset
        "2026-10-09T19:00:51z",
        "2026-02-30T00:00:00Z",
        "2026-10-09T19:00:51+24:00",
        "２０２６-10-09T19:00:51Z",
        "2026-10-09T19:00:51Z\n",
    ],
)
def test_parse_instant_rejects(text: str) -> None:
    with pytest.raises(ValueError):
        parse_instant(text)


def test_topic_name_is_tournament_scoped() -> None:
    assert topic().topic == f"tournament:{TOURNAMENT.id}:market:{MARKET}"
