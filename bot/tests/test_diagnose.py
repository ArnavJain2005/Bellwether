"""Tests for BOT-DEPLOY-15K: offline diagnostics, historical candidate analysis,
high-confidence 2-venue election-night threshold, and ladder cap updates. No network.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path

import pytest
import yaml

from bot import db, diagnose, strategy
from bot.mapping import MappedContract
from bot.refs import MID_ONLY, RefQuote, make_quote
from bot.strategy import Leg, SigQuote
from bot.tests.kit import make_limits

D = Decimal
BEFORE = datetime(2026, 11, 3, 20, 0, tzinfo=UTC)
NIGHT = datetime(2026, 11, 4, 1, 30, tzinfo=UTC)

DUAL_CONTRACT = MappedContract(
    race_key="senate:PA",
    party="D",
    sig_market_id="500",
    sig_exchange_id="1000",
    sig_title="PA Senate D",
    kalshi_ticker="K-PA-D",
    polymarket_token_id="POLY-PA-D",
)
SINGLE_CONTRACT = MappedContract(
    race_key="house:PA-07",
    party="D",
    sig_market_id="501",
    sig_exchange_id="1001",
    sig_title="PA-07 House D",
    kalshi_ticker="K-PA07-D",
    polymarket_token_id=None,
)


def hc_limits(hc_edge: str | None = "0.04", **ref_overrides) -> object:
    raw = json.loads(make_limits(no_orders_enabled=True, no_price_convention="no_price").model_dump_json())
    if hc_edge is not None:
        raw["strategies"]["reference"]["high_confidence_edge_election_night"] = hc_edge
    raw["strategies"]["reference"].update(ref_overrides)
    return make_limits().model_validate(raw)


def k_quote(bid="0.475", ask="0.485", when=NIGHT) -> RefQuote:
    return make_quote("kalshi", DUAL_CONTRACT.kalshi_ticker, D(bid), D(ask), when)


def p_quote(bid="0.475", ask="0.485", when=NIGHT) -> RefQuote:
    return make_quote("polymarket", "POLY-PA-D", D(bid), D(ask), when)


# --- Deliverable 3: High-confidence election-night reference threshold ------------------


def test_default_limits_keep_strict_006_election_night_edge_even_with_two_venues():
    # Fair = 0.480; SIG ask = 0.435 -> edge = 0.045 (< 0.06, >= 0.04).
    # Without high_confidence_edge_election_night configured, election night skips.
    ev = strategy.evaluate_reference(
        DUAL_CONTRACT,
        SigQuote(D("0.410"), D("0.435")),
        k_quote(),
        p_quote(),
        make_limits(),
        NIGHT,
    )
    assert ev.is_skip
    assert "0.060" in ev.reason


def test_high_confidence_2venue_contract_qualifies_at_004_on_election_night():
    # Both Kalshi (0.475/0.485, spread 0.01 <= 0.02) and Poly (0.475/0.485, spread 0.01 <= 0.02)
    # are fresh (0s <= 15s), agree within 0.000 <= 0.015, and fair 0.480 is in [0.05, 0.95].
    limits = hc_limits("0.04")
    ev = strategy.evaluate_reference(
        DUAL_CONTRACT,
        SigQuote(D("0.410"), D("0.435")),
        k_quote(),
        p_quote(),
        limits,
        NIGHT,
    )
    assert not ev.is_skip
    assert ev.legs == (Leg(DUAL_CONTRACT, "yes", D("0.435")),)
    assert ev.edge == D("0.045")
    assert "[high-confidence 2-venue]" in ev.reason
    assert "edge 0.040" in ev.reason


def test_single_venue_or_degraded_quotes_still_require_full_006_on_election_night():
    limits = hc_limits("0.04")
    sig = SigQuote(D("0.410"), D("0.435"))  # edge 0.045: enough for 0.04, not for 0.06

    # 1. Single-venue unmapped Polymarket
    ev_single = strategy.evaluate_reference(
        SINGLE_CONTRACT, sig, k_quote(), None, limits, NIGHT
    )
    assert ev_single.is_skip and "0.060" in ev_single.reason

    # 2. Polymarket mid-only fallback (no two-sided book)
    poly_mid_only = RefQuote("polymarket", "POLY-PA-D", None, None, D("0.48"), None, NIGHT, MID_ONLY)
    ev_mid = strategy.evaluate_reference(
        DUAL_CONTRACT, sig, k_quote(), poly_mid_only, limits, NIGHT
    )
    assert ev_mid.is_skip and "0.060" in ev_mid.reason

    # 3. Wide Polymarket spread (0.03 > 0.02 high-confidence max, though <= 0.04 standard max)
    poly_wide = p_quote("0.465", "0.495")
    ev_wide = strategy.evaluate_reference(
        DUAL_CONTRACT, sig, k_quote(), poly_wide, limits, NIGHT
    )
    assert ev_wide.is_skip and "0.060" in ev_wide.reason

    # 4. Slightly older quote (20s > 15s high-confidence max, though <= 30s standard max)
    k_older = k_quote(when=NIGHT - timedelta(seconds=20))
    ev_older = strategy.evaluate_reference(
        DUAL_CONTRACT, sig, k_older, p_quote(), limits, NIGHT
    )
    assert ev_older.is_skip and "0.060" in ev_older.reason

    # 5. Venue disagreement 0.020 (> 0.015 high-confidence max, though <= 0.030 standard max)
    poly_diff = p_quote("0.495", "0.505")  # mid 0.500 vs Kalshi mid 0.480 -> diff 0.020
    ev_diff = strategy.evaluate_reference(
        DUAL_CONTRACT, sig, k_quote(), poly_diff, limits, NIGHT
    )
    assert ev_diff.is_skip and "0.060" in ev_diff.reason


def test_external_price_jump_to_tail_is_never_treated_as_high_confidence_call():
    # External venues jump to 0.970 on election night; SIG ask is 0.925 (edge 0.045).
    # Because fair 0.970 > high_confidence_max_prob (0.95), it MUST NOT use the 0.04 edge;
    # it requires the full 0.06 election-night edge.
    limits = hc_limits("0.04")
    k_tail = k_quote("0.965", "0.975")
    p_tail = p_quote("0.965", "0.975")
    ev_high_tail = strategy.evaluate_reference(
        DUAL_CONTRACT,
        SigQuote(D("0.900"), D("0.925")),
        k_tail,
        p_tail,
        limits,
        NIGHT,
    )
    assert ev_high_tail.is_skip and "0.060" in ev_high_tail.reason

    # Same for a crash toward 0 (fair = 0.030 < 0.05, SIG bid = 0.075 -> NO edge 0.045 < 0.06)
    k_low = k_quote("0.025", "0.035")
    p_low = p_quote("0.025", "0.035")
    ev_low_tail = strategy.evaluate_reference(
        DUAL_CONTRACT,
        SigQuote(D("0.075"), D("0.100")),
        k_low,
        p_low,
        limits,
        NIGHT,
    )
    assert ev_low_tail.is_skip and "0.060" in ev_low_tail.reason


@pytest.mark.parametrize(
    "bad_ref",
    [
        {"high_confidence_edge_election_night": "0.03"},  # below daytime edge 0.04
        {"high_confidence_edge_election_night": "0.07"},  # above election-night edge 0.06
        {
            "high_confidence_edge_election_night": "0.04",
            "high_confidence_max_ref_spread": "0.05",  # > max_ref_spread 0.04
        },
        {
            "high_confidence_edge_election_night": "0.04",
            "high_confidence_max_ref_age_s": 45,  # > max_ref_age_s 30
        },
        {
            "high_confidence_edge_election_night": "0.04",
            "high_confidence_max_venue_disagreement": "0.04",  # > max_venue_disagreement 0.03
        },
    ],
)
def test_invalid_high_confidence_bounds_are_rejected(bad_ref):
    with pytest.raises(ValueError):
        hc_limits(**bad_ref)


# --- Deliverables 1 & 2: Offline Diagnostic & Reproducible Candidate Analysis ----------


def _seed_live_like_db(tmp_path: Path) -> tuple[Path, Path, Path]:
    """Create a synthetic SQLite DB matching the observed live state:
    cash = 25,000.26, committed = 64,611.30 / 70,000, min_cash_reserve = 25,000,
    election night active (0.060 edge), with recorded quotes and throttled decisions.
    """
    state_dir = tmp_path / "state"
    db_path = state_dir / "bellwether.db"
    conn = db.connect(db_path)

    # Write a map.yaml with 2 races (4 contracts: 2 dual-venue, 2 single-venue)
    map_path = tmp_path / "map.yaml"
    map_data = {
        "races": [
            {
                "race_key": "senate:PA",
                "exclude": False,
                "contracts": [
                    {
                        "party": "D",
                        "sig_market_id": "500",
                        "sig_exchange_id": "1000",
                        "sig_title": "PA Senate D",
                        "kalshi_ticker": "K-PA-D",
                        "polymarket_token_id": "8000",
                        "approved": True,
                    },
                    {
                        "party": "R",
                        "sig_market_id": "501",
                        "sig_exchange_id": "1001",
                        "sig_title": "PA Senate R",
                        "kalshi_ticker": "K-PA-R",
                        "polymarket_token_id": "8001",
                        "approved": True,
                    },
                ],
            },
            {
                "race_key": "governor:GA",
                "exclude": False,
                "contracts": [
                    {
                        "party": "D",
                        "sig_market_id": "502",
                        "sig_exchange_id": "1002",
                        "sig_title": "GA Gov D",
                        "kalshi_ticker": "K-GA-D",
                        "polymarket_token_id": None,
                        "approved": True,
                    },
                    {
                        "party": "R",
                        "sig_market_id": "503",
                        "sig_exchange_id": "1003",
                        "sig_title": "GA Gov R",
                        "kalshi_ticker": "K-GA-R",
                        "polymarket_token_id": None,
                        "approved": True,
                    },
                ],
            },
        ]
    }
    map_path.write_text(yaml.safe_dump(map_data), encoding="utf-8")

    # Filled orders totalling 63,611.67 + preexisting 999.63 = 64,611.30 committed
    db.insert_order(
        conn,
        {
            "key": "fill-1",
            "created_utc": "2026-11-04T00:15:00Z",
            "updated_utc": "2026-11-04T00:15:01Z",
            "race_key": "senate:PA",
            "sig_market_id": "500",
            "sig_exchange_id": "1000",
            "side": "yes",
            "action": "buy",
            "price_side": "0.500",
            "wire_price": "0.500",
            "quantity": 2000,
            "expires_utc": "2026-11-04T00:17:00Z",
            "strategy": "reference",
            "status": "filled",
            "sig_order_id": "9001",
            "quantity_traded": "2000",
            "total_cost": "1000.00",
            "reason": "SIG ask 0.500 <= fair 0.550 - edge 0.040",
        },
    )
    db.insert_order(
        conn,
        {
            "key": "fill-bulk",
            "created_utc": "2026-11-04T00:30:00Z",
            "updated_utc": "2026-11-04T00:30:01Z",
            "race_key": "other:BULK",
            "sig_market_id": "599",
            "sig_exchange_id": "1099",
            "side": "yes",
            "action": "buy",
            "price_side": "0.500",
            "wire_price": "0.500",
            "quantity": 125223,
            "expires_utc": "2026-11-04T00:32:00Z",
            "strategy": "reference",
            "status": "filled",
            "sig_order_id": "9002",
            "quantity_traded": "125223",
            "total_cost": "62611.67",
            "reason": "bulk fill fixture",
        },
    )

    # Snapshot & heartbeat showing balance 25,000.26, committed 64,611.30, cap_total 70,000, reserve 25,000
    db.insert_snapshot(
        conn,
        ts_utc="2026-11-04T01:29:50Z",
        balance="25000.26",
        positions_json="[]",
        open_orders=0,
    )
    db.upsert_heartbeat(
        conn,
        ts_utc="2026-11-04T01:30:00Z",
        mode="live",
        version="bot-ew-4",
        loop_ms=340,
        reads_last_min=8,
        writes_last_min=0,
        paused=0,
        pause_reason=None,
        stop_seen=0,
        balance="25000.26",
        start_balance="99000.37",
        committed="64611.30",
        scale="1.0",
        cap_total="70000.0",
        cap_per_race="6000.0",
        min_cash_reserve="25000",
        edge_now="0.06",
        election_night=1,
        ladder_step="L3",
    )

    # Quotes in quotes_latest:
    # 1. senate:PA (D) - dual-venue high-confidence, fair 0.480, SIG ask 0.435 -> edge 0.045 (4.5c: < 6c, >= 4c)
    db.upsert_quote(
        conn,
        sig_exchange_id="1000",
        race_key="senate:PA",
        party="D",
        ts_utc="2026-11-04T01:30:00Z",
        sig_bid="0.410",
        sig_ask="0.435",
        sig_last="0.430",
        kalshi_ticker="K-PA-D",
        kalshi_bid="0.475",
        kalshi_ask="0.485",
        kalshi_ts_utc="2026-11-04T01:29:55Z",
        poly_token="8000",
        poly_bid="0.475",
        poly_ask="0.485",
        poly_ts_utc="2026-11-04T01:29:55Z",
        fair="0.480",
        edge="0.045",
    )
    # 2. senate:PA (R) - dual-venue high-confidence, fair 0.520, SIG ask 0.455 -> edge 0.065 (6.5c: >= 6c)
    db.upsert_quote(
        conn,
        sig_exchange_id="1001",
        race_key="senate:PA",
        party="R",
        ts_utc="2026-11-04T01:30:00Z",
        sig_bid="0.430",
        sig_ask="0.455",
        sig_last="0.450",
        kalshi_ticker="K-PA-R",
        kalshi_bid="0.515",
        kalshi_ask="0.525",
        kalshi_ts_utc="2026-11-04T01:29:55Z",
        poly_token="8001",
        poly_bid="0.515",
        poly_ask="0.525",
        poly_ts_utc="2026-11-04T01:29:55Z",
        fair="0.520",
        edge="0.065",
    )
    # 3. governor:GA (D) - single-venue Kalshi only, fair 0.400, SIG ask 0.365 -> edge 0.035 (3.5c: >= 3c, < 4c)
    db.upsert_quote(
        conn,
        sig_exchange_id="1002",
        race_key="governor:GA",
        party="D",
        ts_utc="2026-11-04T01:30:00Z",
        sig_bid="0.340",
        sig_ask="0.365",
        sig_last="0.360",
        kalshi_ticker="K-GA-D",
        kalshi_bid="0.395",
        kalshi_ask="0.405",
        kalshi_ts_utc="2026-11-04T01:29:55Z",
        poly_token=None,
        poly_bid=None,
        poly_ask=None,
        poly_ts_utc=None,
        fair="0.400",
        edge="0.035",
    )
    # 4. governor:GA (R) - no edge (fair 0.600, SIG ask 0.595 -> edge 0.005)
    db.upsert_quote(
        conn,
        sig_exchange_id="1003",
        race_key="governor:GA",
        party="R",
        ts_utc="2026-11-04T01:30:00Z",
        sig_bid="0.585",
        sig_ask="0.595",
        sig_last="0.590",
        kalshi_ticker="K-GA-R",
        kalshi_bid="0.595",
        kalshi_ask="0.605",
        kalshi_ts_utc="2026-11-04T01:29:55Z",
        poly_token=None,
        poly_bid=None,
        poly_ask=None,
        poly_ts_utc=None,
        fair="0.600",
        edge="0.015",
    )

    # Throttled decisions rows over 30m and 120m
    db.insert_decision(
        conn,
        ts_utc="2026-11-04T01:10:00Z",
        race_key="senate:PA",
        sig_exchange_id="1000",
        party="D",
        strategy="reference",
        verdict="skip",
        sig_bid="0.410",
        sig_ask="0.435",
        kalshi_mid="0.480",
        poly_mid="0.480",
        fair="0.480",
        edge="0.045",
        reason="no edge: SIG 0.410/0.435 vs fair 0.480 +/- 0.060 (Kalshi 0.48/0.49, Poly 0.48)",
    )
    db.insert_decision(
        conn,
        ts_utc="2026-11-04T01:20:00Z",
        race_key="senate:PA",
        sig_exchange_id="1001",
        party="R",
        strategy="reference",
        verdict="skip",
        sig_bid="0.430",
        sig_ask="0.455",
        kalshi_mid="0.520",
        poly_mid="0.520",
        fair="0.520",
        edge="0.065",
        reason=(
            "SIG ask 0.455 <= fair 0.520 - edge 0.060 (Kalshi 0.52/0.53, Poly 0.52); "
            "not sent: free cash 0.26 (balance 25000.26 - reserve) is at or under 2% of "
            "balance (500.0052): fully deployed"
        ),
    )
    db.insert_decision(
        conn,
        ts_utc="2026-11-04T00:00:00Z",  # 90m ago (in 120m window, outside 30m window)
        race_key="governor:GA",
        sig_exchange_id="1002",
        party="D",
        strategy="reference",
        verdict="skip",
        sig_bid="0.340",
        sig_ask="0.365",
        kalshi_mid="0.400",
        poly_mid=None,
        fair=None,
        edge=None,
        reason="no fair value: kalshi stale (42s > 30s) (Kalshi 0.40/0.41, Poly -)",
    )
    conn.close()
    return db_path, state_dir, map_path


def test_diagnose_report_and_cli_are_read_only_and_detect_drift_and_blockers(tmp_path):
    db_path, state_dir, map_path = _seed_live_like_db(tmp_path)
    before_bytes = db_path.read_bytes()

    report = diagnose.build_report(
        db_path=db_path,
        state_dir=state_dir,
        map_path=map_path,
        now=NIGHT,
    )

    # 1. Config drift between repo risk.yaml (dry_run, 100k cap, 90k reserve) and heartbeat (live, 70k, 25k)
    assert report.config_drift
    assert any("cap_total=70000.0" in d for d in report.config_drift)
    assert any("min_cash_reserve=25000" in d for d in report.config_drift)

    # 2. Effective cash & exposure match live dashboard state
    ae = report.account_and_exposure
    assert ae["effective_cash"] == "25000.26"
    assert ae["total_committed"] == "64611.30"
    assert ae["total_cap"] == "70000.00"
    assert ae["min_cash_reserve"] == "25000.00"
    assert ae["free_cash_above_reserve"] == "0.26"
    assert ae["net_deployable_cash"] == "0.00"
    assert any("min_cash_reserve" in g for g in ae["binding_gates"])
    assert any("reinvest_min_free_fraction" in g for g in ae["binding_gates"])
    assert "does NOT capture mark-to-market" in ae["loss_stop_note"]

    # 3. Throttled decision rows vs actual evaluations in 30m and 120m windows
    w30, w120 = report.windows
    assert w30.minutes == 30 and w120.minutes == 120
    assert w30.verdict_counts == {"skip": 2}
    assert w120.verdict_counts == {"skip": 3}
    # Each throttled skip row represents 6 loop evaluations (60s / 10s loop)
    for b in w30.buckets:
        assert b.est_actual_evaluations == b.throttled_rows * 6

    # 4. Candidate analysis: 0.06 vs 0.04 vs 0.03 vs hc_0.04
    counts = report.candidate_counts_by_threshold
    assert counts["0.06"]["total"] == 1  # 1001 (6.5c)
    assert counts["0.04"]["total"] == 2  # 1001 (6.5c) + 1000 (4.5c)
    assert counts["0.03"]["total"] == 3  # 1001 (6.5c) + 1000 (4.5c) + 1002 (3.5c)
    assert counts["hc_0.04"]["total"] == 2  # 1001 and 1000 both pass 2-venue high-confidence

    # Under active caps (25k reserve), 0 candidates are unblocked; under proposed (+15k, 10k reserve), all 3 are unblocked!
    assert counts["0.06"]["unblocked_active"] == 0
    assert counts["0.06"]["unblocked_proposed"] == 1
    assert counts["hc_0.04"]["unblocked_active"] == 0
    assert counts["hc_0.04"]["unblocked_proposed"] == 2

    # Verify individual candidate rows
    by_ex = {c.sig_exchange_id: c for c in report.candidates}
    assert by_ex["1000"].high_confidence is True
    assert by_ex["1000"].passes_high_confidence_gate is True
    assert any("min_cash_reserve" in b for b in by_ex["1000"].active_blockers)
    assert by_ex["1000"].proposed_blockers == ()
    assert by_ex["1000"].proposed_max_shares > 0

    assert by_ex["1002"].high_confidence is False
    assert "single-venue" in by_ex["1002"].high_confidence_detail

    # 5. CLI text and JSON modes work and never modify the database file
    lines: list[str] = []
    rc = diagnose.main(
        [
            "--db",
            str(db_path),
            "--state-dir",
            str(state_dir),
            "--map",
            str(map_path),
            "--now",
            "2026-11-04T01:30:00Z",
        ],
        out=lines.append,
    )
    assert rc == 0
    text = "\n".join(lines)
    assert "HISTORICAL OFFLINE ANALYSIS ONLY" in text
    assert "NOT CURRENT LIVE TRADE RECOMMENDATIONS" in text
    assert "25000.26 SUSQ" in text
    assert "64611.30 / 70000.00 SUSQ" in text

    json_lines: list[str] = []
    rc_json = diagnose.main(
        [
            "--db",
            str(db_path),
            "--state-dir",
            str(state_dir),
            "--map",
            str(map_path),
            "--now",
            "2026-11-04T01:30:00Z",
            "--json",
        ],
        out=json_lines.append,
    )
    assert rc_json == 0
    parsed = json.loads("\n".join(json_lines))
    assert parsed["account_and_exposure"]["effective_cash"] == "25000.26"
    assert db_path.read_bytes() == before_bytes
