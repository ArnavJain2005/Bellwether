from datetime import UTC, datetime, timedelta, timezone
from decimal import Decimal

import pytest
from pydantic import ValidationError

from shared.export_schema import SCHEMA_PATH, build_schema
from shared.schemas import (
    Action,
    EngineMessage,
    FairValue,
    OrderIntent,
    OverrideCommand,
    Quote,
    QuoteSource,
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
        "exchange_id": "ex-1",
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
