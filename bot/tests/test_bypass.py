"""Hard rule 3: no code path reaches POST /orders without risk.check."""

import ast
import asyncio
from dataclasses import replace
from decimal import Decimal
from pathlib import Path

import pytest

from bot import risk
from bot.risk import HeldPosition
from bot.tests.kit import make_intent, make_limits, make_rig
from bot.tests.test_exits import (
    HELD,
    _exit,
    _row,
    exit_intent,
    exit_rig,
    place_exit,
    run,
    settings,
)

D = Decimal

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


# --- the ladder: only the active step's Limits ever reach the order endpoint -----------


def _assignments(tree: ast.AST, attr: str):
    """(enclosing function name, node) for every `<x>.<attr> = ...` in the tree."""
    for func in ast.walk(tree):
        if not isinstance(func, ast.FunctionDef | ast.AsyncFunctionDef):
            continue
        for node in ast.walk(func):
            targets = []
            if isinstance(node, ast.Assign):
                targets = node.targets
            elif isinstance(node, ast.AnnAssign | ast.AugAssign):
                targets = [node.target]
            for target in targets:
                if isinstance(target, ast.Attribute) and target.attr == attr:
                    yield func.name, node


def test_limits_change_only_through_apply_limits_and_set_limits():
    places = set()
    for path in _bot_sources():
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for attr in ("_limits", "limits"):
            for func, _ in _assignments(tree, attr):
                places.add((path.name, attr, func))
        if path.name not in ("main.py", "orders.py") and list(_references(tree, "set_limits")):
            places.add((path.name, "set_limits", "?"))
    assert places == {
        ("orders.py", "_limits", "__init__"),
        ("orders.py", "_limits", "set_limits"),
        ("main.py", "limits", "__init__"),
        ("main.py", "limits", "apply_limits"),
        ("ladder.py", "limits", "__init__"),
        ("ladder.py", "limits", "_set"),
    }


def test_every_order_is_checked_against_the_active_step(tmp_path, monkeypatch):
    from bot import risk
    from bot.tests.test_ladder import EDGE, ladder_loop, loops, run

    rig = ladder_loop(tmp_path)
    checked = []
    real_check = risk.check

    def spy(intent, state, **kwargs):
        bot = rig.bot
        checked.append((state.limits, bot.limits, bot.path.limits, bot.ladder.limits))
        return real_check(intent, state, **kwargs)

    monkeypatch.setattr(risk, "check", spy)
    posted_steps = []
    real_post = rig.bot.writer.post_order

    async def post(body):
        assert checked and checked[-1][0] is rig.bot.ladder.limits
        posted_steps.append(rig.bot.ladder.step)
        return await real_post(body)

    rig.bot.writer.post_order = post

    async def go():
        await rig.bot.startup()
        ids = list(rig.bot.contracts)
        rig.sig.quotes[ids[0]] = EDGE
        await loops(rig, 6)  # L0, orders under L0
        rig.sig.quotes.pop(ids[0])
        rig.sig.quotes[ids[2]] = EDGE
        await loops(rig, 3)  # L1, L2, L3

    run(go())
    assert checked and all(s is b is p is a for s, b, p, a in checked)
    assert posted_steps[0] == "L0" and "L0" != posted_steps[-1]


# --- exits (BOT-EW-5): no exit reaches POST /orders without risk.check_exit -----------


@pytest.mark.parametrize(
    "setup",
    [
        "stop",
        "paused",
        "ladder_hold",
        "unknown",
        "snapshot",
        "gone",
        "flipped",
        "too_many",
        "live_exit",
        "disabled",
        "side_off",
        "sell",
        "not_exit",
        "probe_two",
    ],
)
def test_refused_exit_makes_zero_requests(tmp_path, setup):
    rig = exit_rig(tmp_path, [])
    intent, held, s, probe = exit_intent(), HELD, settings(), False
    if setup in ("stop", "paused"):
        (rig.state_dir / setup.upper()).write_text("x")
    elif setup == "ladder_hold":
        (rig.state_dir / "LADDER_HOLD").write_text("x")
    elif setup == "unknown":
        _row(rig.conn, "u", status="unknown", exchange="99")
    elif setup == "snapshot":
        held = replace(HELD, fresh=False)
    elif setup == "gone":
        held = None
    elif setup == "flipped":
        held = HeldPosition("36", "no", D(10), fresh=True)
    elif setup == "too_many":
        intent = exit_intent(quantity=11)
    elif setup == "live_exit":
        _exit(rig.conn, "live", price="0.48", status="resting", traded="0", quantity=4)
    elif setup == "disabled":
        s = settings(enabled="false")
    elif setup == "side_off":
        s = settings(sides="no")
    elif setup == "sell":
        intent = exit_intent(action="sell")
    elif setup == "not_exit":
        intent = exit_intent(strategy="reference")
    elif setup == "probe_two":
        intent, probe = exit_intent(strategy="exit_probe", quantity=2), True
    before = len(rig.rows())
    record = place_exit(rig, intent, held=held, s=s, probe=probe)
    assert record.status == "blocked"
    assert rig.sig.seen == []
    assert len(rig.rows()) == before
    verdicts = [r["verdict"] for r in rig.conn.execute("SELECT verdict FROM decisions")]
    assert verdicts == ["blocked"]


def test_exit_strategy_through_place_is_refused(tmp_path):
    rig = exit_rig(tmp_path, [])
    record = run(rig.path.place(exit_intent()))
    assert record.status == "blocked" and rig.sig.seen == []


def _calls(func: ast.AST) -> list[str]:
    out = []
    for node in ast.walk(func):
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute):
            target = node.func.value
            prefix = target.id + "." if isinstance(target, ast.Name) else ""
            out.append(prefix + node.func.attr)
    return out


def test_every_send_is_behind_check_or_check_exit():
    tree = ast.parse((Path(risk.__file__).parent / "orders.py").read_text(encoding="utf-8"))
    funcs = {
        n.name: n for n in ast.walk(tree) if isinstance(n, ast.FunctionDef | ast.AsyncFunctionDef)
    }
    callers = {name: {c.split(".")[-1] for c in _calls(f)} for name, f in funcs.items()}
    assert {n for n, c in callers.items() if "_send" in c} == {"_journal_and_send"}
    assert {n for n, c in callers.items() if "post_order" in c} == {"_send"}
    assert {n for n, c in callers.items() if "_journal_and_send" in c} == {
        "_place",
        "_place_exit",
    }
    assert "risk.check" in _calls(funcs["_place"])
    assert "risk.check_exit" in _calls(funcs["_place_exit"])
    assert "risk.check" not in _calls(funcs["_place_exit"])
