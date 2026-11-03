"""BOT-EW-5 reduce-only exits: config, strategy, risk, accounting, order path, loop.

Fakes only: no network.
"""

import asyncio
import re
import sqlite3
from dataclasses import replace
from datetime import timedelta
from decimal import Decimal
from pathlib import Path

import pytest

from bot import db, risk, strategy
from bot.exit_probe import Facts, judge
from bot.exits import ExitsConfig, ExitsError, load_exits, parse_exits
from bot.orders import OrderRecord
from bot.refs import make_quote
from bot.risk import HeldPosition, RiskState
from bot.strategy import Leg, SigQuote
from bot.tests.kit import FakeReader, make_intent, make_limits, make_rig, ok_body, reply
from bot.tests.loopkit import contracts, make_loop
from bot.tests.test_strategy import BEFORE, NIGHT, kalshi

D = Decimal
DC, RC = contracts(1)
NO_ON = {"no_orders_enabled": True, "no_price_convention": "no_price"}
ZERO = D(0)
KEY = re.compile(rb'"idempotencyKey":"[^"]+"')  # a fresh uuid per run
START = D(100000)
REPO_EXITS = Path(__file__).resolve().parents[2] / "config" / "exits.yaml"

EXITS_YAML = """\
enabled: {enabled}
mechanism: auto
exit_edge: 0.03
exit_edge_election_night: 0.04
max_exit_quantity_fraction: {fraction}
reentry_block_minutes: 10
sides: [{sides}]
"""


def exits_text(enabled="true", fraction="1.0", sides="yes, no"):
    return EXITS_YAML.format(enabled=enabled, fraction=fraction, sides=sides)


def settings(**kw):
    return parse_exits(exits_text(**kw).encode())


def run(coro):
    return asyncio.run(coro)


# --- config/exits.yaml ------------------------------------------------------------------


def test_repository_file_is_valid_and_off():
    loaded = load_exits(REPO_EXITS)
    assert loaded is not None and loaded.enabled is False
    assert loaded.exit_edge == D("0.03") and loaded.exit_edge_election_night == D("0.04")
    assert loaded.sides == ("yes", "no")  # unquoted yes/no stay words, not booleans
    assert loaded.resolved_mechanism == "complement_buy"


def test_absent_file_means_no_exits(tmp_path):
    assert load_exits(tmp_path / "missing.yaml") is None


@pytest.mark.parametrize(
    ("old", "new", "why"),
    [
        ("mechanism: auto", "mechanism: sell", "never sells"),
        ("exit_edge: 0.03", "exit_edge: 0.2", "exit_edge"),
        ("exit_edge: 0.03", "exit_edge: 0", "exit_edge"),
        ("exit_edge: 0.03", 'exit_edge: "0.03"', "must be a number"),
        ("exit_edge_election_night: 0.04", "exit_edge_election_night: 0.25", "election"),
        ("enabled: true", "enabled: yes", "enabled"),
        ("max_exit_quantity_fraction: 1.0", "max_exit_quantity_fraction: 1.5", "fraction"),
        ("max_exit_quantity_fraction: 1.0", "max_exit_quantity_fraction: 0", "fraction"),
        ("sides: [yes, no]", "sides: [yes, yes]", "repeat"),
        ("sides: [yes, no]", "sides: [maybe]", "sides"),
        ("sides: [yes, no]", "sides: []", "sides"),
        ("reentry_block_minutes: 10", "reentry_block_minutes: 1.5", "reentry"),
    ],
)
def test_invalid_values_are_refused(old, new, why):
    text = exits_text().replace(old, new)
    with pytest.raises(ExitsError, match=why):
        parse_exits(text.encode())


def test_extra_keys_are_refused():
    with pytest.raises(ExitsError, match="extra"):
        parse_exits((exits_text() + "max_loss: 5\n").encode())


def test_values_are_exact_decimals_and_integers_allowed():
    s = parse_exits(exits_text(fraction="1").encode())
    assert s.max_exit_quantity_fraction == D(1)
    assert isinstance(s.exit_edge, Decimal) and s.exit_edge == D("0.03")
    assert parse_exits(exits_text(sides="yes").encode()).sides == ("yes",)


def test_reread_turns_exits_off_once_on_an_invalid_file(tmp_path):
    path = tmp_path / "exits.yaml"
    config = ExitsConfig(path)
    errors = []
    assert config.tick(errors.append) is None and errors == []  # absent: silent
    path.write_text(exits_text())
    assert config.tick(errors.append).enabled
    path.write_text(exits_text().replace("exit_edge: 0.03", "exit_edge: 0.5"))
    assert config.tick(errors.append) is None
    assert config.tick(errors.append) is None
    assert len(errors) == 1 and errors[0].startswith("exits off:")
    assert config.reentry_minutes() == 10  # the last valid file's block still holds
    path.write_text(exits_text(enabled="false"))
    assert config.tick(errors.append).enabled is False
    path.write_text("enabled: [")
    assert config.tick(errors.append) is None and len(errors) == 2


# --- strategy ---------------------------------------------------------------------------


def ex(
    sig, held_side="yes", qty="10", *, now=BEFORE, k=None, poly=None, s="default", limits=None, **kw
):
    return strategy.evaluate_exit(
        DC,
        sig,
        k or kalshi(when=now),
        poly,
        held_side,
        D(qty),
        settings() if s == "default" else s,
        limits or make_limits(**NO_ON),
        now,
        **kw,
    )


@pytest.mark.parametrize(("bid", "exits"), [("0.505", False), ("0.510", True), ("0.515", True)])
def test_yes_trigger_at_fair_plus_edge(bid, exits):
    ev = ex(SigQuote(D(bid), D("0.60")))  # fair 0.48 + 0.03 = 0.51
    assert ev.is_skip is not exits
    if exits:
        assert ev.legs == (Leg(DC, "no", strategy.buy_price(1 - D(bid))),)
        assert ev.exit_of_side == "yes" and ev.strategy == "exit"
        assert ev.reason.startswith(f"exit YES: SIG bid {D(bid):.3f} >= fair 0.480 + 0.030")
        assert "(Kalshi 0.47/0.49, Poly -)" in ev.reason
    else:
        assert "hold" in ev.reason


@pytest.mark.parametrize(("ask", "exits"), [("0.455", False), ("0.450", True), ("0.445", True)])
def test_no_trigger_at_no_fair_plus_edge(ask, exits):
    # NO fair 1 - 0.48 = 0.52; NO bid = 1 - ask; threshold 0.55 -> ask <= 0.45.
    ev = ex(SigQuote(D("0.30"), D(ask)), "no")
    assert ev.is_skip is not exits
    if exits:
        assert ev.legs == (Leg(DC, "yes", strategy.buy_price(D(ask))),)
        assert ev.exit_of_side == "no"
        assert "NO fair 0.520" in ev.reason


def test_complement_price_is_floored_so_the_sale_is_never_below_the_threshold():
    ev = ex(SigQuote(D("0.5125"), D("0.60")))  # an off-grid bid
    (leg,) = ev.legs
    assert leg.price_side == D("0.485")  # floor(0.4875)
    assert 1 - leg.price_side >= D("0.51")


def test_edge_switches_to_election_night():
    sig = SigQuote(D("0.51"), D("0.60"))
    assert not ex(sig, now=BEFORE).is_skip  # 0.51 >= 0.48 + 0.03
    night = ex(sig, now=NIGHT)
    assert night.is_skip and "+ 0.040" in night.reason  # 0.51 < 0.48 + 0.04
    assert not ex(SigQuote(D("0.52"), D("0.60")), now=NIGHT).is_skip


@pytest.mark.parametrize(
    "k",
    [
        kalshi(when=BEFORE - timedelta(seconds=60)),  # stale
        kalshi("0.40", "0.50"),  # wide
        make_quote("kalshi", DC.kalshi_ticker, None, D("0.49"), BEFORE),  # one-sided
    ],
)
def test_stale_wide_or_one_sided_reference_means_no_exit(k):
    ev = ex(SigQuote(D("0.70"), D("0.80")), k=k)
    assert ev.is_skip and "no fair value" in ev.reason


def test_disagreeing_venues_mean_no_exit():
    poly = make_quote("polymarket", "tok", D("0.59"), D("0.61"), BEFORE)
    ev = ex(SigQuote(D("0.70"), D("0.80")), poly=poly)
    assert ev.is_skip and "venues disagree" in ev.reason


def test_disabled_side_busy_cooldown_and_no_orders_are_skips():
    sig = SigQuote(D("0.70"), D("0.80"))
    assert "disabled" in ex(sig, s=settings(enabled="false")).reason
    assert "sides" in ex(sig, s=settings(sides="no")).reason
    assert "already has a live order" in ex(sig, busy={DC.sig_exchange_id}).reason
    cooling = ex(sig, last_order={DC.sig_exchange_id: BEFORE - timedelta(seconds=5)})
    assert "cooling down" in cooling.reason
    assert "NO orders are disabled" in ex(sig, limits=make_limits()).reason
    assert ex(sig, qty="0").reason == "exit: nothing held"
    assert ex(sig, s=None).reason == "exit: exits disabled"


def test_winners_near_one_are_not_sold_without_edge():
    # SIG bid 0.985 on a race whose fair is 0.99: holding pays more.
    k = kalshi("0.985", "0.995")
    assert ex(SigQuote(D("0.985"), D("0.99")), k=k).is_skip


def test_exit_quantity():
    assert strategy.exit_quantity(D(10), D(1), D(1000)) == 10
    assert strategy.exit_quantity(D(10), D(1), D("6.5")) == 6
    assert strategy.exit_quantity(D(7), D("0.5"), D(100)) == 3
    assert strategy.exit_quantity(D(10), D(1), D(0)) == 0
    assert strategy.exit_quantity(D("0.5"), D(1), D(100)) == 0


# --- risk.check_exit --------------------------------------------------------------------


def rstate(tmp_path, **overrides) -> RiskState:
    values = {
        "limits": make_limits(**NO_ON),
        "balance": None,
        "total_committed": D(0),
        "race_committed": D(0),
        "writes_last_min": 0,
        "unknown_orders": 0,
        "journal_committed": D(0),
        "state_dir": tmp_path,
    }
    values.update(overrides)
    return RiskState(**values)


def exit_intent(**overrides):
    values = {
        "side": "no",
        "price_side": D("0.480"),
        "quantity": 10,
        "strategy": "exit",
        "exit_of_side": "yes",
    }
    values.update(overrides)
    return make_intent(**values)


HELD = HeldPosition("36", "yes", D(10), fresh=True)


def check(
    tmp_path,
    intent=None,
    *,
    held=HELD,
    s="default",
    live_exit=ZERO,
    live_orders=0,
    fresh=True,
    probe=False,
    **state,
):
    return risk.check_exit(
        intent or exit_intent(),
        rstate(tmp_path, **state),
        held=held,
        settings=settings() if s == "default" else s,
        live_exit_quantity=live_exit,
        live_orders_on_exchange=live_orders,
        require_fresh=fresh,
        probe=probe,
    )


def test_exit_allowed_whatever_the_caps(tmp_path):
    # Balance unknown and every cap exhausted: an exit still passes.
    result = check(tmp_path, total_committed=D(10**9), race_committed=D(10**9))
    assert result.allowed and "closes 10 of 10 YES held" in result.reason


@pytest.mark.parametrize("name", ["STOP", "PAUSED", "LADDER_HOLD"])
def test_exit_refused_by_state_files(tmp_path, name):
    (tmp_path / name).write_text("x")
    result = check(tmp_path)
    assert not result.allowed and name in result.reason


def test_exit_refused_with_an_unknown_order(tmp_path):
    assert "unknown outcome" in check(tmp_path, unknown_orders=1).reason


def test_exit_quantity_never_exceeds_fresh_held_minus_live_exits(tmp_path):
    assert check(tmp_path, exit_intent(quantity=6), live_exit=D(4)).allowed
    result = check(tmp_path, exit_intent(quantity=7), live_exit=D(4))
    assert not result.allowed and "exceeds held 10 minus live exits 4" in result.reason
    assert not check(tmp_path, exit_intent(quantity=11)).allowed


def test_exit_refused_on_a_snapshot_holding_when_live(tmp_path):
    snapshot = replace(HELD, fresh=False)
    assert "fresh positions read" in check(tmp_path, held=snapshot).reason
    assert check(tmp_path, held=snapshot, fresh=False).allowed  # dry run: snapshot is fine


def test_exit_refused_when_the_holding_is_gone_or_on_the_other_side(tmp_path):
    assert "nothing to exit" in check(tmp_path, held=None).reason
    flipped = HeldPosition("36", "no", D(10), fresh=True)
    assert "not the YES being exited" in check(tmp_path, held=flipped).reason
    other = HeldPosition("99", "yes", D(10), fresh=True)
    assert "another exchange" in check(tmp_path, held=other).reason


def test_exit_refused_with_a_live_order_on_the_exchange(tmp_path):
    assert "could change the holding" in check(tmp_path, live_orders=1).reason


def test_exit_refused_when_disabled_or_side_not_listed(tmp_path):
    assert "enabled: false" in check(tmp_path, s=settings(enabled="false")).reason
    assert "sides" in check(tmp_path, s=settings(sides="no")).reason
    assert "exits are off" in check(tmp_path, s=None).reason


@pytest.mark.parametrize(
    ("overrides", "why"),
    [
        ({"action": "sell"}, "never sells"),
        ({"strategy": "reference"}, "not an exit"),
        ({"side": "yes"}, "must buy the other side"),
        ({"exit_of_side": None}, "must name the held side"),
        ({"price_side": D("0.483")}, "grid"),
        ({"price_side": 0.48}, "Decimal"),
        ({"quantity": 0}, "whole number"),
    ],
)
def test_exit_refused_for_malformed_intents(tmp_path, overrides, why):
    result = check(tmp_path, exit_intent(**overrides))
    assert not result.allowed and why in result.reason


def test_exit_of_yes_needs_no_orders_enabled(tmp_path):
    result = check(tmp_path, limits=make_limits())
    assert not result.allowed and "NO orders" in result.reason


def test_exit_refused_on_fractional_held_quantity(tmp_path):
    held = HeldPosition("36", "yes", D("10.5"), fresh=True)
    assert "whole number of shares" in check(tmp_path, held=held).reason


def test_exit_probe_flag(tmp_path):
    probe = exit_intent(strategy="exit_probe", quantity=1)
    assert check(tmp_path, probe, probe=True, s=settings(enabled="false")).allowed
    assert not check(tmp_path, exit_intent(strategy="exit_probe", quantity=2), probe=True).allowed
    assert not check(tmp_path, probe).allowed  # strategy without the flag
    assert not check(tmp_path, exit_intent(quantity=1), probe=True).allowed


def test_buy_check_refuses_exit_strategies(tmp_path):
    result = risk.check(exit_intent(), rstate(tmp_path, balance=D(100000)))
    assert not result.allowed and "unknown strategy" in result.reason


# --- committed accounting ---------------------------------------------------------------


def _row(
    conn,
    key,
    *,
    status="filled",
    side="yes",
    price="0.40",
    quantity=10,
    traded=None,
    cost=None,
    strategy_name="reference",
    exit_of=None,
    created="2026-11-03T00:00:00Z",
    exchange="36",
    race="senate:PA",
):
    traded = str(quantity) if traded is None and status == "filled" else traded
    db.insert_order(
        conn,
        {
            "key": key,
            "created_utc": created,
            "updated_utc": created,
            "race_key": race,
            "sig_market_id": "10",
            "sig_exchange_id": exchange,
            "side": side,
            "action": "buy",
            "price_side": price,
            "wire_price": price,
            "quantity": quantity,
            "expires_utc": "2026-11-03T00:02:00Z",
            "strategy": strategy_name,
            "status": status,
            "quantity_traded": traded,
            "total_cost": cost,
            "exit_of_side": exit_of,
        },
    )


def _exit(
    conn,
    key,
    *,
    price,
    quantity=10,
    status="filled",
    traded=None,
    created="2026-11-03T01:00:00Z",
    exit_of="yes",
    side="no",
    exchange="36",
):
    _row(
        conn,
        key,
        status=status,
        side=side,
        price=price,
        quantity=quantity,
        traded=traded,
        strategy_name="exit",
        exit_of=exit_of,
        created=created,
        exchange=exchange,
    )


def rows(conn):
    return conn.execute("SELECT * FROM orders").fetchall()


def test_exit_releases_average_cost_not_proceeds(tmp_path):
    conn = db.connect(tmp_path / "b.db")
    _row(conn, "b1", price="0.40")  # 10 YES at 0.40: 4.0
    _row(conn, "b2", price="0.50", created="2026-11-03T00:30:00Z")  # 10 more at 0.50: 5.0
    _exit(conn, "x1", price="0.30", quantity=5)  # closes 5 YES at 0.70; avg cost 0.45
    out = risk.exit_commitments(rows(conn))
    assert out == {"x1": D("-2.25")}
    total, by_race = risk.committed(rows(conn), make_limits())
    assert total == by_race["senate:PA"] == D("6.75")  # 9.0 - 0.45 x 5


def test_exits_beyond_the_journal_basis_release_nothing_more(tmp_path):
    conn = db.connect(tmp_path / "b.db")
    _row(conn, "b1", price="0.40")  # 10 YES held by the journal
    _exit(conn, "x1", price="0.30", quantity=8)
    _exit(conn, "x2", price="0.30", quantity=7, created="2026-11-03T02:00:00Z")  # 5 manual
    out = risk.exit_commitments(rows(conn))
    assert out["x1"] == D("-3.2") and out["x2"] == D("-0.8")
    total, _ = risk.committed(rows(conn), make_limits())
    assert total == 0  # never below what the journal paid


def test_live_unknown_and_rejected_exits(tmp_path):
    conn = db.connect(tmp_path / "b.db")
    _row(conn, "b1", price="0.40")
    _exit(conn, "live", price="0.30", status="resting", traded="2")  # 0.3 x 8 - 0.4 x 2
    _exit(conn, "unk", price="0.30", status="unknown", created="2026-11-03T02:00:00Z")
    _exit(conn, "rej", price="0.30", status="rejected", created="2026-11-03T03:00:00Z")
    out = risk.exit_commitments(rows(conn))
    assert out == {"live": D("1.6"), "unk": D("3.0"), "rej": D(0)}


def test_exit_of_no_uses_the_no_basis(tmp_path):
    conn = db.connect(tmp_path / "b.db")
    _row(conn, "n1", side="no", price="0.30")  # 10 NO at 0.30
    _row(conn, "y1", side="yes", price="0.60")  # a YES basis that must not be used
    _exit(conn, "x1", price="0.50", side="yes", exit_of="no", quantity=10)
    assert risk.exit_commitments(rows(conn)) == {"x1": D("-3.0")}


def _proxy(conn, balance, start=START):
    s = risk.build_state(
        conn,
        make_limits(start_balance=int(start)),
        race_key="senate:PA",
        balance=balance,
        writes_last_min=0,
    )
    return s.balance + s.journal_committed, s


@pytest.mark.parametrize(
    ("exit_price", "pnl"),
    [("0.30", D("3.0")), ("0.70", D("-1.0"))],  # sale at 0.70 (profit), at 0.30 (loss)
)
def test_loss_stop_proxy_moves_by_realised_pnl(tmp_path, exit_price, pnl):
    conn = db.connect(tmp_path / "b.db")
    start = D(100000)
    _row(conn, "b1", price="0.40")  # cash 100000 -> 99996
    before, _ = _proxy(conn, start - 4)
    assert before == start
    _exit(conn, "x1", price=exit_price)  # netting returns 1 - price per share
    after, _ = _proxy(conn, start - 4 + 10 * (1 - D(exit_price)))
    assert after - before == pnl


def test_loss_stop_trips_after_a_large_loss_exit(tmp_path):
    conn = db.connect(tmp_path / "b.db")
    _row(conn, "b1", price="0.40", quantity=1000)  # 400 paid
    _exit(conn, "x1", price="0.90", quantity=1000)  # sold at 0.10: a 300 loss
    balance = D(100000) - 400 + 100
    _, s = _proxy(conn, balance)
    result = risk.check(make_intent(quantity=1), s)  # loss_stop 2000 x 0.10 = 200
    assert not result.allowed and "loss stop" in result.reason


# --- journal schema ---------------------------------------------------------------------

EW4_ORDERS = """
CREATE TABLE orders (
  key TEXT PRIMARY KEY, created_utc TEXT NOT NULL, updated_utc TEXT NOT NULL,
  race_key TEXT, sig_market_id TEXT, sig_exchange_id TEXT NOT NULL,
  side TEXT NOT NULL CHECK (side IN ('yes','no')),
  action TEXT NOT NULL CHECK (action = 'buy'),
  price_side TEXT NOT NULL, wire_price TEXT NOT NULL, quantity INTEGER NOT NULL,
  expires_utc TEXT NOT NULL,
  strategy TEXT NOT NULL CHECK (strategy IN ('complement','reference','probe')),
  reason TEXT,
  status TEXT NOT NULL CHECK (status IN ('journaled','sent','resting','filled','partial',
    'cancelled','expired','rejected','unknown')),
  sig_order_id TEXT, quantity_traded TEXT, total_cost TEXT, http_status INTEGER,
  error TEXT, response_json TEXT
);
INSERT INTO orders VALUES ('k1','2026-11-03T00:00:00Z','2026-11-03T00:00:00Z','senate:PA',
  '10','36','yes','buy','0.4','0.4',10,'2026-11-03T00:02:00Z','reference','r','filled',
  '77','10','4','200',NULL,'{}');
"""


def test_an_ew4_journal_is_migrated_in_place(tmp_path):
    path = tmp_path / "old.db"
    old = sqlite3.connect(path)
    old.executescript(EW4_ORDERS)
    old.commit()
    old.close()
    conn = db.connect(path)
    (row,) = rows(conn)
    assert dict(row) | {} == dict(row)
    assert row["key"] == "k1" and row["sig_order_id"] == "77" and row["exit_of_side"] is None
    _exit(conn, "x1", price="0.30")
    with pytest.raises(sqlite3.IntegrityError):
        _exit(conn, "x2", price="0.30", exit_of="maybe")
    conn.close()
    conn = db.connect(path)  # a second open changes nothing
    assert [r["key"] for r in rows(conn)] == ["k1", "x1"]


# --- order path -------------------------------------------------------------------------


def exit_rig(tmp_path, steps=None, **kw):
    return make_rig(tmp_path, steps, limits=make_limits(**NO_ON), **kw)


def filled_body(**overrides):
    values = {
        "side": "no",
        "price": D("0.48"),
        "open": False,
        "quantityTraded": 10,
        "totalCost": D("4.8"),
        "remainingQuantity": 0,
    }
    values.update(overrides)
    return ok_body(**values)


def place_exit(rig, intent=None, held=HELD, s="default", probe=False):
    return run(
        rig.path.place_exit(
            intent or exit_intent(),
            held=held,
            settings=settings() if s == "default" else s,
            probe=probe,
        )
    )


def test_exit_is_journaled_sent_as_a_buy_and_confirmed(tmp_path):
    rig = exit_rig(tmp_path, [reply(200, filled_body())])
    record = place_exit(rig)
    assert record.status == "filled"
    (row,) = rig.rows()
    assert (row["strategy"], row["action"], row["side"], row["exit_of_side"]) == (
        "exit",
        "buy",
        "no",
        "yes",
    )
    (post,) = rig.sig.posts()
    assert b'"action":"buy"' in post.content and b'"side":"no"' in post.content
    assert b'"price":0.48' in post.content
    assert not rig.paused
    assert [e["message"] for e in rig.events("reconcile")][-1].startswith("0 open order")


def test_exit_remainder_is_cancelled_at_once(tmp_path):
    reader = FakeReader(fills={1001: (D(3), D("0.48"))})
    rig = exit_rig(
        tmp_path,
        [
            reply(200, filled_body(open=True, quantityTraded=3, remainingQuantity=7)),
            reply(200, {"orderId": 1001, "message": "cancelled"}),
        ],
        reader=reader,
    )
    record = place_exit(rig)
    assert [(s.method, s.path) for s in rig.sig.seen] == [
        ("POST", "/orders"),
        ("DELETE", "/orders/1001"),
    ]
    assert record.status == "partial" and record.quantity_traded == "3"
    assert not rig.paused


def test_exit_remainder_still_open_pauses(tmp_path):
    reader = FakeReader(open_ids=[1001])
    rig = exit_rig(
        tmp_path,
        [
            reply(200, filled_body(open=True, quantityTraded=0, remainingQuantity=10)),
            reply(200, {"orderId": 1001, "message": "cancelled"}),
        ],
        reader=reader,
    )
    place_exit(rig)
    assert rig.paused
    assert any("still open" in e["message"] for e in rig.events("exit"))


@pytest.mark.parametrize(
    "canonical", [{"action": "sell"}, {"side": "yes"}, {"action": "sell", "side": "yes"}]
)
def test_canonicalised_exit_pauses(tmp_path, canonical):
    rig = exit_rig(tmp_path, [reply(200, filled_body(**canonical))])
    place_exit(rig)
    assert rig.paused
    assert "exit canonicalised" in (rig.state_dir / "PAUSED").read_text()
    assert any(
        e["level"] == "error" and "exit canonicalised" in e["message"] for e in rig.events("exit")
    )


def test_dry_run_exit_is_a_would_order(tmp_path):
    rig = make_rig(tmp_path, [], limits=make_limits(mode="dry_run", **NO_ON), live=False)
    record = place_exit(rig, held=replace(HELD, fresh=False))
    assert record.status == "would_order"
    assert rig.sig.seen == [] and rig.rows() == []
    (decision,) = rig.conn.execute("SELECT * FROM decisions").fetchall()
    assert decision["verdict"] == "would_order" and decision["strategy"] == "exit"


def test_exit_probe_sends_one_share_with_exits_disabled(tmp_path):
    body = filled_body(quantityTraded=1, totalCost=D("0.48"), quantity=1)
    rig = exit_rig(tmp_path, [reply(200, body)])
    probe = exit_intent(strategy="exit_probe", quantity=1)
    record = place_exit(rig, probe, s=settings(enabled="false"), probe=True)
    assert record.status == "filled" and rig.rows()[0]["strategy"] == "exit_probe"


# --- the loop ---------------------------------------------------------------------------

EXIT_QUOTE = (D("0.52"), D("0.60"))  # YES held: 0.52 >= fair 0.48 + 0.04 (election night)
EDGE = (D("0.30"), D("0.40"))  # a reference YES buy at 0.40
X = "1000"  # exchange of DC


def exit_loop(tmp_path, *, text=None, live=True, positions=None, write=True):
    tmp_path.mkdir(parents=True, exist_ok=True)
    path = tmp_path / "exits.yaml"
    if write:
        path.write_text(text if text is not None else exits_text())
    limits = make_limits(
        mode="live" if live else "dry_run",
        start_balance=100000 if live else None,
        **NO_ON,
    )
    rig = make_loop(tmp_path, races=1, limits=limits, live=live, exits_path=path)
    rig.sig.positions.update(positions if positions is not None else {X: D(10)})
    rig.sig.order_reply = rig.sig.fill_now
    return rig, path


async def loops(rig, n, seconds=10.0):
    for _ in range(n):
        assert await rig.bot.iteration()
        await rig.time.sleep(seconds)


def test_live_exit_closes_the_holding_and_reconciles(tmp_path):
    rig, _ = exit_loop(tmp_path)
    rig.sig.quotes[X] = EXIT_QUOTE

    async def go():
        await rig.bot.startup()
        await loops(rig, 1)

    run(go())
    (order,) = rig.orders()
    assert (order["strategy"], order["side"], order["exit_of_side"]) == ("exit", "no", "yes")
    assert order["status"] == "filled" and order["quantity"] == 10
    assert order["price_side"] == "0.480"
    assert rig.sig.positions[X] == 0
    (decision,) = rig.decisions("order")
    assert decision["strategy"] == "exit"
    assert decision["reason"].startswith(
        "exit YES: SIG bid 0.520 >= fair 0.480 + 0.040 (Kalshi 0.47/0.49, Poly -)"
    )
    assert "-> buy NO 10 @ 0.480 to close 10 YES" in decision["reason"]
    gets = [s.path for s in rig.sig.reads()]
    assert gets.count("/tournaments/midterm-elections/portfolio/positions") == 2  # start + fresh

    async def later():
        await loops(rig, 7)  # an account cycle sees the shrunk position

    run(later())
    assert not rig.paused
    assert rig.bot.committed()[0] == 0  # the shares were not the journal's: nothing released


def test_unexplained_shrink_on_an_exit_exchange_pauses(tmp_path):
    rig, _ = exit_loop(tmp_path)
    rig.sig.quotes[X] = EXIT_QUOTE
    rig.bot.exits.path.write_text(exits_text(fraction="0.5"))

    async def go():
        await rig.bot.startup()
        await loops(rig, 1)  # exits 5 of 10
        assert rig.sig.positions[X] == 5
        rig.sig.quotes[X] = (D("0.45"), D("0.55"))  # no more exits
        await loops(rig, 7)
        assert not rig.paused  # 5 left, as the journal explains
        rig.sig.positions[X] = D(2)  # 3 shares vanish
        await loops(rig, 7)

    run(go())
    assert rig.paused
    (event,) = [
        e
        for e in rig.events("reconcile")
        if e["level"] == "error" and not e["message"].startswith("PAUSED")
    ]
    assert "position on exchange 1000 is 2" in event["message"]


def test_position_gone_by_send_time_is_refused(tmp_path):
    rig, _ = exit_loop(tmp_path)
    rig.sig.quotes[X] = EXIT_QUOTE

    async def go():
        await rig.bot.startup()  # snapshot: 10 YES
        rig.sig.positions[X] = D(0)  # sold by hand since
        await loops(rig, 1)

    run(go())
    assert [s for s in rig.sig.writes() if s.path == "/orders"] == []
    assert any("fresh read shows nothing held" in r["reason"] for r in rig.decisions("skip"))


def test_flipped_position_by_send_time_is_not_exited(tmp_path):
    rig, _ = exit_loop(tmp_path)
    rig.sig.quotes[X] = EXIT_QUOTE

    async def go():
        await rig.bot.startup()
        rig.sig.positions[X] = D(-5)  # now NO: its exit test fails (NO bid 0.40)
        await loops(rig, 1)

    run(go())
    assert [s for s in rig.sig.writes() if s.path == "/orders"] == []


def test_reentry_block_after_an_exit(tmp_path):
    rig, _ = exit_loop(tmp_path)
    rig.sig.quotes[X] = EXIT_QUOTE

    async def go():
        await rig.bot.startup()
        await loops(rig, 1)
        rig.sig.order_reply = None  # buys rest
        rig.sig.quotes[X] = EDGE
        await loops(rig, 12)  # 2 minutes: no buy on X
        assert [o["strategy"] for o in rig.orders()] == ["exit"]
        await loops(rig, 54)  # past 10 minutes

    run(go())
    assert any("re-entry block" in r["reason"] for r in rig.decisions("skip"))
    assert [o["strategy"] for o in rig.orders()][:2] == ["exit", "reference"]


def test_dry_run_exits_use_the_snapshot_and_send_nothing(tmp_path):
    rig, _ = exit_loop(tmp_path, live=False)
    rig.sig.quotes[X] = EXIT_QUOTE

    async def go():
        await rig.bot.startup()
        await loops(rig, 1)

    run(go())
    assert rig.sig.writes() == []
    (would,) = [r for r in rig.decisions("would_order") if r["strategy"] == "exit"]
    assert "to close 10 YES" in would["reason"] and would["side"] == "no"
    gets = [s.path for s in rig.sig.reads()]
    assert gets.count("/tournaments/midterm-elections/portfolio/positions") == 1  # snapshot


def _trace(rig):
    return (
        [
            (s.method, s.path, sorted(s.query.items()), KEY.sub(b"", s.content))
            for s in rig.sig.seen
        ],
        [
            (r["verdict"], r["strategy"], r["sig_exchange_id"], r["reason"], r["quantity"])
            for r in rig.decisions()
        ],
        [(o["strategy"], o["side"], o["quantity"], o["status"]) for o in rig.orders()],
    )


def _scenario(tmp_path, **kw):
    rig, _ = exit_loop(tmp_path, **kw)
    rig.sig.order_reply = None
    rig.sig.quotes[X] = EXIT_QUOTE  # would exit, if exits were on
    rig.sig.quotes["1001"] = EDGE  # a reference buy on the other contract

    async def go():
        await rig.bot.startup()
        await loops(rig, 8)

    run(go())
    return rig


@pytest.mark.parametrize("variant", ["disabled", "invalid"])
def test_exits_off_is_identical_to_no_file(tmp_path, variant):
    baseline = _scenario(tmp_path / "absent", write=False)
    text = exits_text(enabled="false") if variant == "disabled" else "enabled: true\n"
    other = _scenario(tmp_path / variant, text=text)
    assert _trace(other) == _trace(baseline)
    assert [o["strategy"] for o in baseline.orders()] == ["reference"]
    exit_events = other.events("exit")
    if variant == "invalid":
        (event,) = exit_events
        assert event["level"] == "error" and event["message"].startswith("exits off:")
    else:
        assert exit_events == []
    assert baseline.events("exit") == []


def test_enabling_and_disabling_without_a_restart(tmp_path):
    rig, path = exit_loop(tmp_path, text=exits_text(enabled="false"))
    rig.sig.quotes[X] = EXIT_QUOTE

    async def go():
        await rig.bot.startup()
        await loops(rig, 3)
        assert rig.orders() == []
        path.write_text(exits_text(fraction="0.5"))
        await loops(rig, 1)
        assert [o["quantity"] for o in rig.orders()] == [5]
        path.write_text(exits_text(enabled="false"))
        await loops(rig, 12)

    run(go())
    assert [o["quantity"] for o in rig.orders()] == [5]
    assert rig.sig.positions[X] == 5


def test_exits_run_before_buys_and_share_the_order_limit(tmp_path):
    rig, _ = exit_loop(tmp_path)
    rig.sig.quotes[X] = EXIT_QUOTE
    rig.sig.quotes["1001"] = EDGE

    async def go():
        await rig.bot.startup()
        rig.sig.order_reply = lambda body: (
            rig.sig.fill_now(body) if body["side"] == "no" else rig.sig.resting(body)
        )
        await loops(rig, 1)

    run(go())
    assert [o["strategy"] for o in rig.orders()] == ["exit", "reference"]
    posts = [s for s in rig.sig.writes() if s.path == "/orders"]
    assert b'"exchangeId":"1000"' in posts[0].content


# --- exit probe verdict -----------------------------------------------------------------


def _facts(after, cash_after, traded="1"):
    record = OrderRecord(key="k", status="filled", reason="", quantity_traded=traded)
    return Facts(
        held_before=HeldPosition("36", "yes", D(10), fresh=True),
        signed_after=after,
        cash_before=D(1000),
        cash_after=cash_after,
        complement_price=D("0.48"),
        record=record,
    )


def test_probe_verdict_ok():
    ok, lines = judge(_facts(D(9), D("1000.52")))
    assert ok, lines


@pytest.mark.parametrize(
    ("after", "cash", "traded", "why"),
    [
        (D(8), D("1000.52"), "1", "should be 9"),
        (D(-1), D("1000.52"), "1", "opposite-side"),
        (D(9), D("999.52"), "1", "cash change"),
        (D(10), D(1000), "0", "did not fill"),
        (None, D(1000), "1", "could not be re-read"),
    ],
)
def test_probe_verdict_not_ok(after, cash, traded, why):
    ok, lines = judge(_facts(after, cash, traded))
    assert not ok and any(why in line for line in lines)


def _exited_half(tmp_path):
    rig, _ = exit_loop(tmp_path, text=exits_text(fraction="0.5"))
    rig.sig.quotes[X] = EXIT_QUOTE

    async def go():
        await rig.bot.startup()
        await loops(rig, 1)  # exits 5 of 10
        rig.sig.quotes[X] = (D("0.45"), D("0.55"))  # no more exits

    run(go())
    assert rig.sig.positions[X] == 5
    return rig


def test_a_market_that_settles_between_cycles_does_not_pause(tmp_path):
    rig = _exited_half(tmp_path)
    del rig.sig.positions[X]  # the row is gone ...
    rig.sig.market_status[DC.sig_market_id] = "settled"  # ... because the market settled

    async def go():
        await loops(rig, 7)

    run(go())
    assert not rig.paused
    assert X not in rig.bot.contracts
    assert any("is settled" in e["message"] for e in rig.events("market_closed"))


def test_shrink_check_resumes_after_a_restart(tmp_path):
    rig = _exited_half(tmp_path)
    rig2 = make_loop(
        tmp_path / "again",
        races=1,
        limits=rig.bot.limits,
        live=True,
        exits_path=rig.bot.exits.path,
    )
    rig2.bot.conn = rig.conn  # the same journal after a restart
    rig2.bot.path._conn = rig.conn
    rig2.sig.positions[X] = D(5)

    async def go():
        await rig2.bot.startup()  # quiet first read: fixes the offset
        rig2.sig.positions[X] = D(1)
        await loops(rig2, 7)

    run(go())
    assert rig2.paused


def test_ladder_hold_stops_exits_without_spending_reads(tmp_path):
    rig, _ = exit_loop(tmp_path)
    rig.sig.quotes[X] = EXIT_QUOTE

    async def go():
        await rig.bot.startup()
        (rig.state_dir / "LADDER_HOLD").write_text("x")
        await loops(rig, 1)

    run(go())
    assert rig.orders() == []
    gets = [s.path for s in rig.sig.reads()]
    assert gets.count("/tournaments/midterm-elections/portfolio/positions") == 1
    assert any("not sent: LADDER_HOLD" in r["reason"] for r in rig.decisions("skip"))
