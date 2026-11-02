import asyncio
import json
import sqlite3
from datetime import timedelta
from decimal import Decimal

import httpx
import pytest

from bot.orders import OrderRefused, format_price, wire_price
from bot.sig_writer import CANCEL_DEADLINE_S
from bot.tests.kit import (
    NOW,
    FakeReader,
    body_of,
    error,
    make_intent,
    make_limits,
    make_rig,
    ok_body,
    reply,
)
from engine.config import MIDTERM_ELECTIONS


def run(coro):
    return asyncio.run(coro)


def place(rig, intent=None, **kwargs):
    return run(rig.path.place(intent or make_intent(), **kwargs))


def assert_identical_retries(rig, count):
    posts = rig.sig.posts()
    assert len(posts) == count
    assert len({p.content for p in posts}) == 1  # byte-identical, same key
    assert body_of(posts[0])["idempotencyKey"] == "key-1"


# --- journal ------------------------------------------------------------------------


def test_journal_row_committed_before_transport_sees_request(tmp_path):
    db_path = tmp_path / "state" / "bellwether.db"
    seen_rows = []

    def check_journal(request: httpx.Request) -> httpx.Response:
        key = json.loads(request.content)["idempotencyKey"]
        other = sqlite3.connect(db_path)  # a separate connection sees only committed rows
        seen_rows.append(
            other.execute("SELECT status FROM orders WHERE key = ?", (key,)).fetchone()
        )
        other.close()
        return reply(200, ok_body())

    rig = make_rig(tmp_path, [check_journal])
    record = place(rig)
    assert seen_rows == [("sent",)]
    assert record.status == "resting"


def test_request_body_fields(tmp_path):
    rig = make_rig(tmp_path, [reply(200, ok_body())])
    place(rig)
    body = body_of(rig.sig.posts()[0])
    assert body == {
        "exchangeId": "36",
        "side": "yes",
        "action": "buy",
        "quantity": 10,
        "price": Decimal("0.435"),
        "tournamentId": MIDTERM_ELECTIONS.id,
        "expirationDate": "2026-11-03T23:32:00Z",
        "idempotencyKey": "key-1",
    }
    assert b'"price":0.435' in rig.sig.posts()[0].content


# --- 200 outcomes -------------------------------------------------------------------


def test_200_resting(tmp_path):
    rig = make_rig(tmp_path, [reply(200, ok_body(quantityTraded=3, totalCost=Decimal("1.305")))])
    record = place(rig)
    assert record.status == "resting"
    assert record.sig_order_id == "1001"
    assert record.quantity_traded == "3"
    assert record.total_cost == "1.305"
    assert json.loads(record.response_json)["orderId"] == 1001
    assert not rig.paused


@pytest.mark.parametrize(("traded", "status"), [(10, "filled"), (4, "partial"), (0, "cancelled")])
def test_200_closed(tmp_path, traded, status):
    rig = make_rig(tmp_path, [reply(200, ok_body(open=False, quantityTraded=traded))])
    assert place(rig).status == status
    assert not rig.paused


@pytest.mark.parametrize(
    "field_value",
    [
        {"price": Decimal("0.44")},
        {"side": "no"},
        {"action": "sell"},
        {"quantity": 9},
        {"exchangeId": "37"},
        {"quantityTraded": 11},
        {"quantityTraded": Decimal("2.5")},
    ],
)
def test_canonical_mismatch_pauses(tmp_path, field_value):
    rig = make_rig(tmp_path, [reply(200, ok_body(**field_value))])
    place(rig)
    assert rig.paused
    assert rig.events("mismatch")


def test_unreadable_200_is_unknown_and_pauses(tmp_path):
    rig = make_rig(tmp_path, [httpx.Response(200, content=b"<html>")])
    assert place(rig).status == "unknown"
    assert rig.paused


def test_200_missing_fields_is_unknown(tmp_path):
    body = ok_body()
    del body["open"]
    rig = make_rig(tmp_path, [reply(200, body)])
    assert place(rig).status == "unknown"
    assert rig.paused


# --- rejections ---------------------------------------------------------------------


@pytest.mark.parametrize(
    ("status", "code"),
    [(400, "VALIDATION_ERROR"), (401, "INVALID_API_KEY"), (404, "NOT_FOUND"), (403, "FORBIDDEN")],
)
def test_rejections_without_pause(tmp_path, status, code):
    rig = make_rig(tmp_path, [error(status, code)])
    record = place(rig)
    assert (record.status, record.error, record.http_status) == ("rejected", code, status)
    assert not rig.paused
    assert len(rig.sig.posts()) == 1


def test_terms_not_acknowledged_rejects_and_pauses(tmp_path):
    rig = make_rig(tmp_path, [error(403, "TERMS_NOT_ACKNOWLEDGED")])
    assert place(rig).status == "rejected"
    assert rig.paused


def test_other_409_rejects_and_pauses(tmp_path):
    rig = make_rig(tmp_path, [error(409, "CONFLICT")])
    assert place(rig).status == "rejected"
    assert rig.paused
    assert len(rig.sig.posts()) == 1


# --- retries with the same key ------------------------------------------------------


def test_request_in_flight_waits_90s_and_retries_same_key(tmp_path):
    rig = make_rig(tmp_path, [error(409, "REQUEST_IN_FLIGHT"), reply(200, ok_body())])
    assert place(rig).status == "resting"
    assert 90.0 in rig.time.sleeps
    posts = rig.sig.posts()
    assert posts[1].at - posts[0].at >= 90
    assert_identical_retries(rig, 2)


def test_request_in_flight_forever_is_unknown(tmp_path):
    rig = make_rig(tmp_path, [error(409, "REQUEST_IN_FLIGHT")] * 4)
    assert place(rig).status == "unknown"
    assert rig.paused
    assert_identical_retries(rig, 4)


def test_429_honours_retry_after_and_retries_same_key(tmp_path):
    rig = make_rig(
        tmp_path, [error(429, "RATE_LIMITED", {"Retry-After": "7"}), reply(200, ok_body())]
    )
    assert place(rig).status == "resting"
    posts = rig.sig.posts()
    assert posts[1].at - posts[0].at >= 7
    assert_identical_retries(rig, 2)
    assert rig.events("rate_limited")


def test_502_order_status_unknown_then_200(tmp_path):
    rig = make_rig(
        tmp_path,
        [
            error(502, "ORDER_STATUS_UNKNOWN"),
            error(503, "SERVICE_UNAVAILABLE"),
            reply(200, ok_body()),
        ],
    )
    assert place(rig).status == "resting"
    assert rig.time.sleeps == [2.0, 4.0]
    assert_identical_retries(rig, 3)


def test_503_five_times_is_unknown_and_pauses(tmp_path):
    rig = make_rig(tmp_path, [error(503, "SERVICE_UNAVAILABLE")] * 5)
    record = place(rig)
    assert record.status == "unknown"
    assert rig.time.sleeps == [2.0, 4.0, 8.0, 16.0]
    assert_identical_retries(rig, 5)
    assert rig.paused
    assert rig.events("order_unknown")


@pytest.mark.parametrize("status", [500, 502])
def test_undocumented_failure_is_unknown_without_retry(tmp_path, status):
    rig = make_rig(tmp_path, [error(status, "INTERNAL_ERROR")])
    assert place(rig).status == "unknown"
    assert rig.paused
    assert len(rig.sig.posts()) == 1


def test_timeout_then_success_uses_same_key(tmp_path):
    rig = make_rig(
        tmp_path, [httpx.ReadTimeout("t"), httpx.ConnectError("c"), reply(200, ok_body())]
    )
    assert place(rig).status == "resting"
    assert_identical_retries(rig, 3)
    assert not rig.paused


def test_no_response_retries_three_times_then_unknown(tmp_path):
    rig = make_rig(tmp_path, [httpx.ReadTimeout("t")] * 4)
    record = place(rig)
    assert record.status == "unknown"
    assert_identical_retries(rig, 4)
    posts = rig.sig.posts()
    assert posts[-1].at - posts[0].at < 300
    assert rig.paused
    assert rig.events("order_unknown")


def test_unknown_blocks_every_new_key(tmp_path):
    rig = make_rig(tmp_path, [httpx.ReadTimeout("t")] * 4)
    place(rig)
    (rig.state_dir / "PAUSED").unlink()  # even if the pause file is removed
    record = place(rig, make_intent(sig_exchange_id="40"))
    assert record.status == "blocked" and "unknown" in record.reason
    assert len(rig.sig.posts()) == 4
    assert [r["key"] for r in rig.rows()] == ["key-1"]


# --- NO orders and wire prices ------------------------------------------------------


def test_no_with_unknown_convention_never_reaches_transport(tmp_path):
    rig = make_rig(tmp_path, [], limits=make_limits(no_orders_enabled=True))
    record = place(rig, make_intent(side="no"))
    assert record.status == "blocked"
    assert rig.sig.seen == []
    assert rig.rows() == []
    with pytest.raises(OrderRefused):
        wire_price(make_intent(side="no"), make_limits(), probe=False)


@pytest.mark.parametrize(("convention", "wire"), [("no_price", "0.435"), ("yes_price", "0.565")])
def test_no_conventions_give_the_right_wire_price(tmp_path, convention, wire):
    limits = make_limits(no_orders_enabled=True, no_price_convention=convention)
    rig = make_rig(tmp_path, [reply(200, ok_body(side="no"))], limits=limits)
    record = place(rig, make_intent(side="no"))
    assert record.status == "resting" and not rig.paused
    assert body_of(rig.sig.posts()[0])["price"] == Decimal(wire)
    assert record.wire_price == wire


def test_every_grid_price_serialises_exactly():
    for ticks in range(1, 200):
        price = Decimal(ticks) * Decimal("0.005")
        text = format_price(price)
        assert Decimal(text) == price
        assert len(text.partition(".")[2]) <= 3
        assert "e" not in text.lower()
    for bad in (Decimal("0.4351"), Decimal("0.437"), Decimal(1), Decimal(0), 0.435):
        with pytest.raises(OrderRefused):
            format_price(bad)


def test_wire_prices_parse_back_on_the_grid(tmp_path):
    rig = make_rig(tmp_path, [reply(200, ok_body(price=Decimal("0.01")))])
    place(rig, make_intent(price_side=Decimal("0.010")))
    raw = rig.sig.posts()[0].content
    assert b'"price":0.01,' in raw
    price = json.loads(raw, parse_float=Decimal)["price"]
    assert isinstance(price, Decimal) and price * 200 == int(price * 200)


# --- dry run and probe --------------------------------------------------------------


def test_dry_run_mode_sends_nothing(tmp_path):
    rig = make_rig(tmp_path, [], limits=make_limits(mode="dry_run"))
    assert place(rig).status == "would_order"
    assert rig.sig.seen == [] and rig.rows() == []
    verdicts = [r["verdict"] for r in rig.conn.execute("SELECT verdict FROM decisions")]
    assert verdicts == ["would_order"]


def test_not_live_sends_nothing(tmp_path):
    rig = make_rig(tmp_path, [], live=False)
    assert place(rig).status == "would_order"
    assert rig.sig.seen == []


def test_probe_no_is_sent_raw_and_price_difference_does_not_pause(tmp_path):
    rig = make_rig(
        tmp_path,
        [reply(200, ok_body(side="no", price=Decimal("0.98"), quantity=1))],
        limits=make_limits(mode="dry_run"),  # a probe needs live=True only
    )
    intent = make_intent(side="no", price_side=Decimal("0.020"), quantity=1, strategy="probe")
    record = place(rig, intent, probe=True, expiry_seconds=60)
    assert record.status == "resting"
    body = body_of(rig.sig.posts()[0])
    assert body["price"] == Decimal("0.02") and body["side"] == "no"
    assert body["expirationDate"] == "2026-11-03T23:31:00Z"
    assert not rig.paused


def test_balance_read_failure_blocks(tmp_path):
    rig = make_rig(tmp_path, [], reader=FakeReader(fail_balance=True))
    assert place(rig).status == "blocked"
    assert rig.sig.seen == []


# --- protective cancellation --------------------------------------------------------


def test_cancel_works_while_paused_and_refreshes(tmp_path):
    reader = FakeReader(open_ids=[1001])
    rig = make_rig(tmp_path, [reply(200, ok_body())], reader=reader)
    record = place(rig)
    (rig.state_dir / "PAUSED").write_text("manual")
    (rig.state_dir / "STOP").write_text("stop")
    reader.open_ids = []
    rig.sig.add(
        reply(200, {"orderId": 1001, "tournamentId": MIDTERM_ELECTIONS.id, "message": "ok"})
    )
    run(rig.path.cancel(record.key))
    last = rig.sig.seen[-1]
    assert (last.method, last.path) == ("DELETE", "/orders/1001")
    assert rig.rows()[0]["status"] == "cancelled"
    assert len(rig.sig.posts()) == 1  # nothing placed by the cancel
    assert any("cancel key-1" in e["message"] for e in rig.events("reconcile"))


def test_cancel_retries_503_until_409(tmp_path):
    rig = make_rig(tmp_path, [reply(200, ok_body())], reader=FakeReader(open_ids=[]))
    record = place(rig)
    rig.reader.fills[1001] = (Decimal(10), Decimal("0.43"))
    rig.sig.add(error(503, "SERVICE_UNAVAILABLE"), error(409, "CONFLICT"))
    run(rig.path.cancel(record.key))
    assert [s.method for s in rig.sig.seen] == ["POST", "DELETE", "DELETE"]
    row = rig.rows()[0]
    assert row["status"] == "filled" and row["quantity_traded"] == "10"


def test_cancel_all_sends_tournament_and_repeats_503(tmp_path):
    rig = make_rig(
        tmp_path,
        [
            error(503, "SERVICE_UNAVAILABLE"),
            error(503, "TX_CONFLICT"),
            reply(200, {"cancelled": 2}),
        ],
    )
    run(rig.path.cancel_all("STOP file"))
    assert [(s.method, s.path) for s in rig.sig.seen] == [("POST", "/orders/cancel-all")] * 3
    assert all(
        json.loads(s.content) == {"tournamentId": MIDTERM_ELECTIONS.id} for s in rig.sig.seen
    )
    assert rig.events("stop")[0]["level"] == "info"


def test_cancel_all_gives_up_after_60s(tmp_path):
    rig = make_rig(tmp_path, [error(503, "SERVICE_UNAVAILABLE")] * 50)
    run(rig.path.cancel_all("STOP file"))
    seen = rig.sig.seen
    assert seen[-1].at - seen[0].at < CANCEL_DEADLINE_S
    assert rig.events("stop")[0]["level"] == "error"


def test_refresh_marks_expired(tmp_path):
    rig = make_rig(tmp_path, [reply(200, ok_body())], reader=FakeReader(open_ids=[1001]))
    place(rig)
    rig.reader.open_ids = []
    rig.path._now = lambda: NOW + timedelta(minutes=5)  # past the 120 s expiry
    run(rig.path.refresh_statuses())
    assert rig.rows()[0]["status"] == "expired"
