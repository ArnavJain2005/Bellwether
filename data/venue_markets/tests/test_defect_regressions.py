"""Regression tests covering confirmed defects in venue-market eligibility,
classification, state extraction, and Windows CP1252 validation output.
"""

from __future__ import annotations

import io
import json
import sys
from typing import Any

import pytest

from data.venue_markets.fetch_venue_markets import (
    RateLimiter,
    classify_race,
    extract_race_state,
    fetch_kalshi_markets,
    is_2026_kalshi_market,
    is_2026_polymarket_market,
    run_validation,
    validate_dataset,
)

# =====================================================================
# Defect 1: Lieutenant Governor contracts leak into intended race universe
# =====================================================================

class TestLieutenantGovernorLeak:
    """Lieutenant Governor contracts must be rejected with a clear exclusion reason,
    while ordinary Governor contracts must be preserved even if background rules
    mention lieutenant governor.
    """

    @pytest.mark.parametrize("market_id,question,slug,event_title", [
        ("4222435", "Will the Democratic Party candidate win the 2026 Georgia Lieutenant Governor election?", "will-the-democratic-party-candidate-win-the-2026-georgia-lieutenant-governor-election", "Georgia Lieutenant Governor Election Winner"),
        ("4222436", "Will the Republican Party candidate win the 2026 Georgia Lieutenant Governor election?", "will-the-republican-party-candidate-win-the-2026-georgia-lieutenant-governor-election", "Georgia Lieutenant Governor Election Winner"),
        ("4222440", "Will the Democratic Party candidate win the 2026 Texas Lieutenant Governor election?", "will-the-democratic-party-candidate-win-the-2026-texas-lieutenant-governor-election", "Texas Lieutenant Governor Election Winner"),
        ("4222441", "Will the Republican Party candidate win the 2026 Texas Lieutenant Governor election?", "will-the-republican-party-candidate-win-the-2026-texas-lieutenant-governor-election", "Texas Lieutenant Governor Election Winner"),
        ("4222445", "Will the Democratic Party candidate win the 2026 Vermont Lieutenant Governor election?", "will-the-democratic-party-candidate-win-the-2026-vermont-lieutenant-governor-election", "Vermont Lieutenant Governor Election Winner"),
        ("4222446", "Will the Republican Party candidate win the 2026 Vermont Lieutenant Governor election?", "will-the-republican-party-candidate-win-the-2026-vermont-lieutenant-governor-election", "Vermont Lieutenant Governor Election Winner"),
    ])
    def test_polymarket_lt_gov_contracts_rejected(self, market_id: str, question: str, slug: str, event_title: str) -> None:
        item = {
            "id": market_id,
            "closed": False,
            "active": True,
            "question": question,
            "slug": slug,
        }
        parent_event = {"title": event_title, "slug": slug}
        eligible, reason = is_2026_polymarket_market(item, parent_event=parent_event, is_tag_102289=True)
        assert not eligible, f"Polymarket Lt Gov contract {market_id} was improperly accepted"
        assert reason == "lieutenant_governor_contest"

    @pytest.mark.parametrize("ticker,series,title,subtitle", [
        ("KXGA1ROUND-26NOV03-LTGOV", "KXGA1ROUND", "Will anyone win the 2026 Georgia Lieutenant Governor general election election in the first round?", "Lieutenant Governor"),
        ("KXNVLTGOV-26NOV03-JHAN", "KXNVLTGOV", "Will Janine Hansen win the 2026 Nevada Lieutenant Governor election?", "Janine Hansen"),
        ("KXCALIEUTENANTGOV-26NOV03-FMA", "KXCALIEUTENANTGOV", "Will Fiona Ma win the 2026 California Lieutenant Governor election?", "Fiona Ma"),
    ])
    def test_kalshi_lt_gov_contracts_rejected(self, ticker: str, series: str, title: str, subtitle: str) -> None:
        m = {
            "ticker": ticker,
            "series_ticker": series,
            "title": title,
            "subtitle": subtitle,
            "status": "open",
        }
        ev = {"event_ticker": series, "series_ticker": series, "title": title}
        eligible, reason = is_2026_kalshi_market(m, ev)
        assert not eligible, f"Kalshi Lt Gov contract {ticker} was improperly accepted"
        assert reason == "lieutenant_governor_contest"

    def test_ordinary_governor_positive_controls_preserved(self) -> None:
        # Polymarket Governor positive control
        poly_item = {
            "id": "629337",
            "closed": False,
            "active": True,
            "question": "Will the Democrats win the Georgia governor race in 2026?",
            "slug": "georgia-governor-race-2026",
            # Passing mention of lieutenant governor in resolution rules
            "description": "Resolves based on the governor race. Lieutenant governor is contested separately.",
        }
        poly_ev = {"title": "Georgia Governor Race 2026", "slug": "georgia-governor-2026"}
        eligible, reason = is_2026_polymarket_market(poly_item, parent_event=poly_ev, is_tag_102289=True)
        assert eligible, f"Ordinary Governor was rejected: {reason}"
        assert classify_race("polymarket", poly_item, event_title="Georgia Governor Race 2026") == "governor"

        # Kalshi Governor positive control
        kalshi_m = {
            "ticker": "GOVPARTYGA-26-D",
            "series_ticker": "GOVPARTYGA",
            "title": "Will the Democratic party win the governorship in Georgia",
            "status": "open",
            "rules_primary": "Resolves for governor. Note: lieutenant governor is not included.",
        }
        kalshi_ev = {"event_ticker": "GOVPARTYGA-26", "series_ticker": "GOVPARTYGA", "title": "Georgia Governor 2026"}
        k_eligible, k_reason = is_2026_kalshi_market(kalshi_m, kalshi_ev)
        assert k_eligible, f"Kalshi ordinary Governor was rejected: {k_reason}"
        assert classify_race("kalshi", kalshi_m, event_title="Georgia Governor 2026") == "governor"


# =====================================================================
# Defect 2: State-legislature supermajority contracts leak into federal races
# =====================================================================

class TestStateLegislatureSupermajorityLeak:
    """State legislature supermajority contracts must be excluded under
    'state_legislative_contest', even when titled 'Maryland Senate' or similar,
    while genuine U.S. Senate/House contracts are preserved.
    """

    @pytest.mark.parametrize("ticker,series,title", [
        ("KXDEMSTATESUPERMAJ-26NOV03-COS", "KXDEMSTATESUPERMAJ", "Will the number of Colorado Senate seats held by Democrats be at least 24 after the November 3, 2026 general election?"),
        ("KXDEMSTATESUPERMAJ-26NOV03-MDS", "KXDEMSTATESUPERMAJ", "Will the number of Maryland Senate seats held by Democrats be at least 29 after the November 3, 2026 general election?"),
        ("KXGOPSTATESUPERMAJ-26NOV03-MOS", "KXGOPSTATESUPERMAJ", "Will the number of Missouri Senate seats held by Republicans be at least 23 after the November 3, 2026 general election?"),
        ("KXGOPSTATESUPERMAJ-26NOV03-AZH", "KXGOPSTATESUPERMAJ", "Will the number of Arizona House of Representatives seats held by Republicans be at least 40 after the November 3, 2026 general election?"),
    ])
    def test_kalshi_state_supermajority_contracts_excluded(self, ticker: str, series: str, title: str) -> None:
        m = {
            "ticker": ticker,
            "series_ticker": series,
            "title": title,
            "status": "open",
        }
        ev = {"event_ticker": series, "series_ticker": series, "title": title}
        eligible, reason = is_2026_kalshi_market(m, ev)
        assert not eligible, f"State supermajority {ticker} was improperly accepted"
        assert reason == "state_legislative_contest"

    def test_genuine_federal_senate_and_house_preserved(self) -> None:
        # Federal U.S. Senate positive control
        senate_m = {
            "ticker": "SENATETX-26-R",
            "series_ticker": "SENATETX",
            "title": "Will Republicans win the Senate race in Texas?",
            "status": "open",
        }
        senate_ev = {"event_ticker": "SENATETX-26", "series_ticker": "SENATETX", "title": "Texas Senate 2026"}
        eligible, reason = is_2026_kalshi_market(senate_m, senate_ev)
        assert eligible, f"Genuine U.S. Senate was rejected: {reason}"

        # Federal U.S. House seat count positive control
        house_m = {
            "ticker": "KXDHOUSESUPERMAJ-26NOV03-AL290",
            "series_ticker": "KXDHOUSESUPERMAJ",
            "title": "Will the number of U.S. House seats won by Democratic Party candidates be at least 290 in the 2026 U.S. House elections?",
            "status": "open",
        }
        house_ev = {"event_ticker": "KXDHOUSESUPERMAJ", "series_ticker": "KXDHOUSESUPERMAJ", "title": "U.S. House Supermajority 2026"}
        h_eligible, h_reason = is_2026_kalshi_market(house_m, house_ev)
        assert h_eligible, f"Genuine federal House seat count was rejected: {h_reason}"

    @pytest.mark.parametrize("question", [
        "Will Democrats win at least 15 Texas House of Representatives seats in the 2026 U.S. congressional elections?",
        "Will Republicans win both Texas Senate seats in the U.S. Senate after the 2026 elections?",
    ])
    def test_explicitly_federal_seat_count_contracts_preserved(self, question: str) -> None:
        item = {
            "closed": False,
            "active": True,
            "question": question,
        }
        eligible, reason = is_2026_polymarket_market(item, is_tag_102289=True)
        assert eligible, f"Explicitly federal seat count was falsely excluded: {reason}"


# =====================================================================
# Defect 3: State-specific U.S. Senate winners misclassified as control
# =====================================================================

class TestStateSenateWinnersClassification:
    """State-specific U.S. Senate winners must be classified as 'senate',
    not 'control', even if the title says 'win the Senate'.
    Genuine national control (CONTROLH/CONTROLS) must remain 'control'.
    """

    @pytest.mark.parametrize("ticker,series,title", [
        ("SENATEWY-26-R", "SENATEWY", "Will Republicans win the Senate race in Wyoming?"),
        ("SENATEWY-26-D", "SENATEWY", "Will Democratics win the Senate race in Wyoming?"),
        ("SENATETX-26-R", "SENATETX", "Will Republicans win the Senate race in Texas?"),
    ])
    def test_state_senate_winners_classified_as_senate(self, ticker: str, series: str, title: str) -> None:
        m = {
            "market_ticker": ticker,
            "series_ticker": series,
            "title": title,
        }
        race_type = classify_race("kalshi", m)
        assert race_type == "senate", f"State Senate winner {ticker} classified as '{race_type}' instead of 'senate'"

    @pytest.mark.parametrize("ticker,series,title", [
        ("CONTROLH-2026-D", "CONTROLH", "Will Democrats win the House in 2026?"),
        ("CONTROLS-2026-R", "CONTROLS", "Will Republicans win the U.S. Senate in 2026?"),
        ("CONTROLS-2026-D", "CONTROLS", "Will Democrats win the U.S. Senate in 2026?"),
    ])
    def test_national_control_preserved_as_control(self, ticker: str, series: str, title: str) -> None:
        m = {
            "market_ticker": ticker,
            "series_ticker": series,
            "title": title,
        }
        race_type = classify_race("kalshi", m)
        assert race_type == "control", f"National chamber control {ticker} classified as '{race_type}' instead of 'control'"


# =====================================================================
# Defect 4: KXSENATEWITHIN5 falsely interpreted as Wisconsin
# =====================================================================

class TestWithin5StateExtraction:
    """KXSENATEWITHIN5 must not extract 'wisconsin' from the 'WI' substring of 'WITHIN'.
    It must resolve to the real state (Texas, Alaska, etc.) from anchored evidence
    or return None, never fabricating a state.
    """

    @pytest.mark.parametrize("ticker,series,title,expected_state", [
        ("KXSENATEWITHIN5-26NOV03-TX", "KXSENATEWITHIN5", "Will the 2026 U.S. Senate general election in Texas be decided by 5 percentage points or less?", "texas"),
        ("KXSENATEWITHIN5-26NOV03-AK", "KXSENATEWITHIN5", "Will the 2026 U.S. Senate general election in Alaska be decided by 5 percentage points or less?", "alaska"),
        ("KXSENATEWITHIN5-26NOV03-ME", "KXSENATEWITHIN5", "Will the 2026 U.S. Senate general election in Maine be decided by 5 percentage points or less?", "maine"),
    ])
    def test_kxsenatewithin_extracts_correct_state(self, ticker: str, series: str, title: str, expected_state: str) -> None:
        m = {
            "market_ticker": ticker,
            "series_ticker": series,
            "title": title,
        }
        st = extract_race_state("kalshi", m, "senate")
        assert st == expected_state, f"Expected state '{expected_state}' but got '{st}' for {ticker}"

    def test_insufficient_evidence_returns_none_never_fabricates(self) -> None:
        m = {
            "market_ticker": "KXSENATEWITHIN5-26NOV03",
            "series_ticker": "KXSENATEWITHIN5",
            "title": "Will the 2026 U.S. Senate general election margin be within 5 points?",
        }
        st = extract_race_state("kalshi", m, "senate")
        assert st is None, f"Fabricated state '{st}' from symbol with insufficient evidence"

    def test_legitimate_wisconsin_positive_control(self) -> None:
        m = {
            "market_ticker": "GOVPARTYWI-26-D",
            "series_ticker": "GOVPARTYWI",
            "title": "Will the Democratic party win the governorship in Wisconsin",
        }
        st = extract_race_state("kalshi", m, "governor")
        assert st == "wisconsin", f"Legitimate Wisconsin symbol failed extraction: got '{st}'"

    def test_unsupported_ticker_suffix_returns_none_not_indiana(self) -> None:
        """Arbitrary trailing suffix like -IN in KXSENATEODDS-26-JOHN-IN must not invent Indiana."""
        m = {
            "series_ticker": "KXSENATEODDS",
            "market_ticker": "KXSENATEODDS-26-JOHN-IN",
            "title": "Will the 2026 U.S. Senate election be close?",
        }
        st = extract_race_state("kalshi", m, "senate")
        assert st is None, f"Expected state None but got '{st}'"

    def test_within5_wisconsin_with_parent_event_evidence_accepted(self) -> None:
        """WITHIN5 record with Wisconsin evidence in event_title and -WI suffix must not be flagged
        as fabricated by validate_dataset.
        """
        payload = {
            "polymarket": [
                {"market_id": "ctrl-sen", "question": "senate control", "race_type": "control"},
                {"market_id": "ctrl-hse", "question": "house control", "race_type": "control"},
            ],
            "kalshi": [
                {"market_ticker": "CONTROLS-26", "series_ticker": "CONTROLS", "title": "Senate control", "race_type": "control"},
                {"market_ticker": "CONTROLH-26", "series_ticker": "CONTROLH", "title": "House control", "race_type": "control"},
                {
                    "market_ticker": "KXSENATEWITHIN5-26NOV03-WI",
                    "series_ticker": "KXSENATEWITHIN5",
                    "title": "Will the 2026 U.S. Senate race be decided within 5 points?",
                    "event_title": "Wisconsin U.S. Senate margin in 2026",
                    "race_type": "senate",
                }
            ]
        }
        errors = validate_dataset(payload)
        within_errors = [e for e in errors if "WITHIN contracts falsely extracted as Wisconsin" in e]
        assert not within_errors, f"Legitimate Wisconsin margin contract rejected by validation: {within_errors}"


# =====================================================================
# Defect 5: Fetch-time and validation-time classifications disagree
# =====================================================================

class TestFetchValidationClassificationAgreement:
    """Saved records must retain parent-event context so that validation-time
    classification reproduces the fetch-time classification and state exactly.
    """

    def test_kalshi_retains_parent_event_context(self) -> None:
        # Example: Louisiana House open primary outright winner
        # Ticker has KXLAHOUSE1R, title mentions '1st Congressional District'
        # ev_title mentions 'House'
        ev_title = "Louisiana House 1st Congressional District 2026"
        m = {
            "venue": "kalshi",
            "market_ticker": "KXLAHOUSE1R-26NOV03-LA01",
            "series_ticker": "KXLAHOUSE1R",
            "title": "Will anyone win the 2026 Louisiana 1st Congressional District open primary outright?",
            "subtitle": "LA-01",
            "event_title": ev_title,
        }
        # Both with explicit ev_title and relying on normalized record's event_title:
        classified = classify_race("kalshi", m)
        assert classified == "house", f"Validation-time classification got '{classified}', expected 'house'"

    def test_synthetic_kalshi_event_fetch_save_validation_agreement(self, tmp_path: Any) -> None:
        """Fetch/save classification disagreement regression:
        Injected Kalshi requester returning KXSENATEWIN-26, title '2026 U.S. Senate elections',
        sub_title 'Texas', nested market 'Will Republicans win the Senate race in 2026?'.
        Must store race_type='senate' and match reclassification after round-trip serialization.
        """
        def mock_requester(host: str, path: str) -> tuple[int, bytes]:
            data = {
                "events": [{
                    "series_ticker": "KXSENATEWIN",
                    "event_ticker": "KXSENATEWIN-26",
                    "title": "2026 U.S. Senate elections",
                    "sub_title": "Texas",
                    "markets": [{
                        "ticker": "KXSENATEWIN-26-CANDIDATE",
                        "title": "Will Republicans win the Senate race in 2026?",
                        "status": "open",
                    }]
                }],
                "cursor": ""
            }
            return 200, json.dumps(data).encode("utf-8")

        host, markets, _ex = fetch_kalshi_markets(RateLimiter(0), requester=mock_requester)
        assert len(markets) == 1
        fetched_rec = markets[0]
        # At fetch time, must classify as senate (not control)
        assert fetched_rec["race_type"] == "senate", (
            f"Expected fetch-time race_type 'senate', got '{fetched_rec['race_type']}'"
        )

        # Real fetch path through JSON serialization/reload
        dump_path = tmp_path / "venue_markets_latest.json"
        payload = {
            "fetched_at": "2026-10-10T12:00:00+00:00",
            "kalshi_host": host,
            "polymarket_count": 0,
            "kalshi_count": len(markets),
            "polymarket": [],
            "kalshi": markets,
        }
        with open(dump_path, "w", encoding="utf-8") as f:
            json.dump(payload, f)

        with open(dump_path, "r", encoding="utf-8") as f:
            loaded = json.load(f)

        loaded_rec = loaded["kalshi"][0]
        assert loaded_rec["race_type"] == "senate"
        recalc_type = classify_race("kalshi", loaded_rec)
        assert recalc_type == "senate", f"Reclassified race_type is '{recalc_type}', expected 'senate'"
        assert loaded_rec["race_type"] == recalc_type

        # State extraction on loaded record
        st = extract_race_state("kalshi", loaded_rec, "senate")
        assert st == "texas", f"Expected state 'texas', got '{st}'"

    def test_primary_office_subtitle_lt_gov_excluded_at_ingestion(self) -> None:
        """Primary-office subtitle 'Lieutenant Governor' must be excluded during ingestion,
        without rejecting ordinary Governor contracts whose background rules mention Lieutenant Governor.
        """
        # Primary contest subtitle is Lieutenant Governor
        lt_gov_m = {
            "ticker": "KXGENERIC-26-CANDIDATE",
            "series_ticker": "KXGENERIC",
            "title": "Will Candidate win the election in the first round?",
            "subtitle": "Lieutenant Governor",
            "status": "open",
        }
        lt_gov_ev = {
            "event_ticker": "KXGENERIC-26",
            "series_ticker": "KXGENERIC",
            "title": "Georgia General Election 2026",
            "sub_title": "Lieutenant Governor",
        }
        eligible, reason = is_2026_kalshi_market(lt_gov_m, lt_gov_ev)
        assert not eligible, "Primary-office subtitle 'Lieutenant Governor' was improperly accepted"
        assert reason == "lieutenant_governor_contest"

        # Ordinary Governor with background rules mentioning Lieutenant Governor
        gov_m = {
            "ticker": "KXGOV-26-CANDIDATE",
            "series_ticker": "KXGOV",
            "title": "Who will win the 2026 Georgia governor race?",
            "subtitle": "Brian Kemp",
            "status": "open",
            "rules_primary": "Resolves for governor. If the governor resigns, the lieutenant governor succeeds them.",
        }
        gov_ev = {
            "event_ticker": "KXGOV-26",
            "series_ticker": "KXGOV",
            "title": "Georgia Governor 2026",
            "sub_title": "Governor",
        }
        g_eligible, g_reason = is_2026_kalshi_market(gov_m, gov_ev)
        assert g_eligible, f"Ordinary Governor was improperly rejected: {g_reason}"

    def test_round_trip_retains_race_type_and_state(self, tmp_path: Any) -> None:
        sample_records: list[dict[str, Any]] = [
            {
                "venue": "polymarket",
                "market_id": "629337",
                "question": "Will the Democrats win the Georgia governor race in 2026?",
                "slug": "georgia-governor-race-2026",
                "event_title": "Georgia Governor Race 2026",
                "race_type": "governor",
            },
            {
                "venue": "kalshi",
                "market_ticker": "SENATETX-26-R",
                "series_ticker": "SENATETX",
                "title": "Will Republicans win the Senate race in Texas?",
                "subtitle": "Ted Cruz",
                "event_title": "Texas Senate 2026",
                "race_type": "senate",
            },
            {
                "venue": "kalshi",
                "market_ticker": "CONTROLH-2026-D",
                "series_ticker": "CONTROLH",
                "title": "Will Democrats win the House in 2026?",
                "subtitle": "Democratic Party",
                "event_title": "House Control 2026",
                "race_type": "control",
            },
            {
                "venue": "kalshi",
                "market_ticker": "KXLAHOUSE1R-26NOV03-LA01",
                "series_ticker": "KXLAHOUSE1R",
                "title": "Will anyone win the 2026 Louisiana 1st Congressional District open primary outright?",
                "subtitle": "LA-01",
                "event_title": "Louisiana House District 1",
                "race_type": "house",
            },
            {
                "venue": "kalshi",
                "market_ticker": "KXHOUSEWA8-26-D",
                "series_ticker": "KXHOUSEWA8",
                "title": "Will a Democrat win the House race for WA-08?",
                "subtitle": "WA-08",
                "event_title": "Washington 8th Congressional District 2026",
                "race_type": "house",
            },
            {
                "venue": "kalshi",
                "market_ticker": "HOUSEAKAL-26-D",
                "series_ticker": "HOUSEAKAL",
                "title": "Will Democratic win the House race for AK-AL?",
                "subtitle": "AK-AL",
                "event_title": "Alaska At-Large Congressional District 2026",
                "race_type": "house",
            },
        ]

        # Simulate save to JSON
        json_path = tmp_path / "test_round_trip.json"
        with open(json_path, "w", encoding="utf-8") as f:
            json.dump({"markets": sample_records}, f)

        # Load back from JSON
        with open(json_path, "r", encoding="utf-8") as f:
            loaded = json.load(f)["markets"]

        for rec in loaded:
            venue = rec["venue"]
            recalc_type = classify_race(venue, rec)
            assert recalc_type == rec["race_type"], (
                f"Round-trip classification mismatch for {rec.get('market_ticker') or rec.get('market_id')}: "
                f"saved '{rec['race_type']}' != recalc '{recalc_type}'"
            )
            if recalc_type in ("senate", "governor"):
                st = extract_race_state(venue, rec, recalc_type)
                assert st is not None, f"State extraction failed on reloaded record {rec}"
            elif recalc_type == "house":
                # extract_race_state only scopes to senate and governor; house returns None
                st = extract_race_state(venue, rec, recalc_type)
                assert st is None, f"State extraction for house record should return None, got '{st}'"


# =====================================================================
# Defect 6: Windows CP1252 validation crash
# =====================================================================

class TestWindowsCP1252ValidationOutput:
    """Diagnostic text containing unencodable Unicode (e.g. Romanian/foreign names)
    must not crash under a strict CP1252 output stream, while keeping JSON UTF-8.
    """

    def test_diagnostic_output_strict_cp1252(self, tmp_path: Any, monkeypatch: Any) -> None:
        # Create a test dataset with Unicode in exclusion samples that would crash cp1252
        unicode_str = "Will Brazil President be Fl\u0103vio Bolsonaro? \u4e01\u859b\u7965"
        payload = {
            "fetched_at": "2026-10-10T12:00:00+00:00",
            "kalshi_host": "api.elections.kalshi.com",
            "polymarket_count": 0,
            "kalshi_count": 0,
            "exclusion_samples": {
                "kalshi": {
                    "foreign_election": [unicode_str],
                }
            },
            "polymarket": [],
            "kalshi": [],
        }

        latest_path = tmp_path / "venue_markets_latest.json"
        with open(latest_path, "w", encoding="utf-8") as f:
            json.dump(payload, f)

        # Create a strict CP1252 buffer for stdout and stderr
        raw_out = io.BytesIO()
        strict_stdout = io.TextIOWrapper(raw_out, encoding="cp1252", errors="strict")
        raw_err = io.BytesIO()
        strict_stderr = io.TextIOWrapper(raw_err, encoding="cp1252", errors="strict")

        monkeypatch.setattr(sys, "stdout", strict_stdout)
        monkeypatch.setattr(sys, "stderr", strict_stderr)

        # Must not raise UnicodeEncodeError, and should exit with code 1 (since counts < 100)
        exit_code = run_validation(latest_path)
        assert exit_code != 0, "Validation should have exited nonzero due to empty markets"

        # Verify JSON file remains untouched UTF-8
        with open(latest_path, "r", encoding="utf-8") as f:
            data = json.load(f)
        assert data["exclusion_samples"]["kalshi"]["foreign_election"][0] == unicode_str

    def test_diagnostic_output_utf8(self, tmp_path: Any, monkeypatch: Any) -> None:
        unicode_str = "Will Brazil President be Fl\u0103vio Bolsonaro? \u4e01\u859b\u7965"
        payload = {
            "fetched_at": "2026-10-10T12:00:00+00:00",
            "kalshi_host": "api.elections.kalshi.com",
            "polymarket_count": 0,
            "kalshi_count": 0,
            "exclusion_samples": {
                "kalshi": {
                    "foreign_election": [unicode_str],
                }
            },
            "polymarket": [],
            "kalshi": [],
        }

        latest_path = tmp_path / "venue_markets_latest.json"
        with open(latest_path, "w", encoding="utf-8") as f:
            json.dump(payload, f)

        raw_out = io.BytesIO()
        utf8_stdout = io.TextIOWrapper(raw_out, encoding="utf-8", errors="strict")
        raw_err = io.BytesIO()
        utf8_stderr = io.TextIOWrapper(raw_err, encoding="utf-8", errors="strict")

        monkeypatch.setattr(sys, "stdout", utf8_stdout)
        monkeypatch.setattr(sys, "stderr", utf8_stderr)

        exit_code = run_validation(latest_path)
        assert exit_code != 0


# =====================================================================
# Defect 7: Candidate/outcome subtitle leaking into jurisdiction evidence
# =====================================================================

class TestCandidateSubtitleSeparationFromJurisdiction:
    """Candidate names and outcome labels (e.g. 'John Welsh', 'John Washington')
    must not be treated as jurisdiction, foreign-country, or state evidence.
    Targeted exclusion for primary-office Lieutenant Governor subtitle must be preserved.
    """

    def test_texas_governor_candidate_welsh_eligible(self) -> None:
        """Texas Governor with candidate 'John Welsh' must not be rejected as conflicting_jurisdiction."""
        m = {
            "ticker": "GOVPARTYTX-26-D",
            "series_ticker": "GOVPARTYTX",
            "title": "Will the Democratic candidate win the 2026 Texas governor election?",
            "yes_sub_title": "John Welsh",
            "status": "open",
        }
        ev = {
            "event_ticker": "GOVPARTYTX-26",
            "series_ticker": "GOVPARTYTX",
            "title": "Texas Governor 2026",
        }
        eligible, reason = is_2026_kalshi_market(m, ev)
        assert eligible, f"Texas Governor with candidate John Welsh falsely rejected: {reason}"
        assert reason is None

    def test_texas_senate_candidate_washington_eligible_and_texas_state(self) -> None:
        """Texas Senate with candidate 'John Washington' must not be rejected as conflicting_state_context
        and must extract state 'texas'.
        """
        m = {
            "ticker": "SENATETX-26-D",
            "series_ticker": "SENATETX",
            "title": "Will the Democratic candidate win the 2026 Texas U.S. Senate election?",
            "yes_sub_title": "John Washington",
            "status": "open",
        }
        ev = {
            "event_ticker": "SENATETX-26",
            "series_ticker": "SENATETX",
            "title": "Texas Senate 2026",
        }
        eligible, reason = is_2026_kalshi_market(m, ev)
        assert eligible, f"Texas Senate with candidate John Washington falsely rejected: {reason}"
        assert reason is None
        st = extract_race_state("kalshi", m, "senate")
        assert st == "texas", f"Expected state 'texas', got '{st}'"

    def test_candidate_subtitle_offline_fetch_and_json_reload(self, tmp_path: Any) -> None:
        """Test both records through offline fetch and JSON reload to verify end-to-end preservation."""
        def mock_requester(host: str, path: str) -> tuple[int, bytes]:
            data = {
                "events": [
                    {
                        "series_ticker": "GOVPARTYTX",
                        "event_ticker": "GOVPARTYTX-26",
                        "title": "Texas Governor 2026",
                        "markets": [{
                            "ticker": "GOVPARTYTX-26-D",
                            "series_ticker": "GOVPARTYTX",
                            "title": "Will the Democratic candidate win the 2026 Texas governor election?",
                            "yes_sub_title": "John Welsh",
                            "status": "open",
                        }]
                    },
                    {
                        "series_ticker": "SENATETX",
                        "event_ticker": "SENATETX-26",
                        "title": "Texas Senate 2026",
                        "markets": [{
                            "ticker": "SENATETX-26-D",
                            "series_ticker": "SENATETX",
                            "title": "Will the Democratic candidate win the 2026 Texas U.S. Senate election?",
                            "yes_sub_title": "John Washington",
                            "status": "open",
                        }]
                    }
                ],
                "cursor": ""
            }
            return 200, json.dumps(data).encode("utf-8")

        host, markets, _ex = fetch_kalshi_markets(RateLimiter(0), requester=mock_requester)
        assert len(markets) == 2, f"Expected 2 eligible markets, got {len(markets)}"

        dump_path = tmp_path / "venue_markets_latest.json"
        payload = {
            "fetched_at": "2026-10-10T12:00:00+00:00",
            "kalshi_host": host,
            "polymarket_count": 0,
            "kalshi_count": len(markets),
            "polymarket": [],
            "kalshi": markets,
        }
        with open(dump_path, "w", encoding="utf-8") as f:
            json.dump(payload, f)

        with open(dump_path, "r", encoding="utf-8") as f:
            loaded = json.load(f)["kalshi"]

        by_ticker = {m["market_ticker"]: m for m in loaded}
        gov_rec = by_ticker["GOVPARTYTX-26-D"]
        sen_rec = by_ticker["SENATETX-26-D"]

        assert gov_rec["race_type"] == "governor"
        assert classify_race("kalshi", gov_rec) == "governor"
        assert extract_race_state("kalshi", gov_rec, "governor") == "texas"

        assert sen_rec["race_type"] == "senate"
        assert classify_race("kalshi", sen_rec) == "senate"
        assert extract_race_state("kalshi", sen_rec, "senate") == "texas"


class TestCandidateLabelRolesAndFederalWording:
    """Outcome labels keep their role even if they resemble jurisdictions."""

    @pytest.mark.parametrize("label", ["Washington", "Virginia", "Georgia"])
    def test_state_named_candidate_label_does_not_reject_texas(self, label: str) -> None:
        market = {
            "ticker": "SENATETX-26-D",
            "series_ticker": "SENATETX",
            "title": "Will the Democratic candidate win the 2026 Texas U.S. Senate election?",
            "yes_sub_title": label,
            "status": "open",
        }
        eligible, reason = is_2026_kalshi_market(market, {"title": "Texas Senate 2026"})
        assert eligible, reason
        normalized = {
            "market_ticker": market["ticker"],
            "series_ticker": market["series_ticker"],
            "title": market["title"],
            "subtitle": label,
            "event_title": "Texas Senate 2026",
        }
        assert classify_race("kalshi", normalized) == "senate"
        assert extract_race_state("kalshi", normalized, "senate") == "texas"

    def test_inherited_event_state_survives_state_named_candidate(self) -> None:
        event = {
            "series_ticker": "KXSENATEWIN",
            "event_ticker": "KXSENATEWIN-26",
            "title": "2026 U.S. Senate elections",
            "sub_title": "Texas",
            "markets": [{
                "ticker": "KXSENATEWIN-26-CANDIDATE",
                "title": "Will the candidate win the Senate race in 2026?",
                "yes_sub_title": "Washington",
                "status": "open",
            }],
        }

        def requester(_host: str, _path: str) -> tuple[int, bytes]:
            return 200, json.dumps({"events": [event], "cursor": ""}).encode("utf-8")

        _host, markets, exclusions = fetch_kalshi_markets(RateLimiter(0), requester=requester)
        assert exclusions == {}
        assert len(markets) == 1
        record = json.loads(json.dumps(markets[0]))
        assert record["subtitle"] == "Washington"
        assert record["race_type"] == classify_race("kalshi", record) == "senate"
        assert extract_race_state("kalshi", record, "senate") == "texas"

    def test_candidate_label_alone_cannot_fabricate_state(self) -> None:
        record = {
            "market_ticker": "KXSENATEODDS-26-CANDIDATE",
            "series_ticker": "KXSENATEODDS",
            "title": "Will the 2026 U.S. Senate election be close?",
            "subtitle": "Washington",
        }
        assert extract_race_state("kalshi", record, "senate") is None

    @pytest.mark.parametrize("question", [
        "Will Democrats win at least 15 Texas House of Representatives seats in the 2026 U.S. House elections?",
        "Will Democrats win at least 15 Texas House of Representatives seats in the 2026 United States House elections?",
        "Will Democrats win at least 15 Texas House of Representatives seats in the 2026 federal House elections?",
        "Will Republicans hold both Texas Senate seats after the 2026 U.S. Senate elections?",
    ])
    def test_explicit_federal_chamber_wording_is_preserved(self, question: str) -> None:
        eligible, reason = is_2026_polymarket_market({
            "active": True,
            "closed": False,
            "question": question,
        })
        assert eligible, reason

# =====================================================================
# Defect 9: State legislative House chamber contracts leak into federal House universe (S1-T5-F7)
# =====================================================================

class TestStateLegislativeHouseChamberAdmission:
    """State legislative House chamber contracts (e.g. KXTXHOUSE 'Texas House of
    Representatives winner') must not be admitted as federal House references,
    while federal district races (HOUSETX15, KXHOUSERACE), genuine federal House
    seat count markets (KXTXHOUSEDEMSEATS, KXCAHOUSEDEM), and federal wording
    must be preserved.
    """

    @pytest.mark.parametrize("suffix,party", [
        ("R", "Republican"),
        ("D", "Democratic"),
    ])
    def test_recorded_kxtxhouse_markets_excluded(self, suffix: str, party: str) -> None:
        """RECORDED: KXTXHOUSE-26-R and KXTXHOUSE-26-D from 0035_kalshi_standard.json
        must be excluded as state legislative contests, not admitted as federal House.
        """
        market = {
            "ticker": f"KXTXHOUSE-26-{suffix}",
            "title": "Who will win the Texas House of Representatives?",
            "status": "active",
            "rules_primary": f"If the {party} Party wins the Texas House of Representatives in 2026, then the market resolves to Yes.",
            "rules_secondary": "If both parties are tied, then the market resolves to No.",
            "yes_sub_title": f"{party} Party",
        }
        event = {
            "event_ticker": "KXTXHOUSE-26",
            "series_ticker": "KXTXHOUSE",
            "title": "Texas House of Representatives winner",
            "sub_title": "In 2026",
        }
        eligible, reason = is_2026_kalshi_market(market, event)
        assert not eligible, f"KXTXHOUSE {suffix} was improperly admitted"
        assert reason == "state_legislative_contest"

        # Classification must not be federal 'house'
        race_type = classify_race("kalshi", market, event_title=event["title"])
        assert race_type == "other", f"KXTXHOUSE classified as '{race_type}' instead of 'other'"
        # Without event_title fallback
        assert classify_race("kalshi", market) == "other"

    @pytest.mark.parametrize("ticker,series,title,event_title", [
        # SYNTHETIC CONTROL: Unprefixed TXHOUSE series
        ("TXHOUSE-26-R", "TXHOUSE", "Who will win the Texas House of Representatives?", "Texas House of Representatives winner"),
        # RECORDED / SYNTHETIC: KXPAHOUSE state legislative lower chamber
        ("KXPAHOUSE-262-GOP", "KXPAHOUSE", "Who will win the Pennsylvania State House election?", "Pennsylvania State House winner?"),
        # SYNTHETIC CONTROL: Florida House of Representatives state chamber
        ("KXFLHOUSE-26-R", "KXFLHOUSE", "Who will win the Florida House of Representatives?", "Florida House of Representatives winner"),
        # SYNTHETIC CONTROL: Ohio House of Representatives state chamber
        ("KXOHHOUSE-26-R", "KXOHHOUSE", "Who will win the Ohio House of Representatives?", "Ohio House of Representatives winner"),
    ])
    def test_kalshi_state_chamber_series_and_titles_excluded(
        self, ticker: str, series: str, title: str, event_title: str
    ) -> None:
        market = {
            "ticker": ticker,
            "title": title,
            "status": "active",
        }
        event = {
            "event_ticker": series,
            "series_ticker": series,
            "title": event_title,
            "sub_title": "In 2026",
        }
        eligible, reason = is_2026_kalshi_market(market, event)
        assert not eligible, f"State chamber market {ticker} was improperly admitted"
        assert reason == "state_legislative_contest"
        assert classify_race("kalshi", market, event_title=event_title) == "other"

    @pytest.mark.parametrize("question,slug", [
        # SYNTHETIC CONTROL: Polymarket state House of Representatives contests
        ("Who will win the Texas House of Representatives in 2026?", "who-will-win-the-texas-house-of-representatives-in-2026"),
        ("Who will win the Florida House of Representatives in 2026?", "who-will-win-the-florida-house-of-representatives-in-2026"),
        ("Who will win the Ohio House of Representatives in 2026?", "who-will-win-the-ohio-house-of-representatives-in-2026"),
    ])
    def test_polymarket_state_house_of_reps_excluded(self, question: str, slug: str) -> None:
        item = {
            "active": True,
            "closed": False,
            "question": question,
            "slug": slug,
        }
        eligible, reason = is_2026_polymarket_market(item, is_tag_102289=True)
        assert not eligible, f"Polymarket state chamber was improperly admitted: {question}"
        assert reason == "state_legislative_contest"
        assert classify_race("polymarket", item) == "other"

    def test_federal_seat_count_and_district_boundaries_preserved(self) -> None:
        """Boundary preservation: federal district contracts, KXHOUSERACE, and
        genuine federal House seat count series must remain admitted.
        """
        # RECORDED: KXTXHOUSEDEMSEATS genuine federal seat count
        demseats_m = {
            "ticker": "KXTXHOUSEDEMSEATS-26NOV03-B8",
            "title": "Will Democrats win below 8 seats in 2026 U.S. House of Representatives elections in Texas?",
            "status": "active",
            "rules_primary": "If Democrats win below 8 seats in the 2026 U.S. House of Representatives elections in Texas, then the market resolves to Yes.",
        }
        demseats_ev = {
            "event_ticker": "KXTXHOUSEDEMSEATS",
            "series_ticker": "KXTXHOUSEDEMSEATS",
            "title": "How many House seats will Democrats win in Texas?",
        }
        eligible, reason = is_2026_kalshi_market(demseats_m, demseats_ev)
        assert eligible, f"Federal seat count was improperly excluded: {reason}"
        assert classify_race("kalshi", demseats_m, event_title=demseats_ev["title"]) == "house"

        # RECORDED: KXCAHOUSEDEM genuine federal seat count
        ca_m = {
            "ticker": "KXCAHOUSEDEM-26NOV03-B44",
            "title": "Will Democrats win below 44 seats in California?",
            "status": "active",
        }
        ca_ev = {
            "event_ticker": "KXCAHOUSEDEM",
            "series_ticker": "KXCAHOUSEDEM",
            "title": "How many House seats will Democrats win in California?",
            "sub_title": "In 2026",
        }
        eligible, reason = is_2026_kalshi_market(ca_m, ca_ev)
        assert eligible, f"KXCAHOUSEDEM was improperly excluded: {reason}"
        assert classify_race("kalshi", ca_m, event_title=ca_ev["title"]) == "house"

        # RECORDED: Federal district race mentioning Texas House
        # (must not be excluded by a broad 'Texas House' substring check)
        dist_m = {
            "ticker": "HOUSETX15-26-D",
            "event_ticker": "HOUSETX15-26",
            "title": "Will Democrats win the House race for TX-15?",
            "status": "active",
            "rules_primary": "If the House member sworn in for TX-15 for the term beginning in 2027 is a member of the Democratic Party, then the market resolves to Yes.",
            "rules_secondary": "This market is eligible for accelerated determination after a consensus of media organizations project the winner. See full rules for details.",
        }
        dist_ev = {
            "event_ticker": "HOUSETX15-26",
            "series_ticker": "HOUSETX15",
            "title": "TX-15 House winner?",
            "sub_title": "TX-15",
        }
        eligible, reason = is_2026_kalshi_market(dist_m, dist_ev)
        assert eligible, f"Federal district TX-15 was improperly excluded: {reason}"
        assert classify_race("kalshi", dist_m, event_title=dist_ev["title"]) == "house"

        # SYNTHETIC CONTROL: Question wording with 'Texas House race for TX-15'
        # Broad substring check for 'Texas House' would break this.
        poly_dist = {
            "active": True,
            "closed": False,
            "question": "Will Democrats win the Texas House race for TX-15 in 2026?",
            "slug": "texas-house-race-tx-15-2026",
        }
        eligible, reason = is_2026_polymarket_market(poly_dist, is_tag_102289=True)
        assert eligible, f"District question was improperly excluded: {reason}"
        assert classify_race("polymarket", poly_dist) == "house"

    @pytest.mark.parametrize("parent_title", [
        # Polymarket state chamber with unrelated federal House parent event title
        "2026 U.S. House elections",
        # Polymarket state chamber with unrelated seat-count parent event title
        "How many seats in the 2026 election?",
    ])
    def test_polymarket_state_house_of_reps_parent_event_wording_excluded(self, parent_title: str) -> None:
        """Unrelated parent event wording ('2026 U.S. House elections' or
        'How many seats in the 2026 election?') must not cancel a state-chamber
        exclusion on the actual contest proposition.
        """
        item = {
            "active": True,
            "closed": False,
            "question": "Who will win the Texas House of Representatives in 2026?",
            "slug": "who-will-win-the-texas-house-of-representatives-in-2026",
        }
        parent_event = {"title": parent_title}
        eligible, reason = is_2026_polymarket_market(item, parent_event=parent_event, is_tag_102289=True)
        assert not eligible, f"State chamber market was improperly admitted with parent '{parent_title}': {reason}"
        assert reason == "state_legislative_contest"
        assert classify_race("polymarket", item, event_title=parent_title) == "other"

    @pytest.mark.parametrize("question,slug", [
        # Ambiguous questions combining state chamber proposition with incidental federal wording
        (
            "Who will win the Texas House of Representatives in the 2026 congressional elections?",
            "who-will-win-the-texas-house-of-representatives-in-the-2026-congressional-elections",
        ),
        (
            "Who will win the Texas House of Representatives in the 2026 U.S. House elections?",
            "who-will-win-the-texas-house-of-representatives-in-the-2026-u-s-house-elections",
        ),
        (
            "Who will win the Texas House of Representatives in the 2026 federal midterms?",
            "who-will-win-the-texas-house-of-representatives-in-the-2026-federal-midterms",
        ),
    ])
    def test_polymarket_state_chamber_incidental_federal_wording_fails_closed(
        self, question: str, slug: str
    ) -> None:
        """Ambiguous identity combining a state chamber proposition with incidental
        federal wording must fail closed as state_legislative_contest and 'other'.
        """
        item = {
            "active": True,
            "closed": False,
            "question": question,
            "slug": slug,
        }
        eligible, reason = is_2026_polymarket_market(item, is_tag_102289=True)
        assert not eligible, f"Ambiguous state chamber market was improperly admitted: {question}"
        assert reason == "state_legislative_contest"
        assert classify_race("polymarket", item) == "other"

    def test_polymarket_texas_house_seats_in_us_house_elections_preserved(self) -> None:
        """A genuine federal seat-count question specifying 'Texas House of Representatives
        seats in the 2026 U.S. House elections' must remain admitted as a federal reference.
        """
        item = {
            "active": True,
            "closed": False,
            "question": "Texas House of Representatives seats in the 2026 U.S. House elections",
            "slug": "texas-house-of-representatives-seats-in-the-2026-u-s-house-elections",
        }
        eligible, reason = is_2026_polymarket_market(item, is_tag_102289=True)
        assert eligible, f"Federal seat-count market was improperly excluded: {reason}"
        assert classify_race("polymarket", item) == "house"

    def test_polymarket_state_chamber_slug_containing_district_excluded(self) -> None:
        """State-chamber question 'Who will win the Texas House of Representatives in 2026?'
        with a slug containing 'tx-15' must not be admitted.
        A district code in the slug must not override an explicit state-chamber winner proposition.
        Must be excluded as state_legislative_contest and classified other.
        """
        item = {
            "active": True,
            "closed": False,
            "question": "Who will win the Texas House of Representatives in 2026?",
            "slug": "who-will-win-the-texas-house-of-representatives-in-2026-tx-15",
        }
        eligible, reason = is_2026_polymarket_market(item, is_tag_102289=True)
        assert not eligible, f"State-chamber market with district slug was improperly admitted: {reason}"
        assert reason == "state_legislative_contest"
        assert classify_race("polymarket", item) == "other"

    def test_polymarket_state_chamber_incidental_district_appended_excluded(self) -> None:
        """State-chamber question with 'TX-15 is also on the ballot' appended must
        not be admitted. A district code mentioned elsewhere must not override
        an explicit state-chamber winner proposition.
        Must be excluded as state_legislative_contest and classified other.
        """
        item = {
            "active": True,
            "closed": False,
            "question": "Who will win the Texas House of Representatives in 2026? TX-15 is also on the ballot",
            "slug": "who-will-win-the-texas-house-of-representatives-in-2026",
        }
        eligible, reason = is_2026_polymarket_market(item, is_tag_102289=True)
        assert not eligible, f"State-chamber market with incidental district text was improperly admitted: {reason}"
        assert reason == "state_legislative_contest"
        assert classify_race("polymarket", item) == "other"

    def test_polymarket_state_chamber_how_many_seats_parent_event_excluded(self) -> None:
        """Question 'How many seats will Democrats win in the Texas House of Representatives in 2026?'
        with parent event title '2026 U.S. House elections' must not be admitted.
        Parent event federal wording cannot convert a question about the state chamber's seats
        into a federal House contest.
        Must be excluded as state_legislative_contest and classified other.
        """
        item = {
            "active": True,
            "closed": False,
            "question": "How many seats will Democrats win in the Texas House of Representatives in 2026?",
            "slug": "how-many-seats-will-democrats-win-in-the-texas-house-of-representatives-in-2026",
        }
        parent_event = {"title": "2026 U.S. House elections"}
        eligible, reason = is_2026_polymarket_market(item, parent_event=parent_event, is_tag_102289=True)
        assert not eligible, f"State-chamber seats market was improperly admitted: {reason}"
        assert reason == "state_legislative_contest"
        assert classify_race("polymarket", item, event_title=parent_event["title"]) == "other"

    def test_polymarket_state_chamber_specific_seats_parent_event_excluded(self) -> None:
        """Question 'Will Democrats win 51 seats in the Texas House of Representatives in 2026?'
        with parent event title '2026 U.S. House elections' must not be admitted.
        Parent event federal wording cannot convert a question about the state chamber's seats
        into a federal House contest.
        Must be excluded as state_legislative_contest and classified other.
        """
        item = {
            "active": True,
            "closed": False,
            "question": "Will Democrats win 51 seats in the Texas House of Representatives in 2026?",
            "slug": "will-democrats-win-51-seats-in-the-texas-house-of-representatives-in-2026",
        }
        parent_event = {"title": "2026 U.S. House elections"}
        eligible, reason = is_2026_polymarket_market(item, parent_event=parent_event, is_tag_102289=True)
        assert not eligible, f"State-chamber specific seats market was improperly admitted: {reason}"
        assert reason == "state_legislative_contest"
        assert classify_race("polymarket", item, event_title=parent_event["title"]) == "other"

    def test_polymarket_state_chamber_seats_incidental_district_target_excluded(self) -> None:
        """Question 'How many seats will Democrats win in the Texas House of Representatives in 2026? TX-15 House race also on ballot'
        with matching state-chamber slug must not be admitted.
        An incidental district target cannot convert a question about the state chamber's seats
        into a federal House contest.
        Must be excluded as state_legislative_contest and classified other.
        """
        item = {
            "active": True,
            "closed": False,
            "question": "How many seats will Democrats win in the Texas House of Representatives in 2026? TX-15 House race also on ballot",
            "slug": "how-many-seats-will-democrats-win-in-the-texas-house-of-representatives-in-2026",
        }
        eligible, reason = is_2026_polymarket_market(item, is_tag_102289=True)
        assert not eligible, f"State-chamber seats with incidental district target was improperly admitted: {reason}"
        assert reason == "state_legislative_contest"
        assert classify_race("polymarket", item) == "other"
