"""Hard rule 3: no code path reaches POST /orders without risk.check."""

import ast
import asyncio
from decimal import Decimal
from pathlib import Path

import pytest

from bot.tests.kit import make_intent, make_limits, make_rig

BOT_DIR = Path(__file__).resolve().parent.parent
ALLOWED_CALLERS = {"orders.py", "probe.py"}


def _bot_sources():
    for path in sorted(BOT_DIR.rglob("*.py")):
        if "tests" not in path.relative_to(BOT_DIR).parts:
            yield path


def _references(tree: ast.AST, name: str):
    for node in ast.walk(tree):
        if isinstance(node, ast.Attribute):
            found = node.attr
        elif isinstance(node, ast.Name):
            found = node.id
        elif isinstance(node, ast.Constant):
            found = node.value  # getattr(writer, "post_order")
        else:
            continue
        if found == name:
            yield node


def test_post_order_is_referenced_only_by_orders_and_probe():
    offenders = []
    for path in _bot_sources():
        tree = ast.parse(path.read_text(encoding="utf-8"))
        if list(_references(tree, "post_order")) and path.name not in ALLOWED_CALLERS:
            offenders.append(path.name)
    assert offenders == []


def test_post_order_is_referenced_by_orders():
    tree = ast.parse((BOT_DIR / "orders.py").read_text(encoding="utf-8"))
    assert list(_references(tree, "post_order"))


def test_only_the_writer_names_the_orders_endpoint():
    # Any other module building its own POST /orders request would bypass the path.
    offenders = []
    for path in _bot_sources():
        if path.name == "sig_writer.py":
            continue
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
            value = node.value if isinstance(node, ast.Constant) else None
            if isinstance(value, str) and value.startswith("/orders"):
                offenders.append(path.name)
    assert offenders == []


@pytest.mark.parametrize(
    "setup",
    ["stop", "paused", "sell", "off_grid", "over_cap", "no_disabled"],
)
def test_denied_intent_makes_zero_requests(tmp_path, setup):
    rig = make_rig(tmp_path, [], limits=make_limits())
    intent = make_intent()
    if setup == "stop":
        (rig.state_dir / "STOP").write_text("x")
    elif setup == "paused":
        (rig.state_dir / "PAUSED").write_text("x")
    elif setup == "sell":
        intent = make_intent(action="sell")
    elif setup == "off_grid":
        intent = make_intent(price_side=Decimal("0.436"))
    elif setup == "over_cap":
        intent = make_intent(quantity=10_000)
    elif setup == "no_disabled":
        intent = make_intent(side="no")
    record = asyncio.run(rig.path.place(intent))
    assert record.status == "blocked"
    assert rig.sig.seen == []
    assert rig.rows() == []
    verdicts = [r["verdict"] for r in rig.conn.execute("SELECT verdict FROM decisions")]
    assert verdicts == ["blocked"]
