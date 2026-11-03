"""BOT-ROTATE: deployment caps, take-profit and edge-gone exits, reinvest, cooldown.

Fakes only: no network. Fair value in the loop is Kalshi 0.47/0.49 -> 0.48 unless a
test moves it; NOW is election night, so the reference buy edge is 0.06.
"""

import asyncio
from datetime import datetime
from decimal import Decimal
from pathlib import Path

import pytest

from bot import risk, strategy
from bot.limits import load_limits
from bot.risk import RiskState
from bot.strategy import Evaluation, SigQuote
from bot.tests.kit import make_intent, make_limits
from bot.tests.loopkit import contracts, make_loop
from bot.tests.test_exits import EXITS_YAML, NO_ON, settings
from bot.tests.test_strategy import NIGHT, kalshi

D = Decimal
DC, RC = contracts(1)
X = "1000"
REPO_RISK = Path(__file__).resolve().parents[2] / "config" / "risk.yaml"
POSITIONS = "/tournaments/midterm-elections/portfolio/positions"

ROTATION = {
    "enabled": True,
    "take_profit_pct": "0.08",
    "edge_gone": True,
    "reinvest_min_free_fraction": "0.02",
    "reentry_cooldown_minutes": 10,
}


def run(coro):
    return asyncio.run(coro)


def rot_limits(**overrides):
    values = {"rotation": ROTATION, "target_deployment": "1.0", "max_race_fraction": "0.15"}
    values.update(NO_ON)
    values.update(overrides)
    return make_limits(**values)


def rotation(**overrides):
    return rot_limits(rotation={**ROTATION, **overrides}).rotation


# --- config -----------------------------------------------------------------------------


def test_repository_risk_yaml_is_valid_with_rotation():
    limits = load_limits(REPO_RISK)
    assert limits.target_deployment == D("1.0")
    assert limits.max_race_fraction == D("0.15")
    assert limits.scale == D("1.0")
    assert limits.rotating and limits.rotation.take_profit_pct == D("0.08")


def test_rotation_keys_are_optional_and_off_by_default():
    limits = make_limits()
    assert limits.target_deployment is None and limits.max_race_fraction is None
    assert limits.rotation is None and not limits.rotating


@pytest.mark.parametrize(
    "overrides",
    [
        {"target_deployment": "1.5"},
        {"target_deployment": "0"},
        {"max_race_fraction": "-0.1"},
        {"rotation": {**ROTATION, "take_profit_pct": "0"}},
        {"rotation": {**ROTATION, "reinvest_min_free_fraction": "1"}},
        {"rotation": {**ROTATION, "extra": 1}},
    ],
)
def test_out_of_range_rotation_values_are_refused(overrides):
    with pytest.raises(ValueError):
        make_limits(**overrides)


# --- risk.check: max_race_fraction and target_deployment ----------------------------------


def state(limits, **overrides) -> RiskState:
    values = {
        "limits": limits,
        "balance": D(100000),
        "total_committed": D(0),
        "race_committed": D(0),
        "writes_last_min": 0,
        "unknown_orders": 0,
        "journal_committed": D(0),
    }
    values.update(overrides)
    return RiskState(**values)


BIG = {
    "scale": 1,
    "per_order": 100000,
    "per_race": 100000,
    "total_committed": 100000,
    "min_cash_reserve": 0,
}


def test_max_race_fraction_caps_one_race(tmp_path):
    limits = rot_limits(**BIG)
    # Equity 100000 (balance 80000 + committed 20000): race room 15000 - 14000 = 1000.
    s = state(
        limits,
        balance=D(80000),
        total_committed=D(20000),
        race_committed=D(14000),
        state_dir=tmp_path,
    )
    ok = risk.check(make_intent(price_side=D("0.50"), quantity=2000), s)
    assert ok.allowed and ok.max_quantity == 2000
    too_much = risk.check(make_intent(price_side=D("0.50"), quantity=2001), s)
    assert not too_much.allowed and "max race fraction 0.15" in too_much.reason


def test_target_deployment_caps_the_total(tmp_path):
    limits = rot_limits(target_deployment="0.5", max_race_fraction=None, **BIG)
    s = state(limits, balance=D(60000), total_committed=D(40000), state_dir=tmp_path)
    # Equity 100000; target 50000; room 10000 -> 20000 shares at 0.50.
    result = risk.check(make_intent(price_side=D("0.50"), quantity=20001), s)
    assert not result.allowed and "target deployment 0.5" in result.reason
    assert result.max_quantity == 20000


def test_existing_guards_still_win_over_full_deployment(tmp_path):
    limits = rot_limits()  # the base caps: reserve 90000, scale 0.10
    s = state(limits, balance=D(100000), state_dir=tmp_path)
    result = risk.check(make_intent(price_side=D("0.50"), quantity=101), s)
    assert not result.allowed and "per-order cap" in result.reason


# --- entry price ------------------------------------------------------------------------


def lot(key, created, side="yes", price="0.40", qty=10, traded=None, status="filled", **kw):
    row = {
        "key": key,
        "created_utc": created,
        "sig_exchange_id": X,
        "race_key": "senate:S0",
        "side": side,
        "price_side": price,
        "quantity": qty,
        "quantity_traded": qty if traded is None else traded,
        "total_cost": None,
        "status": status,
        "strategy": "reference",
        "exit_of_side": None,
    }
    row.update(kw)
    return row


def test_entry_price_uses_the_newest_lots():
    rows = [lot("a", "2026-11-03T23:00:00Z", price="0.30"), lot("b", "2026-11-03T23:10:00Z")]
    assert risk.entry_price(rows, X, "yes", D(10)) == D("0.40")
    assert risk.entry_price(rows, X, "yes", D(20)) == D("0.35")


def test_entry_price_unknown_beyond_the_journal_or_unestablished():
    rows = [lot("a", "2026-11-03T23:00:00Z")]
    assert risk.entry_price(rows, X, "yes", D(11)) is None  # manual shares
    assert risk.entry_price(rows, X, "no", D(1)) is None
    unknown = [lot("a", "2026-11-03T23:00:00Z", traded=None, status="unknown")]
    unknown[0]["quantity_traded"] = None
    assert risk.entry_price(unknown, X, "yes", D(1)) is None


def test_entry_price_ignores_exits_and_rejected():
    rows = [
        lot("a", "2026-11-03T23:00:00Z"),
        lot("b", "2026-11-03T23:05:00Z", side="no", strategy="exit", exit_of_side="yes"),
        lot("c", "2026-11-03T23:06:00Z", price="0.10", status="rejected", traded=0),
    ]
    assert risk.entry_price(rows, X, "yes", D(10)) == D("0.40")


# --- strategy: the exit triggers --------------------------------------------------------


ENTRY = D("0.40")


def evaluate(bid, ask, *, entry=ENTRY, held="yes", fair_quote=("0.47", "0.49"), rot="on"):
    return strategy.evaluate_exit(
        DC,
        SigQuote(D(bid), D(ask)),
        kalshi(*fair_quote, when=NIGHT),
        None,
        held,
        D(10),
        settings(),
        rot_limits(),
        NIGHT,
        rotation=rotation() if rot == "on" else rot,
        avg_entry=entry,
    )


def test_take_profit_trigger():
    ev = evaluate("0.44", "0.46")  # (0.44 - 0.40) / 0.40 = 10%
    assert not ev.is_skip
    (leg,) = ev.legs
    assert (leg.side, leg.price_side) == ("no", D("0.560"))  # closes YES at 0.44
    assert ev.reason.startswith("exit YES: take-profit: SIG bid 0.440 is 10.00% over entry")


def test_take_profit_at_exactly_eight_percent():
    ev = evaluate("0.432", "0.50", fair_quote=("0.55", "0.57"))  # fair 0.56: edge not gone
    assert not ev.is_skip and "take-profit" in ev.reason
    held = evaluate("0.431", "0.50", fair_quote=("0.55", "0.57"))
    assert held.is_skip and held.reason.startswith("exit: hold:")


def test_edge_gone_exit():
    # 0.42 is 5% over entry (no take-profit) but >= fair 0.48 - edge 0.06.
    ev = evaluate("0.42", "0.45")
    assert not ev.is_skip and "edge gone: SIG bid 0.420 >= fair 0.480 - edge 0.060" in ev.reason
    assert ev.legs[0].price_side == D("0.580")
    off = evaluate("0.42", "0.45", rot=rotation(edge_gone=False))
    assert off.is_skip


def test_no_sale_below_entry():
    # Fair fell to 0.40: 0.39 >= 0.40 - 0.06, so the edge is gone, but 0.39 < entry 0.40.
    ev = evaluate("0.39", "0.41", fair_quote=("0.39", "0.41"))
    assert ev.is_skip and "entry 0.4000" in ev.reason
    # Unknown entry: neither rotation trigger fires.
    assert evaluate("0.60", "0.62", entry=None, fair_quote=("0.60", "0.62")).is_skip


def test_no_holding_take_profit_uses_the_no_bid():
    # NO held at entry 0.50; NO bid = 1 - ask 0.45 = 0.55 -> 10% over entry.
    ev = evaluate("0.43", "0.45", held="no", entry=D("0.50"), fair_quote=("0.43", "0.45"))
    assert not ev.is_skip and "take-profit: SIG NO bid 0.550" in ev.reason
    assert (ev.legs[0].side, ev.legs[0].price_side) == ("yes", D("0.450"))


def test_rotation_off_keeps_the_old_exit_rule():
    assert evaluate("0.44", "0.46", rot=None).is_skip
    assert not evaluate("0.52", "0.60", rot=None).is_skip  # fair 0.48 + exit edge 0.04


# --- reinvest ranking -------------------------------------------------------------------


def _ev(edge, legs=1):
    leg = strategy.Leg(DC, "yes", D("0.40"))
    return Evaluation("reference", "r", str(edge), (leg,) * legs, "x", edge=edge)


def test_rank_buys_highest_edge_first_skips_last():
    skip = Evaluation("reference", "r", "s", (), "skip")
    ranked = strategy.rank_buys([_ev(D("0.07")), skip, _ev(D("0.12")), _ev(D("0.09"))])
    assert [e.edge for e in ranked] == [D("0.12"), D("0.09"), D("0.07"), None]


# --- the loop ---------------------------------------------------------------------------

EXITS = EXITS_YAML.format(enabled="true", fraction="1.0", sides="yes, no").replace(
    "reentry_block_minutes: 10", "reentry_block_minutes: 0"
)
BUY = (D("0.38"), D("0.40"))  # 0.40 <= fair 0.48 - 0.06: buy YES at 0.40
TAKE = (D("0.44"), D("0.46"))  # 10% over the 0.40 entry
HOLD = (D("0.39"), D("0.45"))  # below entry, no buy


def rot_loop(tmp_path, *, live=True, races=1, **limit_overrides):
    tmp_path.mkdir(parents=True, exist_ok=True)
    path = tmp_path / "exits.yaml"
    path.write_text(EXITS)
    limits = rot_limits(
        mode="live" if live else "dry_run",
        start_balance=100000 if live else None,
        **limit_overrides,
    )
    rig = make_loop(tmp_path, races=races, limits=limits, live=live, exits_path=path)
    rig.sig.order_reply = rig.sig.fill_now
    return rig


async def loops(rig, n, seconds=10.0):
    for _ in range(n):
        assert await rig.bot.iteration()
        await rig.time.sleep(seconds)


def test_loop_take_profit_exit_then_ten_minute_cooldown(tmp_path):
    rig = rot_loop(tmp_path)
    rig.sig.quotes[X] = BUY

    async def go():
        await rig.bot.startup()
        await loops(rig, 1)  # buys YES at 0.40
        rig.sig.quotes[X] = HOLD
        await loops(rig, 7)  # an account cycle sees the holding
        rig.sig.quotes[X] = TAKE
        await loops(rig, 1)  # take-profit
        rig.sig.quotes[X] = BUY  # the buy signal is back at once
        await loops(rig, 55)  # 9 min 10 s after the exit: still blocked
        assert [o["strategy"] for o in rig.orders()] == ["reference", "exit"]
        await loops(rig, 6)

    run(go())
    orders = rig.orders()
    assert [o["strategy"] for o in orders][:3] == ["reference", "exit", "reference"]
    buy, sell, rebuy = orders[:3]
    assert (sell["side"], sell["exit_of_side"], sell["price_side"]) == ("no", "yes", "0.560")
    assert sell["quantity"] == buy["quantity"] and sell["status"] == "filled"
    assert sell["reason"].startswith("exit YES: take-profit: SIG bid 0.440 is 10.00% over")
    assert any("re-entry block" in r["reason"] for r in rig.decisions("skip"))
    gap = (
        datetime.fromisoformat(rebuy["created_utc"]) - datetime.fromisoformat(sell["created_utc"])
    ).total_seconds()
    assert gap >= 600
    exits = rig.events("exit")
    assert len(exits) == 1 and "filled: exit YES: take-profit" in exits[0]["message"]
    reinvest = rig.events("reinvest")
    assert len(reinvest) == 2 and all("edge 0.080" in e["message"] for e in reinvest)
    # Every write went through the order path: one POST /orders per journal row.
    posts = [s for s in rig.sig.writes() if s.method == "POST" and s.path == "/orders"]
    assert len(posts) == len(orders)
    assert not rig.paused


def test_loop_edge_gone_exit_and_no_sale_below_entry(tmp_path):
    rig = rot_loop(tmp_path)
    rig.sig.quotes[X] = BUY

    async def go():
        await rig.bot.startup()
        await loops(rig, 1)
        rig.sig.quotes[X] = (D("0.39"), D("0.45"))  # below entry 0.40
        rig.kalshi.quotes["K0-D"] = ("0.40", "0.42")  # fair 0.41: edge gone
        await loops(rig, 8)
        assert [o["strategy"] for o in rig.orders()] == ["reference"]
        rig.sig.quotes[X] = (D("0.41"), D("0.45"))  # at/above entry: edge-gone exit
        await loops(rig, 1)

    run(go())
    assert [o["strategy"] for o in rig.orders()] == ["reference", "exit"]
    assert "edge gone" in rig.orders()[1]["reason"]


def test_loop_reinvests_highest_edge_first(tmp_path):
    rig = rot_loop(tmp_path, races=2)
    z = "1002"  # race S1's D, listed after X; asks with R 0.60 sum above 0.97
    rig.sig.quotes[X] = BUY  # edge 0.08
    rig.sig.quotes[z] = (D("0.36"), D("0.38"))  # edge 0.10

    async def go():
        await rig.bot.startup()
        await loops(rig, 1)

    run(go())
    placed = [r["sig_exchange_id"] for r in rig.decisions("order")]
    assert placed == [z, X]
    assert "edge 0.100" in rig.events("reinvest")[0]["message"]


def test_loop_max_race_fraction_caps_the_race(tmp_path):
    caps = {**BIG, "per_order": 1000}
    rig = rot_loop(tmp_path, max_race_fraction="0.03", **caps)
    rig.sig.quotes[X] = BUY

    async def go():
        await rig.bot.startup()
        await loops(rig, 30)

    run(go())
    _, by_race = rig.bot.committed()
    # 3% of equity 100000 = 3000 of cost in the race, never more.
    assert D(2900) <= by_race["senate:S0"] <= D(3000)
    assert any("max race fraction" in r["reason"] for r in rig.decisions("skip"))


def test_loop_full_deployment_is_reached(tmp_path):
    caps = {**BIG, "per_order": 5000}
    rig = rot_loop(tmp_path, races=3, max_race_fraction=None, **caps)
    for exchange_id in ("1000", "1002", "1004"):
        rig.sig.quotes[exchange_id] = BUY
    rig.sig.books = {
        e: ([(D("0.38"), D(10**6))], [(D("0.40"), D(10**6))]) for e in ("1000", "1002", "1004")
    }

    async def go():
        await rig.bot.startup()
        await loops(rig, 60)

    run(go())
    total, _ = rig.bot.committed()
    assert rig.sig.balance <= D(2000)  # 98%+ of 100000 deployed
    assert total >= D(98000)
    assert any("fully deployed" in r["reason"] for r in rig.decisions("skip"))
    assert not rig.paused


def test_dry_run_ten_minutes_sends_nothing_and_uses_the_order_path(tmp_path):
    """A 10-minute dry run on fakes: would-be exits and re-buys, no write at all."""
    rig = rot_loop(tmp_path, live=False)
    rig.sig.positions[X] = D(0)
    rig.sig.quotes[X] = BUY

    async def go():
        await rig.bot.startup()
        await loops(rig, 60)

    run(go())
    assert rig.sig.writes() == []
    would = rig.decisions("would_order")
    assert would and all(r["strategy"] == "reference" for r in would)
    assert all(e["message"].startswith("dry run: would_order") for e in rig.events("reinvest"))
    reads = [s.path for s in rig.sig.reads()]
    assert POSITIONS in reads
