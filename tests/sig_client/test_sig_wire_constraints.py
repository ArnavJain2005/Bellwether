"""F1 area 5: documented wire constraints on orders."""

import copy
from decimal import Decimal
from typing import Any

import pytest
from pydantic import ValidationError
from sig_testkit import API, Router, fixture, json_response, make_client, run

from engine.sig_client.errors import SigResponseError
from engine.sig_client.wire import OrderCoverage

ORDERS = f"{API}/orders"


def orders_with(mutate: Any) -> Router:
    body = copy.deepcopy(fixture("orders_page"))
    mutate(body)
    return Router({ORDERS: json_response(200, body)})


@pytest.mark.parametrize("quantity", [0, -1, -0.5, 0.0])
def test_order_quantity_must_be_strictly_positive(quantity: Any) -> None:
    client, _, _ = make_client(orders_with(lambda b: b["data"][0].update(quantity=quantity)))
    with pytest.raises(SigResponseError, match="quantity"):
        run(client.list_orders_page())


@pytest.mark.parametrize(
    ("raw", "exact"),
    [(0.25, Decimal("0.25")), (0.0000001, Decimal("1E-7")), (2147483647, Decimal(2147483647))],
)
def test_positive_fractional_quantities_are_kept_exactly(raw: Any, exact: Decimal) -> None:
    client, _, _ = make_client(orders_with(lambda b: b["data"][0].update(quantity=raw)))
    assert run(client.list_orders_page()).data[0].quantity == exact


@pytest.mark.parametrize("complete", [False, 1, 1.0, "true", None, 0])
def test_coverage_complete_accepts_only_json_true(complete: Any) -> None:
    client, _, _ = make_client(orders_with(lambda b: b["coverage"].update(complete=complete)))
    with pytest.raises(SigResponseError, match="complete"):
        run(client.list_orders_page())


def test_coverage_true_and_missing_coverage_still_work() -> None:
    client, _, _ = make_client()
    assert run(client.list_orders_page()).coverage.complete is True  # type: ignore[union-attr]
    client, _, _ = make_client(orders_with(lambda b: b.pop("coverage")))
    assert run(client.list_orders_page()).coverage is None


def test_coverage_model_rejects_integer_one_directly() -> None:
    with pytest.raises(ValidationError):
        OrderCoverage.model_validate({"complete": 1, "projectedThroughSequence": None})
    assert OrderCoverage.model_validate({"complete": True, "projectedThroughSequence": 5})


def test_null_price_limit_and_unknown_fields_are_unchanged() -> None:
    def mutate(body: dict[str, Any]) -> None:
        body["data"][0]["priceLimit"] = None
        body["data"][0]["futureField"] = {"x": 1}

    client, _, _ = make_client(orders_with(mutate))
    order = run(client.list_orders_page()).data[0]
    assert order.priceLimit is None
