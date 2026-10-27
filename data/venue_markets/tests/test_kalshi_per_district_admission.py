"""Regression tests: Kalshi per-district House series (HOUSE<ST><N>, KXHOUSE<ST><N>).

Before this repair, these contracts were admitted on ticker symbol and year alone: the rules
were never read, so a named-candidate contract and contracts whose rules contradict their
ticker or title could enter the reference set as party-winner contracts.

Fixture provenance:
- RECORDED: field values from the 2026-10-19 fresh snapshot
  (s1_t5_f6_fresh_validation_20261019T162104Z, raw/0039 and raw/0040_kalshi_standard.json).
  Upstream `market.series_ticker` is null; the parent event supplies the series.
- SYNTHETIC: constructed mutations of a recorded contract. They are not observed upstream.
"""

from __future__ import annotations

from typing import Any

import pytest

from data.venue_markets.fetch_venue_markets import classify_race, is_2026_kalshi_market

RECORDED_RULES_SECONDARY = (
    "This market is eligible for accelerated determination after a consensus of media "
    "organizations project the winner. See full rules for details."
)


def _rules(district: str, party_phrase: str) -> str:
    return (
        f"If the House member sworn in for {district} for the term beginning in 2027 is a member of the "
        f"{party_phrase}, then the market resolves to Yes."
    )


def _pd(series: str, suffix: str, title_text: str, rules_text: str | None, event_title_text: str, **overrides: Any):
    """Build a per-district (market, event) pair in the recorded shape; overrides prefixed ev_ apply to the event."""
    event_ticker = f"{series}-26"
    market = {
        "ticker": f"{event_ticker}-{suffix}",
        "event_ticker": event_ticker,
        "series_ticker": None,
        "title": title_text,
        "status": "active",
        "rules_primary": rules_text,
        "rules_secondary": RECORDED_RULES_SECONDARY,
    }
    event = {"event_ticker": event_ticker, "series_ticker": series, "title": event_title_text, "sub_title": "In 2026"}
    for key, value in overrides.items():
        if key.startswith("ev_"):
            event[key[3:]] = value
        else:
            market[key] = value
    return market, event


def _az1_d(**overrides: Any):
    """RECORDED HOUSEAZ1-26-D."""
    return _pd("HOUSEAZ1", "D", "Will Democratic win the House race for AZ-1?",
               _rules("AZ-01", "Democratic party"), "AZ-01 House winner?", **overrides)


RECORDED_ADMITTED = {
    "HOUSEAZ1-26-D": _az1_d(),
    "HOUSEAZ1-26-R": _pd("HOUSEAZ1", "R", "Will Republican win the House race for AZ-1?",
                         _rules("AZ-01", "Republican party"), "AZ-01 House winner?"),
    # Minnesota's Democratic affiliate, stated in the contract's own rules.
    "HOUSEMN2-26-D": _pd("HOUSEMN2", "D", "Will a Democrat win the House race for MN-2?",
                         _rules("MN-02", "Democratic (DFL) Party"), "MN-02 House winner?"),
    "HOUSEMN2-26-R": _pd("HOUSEMN2", "R", "Will a Republican win the House race for MN-2?",
                         _rules("MN-02", "Republican Party"), "MN-02 House winner?"),
    "HOUSENH1-26-D": _pd("HOUSENH1", "D", "Will a Democrat win the House race for NH-1?",
                         _rules("NH-01", "Democratic Party"), "NH-01 House winner?"),
    "KXHOUSENC11-26-DEM": _pd("KXHOUSENC11", "DEM", "Will Democratic win the House race for NC-11?",
                              _rules("NC-11", "Democratic Party"), "NC-11 House winner?"),
    "KXHOUSENC11-26-GOP": _pd("KXHOUSENC11", "GOP", "Will Republican win the House race for NC-11?",
                              _rules("NC-11", "Republican Party"), "NC-11 House winner?"),
    "KXHOUSEWA8-26-D": _pd("KXHOUSEWA8", "D", "Will a Democrat win the House race for WA-08?",
                           _rules("WA-08", "Democratic party"), "WA-08 House winner?"),
    "KXHOUSEIN7-26-D": _pd("KXHOUSEIN7", "D", "Will Democrats win the House race for IN-07?",
                           _rules("IN-07", "Democratic Party"), "IN-07 House winner?"),
}


class TestRecordedPerDistrictContracts:
    @pytest.mark.parametrize("ticker", sorted(RECORDED_ADMITTED))
    def test_recorded_party_contracts_admitted(self, ticker: str) -> None:
        market, event = RECORDED_ADMITTED[ticker]
        assert is_2026_kalshi_market(market, event) == (True, None)
        record = {"venue": "kalshi", "market_ticker": ticker, "event_ticker": event["event_ticker"],
                  "series_ticker": event["series_ticker"], "title": market["title"], "event_title": event["title"]}
        assert classify_race("kalshi", record) == "house"

    def test_recorded_named_candidate_contract_rejected(self) -> None:
        """RECORDED HOUSEAKAL-26-BHIL: a named person's swearing-in is not a party outcome."""
        market, event = _pd("HOUSEAKAL", "BHIL", "Will Bill Hill win the House race for AK-AL?",
                            "If Bill Hill is sworn in for AK-AL for the term beginning in 2027, then the market resolves to Yes.",
                            "Alaska House winner?")
        assert is_2026_kalshi_market(market, event) == (False, "unsupported_resolution_subject")

    def test_recorded_independent_contract_rejected(self) -> None:
        """RECORDED HOUSEIA1-26-I: only Democratic and Republican outcomes are supported."""
        market, event = _pd("HOUSEIA1", "I", "Will Independent win the House race for IA-1?",
                            _rules("IA-01", "Independent party"), "IA-01 House winner?")
        assert is_2026_kalshi_market(market, event) == (False, "unsupported_party_outcome")

    @pytest.mark.parametrize("suffix,party", [("D", "Democratic"), ("R", "Republican")])
    def test_recorded_alaska_member_first_wording_rejected(self, suffix: str, party: str) -> None:
        """RECORDED HOUSEAKAL-26-D/R: unreviewed wording ("If a member of the ... party is sworn in ...") stays excluded."""
        market, event = _pd("HOUSEAKAL", suffix, f"Will {party} win the House race for AK-AL?",
                            f"If a member of the {party} party is sworn in for AK-AL for the term beginning in 2027, "
                            "then the market resolves to Yes.", "Alaska House winner?")
        assert is_2026_kalshi_market(market, event) == (False, "unsupported_resolution_subject")


class TestPerDistrictSemanticChecks:
    """SYNTHETIC mutations of RECORDED HOUSEAZ1-26-D; each was admitted before this repair."""

    @pytest.mark.parametrize(
        "overrides,reason",
        [
            ({"rules_primary": _rules("AZ-01", "Republican party")}, "contradictory_party_winner_rules"),
            ({"rules_primary": _rules("AZ-02", "Democratic party")}, "mismatched_district"),
            ({"rules_primary": _rules("AZ-01", "Democratic party").replace("to Yes", "to No")}, "contradictory_party_winner_rules"),
            ({"rules_primary": _rules("AZ-01", "Democratic party").replace("2027", "2029")}, "non_2026_primary_contest"),
            ({"rules_primary": None}, "insufficient_resolution_evidence"),
            ({"rules_primary": "Resolves according to the AZ-01 result."}, "insufficient_resolution_evidence"),
            ({"rules_primary": _rules("AZ-01", "Democratic (DFL) party")}, "unsupported_party_label"),
            ({"rules_secondary": "The market resolves to No if the winner changes parties."}, "unsupported_background_sentence"),
            ({"title": "Will Republican win the House race for AZ-1?"}, "contradictory_party_winner_rules"),
            ({"title": "Will Democratic win the House race for AZ-2?"}, "mismatched_district"),
            ({"title": "Will Jane Doe win the House race for AZ-1?"}, "not_party_winner_proposition"),
            ({"title": "Will Democratic win the House primary for AZ-1?"}, "non_2026_primary_contest"),
            ({"ticker": "HOUSEAZ1-26-X"}, "arbitrary_ticker_suffix"),
            ({"event_ticker": "HOUSEAZ2-26"}, "mismatched_market_event_relationship"),
            ({"series_ticker": "HOUSEAZ2"}, "mismatched_market_event_relationship"),
            ({"ev_title": "AZ-02 House winner?"}, "mismatched_district"),
            ({"ev_sub_title": "In 2028"}, "non_2026_primary_contest"),
        ],
        ids=[
            "rules_party_contradicts_suffix", "rules_district_differs", "payout_no", "term_2029", "missing_rules",
            "unsupported_rules_frame", "dfl_outside_minnesota", "modifying_rules_secondary", "title_party_contradicts",
            "title_district_differs", "candidate_title", "primary_title", "unknown_suffix", "market_event_link",
            "explicit_market_series_conflict", "parent_district_conflict", "parent_year_conflict",
        ],
    )
    def test_mutation_rejected(self, overrides: dict, reason: str) -> None:
        market, event = _az1_d(**overrides)
        assert is_2026_kalshi_market(market, event) == (False, reason)

    def test_republican_dfl_label_rejected(self) -> None:
        market, event = _pd("HOUSEMN2", "R", "Will a Republican win the House race for MN-2?",
                            _rules("MN-02", "Republican (DFL) Party"), "MN-02 House winner?")
        assert is_2026_kalshi_market(market, event) == (False, "unsupported_party_label")

    @pytest.mark.parametrize("event_ticker,reason", [("HOUSEAZ1-24", "non_2026_primary_contest"), ("HOUSEAZ1", "lacks_2026_contest_year")])
    def test_event_cycle_required(self, event_ticker: str, reason: str) -> None:
        market, event = _az1_d(event_ticker=event_ticker, ev_event_ticker=event_ticker, ticker=f"{event_ticker}-D")
        assert is_2026_kalshi_market(market, event) == (False, reason)

    def test_parent_must_supply_matching_series(self) -> None:
        market, event = _az1_d(ev_series_ticker=None)
        assert is_2026_kalshi_market(market, event) == (False, "mismatched_market_event_relationship")

    def test_explicit_matching_market_series_accepted(self) -> None:
        market, event = _az1_d(series_ticker="HOUSEAZ1")
        assert is_2026_kalshi_market(market, event) == (True, None)


class TestSharedDflWording:
    def test_kxhouserace_minnesota_dfl_wording_admitted(self) -> None:
        """SYNTHETIC: the shared decisive-sentence grammar accepts DFL wording only in Minnesota."""
        market = {
            "ticker": "KXHOUSERACE-MN05-26-D", "event_ticker": "KXHOUSERACE-MN05-26", "series_ticker": None,
            "title": "Will Democratic win the House race for MN-05?", "status": "active",
            "rules_primary": _rules("MN-05", "Democratic (DFL) Party"), "rules_secondary": RECORDED_RULES_SECONDARY,
        }
        event = {"event_ticker": "KXHOUSERACE-MN05-26", "series_ticker": "KXHOUSERACE", "title": "MN-05 House winner?", "sub_title": "MN-05"}
        assert is_2026_kalshi_market(market, event) == (True, None)
