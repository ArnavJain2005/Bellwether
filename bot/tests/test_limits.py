from decimal import Decimal
from pathlib import Path

import pytest
import yaml

from bot.limits import DEFAULT_PATH, LimitsError, load_limits
from bot.tests.kit import make_limits


def _write(tmp_path: Path, raw: dict) -> Path:
    path = tmp_path / "risk.yaml"
    path.write_text(yaml.safe_dump(raw), encoding="utf-8")
    return path


def _repo_raw() -> dict:
    return yaml.safe_load(DEFAULT_PATH.read_text(encoding="utf-8"))


def test_repo_risk_yaml_loads_with_exact_decimals():
    limits = load_limits()
    assert limits.scale == Decimal("1.0")
    assert str(limits.scale) == "1.0"  # from the YAML text, never through float
    assert limits.strategies.complement.max_sum_ask == Decimal("0.97")
    assert limits.no_price_convention == "unknown"
    assert limits.no_orders_enabled is False


def test_effective_cap_scales_caps_but_not_the_reserve():
    limits = make_limits(scale="0.5")
    assert limits.effective_cap("per_order") == Decimal(250)
    assert limits.effective_cap("per_race") == Decimal(1000)
    assert limits.effective_cap("total_committed") == Decimal(5000)
    assert limits.effective_cap("loss_stop") == Decimal(1000)
    assert limits.effective_cap("min_cash_reserve") == Decimal(90000)


@pytest.mark.parametrize(
    "path",
    [("mode",), ("caps", "per_order"), ("start_balance",), ("strategies", "reference", "edge")],
)
def test_missing_value_refuses_to_start(tmp_path, path):
    raw = _repo_raw()
    target = raw
    for part in path[:-1]:
        target = target[part]
    del target[path[-1]]
    with pytest.raises(LimitsError):
        load_limits(_write(tmp_path, raw))


@pytest.mark.parametrize(
    ("path", "value"),
    [
        (("scale",), 1.5),
        (("scale",), 0),
        (("caps", "per_order"), -1),
        (("max_writes_per_min",), 30),
        (("order_expiry_seconds",), 0),
        (("no_price_convention",), "maybe"),
        (("mode",), "yolo"),
        (("tournament_id",), "00000000-0000-0000-0000-000000000000"),
        (("extra_key",), 1),
    ],
)
def test_out_of_range_value_refuses_to_start(tmp_path, path, value):
    raw = _repo_raw()
    target = raw
    for part in path[:-1]:
        target = target[part]
    target[path[-1]] = value
    with pytest.raises(LimitsError):
        load_limits(_write(tmp_path, raw))


def test_non_finite_number_refuses_to_start(tmp_path):
    text = DEFAULT_PATH.read_text(encoding="utf-8").replace("scale: 1.0 ", "scale: .nan ")
    path = tmp_path / "risk.yaml"
    path.write_text(text, encoding="utf-8")
    with pytest.raises(LimitsError):
        load_limits(path)


def test_missing_file_refuses_to_start(tmp_path):
    with pytest.raises(LimitsError):
        load_limits(tmp_path / "absent.yaml")


def test_limits_are_frozen():
    limits = make_limits()
    with pytest.raises(ValueError):
        limits.scale = Decimal(1)
