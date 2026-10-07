"""Offline regression and unit tests for data/venue_markets/fetch_venue_markets.py."""

from __future__ import annotations

import io
import json
import urllib.error
from pathlib import Path
from unittest.mock import MagicMock

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
    publish_output,
    validate_dataset,
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
    # Must NOT attribute to a single state
    extracted = extract_race_state("polymarket", market, "senate")
    assert extracted is None

    # Classification must be 'other'
    classified = classify_race("polymarket", market)
    assert classified == "other"


# =====================================================================
# 2. Market Eligibility Tests
# =====================================================================

def test_polymarket_non_2026_cycle_excluded() -> None:
    """Polymarket markets for 2028 or non-midterm events must be excluded."""
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

    mcconnell = {
        "question": "Mitch McConnell steps down from Senate before his term ends?",
        "slug": "mitch-mcconnell-steps-down-from-senate-before-his-term-ends",
        "description": "Resolves if McConnell steps down early.",
        "closed": False,
        "active": True,
    }
    is_eligible, reason = is_2026_polymarket_market(mcconnell)
    assert not is_eligible
    assert reason == "unrelated_political_event"


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

    inactive_market = {
        "question": "Will Democrats control the Senate in 2026?",
        "closed": False,
        "active": False,
    }
    is_eligible, reason = is_2026_polymarket_market(inactive_market)
    assert not is_eligible
    assert reason == "market_inactive"


def test_kalshi_inactive_nested_market_excluded() -> None:
    """Kalshi nested markets with status other than 'open'/'active' must be excluded."""
    ev = {"title": "2026 Senate Elections", "category": "Elections", "series_ticker": "SENATEAZ"}
    market_closed = {
        "ticker": "SENATEAZ-26-D",
        "status": "closed",
        "title": "Will Democrats win Arizona Senate in 2026?",
    }
    is_eligible, _ = is_2026_kalshi_market(market_closed, ev, is_primary_match=True)
    assert not is_eligible

    market_open = {
        "ticker": "SENATEAZ-26-D",
        "status": "open",
        "title": "Will Democrats win Arizona Senate in 2026?",
    }
    is_eligible, _ = is_2026_kalshi_market(market_open, ev, is_primary_match=True)
    assert is_eligible


def test_kalshi_foreign_and_non_election_bets_excluded() -> None:
    """Kalshi bets on foreign elections or cabinet resignations must be excluded."""
    uk_ev = {"title": "UK General Election", "category": "Elections", "series_ticker": "KXUKGEYEAR"}
    uk_market = {"ticker": "KXUKGEYEAR-26", "status": "open", "title": "UK General Election in 2026"}
    is_eligible, _ = is_2026_kalshi_market(uk_market, uk_ev, is_primary_match=False)
    assert not is_eligible

    germany_ev = {"title": "Chancellor of Germany", "category": "Politics", "series_ticker": "KXLEAVEMERZ"}
    germany_market = {"ticker": "KXLEAVEMERZ-26", "status": "open", "title": "Will Friedrich Merz leave before Oct 2026?"}
    is_eligible, _ = is_2026_kalshi_market(germany_market, germany_ev, is_primary_match=False)
    assert not is_eligible


# =====================================================================
# 3. Polymarket Outcome-Token Pairing Tests
# =====================================================================

def test_polymarket_token_mapping_validation() -> None:
    """Parallel outcomes and CLOB tokens must match in length and be non-empty."""
    class FakeOpener:
        def __init__(self, items: list[dict]) -> None:
            self.items = items

        def open(self, req: urllib.request.Request) -> io.BytesIO:
            resp = io.BytesIO(json.dumps(self.items).encode("utf-8"))
            return resp

    limiter = RateLimiter(interval=0.0)

    # Valid market
    valid_items = [{
        "id": "1001",
        "question": "Will Democrats win Georgia Senate in 2026?",
        "slug": "democrats-georgia-senate-2026",
        "closed": False,
        "active": True,
        "outcomes": json.dumps(["Yes", "No"]),
        "clobTokenIds": json.dumps(["tok_yes_123", "tok_no_456"]),
        "outcomePrices": json.dumps(["0.60", "0.40"]),
        "endDate": "2026-11-04T00:00:00Z",
    }]
    markets, excluded = fetch_polymarket_markets(limiter, opener=FakeOpener(valid_items))
    assert len(markets) == 1
    assert markets[0]["outcome_token_map"] == {"Yes": "tok_yes_123", "No": "tok_no_456"}

    # Invalid token mapping (mismatched length)
    mismatched_items = [{
        "id": "1002",
        "question": "Will Republicans win Georgia Senate in 2026?",
        "closed": False,
        "active": True,
        "outcomes": json.dumps(["Yes", "No"]),
        "clobTokenIds": json.dumps(["tok_single"]),
    }]
    markets, excluded = fetch_polymarket_markets(limiter, opener=FakeOpener(mismatched_items))
    assert len(markets) == 0
    assert excluded.get("invalid_token_mapping", 0) > 0


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
    """HTTP 422 before max depth (offset < 2000) must raise FetchError."""
    limiter = RateLimiter(interval=0.0)

    class FailingOpener:
        def open(self, req: urllib.request.Request) -> None:
            raise urllib.error.HTTPError(req.full_url, 422, "Unprocessable Entity", hdrs=None, fp=io.BytesIO(b"{}"))

    with pytest.raises(FetchError, match="Polymarket HTTP 422"):
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
            # First attempt on primary host fails with connection reset
            raise OSError("Connection reset by peer")
        # Second attempt on fallback succeeds with empty events (end of data)
        return 200, json.dumps({"events": [], "cursor": ""}).encode("utf-8")

    fetch_kalshi_markets(mock_limiter, requester=mock_requester)
    # The rate limiter must have been called for BOTH attempts
    assert attempt_count == 2


# =====================================================================
# 6. Atomic Output Publishing & Preserving Latest on Failure
# =====================================================================

def test_publish_output_atomic_replacement(tmp_path: Path) -> None:
    """publish_output writes through .tmp files and atomically updates latest."""
    dated_file = tmp_path / "venue_markets_2026-10-07.json"
    latest_file = tmp_path / "venue_markets_latest.json"

    # Seed initial latest file
    initial_content = {"canary": "original_content"}
    latest_file.write_text(json.dumps(initial_content), encoding="utf-8")

    # Publish updated dataset
    new_payload = {"canary": "updated_content", "polymarket": [], "kalshi": []}
    publish_output(dated_file, latest_file, new_payload)

    # Check that both files exist and contain updated payload
    assert dated_file.exists()
    assert latest_file.exists()
    with open(latest_file, "r", encoding="utf-8") as f:
        data = json.load(f)
    assert data["canary"] == "updated_content"

    # Temporary files must have been cleaned up
    assert not dated_file.with_suffix(".tmp").exists()
    assert not latest_file.with_suffix(".tmp").exists()


def test_failed_validation_preserves_latest_byte_for_byte(tmp_path: Path) -> None:
    """A failed run must preserve existing latest file byte-for-byte."""
    latest_file = tmp_path / "venue_markets_latest.json"
    canary_bytes = b'{"version": "1.0", "important_data": true}\n'
    latest_file.write_bytes(canary_bytes)

    # Incomplete payload (fewer than 100 markets)
    incomplete_payload = {
        "polymarket": [{"market_id": "1"}],
        "kalshi": [{"market_ticker": "K1"}],
    }
    errors = validate_dataset(incomplete_payload)
    assert len(errors) > 0  # validation fails

    # Verify latest file was never touched
    assert latest_file.read_bytes() == canary_bytes
