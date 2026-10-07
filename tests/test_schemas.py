import json
from datetime import UTC, datetime, timedelta, timezone
from decimal import Decimal
from typing import Any

import pytest
from pydantic import BaseModel, ValidationError

from shared.export_schema import SCHEMA_PATH, build_schema
from shared.schemas import (
    MAX_ORDER_QTY,
    Action,
    EngineMessage,
    FairValue,
    Fill,
    MarketMapping,
    OrderIntent,
    OverrideCommand,
    Position,
    Quote,
    QuoteSource,
    RiskLimits,
    Side,
    Signal,
    price_to_milli,
    price_to_ticks,
    ticks_to_price,
)

NOW = datetime(2026, 10, 8, 14, 2, 11, tzinfo=UTC)


def intent(**overrides: object) -> OrderIntent:
    fields: dict[str, object] = {
        "idempotency_key": "k-1",
        "exchange_id": "36",
        "side": Side.YES,
        "action": Action.BUY,
        "qty": 10,
        "price": 100,
        "reason": "test",
    }
    fields.update(overrides)
    return OrderIntent.model_validate(fields)


@pytest.mark.parametrize(
    ("price", "ticks"),
    [("0.005", 1), ("0.5", 100), ("0.995", 199), (Decimal("0.535"), 107), (0.25, 50)],
)
def test_price_to_ticks_round_trips(price: object, ticks: int) -> None:
    assert price_to_ticks(price) == ticks  # type: ignore[arg-type]
    assert ticks_to_price(ticks) == Decimal(str(price))


@pytest.mark.parametrize("price", ["0.537", "0.0001", "1.005", "-0.005"])
def test_price_to_ticks_rejects_off_grid_or_out_of_range(price: str) -> None:
    with pytest.raises(ValueError):
        price_to_ticks(price)


@pytest.mark.parametrize(("price", "milli"), [("0.537", 537), ("0.005", 5), ("0", 0), ("1", 1000)])
def test_price_to_milli(price: str, milli: int) -> None:
    assert price_to_milli(price) == milli


@pytest.mark.parametrize("price", ["0.5375", "1.001", "-0.001"])
def test_price_to_milli_rejects_off_grid_or_out_of_range(price: str) -> None:
    with pytest.raises(ValueError):
        price_to_milli(price)


def test_quote_keeps_polymarket_precision() -> None:
    quote = Quote(source=QuoteSource.POLYMARKET, exchange_id="e", bid=537, ask=539, ts=NOW)
    assert quote.mid == 538
    with pytest.raises(ValidationError):
        Quote(source=QuoteSource.POLYMARKET, exchange_id="e", bid=537, ask=1001, ts=NOW)


def test_fair_value_and_override_use_milli() -> None:
    fv = FairValue(
        exchange_id="e", value=537.4, inputs={"polymarket": 1.0}, model_version="v1", ts=NOW
    )
    assert fv.value == 537.4
    assert OverrideCommand(exchange_id="e", value=1000).value == 1000
    with pytest.raises(ValidationError):
        FairValue(exchange_id="e", value=1000.1, inputs={}, model_version="v1", ts=NOW)
    with pytest.raises(ValidationError):
        OverrideCommand(exchange_id="e", value=-1)


def test_valid_order_intent() -> None:
    assert intent().price == 100


@pytest.mark.parametrize("price", [0, 200, -1, 201])
def test_order_intent_cannot_encode_market_order(price: int) -> None:
    with pytest.raises(ValidationError):
        intent(price=price)


@pytest.mark.parametrize(
    "overrides",
    [
        {"price": 100.5},
        {"price": "100"},
        {"qty": 0},
        {"qty": -5},
        {"idempotency_key": ""},
        {"reason": ""},
        {"side": "maybe"},
        {"action": "short"},
        {"unexpected": 1},
    ],
)
def test_order_intent_rejects_bad_fields(overrides: dict[str, object]) -> None:
    with pytest.raises(ValidationError):
        intent(**overrides)


def test_order_intent_is_immutable() -> None:
    order = intent()
    with pytest.raises(ValidationError):
        order.price = 150  # type: ignore[misc]


def test_naive_timestamps_are_rejected() -> None:
    naive = datetime(2026, 10, 8)  # noqa: DTZ001 - deliberately naive
    with pytest.raises(ValidationError):
        Quote(source=QuoteSource.SIG, exchange_id="e", bid=1, ask=2, ts=naive)


def test_timestamps_are_normalised_to_utc() -> None:
    ist = timezone(timedelta(hours=5, minutes=30))
    quote = Quote(
        source=QuoteSource.SIG,
        exchange_id="e",
        bid=1,
        ask=2,
        ts=datetime(2026, 10, 8, 19, 32, 11, tzinfo=ist),
    )
    assert quote.ts == NOW
    assert quote.ts.tzinfo == UTC


def test_quote_mid() -> None:
    quote = Quote(source=QuoteSource.KALSHI, exchange_id="e", bid=99, ask=100, ts=NOW)
    assert quote.mid == 99.5
    one_sided = Quote(source=QuoteSource.KALSHI, exchange_id="e", bid=None, ask=100, ts=NOW)
    assert one_sided.mid is None


def test_signal_requires_reason() -> None:
    with pytest.raises(ValidationError):
        Signal(
            exchange_id="e", strategy="s4", side=Side.YES, edge_cents=3, size=1, reason="", ts=NOW
        )


def test_engine_message_envelope() -> None:
    msg = EngineMessage(type="health", ts=NOW, data={"ok": True})
    assert msg.model_dump(mode="json")["ts"] == "2026-10-08T14:02:11Z"
    with pytest.raises(ValidationError):
        EngineMessage(type="trade", ts=NOW, data={})  # type: ignore[arg-type]


def test_exported_json_schema_is_up_to_date() -> None:
    assert SCHEMA_PATH.read_text(encoding="utf-8") == build_schema(), (
        "shared/schemas.json is stale: run `python -m shared.export_schema`"
    )


# S1-T1-F1 regression tests: numeric hardening -----------------------------------

NON_FINITE = [float("nan"), float("inf"), float("-inf")]

VALID: dict[type[BaseModel], dict[str, Any]] = {
    RiskLimits: {
        "max_race_pct": 5,
        "max_order_susqies": 2000,
        "max_scenario_loss_pct": 10,
        "fair_value_max_age_s": 30,
        "min_cash_pct": 50,
    },
    FairValue: {
        "exchange_id": "36",
        "value": 537.4,
        "inputs": {"polymarket": 0.6, "kalshi": 0.4},
        "model_version": "v1",
        "ts": NOW,
    },
    OverrideCommand: {"exchange_id": "36", "value": 500},
    Signal: {
        "exchange_id": "36",
        "strategy": "s4",
        "side": Side.YES,
        "edge_cents": 3.5,
        "size": 10,
        "reason": "test",
        "ts": NOW,
    },
    Fill: {
        "id": 1,
        "order_id": 2,
        "exchange_id": "36",
        "market_id": "m",
        "price": 100,
        "quantity": -5.0,
        "side": Side.NO,
        "filled_at": NOW,
    },
    Position: {
        "exchange_id": "36",
        "market_id": "m",
        "market_title": "t",
        "settled": False,
        "quantity": -5.0,
        "avg_cost": 0.45,
        "current_price": 0.5,
        "unrealized_pnl": -12.5,
    },
    MarketMapping: {"sig_exchange_id": "36", "match_confidence": 0.9},
}


def build(model: type[BaseModel], **overrides: Any) -> BaseModel:
    return model.model_validate({**VALID[model], **overrides})


# 1. Non-finite values -----------------------------------------------------------

FLOAT_FIELDS = [
    (RiskLimits, "max_race_pct"),
    (RiskLimits, "max_order_susqies"),
    (RiskLimits, "max_scenario_loss_pct"),
    (RiskLimits, "fair_value_max_age_s"),
    (RiskLimits, "min_cash_pct"),
    (FairValue, "value"),
    (OverrideCommand, "value"),
    (Signal, "edge_cents"),
    (Fill, "quantity"),
    (Position, "quantity"),
    (Position, "avg_cost"),
    (Position, "current_price"),
    (Position, "unrealized_pnl"),
    (MarketMapping, "match_confidence"),
]


@pytest.mark.parametrize("value", NON_FINITE, ids=["nan", "inf", "-inf"])
@pytest.mark.parametrize(("model", "field"), FLOAT_FIELDS, ids=lambda x: getattr(x, "__name__", x))
def test_float_fields_reject_non_finite(model: type[BaseModel], field: str, value: float) -> None:
    with pytest.raises(ValidationError):
        build(model, **{field: value})


@pytest.mark.parametrize("value", NON_FINITE, ids=["nan", "inf", "-inf"])
def test_fair_value_inputs_reject_non_finite_weights(value: float) -> None:
    with pytest.raises(ValidationError):
        build(FairValue, inputs={"polymarket": value, "kalshi": 0.5})


@pytest.mark.parametrize("field", ["max_order_susqies", "fair_value_max_age_s"])
def test_unbounded_risk_limits_reject_positive_infinity(field: str) -> None:
    """These two have no upper bound, so only the finite check stops +inf."""
    with pytest.raises(ValidationError):
        build(RiskLimits, **{field: float("inf")})
    assert getattr(build(RiskLimits, **{field: 1e12}), field) == 1e12


@pytest.mark.parametrize("text", ["NaN", "Infinity", "-Infinity"])
def test_non_finite_rejected_from_json_and_strings(text: str) -> None:
    payload = json.dumps(VALID[RiskLimits]).replace("2000", text)
    with pytest.raises(ValidationError):
        RiskLimits.model_validate_json(payload)
    with pytest.raises(ValidationError):
        build(RiskLimits, max_order_susqies=text.lower().replace("infinity", "inf"))


def test_valid_finite_values_still_accepted() -> None:
    position = build(Position, quantity=-250.5, unrealized_pnl=-1234.75)
    assert isinstance(position, Position)
    assert position.quantity == -250.5
    assert position.unrealized_pnl == -1234.75
    fill = build(Fill, quantity=-5.0)
    assert isinstance(fill, Fill)
    assert fill.quantity == -5.0
    signal = build(Signal, edge_cents=-3.0)
    assert isinstance(signal, Signal)
    assert signal.edge_cents == -3.0
    assert isinstance(build(FairValue, inputs={"polymarket": 0.0}), FairValue)


@pytest.mark.parametrize("value", [*NON_FINITE, "nan", "inf", "-inf"])
def test_price_helpers_reject_non_finite(value: float | str) -> None:
    with pytest.raises(ValueError):
        price_to_ticks(value)
    with pytest.raises(ValueError):
        price_to_milli(value)


# 2 and 3. Strict quantities and OrderInput constraints --------------------------


@pytest.mark.parametrize("qty", [1, 10, MAX_ORDER_QTY])
def test_order_qty_accepts_api_range(qty: int) -> None:
    assert intent(qty=qty).qty == qty


@pytest.mark.parametrize(
    "qty", [0, -1, MAX_ORDER_QTY + 1, True, False, "10", 10.0, 10.5, None], ids=repr
)
def test_order_qty_rejects_out_of_range_and_non_int(qty: object) -> None:
    with pytest.raises(ValidationError):
        intent(qty=qty)


def test_order_qty_rejects_float_from_json() -> None:
    payload = intent().model_dump(mode="json")
    payload["qty"] = 10.0
    with pytest.raises(ValidationError):
        OrderIntent.model_validate_json(json.dumps(payload))


@pytest.mark.parametrize("size", [0, -1, True, False, "10", 10.0, 1.5], ids=repr)
def test_signal_size_is_strict_positive_int(size: object) -> None:
    with pytest.raises(ValidationError):
        build(Signal, size=size)
    signal = build(Signal, size=1)
    assert isinstance(signal, Signal)
    assert signal.size == 1


@pytest.mark.parametrize("length", [1, 255])
def test_idempotency_key_accepts_api_lengths(length: int) -> None:
    assert len(intent(idempotency_key="k" * length).idempotency_key) == length


@pytest.mark.parametrize("key", ["", "k" * 256])
def test_idempotency_key_rejects_outside_api_lengths(key: str) -> None:
    with pytest.raises(ValidationError):
        intent(idempotency_key=key)


@pytest.mark.parametrize("exchange_id", ["0", "36", "1234567890"])
def test_exchange_id_accepts_ascii_digits(exchange_id: str) -> None:
    assert intent(exchange_id=exchange_id).exchange_id == exchange_id


@pytest.mark.parametrize(
    "exchange_id",
    ["", "ex-1", "36a", "-36", "3.6", " 36", "36 ", "36\n", "٣٦", "３６", 36],
    ids=repr,
)
def test_exchange_id_rejects_non_ascii_digit_strings(exchange_id: object) -> None:
    with pytest.raises(ValidationError):
        intent(exchange_id=exchange_id)


# 4. Unit descriptions in the exported JSON Schema -------------------------------

UNIT_FIELDS = [
    ("Quote", "bid", "milli", "x 1000"),
    ("Quote", "ask", "milli", "x 1000"),
    ("Quote", "mid", "milli", "x 1000"),
    ("FairValue", "value", "milli", "x 1000"),
    ("OverrideCommand", "value", "milli", "x 1000"),
    ("OrderIntent", "price", "ticks", "x 200"),
    ("Fill", "price", "ticks", "x 200"),
]


@pytest.mark.parametrize(("model", "field", "unit", "scale"), UNIT_FIELDS)
def test_exported_schema_describes_price_units(
    model: str, field: str, unit: str, scale: str
) -> None:
    schema = json.loads(SCHEMA_PATH.read_text(encoding="utf-8"))
    description = schema["$defs"][model]["properties"][field]["description"]
    assert unit in description.lower()
    assert scale in description
    assert ("YES" in description) or (model == "OrderIntent" and "NO" in description)


# 5. Quote serialization: dashboard output vs storage/replay ---------------------


def quote() -> Quote:
    return Quote(source=QuoteSource.POLYMARKET, exchange_id="tok", bid=537, ask=540, ts=NOW)


def test_dashboard_dump_includes_computed_mid() -> None:
    dumped = quote().model_dump(mode="json")
    assert dumped["mid"] == 538.5
    assert json.loads(quote().model_dump_json())["mid"] == 538.5


def test_dashboard_dump_is_not_valid_input() -> None:
    """Extra fields stay forbidden, so a dumped mid is never read back as input."""
    with pytest.raises(ValidationError):
        Quote.model_validate(quote().model_dump(mode="json"))


def test_round_trip_dump_replays_into_equal_quote() -> None:
    original = quote()
    stored = original.model_dump(mode="json", round_trip=True)
    assert "mid" not in stored
    assert Quote.model_validate(stored) == original
    stored_json = original.model_dump_json(round_trip=True)
    assert "mid" not in json.loads(stored_json)
    replayed = Quote.model_validate_json(stored_json)
    assert replayed == original
    assert replayed.mid == 538.5


@pytest.mark.parametrize("mid", [538.5, 999.0])
def test_externally_supplied_mid_is_rejected(mid: float) -> None:
    stored = quote().model_dump(mode="json", round_trip=True)
    with pytest.raises(ValidationError):
        Quote.model_validate({**stored, "mid": mid})
