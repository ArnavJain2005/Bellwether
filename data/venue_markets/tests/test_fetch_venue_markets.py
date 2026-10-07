"""Offline regression and unit tests for data/venue_markets/fetch_venue_markets.py."""

from __future__ import annotations

import io
import json
import urllib.error
import urllib.request
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from data.venue_markets.fetch_venue_markets import (
    FetchError,
    RateLimiter,
    classify_race,
    extract_race_state,
    extract_states_from_text,
    fetch_kalshi_markets,
    fetch_polymarket_markets,
    is_2026_kalshi_market,
    is_2026_polymarket_market,
    main,
)

# =====================================================================
# 1. State Overlap & Multi-State Classification Tests
# =====================================================================

def test_west_virginia_not_misidentified_as_virginia() -> None:
    """West Virginia must be matched as 'west virginia', not 'virginia'."""
    wv_text = "Which party will win the West Virginia Senate election?"
    states = extract_states_from_text(wv_text)
    assert states == ["west virginia"]

    market = {
        "question": "Which party will win the West Virginia Senate election?",
        "slug": "which-party-will-win-the-west-virginia-senate-election",
    }
    extracted = extract_race_state("polymarket", market, "senate")
    assert extracted == "west virginia"


def test_virginia_matches_correctly() -> None:
    """Virginia alone must match 'virginia'."""
    va_text = "Which party will win the Virginia Senate election?"
    states = extract_states_from_text(va_text)
    assert states == ["virginia"]

    market = {
        "question": "Which party will win the Virginia Senate election?",
        "slug": "which-party-will-win-the-virginia-senate-election",
    }
    extracted = extract_race_state("polymarket", market, "senate")
    assert extracted == "virginia"


def test_multi_state_comparison_rejected_from_single_race() -> None:
    """Comparisons between two states must return None and classify as 'other'."""
    comp_text = "Closer Senate Race: Florida or South Carolina?"
    states = extract_states_from_text(comp_text)
    assert len(states) == 2
    assert "florida" in states
    assert "south carolina" in states

    market = {
        "question": "Closer Senate Race: Florida or South Carolina?",
        "slug": "closer-senate-race-florida-or-south-carolina",
    }
    extracted = extract_race_state("polymarket", market, "senate")
    assert extracted is None

    classified = classify_race("polymarket", market)
    assert classified == "other"


def test_state_abbreviation_inference_no_ordinary_words() -> None:
    """extract_race_state must not infer a state from ordinary words like 'in'."""
    market = {
        "question": "Will Democrats win the Senate in 2026?",
        "slug": "democrats-win-senate-in-2026",
    }
    # 'in' should not be extracted as Indiana (IN) unless it is a structured identifier
    extracted = extract_race_state("polymarket", market, "senate")
    assert extracted is None

    # explicit valid abbreviation test
    kalshi_market = {
        "market_ticker": "SENATEAZ-26-D",
        "series_ticker": "SENATEAZ",
    }
    extracted_kalshi = extract_race_state("kalshi", kalshi_market, "senate")
    assert extracted_kalshi == "arizona"


# =====================================================================
# 2. Market Eligibility Tests
# =====================================================================

def test_polymarket_non_2026_cycle_excluded() -> None:
    """Polymarket markets for 2028 or non-midterm events must be excluded."""
    # Active/open Brazilian presidential election in 2026
    brazil_2026 = {
        "question": "Brazilian Presidential Election in 2026",
        "slug": "brazilian-presidential-election-2026",
        "closed": False,
        "active": True,
    }
    is_eligible, reason = is_2026_polymarket_market(brazil_2026)
    assert not is_eligible
    assert reason in ("unrelated_political_event", "lacks_election_evidence", "foreign_election")

    # Active/open "Who controls the Senate after midterms?" with NO election year
    senate_midterms = {
        "question": "Who controls the Senate after midterms?",
        "slug": "who-controls-senate-after-midterms",
        "closed": False,
        "active": True,
    }
    is_eligible, reason = is_2026_polymarket_market(senate_midterms)
    assert not is_eligible

    aoc_2028 = {
        "question": "Will Alexandria Ocasio-Cortez run for Senate in 2028?",
        "slug": "will-alexandria-ocasio-cortez-run-for-senate-in-2028",
        "description": "Resolves according to whether AOC runs for Senate in 2028.",
        "closed": False,
        "active": True,
    }
    is_eligible, reason = is_2026_polymarket_market(aoc_2028)
    assert not is_eligible
    assert reason == "non_2026_cycle"


def test_polymarket_closed_or_inactive_excluded() -> None:
    """Closed or inactive Polymarket markets must be rejected."""
    closed_market = {
        "question": "Will Democrats control the Senate in 2026?",
        "closed": True,
        "active": True,
    }
    is_eligible, reason = is_2026_polymarket_market(closed_market)
    assert not is_eligible
    assert reason == "market_closed"


def test_kalshi_eligibility_checks() -> None:
    """Kalshi bets on foreign elections or missing status must be excluded."""
    uk_ev = {"title": "UK General Election", "category": "Elections", "series_ticker": "KXUKGEYEAR"}
    uk_market = {"ticker": "KXUKGEYEAR-26", "status": "open", "title": "UK General Election in 2026"}
    is_eligible, _ = is_2026_kalshi_market(uk_market, uk_ev, is_primary_match=False)
    assert not is_eligible

    # Kalshi: no status
    no_status = {
        "ticker": "SENATEAZ-26-D",
        "title": "2026 Arizona Senate election",
    }
    is_eligible, _ = is_2026_kalshi_market(no_status, {"title": "Arizona Senate"}, is_primary_match=True)
    assert not is_eligible

    # Kalshi: active "2026 NBA Finals winner", category Sports
    nba = {
        "ticker": "NBA-26",
        "status": "active",
        "title": "2026 NBA Finals winner",
    }
    is_eligible, _ = is_2026_kalshi_market(nba, {"category": "Sports"}, is_primary_match=False)
    assert not is_eligible

    # Genuine 2026 rules mentioning another year as background
    genuine = {
        "ticker": "SENATE-26",
        "status": "open",
        "title": "2026 Senate control",
        "rules_primary": "If the Democrats hold the Senate in 2026. (Unlike in 2028)",
    }
    is_eligible, _ = is_2026_kalshi_market(genuine, {}, is_primary_match=True)
    assert is_eligible


# =====================================================================
# 3. Polymarket Outcome-Token Pairing Tests
# =====================================================================

def test_polymarket_token_mapping_validation() -> None:
    """Parallel outcomes and CLOB tokens must match and not be repaired."""
    limiter = RateLimiter(interval=0.0)

    class FakeOpener:
        def __init__(self, data: dict) -> None:
            self.data = data

        def open(self, req: urllib.request.Request) -> io.BytesIO:
            return io.BytesIO(json.dumps(self.data).encode("utf-8"))

    # Valid market
    valid_items = {
        "data": [{
            "condition_id": "1001",
            "question": "Will Democrats win Georgia Senate in 2026?",
            "market_slug": "democrats-georgia-senate-2026",
            "closed": False,
            "active": True,
            "tokens": [
                {"outcome": "Yes", "token_id": "tok_yes_123", "price": 0.60},
                {"outcome": "No", "token_id": "tok_no_456", "price": 0.40}
            ],
            "end_date_iso": "2026-11-04T00:00:00Z",
        }],
        "next_cursor": "LTE="
    }
    markets, _excluded = fetch_polymarket_markets(limiter, opener=FakeOpener(valid_items))
    assert len(markets) == 1
    assert markets[0]["outcome_token_map"] == {"Yes": "tok_yes_123", "No": "tok_no_456"}

    # Invalid token mapping (duplicate labels)
    mismatched_items = {
        "data": [{
            "condition_id": "1002",
            "question": "Will Republicans win Georgia Senate in 2026?",
            "closed": False,
            "active": True,
            "tokens": [
                {"outcome": "Yes", "token_id": "tok_1"},
                {"outcome": "Yes", "token_id": "tok_2"}
            ],
        }],
        "next_cursor": "LTE="
    }
    markets, _excluded = fetch_polymarket_markets(limiter, opener=FakeOpener(mismatched_items))
    assert len(markets) == 0

    # Invalid token mapping (null outcome label) - must reject entirely
    null_outcome_items = {
        "data": [{
            "condition_id": "1003",
            "question": "Will Democrats win Georgia Senate in 2026?",
            "closed": False,
            "active": True,
            "tokens": [
                {"outcome": None, "token_id": "tok_1"},
                {"outcome": "No", "token_id": "tok_2"}
            ],
        }],
        "next_cursor": "LTE="
    }
    markets, _excluded = fetch_polymarket_markets(limiter, opener=FakeOpener(null_outcome_items))
    assert len(markets) == 0


# =====================================================================
# 4. Pagination & Cycle Detection Tests
# =====================================================================

def test_kalshi_pagination_cycle_detection() -> None:
    """Repeated pagination cursor must trigger FetchError to prevent infinite loops."""
    limiter = RateLimiter(interval=0.0)
    seen_calls: list[str] = []

    def mock_requester(host: str, path: str) -> tuple[int, bytes]:
        seen_calls.append(path)
        # Returns cursor 'loop_cursor' repeatedly
        body = json.dumps({
            "events": [{"series_ticker": "CONTROLS", "markets": []}],
            "cursor": "loop_cursor",
        }).encode("utf-8")
        return 200, body

    with pytest.raises(FetchError, match="loop detected"):
        fetch_kalshi_markets(limiter, requester=mock_requester)


def test_polymarket_unexpected_422_raises_fetch_error() -> None:
    """HTTP 422 must raise FetchError."""
    limiter = RateLimiter(interval=0.0)

    class FailingOpener:
        def open(self, req: urllib.request.Request) -> None:
            raise urllib.error.HTTPError(req.full_url, 422, "Unprocessable Entity", hdrs=None, fp=io.BytesIO(b"{}"))

    with pytest.raises(FetchError, match="Polymarket HTTP 422"), patch("time.sleep"):
        fetch_polymarket_markets(limiter, opener=FailingOpener())


# =====================================================================
# 5. Rate Limiting on Fallback & Retries
# =====================================================================

def test_rate_limiter_invoked_on_fallback_and_retries() -> None:
    """Rate limiter wait() must be invoked before EVERY request attempt."""
    mock_limiter = MagicMock(spec=RateLimiter)
    attempt_count = 0

    def mock_requester(host: str, path: str) -> tuple[int, bytes]:
        nonlocal attempt_count
        attempt_count += 1
        if attempt_count == 1:
            raise OSError("Connection reset by peer")
        return 200, json.dumps({"events": [], "cursor": ""}).encode("utf-8")

    with patch("time.sleep"):
        fetch_kalshi_markets(mock_limiter, requester=mock_requester)
    # The rate limiter must have been called for BOTH attempts
    assert attempt_count == 2
    assert mock_limiter.wait.call_count == 2


# =====================================================================
# 6. Fetch-to-Main Preservation Regressions (End-to-End)
# =====================================================================

def test_polymarket_malformed_page_publishes_success(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A malformed polymarket page (data: null) must cause nonzero exit and preserve latest byte-for-byte."""
    latest_file = tmp_path / "venue_markets_latest.json"
    canary_bytes = b'{"version": "1.0", "important_data": true}\n'
    latest_file.write_bytes(canary_bytes)

    # We patch __file__ in the module so main() writes here
    monkeypatch.setattr("data.venue_markets.fetch_venue_markets.__file__", str(tmp_path / "fetch_venue_markets.py"))

    # Mock Polymarket responses
    call_count = 0
    class MalformedOpener:
        def open(self, req: urllib.request.Request) -> io.BytesIO:
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                # Page 1: valid chamber control markets
                payload = {
                    "data": [
                        {
                            "condition_id": "1",
                            "question": "Will Democrats control the Senate in 2026?",
                            "market_slug": "dems-senate-2026",
                            "closed": False,
                            "active": True,
                            "tokens": [{"outcome": "Yes", "token_id": "t1"}, {"outcome": "No", "token_id": "t2"}],
                        },
                        {
                            "condition_id": "2",
                            "question": "Will Democrats control the House in 2026?",
                            "market_slug": "dems-house-2026",
                            "closed": False,
                            "active": True,
                            "tokens": [{"outcome": "Yes", "token_id": "t3"}, {"outcome": "No", "token_id": "t4"}],
                        }
                    ],
                    "next_cursor": "page2"
                }
            else:
                # Page 2: malformed
                payload = {"data": None, "next_cursor": "LTE="}
            return io.BytesIO(json.dumps(payload).encode("utf-8"))

    # Mock Kalshi response (valid)
    def mock_kalshi_requester(host: str, path: str) -> tuple[int, bytes]:
        payload = {
            "events": [
                {
                    "title": "2026 Senate Control",
                    "series_ticker": "CONTROLS",
                    "markets": [{"ticker": "SENATE-26", "status": "open", "title": "2026 Senate Control"}]
                },
                {
                    "title": "2026 House Control",
                    "series_ticker": "CONTROLH",
                    "markets": [{"ticker": "HOUSE-26", "status": "open", "title": "2026 House Control"}]
                }
            ],
            "cursor": ""
        }
        return 200, json.dumps(payload).encode("utf-8")

    # Patch the fetchers to use our mocks
    original_fetch_poly = fetch_polymarket_markets
    original_fetch_kalshi = fetch_kalshi_markets

    def patched_fetch_poly(limiter, base_url="https://clob.polymarket.com/markets", opener=None):
        return original_fetch_poly(limiter, base_url, opener=MalformedOpener())

    def patched_fetch_kalshi(limiter, primary_host="api.elections.kalshi.com", fallback_host="external-api.kalshi.com", requester=None):
        return original_fetch_kalshi(limiter, primary_host, fallback_host, requester=mock_kalshi_requester)

    monkeypatch.setattr("data.venue_markets.fetch_venue_markets.fetch_polymarket_markets", patched_fetch_poly)
    monkeypatch.setattr("data.venue_markets.fetch_venue_markets.fetch_kalshi_markets", patched_fetch_kalshi)
    monkeypatch.setattr("time.sleep", lambda x: None)

    # Execute main. It should exit non-zero since Polymarket threw an exception.
    # main catches the exception and exits. Wait, main() currently catches the exception,
    # prints "FETCH FAILED", and exits 0 naturally, but wait...
    # The prompt says: "The current code exits 0 and overwrites latest... Malformed data must cause nonzero exit and preserve latest byte-for-byte."
    with pytest.raises(SystemExit) as excinfo:
        main()

    assert excinfo.value.code != 0
    assert latest_file.read_bytes() == canary_bytes


def test_kalshi_prematurely_ends_traversal(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Kalshi missing events but valid continuation cursor must not prematurely end."""
    latest_file = tmp_path / "venue_markets_latest.json"
    canary_bytes = b'{"version": "1.0", "important_data": true}\n'
    latest_file.write_bytes(canary_bytes)

    # We patch __file__ in the module so main() writes here
    monkeypatch.setattr("data.venue_markets.fetch_venue_markets.__file__", str(tmp_path / "fetch_venue_markets.py"))

    # Mock Polymarket response (valid)
    class ValidPolyOpener:
        def open(self, req: urllib.request.Request) -> io.BytesIO:
            payload = {
                "data": [
                    {
                        "condition_id": "1",
                        "question": "Will Democrats control the Senate in 2026?",
                        "market_slug": "dems-senate-2026",
                        "closed": False,
                        "active": True,
                        "tokens": [{"outcome": "Yes", "token_id": "t1"}, {"outcome": "No", "token_id": "t2"}],
                    },
                    {
                        "condition_id": "2",
                        "question": "Will Democrats control the House in 2026?",
                        "market_slug": "dems-house-2026",
                        "closed": False,
                        "active": True,
                        "tokens": [{"outcome": "Yes", "token_id": "t3"}, {"outcome": "No", "token_id": "t4"}],
                    }
                ],
                "next_cursor": "LTE="
            }
            return io.BytesIO(json.dumps(payload).encode("utf-8"))

    # Mock Kalshi responses
    call_count = 0
    def mock_kalshi_requester(host: str, path: str) -> tuple[int, bytes]:
        nonlocal call_count
        call_count += 1
        if call_count == 1:
            payload = {
                "events": [],
                "cursor": "page2"
            }
        else:
            payload = {
                "events": [
                    {
                        "title": "2026 Senate Control",
                        "series_ticker": "CONTROLS",
                        "markets": [{"ticker": "SENATE-26", "status": "open", "title": "2026 Senate Control"}]
                    },
                    {
                        "title": "2026 House Control",
                        "series_ticker": "CONTROLH",
                        "markets": [{"ticker": "HOUSE-26", "status": "open", "title": "2026 House Control"}]
                    }
                ],
                "cursor": ""
            }
        return 200, json.dumps(payload).encode("utf-8")

    original_fetch_poly = fetch_polymarket_markets
    original_fetch_kalshi = fetch_kalshi_markets

    def patched_fetch_poly(limiter, base_url="https://clob.polymarket.com/markets", opener=None):
        return original_fetch_poly(limiter, base_url, opener=ValidPolyOpener())

    def patched_fetch_kalshi(limiter, primary_host="api.elections.kalshi.com", fallback_host="external-api.kalshi.com", requester=None):
        return original_fetch_kalshi(limiter, primary_host, fallback_host, requester=mock_kalshi_requester)

    monkeypatch.setattr("data.venue_markets.fetch_venue_markets.fetch_polymarket_markets", patched_fetch_poly)
    monkeypatch.setattr("data.venue_markets.fetch_venue_markets.fetch_kalshi_markets", patched_fetch_kalshi)
    monkeypatch.setattr("time.sleep", lambda x: None)

    # It should succeed completely if Kalshi continues traversing
    # Wait, the problem says "The current `if not new_cursor or not events: break` accepts an empty page despite a continuation cursor."
    # We want it to RAISE an error OR succeed if it's supposed to continue.
    # The instructions say: "Never call that complete: follow a valid continuation with loop protection, or fail explicitly on an inconsistent response. Malformed/missing pagination fields must not silently become EOF. Add a fetch-to-main preservation regression for this case."
    # If Kalshi returns empty events with a cursor, we should fail explicitly, or continue?
    # Let's say if we fail explicitly, main() raises SystemExit and preserves byte-for-byte.
    with pytest.raises(SystemExit) as excinfo:
        main()

    assert excinfo.value.code != 0
    assert latest_file.read_bytes() == canary_bytes
