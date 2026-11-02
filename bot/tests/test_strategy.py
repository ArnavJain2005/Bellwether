from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest

from bot import strategy
from bot.refs import RefSnapshot, make_quote
from bot.strategy import Leg, SigQuote
from bot.tests.kit import make_limits
from bot.tests.loopkit import contracts

D = Decimal
BEFORE = datetime(2026, 11, 3, 20, 0, tzinfo=UTC)  # edge 0.04
NIGHT = datetime(2026, 11, 3, 23, 30, tzinfo=UTC)  # edge_election_night 0.06
DC, RC = contracts(1)
NO_ON = {"no_orders_enabled": True, "no_price_convention": "no_price"}


def kalshi(bid="0.47", ask="0.49", when=BEFORE):
    return make_quote("kalshi", DC.kalshi_ticker, D(bid), D(ask), when)


def ref(sig, *, now=BEFORE, limits=None, k=None, **kw):
    return strategy.evaluate_reference(
        DC, sig, k or kalshi(when=now), None, limits or make_limits(), now, **kw
    )


# --- reference --------------------------------------------------------------------------


def test_reference_buys_yes_at_the_ask_with_a_readable_reason():
    ev = ref(SigQuote(D("0.40"), D("0.44")))
    assert ev.legs == (Leg(DC, "yes", D("0.44")),)
    assert ev.reason == "SIG ask 0.440 <= fair 0.480 - edge 0.040 (Kalshi 0.47/0.49, Poly -)"
    assert ev.fair == D("0.48") and ev.edge == D("0.04")
    assert strategy.order_text(ev.legs[0], 120) == "buy YES 120 @ 0.440"


def test_reference_no_edge_is_a_skip():
    ev = ref(SigQuote(D("0.45"), D("0.45")))
    assert ev.is_skip and ev.reason.startswith("no edge")


def test_reference_no_buy_needs_no_orders_enabled():
    sig = SigQuote(D("0.52"), D("0.60"))
    assert "NO orders are disabled" in ref(sig).reason
    ev = ref(sig, limits=make_limits(**NO_ON))
    assert ev.legs == (Leg(DC, "no", D("0.48")),)  # NO price 1 - 0.52


def test_reference_no_buy_needs_a_known_convention():
    ev = ref(SigQuote(D("0.52"), D("0.60")), limits=make_limits(no_orders_enabled=True))
    assert ev.is_skip and "disabled" in ev.reason


def test_election_night_switches_the_edge():
    sig = SigQuote(D("0.40"), D("0.43"))  # 0.05 under fair: enough for 0.04, not for 0.06
    assert not ref(sig, now=BEFORE).is_skip
    assert ref(sig, now=NIGHT).is_skip
    limits = make_limits()
    assert strategy.current_edge(limits, BEFORE) == D("0.04")
    assert strategy.current_edge(limits, NIGHT) == D("0.06")
    assert not strategy.is_election_night(limits, BEFORE)
    assert strategy.is_election_night(limits, NIGHT)


def test_no_fair_value_is_a_skip():
    stale = kalshi(when=BEFORE - timedelta(minutes=5))
    ev = ref(SigQuote(D("0.40"), D("0.41")), k=stale)
    assert ev.is_skip and "no fair value" in ev.reason and "stale" in ev.reason


def test_reference_disabled():
    raw = make_limits().model_dump()
    raw["strategies"]["reference"]["enabled"] = False
    limits = make_limits().model_validate(raw)
    assert (
        ref(SigQuote(D("0.40"), D("0.41")), limits=limits).reason == "reference strategy disabled"
    )


# --- grid snapping ----------------------------------------------------------------------


@pytest.mark.parametrize(
    ("raw", "snapped"),
    [
        ("0.4371", "0.435"),
        ("0.435", "0.435"),
        ("0.4399", "0.435"),
        ("0.0049", None),
        ("0.9999", "0.995"),
    ],
)
def test_buy_prices_snap_down(raw, snapped):
    assert strategy.buy_price(D(raw)) == (None if snapped is None else D(snapped))


def test_off_grid_quotes_snap_in_the_less_aggressive_direction():
    ev = ref(SigQuote(D("0.40"), D("0.4371")))
    assert ev.legs[0].price_side == D("0.435")  # a YES buy below the ask, never above
    no = ref(SigQuote(D("0.5629"), D("0.60")), limits=make_limits(**NO_ON))
    assert no.legs[0].price_side == D("0.435")  # NO price 0.4371 snapped down
    assert strategy.ceil_to_grid(D("0.4371")) == D("0.440")


# --- cooldown and one live order per exchange -------------------------------------------


def test_live_order_blocks_the_exchange():
    ev = ref(SigQuote(D("0.40"), D("0.44")), busy={DC.sig_exchange_id})
    assert ev.is_skip and "already has a live order" in ev.reason


def test_cooldown_30s():
    sig = SigQuote(D("0.40"), D("0.44"))
    recent = {DC.sig_exchange_id: BEFORE - timedelta(seconds=29)}
    assert "cooling down" in ref(sig, last_order=recent).reason
    old = {DC.sig_exchange_id: BEFORE - timedelta(seconds=30)}
    assert not ref(sig, last_order=old).is_skip


# --- complement -------------------------------------------------------------------------


def comp(dq, rq, limits=None, **kw):
    quotes = {DC.sig_exchange_id: dq, RC.sig_exchange_id: rq}
    return strategy.evaluate_complement(
        DC.race_key, [DC, RC], quotes, limits or make_limits(), BEFORE, **kw
    )


def test_complement_buys_yes_on_both_when_asks_are_cheap():
    ev = comp(SigQuote(D("0.40"), D("0.45")), SigQuote(D("0.45"), D("0.52")))
    assert ev.legs == (Leg(DC, "yes", D("0.45")), Leg(RC, "yes", D("0.52")))
    assert ev.edge == D("0.03") and "0.970 <= 0.970" in ev.reason


def test_complement_skips_when_asks_are_dear():
    assert comp(SigQuote(D("0.40"), D("0.46")), SigQuote(D("0.45"), D("0.52"))).is_skip


def test_complement_buys_no_on_both_when_bids_are_rich():
    d, r = SigQuote(D("0.55"), D("0.60")), SigQuote(D("0.48"), D("0.50"))  # bids sum 1.03
    assert "NO orders are disabled" in comp(d, r).reason
    ev = comp(d, r, limits=make_limits(**NO_ON))
    assert ev.legs == (Leg(DC, "no", D("0.45")), Leg(RC, "no", D("0.52")))


def test_complement_only_for_exactly_d_and_r():
    assert strategy.evaluate_complement("x", [DC], {}, make_limits(), BEFORE) is None
    independent = DC.__class__(**{**DC.__dict__, "party": "I", "sig_exchange_id": "9"})
    trio = [DC, RC, independent]
    assert strategy.evaluate_complement("x", trio, {}, make_limits(), BEFORE) is None


def test_complement_blocked_if_either_leg_is_busy():
    ev = comp(
        SigQuote(D("0.40"), D("0.45")), SigQuote(D("0.45"), D("0.52")), busy={RC.sig_exchange_id}
    )
    assert ev.is_skip and "live order" in ev.reason


def test_evaluate_all_covers_races_and_contracts():
    quotes = {c.sig_exchange_id: SigQuote(D("0.45"), D("0.55")) for c in (DC, RC)}
    snap = RefSnapshot(kalshi={c.kalshi_ticker: kalshi() for c in (DC, RC)})
    evs = strategy.evaluate_all([DC, RC], quotes, snap, make_limits(), BEFORE)
    assert [e.strategy for e in evs] == ["complement", "reference", "reference"]


# --- sizing -----------------------------------------------------------------------------


def test_depth_at_price():
    bids = [(D("0.52"), D(10)), (D("0.51"), D(5)), (D("0.50"), D(7))]
    asks = [(D("0.44"), D("12.5")), (D("0.45"), D(100))]
    assert strategy.depth_at(bids, asks, Leg(DC, "yes", D("0.44"))) == D("12.5")
    # NO at 0.49 matches YES bids >= 0.51
    assert strategy.depth_at(bids, asks, Leg(DC, "no", D("0.49"))) == D(15)


@pytest.mark.parametrize(
    ("max_q", "depth", "size"),
    [(114, D(1000), 114), (114, D("12.5"), 12), (0, D(100), 0), (5, D("0.9"), 0)],
)
def test_size_order(max_q, depth, size):
    assert strategy.size_order(max_q, depth) == size


def test_joint_quantity_finds_the_largest_passing_size():
    assert strategy.joint_quantity(100, lambda q: q * D("0.9") <= 50) == 55
    assert strategy.joint_quantity(100, lambda q: False) == 0
    assert strategy.joint_quantity(0, lambda q: True) == 0
