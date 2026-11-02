from dataclasses import replace
from decimal import Decimal

import pytest

from bot import db, risk
from bot.risk import RiskState, check
from bot.tests.kit import make_intent, make_limits

# At scale 0.10: per_order 50, per_race 200, total 1000, loss_stop 200; reserve 90000.


def state(tmp_path, **overrides) -> RiskState:
    values = {
        "limits": make_limits(),
        "balance": Decimal(100000),
        "total_committed": Decimal(0),
        "race_committed": Decimal(0),
        "writes_last_min": 0,
        "unknown_orders": 0,
        "state_dir": tmp_path,
    }
    values.update(overrides)
    return RiskState(**values)


def test_allows_and_reports_max_quantity(tmp_path):
    result = check(make_intent(), state(tmp_path))
    assert result.allowed
    assert result.max_quantity == 114  # floor(50 / 0.435)
    assert "within every cap" in result.reason


@pytest.mark.parametrize("name", ["STOP", "PAUSED"])
def test_stop_and_paused_files_deny(tmp_path, name):
    (tmp_path / name).write_text("x")
    result = check(make_intent(), state(tmp_path))
    assert not result.allowed and name in result.reason and result.max_quantity == 0


def test_unknown_order_denies(tmp_path):
    result = check(make_intent(), state(tmp_path, unknown_orders=1))
    assert not result.allowed and "unknown outcome" in result.reason


def test_sell_denied(tmp_path):
    result = check(make_intent(action="sell"), state(tmp_path))
    assert not result.allowed and "only buys" in result.reason


def test_no_denied_while_disabled(tmp_path):
    result = check(make_intent(side="no"), state(tmp_path))
    assert not result.allowed and "disabled" in result.reason


def test_no_denied_with_unknown_convention_even_if_enabled(tmp_path):
    limits = make_limits(no_orders_enabled=True)
    result = check(make_intent(side="no"), state(tmp_path, limits=limits))
    assert not result.allowed and "OQ-W3" in result.reason


def test_no_allowed_with_known_convention(tmp_path):
    limits = make_limits(no_orders_enabled=True, no_price_convention="no_price")
    assert check(make_intent(side="no"), state(tmp_path, limits=limits)).allowed


@pytest.mark.parametrize(
    "price", [0.435, Decimal("0.437"), Decimal(0), Decimal(1), Decimal("0.000"), Decimal("NaN")]
)
def test_bad_prices_denied(tmp_path, price):
    result = check(make_intent(price_side=price), state(tmp_path))
    assert not result.allowed


@pytest.mark.parametrize("quantity", [0, -1, True, 1.0])
def test_bad_quantity_denied(tmp_path, quantity):
    assert not check(make_intent(quantity=quantity), state(tmp_path)).allowed


def test_per_order_cap(tmp_path):
    result = check(make_intent(quantity=115), state(tmp_path))  # 50.025 > 50
    assert not result.allowed and "per-order cap" in result.reason
    assert result.max_quantity == 114
    assert check(make_intent(quantity=114), state(tmp_path)).allowed


def test_per_race_cap(tmp_path):
    s = state(tmp_path, race_committed=Decimal(190), total_committed=Decimal(190))
    result = check(make_intent(quantity=23), s)  # 10.005 > 10
    assert not result.allowed and "per-race cap" in result.reason
    assert result.max_quantity == 22


def test_total_committed_cap(tmp_path):
    result = check(make_intent(quantity=23), state(tmp_path, total_committed=Decimal(990)))
    assert not result.allowed and "total committed cap" in result.reason
    assert result.max_quantity == 22


def test_min_cash_reserve(tmp_path):
    result = check(make_intent(quantity=23), state(tmp_path, balance=Decimal(90010)))
    assert not result.allowed and "reserve" in result.reason
    assert result.max_quantity == 22


def test_balance_unknown_denied(tmp_path):
    result = check(make_intent(), state(tmp_path, balance=None))
    assert not result.allowed and "balance is unknown" in result.reason


def test_loss_stop(tmp_path):
    limits = make_limits(start_balance=100000)
    # loss_stop 2000 x 0.10 = 200
    assert not check(make_intent(), state(tmp_path, limits=limits, balance=Decimal(99799))).allowed
    assert check(make_intent(), state(tmp_path, limits=limits, balance=Decimal(99800))).allowed


def test_writes_per_minute(tmp_path):
    result = check(make_intent(), state(tmp_path, writes_last_min=20))
    assert not result.allowed and "writes" in result.reason
    assert check(make_intent(), state(tmp_path, writes_last_min=19)).allowed


def test_scale_raises_caps(tmp_path):
    s = state(tmp_path, limits=make_limits(scale=1))
    assert check(make_intent(quantity=1149), s).max_quantity == 1149  # floor(500 / 0.435)


def test_probe_permits_no_and_caps_cost_at_one(tmp_path):
    probe = make_intent(side="no", price_side=Decimal("0.020"), quantity=1, strategy="probe")
    assert check(probe, state(tmp_path), probe=True).allowed
    # A probe NO counts at max(p, 1 - p) = 0.98 per share: two shares exceed 1 SUSQie.
    result = check(replace(probe, quantity=2), state(tmp_path), probe=True)
    assert not result.allowed and result.max_quantity == 1


def test_probe_flag_and_strategy_go_together(tmp_path):
    assert not check(make_intent(), state(tmp_path), probe=True).allowed
    assert not check(make_intent(strategy="probe"), state(tmp_path)).allowed


def _order(conn, key, status, *, price="0.400", quantity=10, traded=None, cost=None, **extra):
    row = {
        "key": key,
        "created_utc": "2026-11-03T00:00:00Z",
        "updated_utc": "2026-11-03T00:00:00Z",
        "race_key": extra.get("race_key", "senate:PA"),
        "sig_market_id": "10",
        "sig_exchange_id": "36",
        "side": extra.get("side", "yes"),
        "action": "buy",
        "price_side": price,
        "wire_price": price,
        "quantity": quantity,
        "expires_utc": "2026-11-03T00:02:00Z",
        "strategy": extra.get("strategy", "reference"),
        "status": status,
        "quantity_traded": traded,
        "total_cost": cost,
    }
    db.insert_order(conn, row)


def test_committed_math(tmp_path):
    conn = db.connect(tmp_path / "b.db")
    _order(conn, "filled", "filled", traded="10", cost="3.9")  # max(3.9, 4.0) = 4.0
    _order(conn, "partial", "partial", traded="4", cost="1.7")  # max(1.7, 1.6) = 1.7
    _order(conn, "resting", "resting", traded="2", cost="0.8")  # 0.8 + 0.4 x 8 = 4.0
    _order(conn, "journaled", "journaled")  # 4.0
    _order(conn, "sent", "sent")  # 4.0
    _order(conn, "unknown", "unknown")  # 4.0 (full size)
    _order(conn, "rejected", "rejected")  # 0
    _order(conn, "expired", "expired", traded="0", cost="0")  # 0
    _order(conn, "cancelled-unread", "cancelled")  # fills unknown: 4.0
    _order(conn, "other-race", "resting", traded="0", cost="0", race_key="house_control")  # 4.0
    _order(
        conn,
        "probe-no",
        "resting",
        price="0.020",
        quantity=1,
        side="no",
        strategy="probe",
        race_key="probe",
    )  # 0.98
    limits = make_limits(preexisting_committed=100)
    s = risk.build_state(
        conn,
        limits,
        race_key="senate:PA",
        balance=Decimal(1),
        writes_last_min=0,
        state_dir=tmp_path,
    )
    assert s.race_committed == Decimal("25.7")
    assert s.total_committed == Decimal("25.7") + 4 + Decimal("0.98") + 100
    assert s.unknown_orders == 1
