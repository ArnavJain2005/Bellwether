"""Offline regression and unit tests for data/venue_markets/fetch_venue_markets.py."""

from __future__ import annotations

import copy
import io
import json
import socket
import sys
import types
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Self
from unittest.mock import MagicMock, patch

import pytest

from data.venue_markets import fetch_venue_markets as fvm
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
# 6. End-to-End Harness: main() with fake transports and a temp output dir
# =====================================================================
#
# These tests drive the real main() -> fetch_* -> validate_dataset -> publish_output path.
# Only the network transports, the clock/sleep and the module location are replaced.
# validate_dataset and publish_output are NOT mocked.

CANARY = b'{"canary": "previous-good-run"}\n'
POLY_HOST = "clob.polymarket.com"
KALSHI_PRIMARY = "api.elections.kalshi.com"


class UnexpectedRequest(BaseException):
    """Raised by the harness on any request the test did not script.

    Derives from BaseException so the fetchers' broad ``except Exception`` retry
    handlers cannot swallow it.
    """


def _poly_item(cid: str, question: str, slug: str) -> dict:
    return {
        "condition_id": cid,
        "question": question,
        "market_slug": slug,
        "closed": False,
        "active": True,
        "tokens": [
            {"outcome": "Yes", "token_id": f"{cid}-yes", "price": 0.5},
            {"outcome": "No", "token_id": f"{cid}-no", "price": 0.5},
        ],
        "end_date_iso": "2026-11-03T00:00:00Z",
    }


def _poly_controls() -> list[dict]:
    return [
        _poly_item("poly-control-senate", "Will Democrats control the Senate in 2026?", "dems-control-senate-2026"),
        _poly_item("poly-control-house", "Will Democrats control the House in 2026?", "dems-control-house-2026"),
    ]


def _poly_seats(start: int, count: int) -> list[dict]:
    return [
        _poly_item(f"poly-seat-{i}", f"Will Democrats win Senate seat {i} in 2026?", f"senate-seat-{i}-2026")
        for i in range(start, start + count)
    ]


def _kalshi_controls() -> list[dict]:
    return [
        {
            "title": "2026 Senate Control",
            "series_ticker": "CONTROLS",
            "event_ticker": "CONTROLS-26",
            "markets": [{"ticker": "CONTROLS-26-D", "status": "open", "title": "2026 Senate Control"}],
        },
        {
            "title": "2026 House Control",
            "series_ticker": "CONTROLH",
            "event_ticker": "CONTROLH-26",
            "markets": [{"ticker": "CONTROLH-26-D", "status": "open", "title": "2026 House Control"}],
        },
    ]


def _kalshi_seats(start: int, count: int) -> list[dict]:
    return [
        {
            "title": f"2026 Senate seat {i}",
            "series_ticker": "SENATE",
            "event_ticker": f"SENATE-26-{i}",
            "markets": [{"ticker": f"SENATE-26-{i}", "status": "open", "title": f"2026 Senate seat {i}"}],
        }
        for i in range(start, start + count)
    ]


def _valid_poly() -> dict[str, dict]:
    """Two pages (cursor '' then 'c2'); 122 markets including both chamber-control markets."""
    return {
        "": {"data": _poly_controls() + _poly_seats(0, 60), "next_cursor": "c2"},
        "c2": {"data": _poly_seats(60, 60), "next_cursor": "LTE="},
    }


def _valid_kalshi() -> dict[str, dict]:
    """Two pages (cursor '' then 'k2'); 122 markets, explicit empty terminal cursor."""
    return {
        "": {"events": _kalshi_controls() + _kalshi_seats(0, 60), "cursor": "k2"},
        "k2": {"events": _kalshi_seats(60, 60), "cursor": ""},
    }


POLY_IDS = {"poly-control-senate", "poly-control-house"} | {f"poly-seat-{i}" for i in range(120)}
KALSHI_IDS = {"CONTROLS-26-D", "CONTROLH-26-D"} | {f"SENATE-26-{i}" for i in range(120)}


class _FakeResponse:
    def __init__(self, body: bytes) -> None:
        self._body = body

    def read(self) -> bytes:
        return self._body

    def __enter__(self) -> Self:
        return self

    def __exit__(self, *exc: object) -> None:
        return None


class _Env:
    def __init__(self, out_dir: Path, poly: dict, kalshi: dict) -> None:
        self.out_dir = out_dir
        self.latest = out_dir / "venue_markets_latest.json"
        self.poly = poly
        self.kalshi = kalshi
        self.poly_cursors: list[str | None] = []
        self.kalshi_cursors: list[str | None] = []
        self.sleeps: list[float] = []
        self.now = 1000.0

    # fake clock
    def time(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)
        self.now += seconds


def _install(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, poly: dict, kalshi: dict) -> _Env:
    """Wire main() to fake transports, a temp output directory, a fake clock and no network."""
    out_dir = tmp_path / "output"
    out_dir.mkdir()
    (out_dir / "venue_markets_latest.json").write_bytes(CANARY)
    env = _Env(out_dir, poly, kalshi)

    # main() derives its output directory from the module's __file__
    monkeypatch.setattr(fvm, "__file__", str(tmp_path / "fetch_venue_markets.py"))
    # main() runs argparse over sys.argv; never let it see pytest's arguments
    monkeypatch.setattr(sys, "argv", ["fetch_venue_markets.py"])
    monkeypatch.setattr(fvm, "time", types.SimpleNamespace(time=env.time, sleep=env.sleep))

    def fake_urlopen(req: urllib.request.Request, timeout: float | None = None) -> _FakeResponse:
        parsed = urllib.parse.urlparse(req.full_url)
        if parsed.netloc != POLY_HOST or parsed.path != "/markets":
            raise UnexpectedRequest(f"unexpected Polymarket URL: {req.full_url}")
        cursor = urllib.parse.parse_qs(parsed.query).get("next_cursor", [""])[0]
        env.poly_cursors.append(cursor or None)
        if cursor not in env.poly:
            raise UnexpectedRequest(f"unscripted Polymarket cursor: {cursor!r}")
        return _FakeResponse(json.dumps(env.poly[cursor]).encode("utf-8"))

    def fake_kalshi(host: str, path: str, timeout: float = 25.0) -> tuple[int, bytes]:
        parsed = urllib.parse.urlparse(path)
        if host != KALSHI_PRIMARY or parsed.path != "/trade-api/v2/events":
            raise UnexpectedRequest(f"unexpected Kalshi request: {host}{path}")
        cursor = urllib.parse.parse_qs(parsed.query).get("cursor", [""])[0]
        env.kalshi_cursors.append(cursor or None)
        if cursor not in env.kalshi:
            raise UnexpectedRequest(f"unscripted Kalshi cursor: {cursor!r}")
        return 200, json.dumps(env.kalshi[cursor]).encode("utf-8")

    def blocked(*_a: object, **_k: object) -> None:
        raise UnexpectedRequest("real network access attempted")

    monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)
    monkeypatch.setattr(fvm, "_kalshi_request", fake_kalshi)
    monkeypatch.setattr(socket.socket, "connect", blocked)
    monkeypatch.setattr(socket, "create_connection", blocked)
    monkeypatch.setattr(socket, "getaddrinfo", blocked)
    return env


def _run_main(capsys: pytest.CaptureFixture[str]) -> tuple[object, str, str]:
    with pytest.raises(SystemExit) as excinfo:
        main()
    captured = capsys.readouterr()
    return excinfo.value.code, captured.out, captured.err


def _assert_preserved(env: _Env) -> None:
    assert env.latest.read_bytes() == CANARY
    # no dated file and no leftover temp files may appear on failure
    assert sorted(p.name for p in env.out_dir.iterdir()) == ["venue_markets_latest.json"]


def _assert_failure(
    env: _Env,
    code: object,
    err: str,
    reason: str,
    poly_cursors: list[str | None],
    kalshi_cursors: list[str | None],
) -> None:
    assert env.poly_cursors == poly_cursors
    assert env.kalshi_cursors == kalshi_cursors
    assert code == 1  # exactly 1, not merely nonzero
    assert "FETCH FAILED" in err
    assert reason in err
    _assert_preserved(env)


def _assert_success(env: _Env, code: object, poly_ids: set[str], kalshi_ids: set[str]) -> dict:
    assert code == 0
    published = json.loads(env.latest.read_bytes())
    assert env.latest.read_bytes() != CANARY
    assert {m["market_id"] for m in published["polymarket"]} == poly_ids
    assert {m["market_ticker"] for m in published["kalshi"]} == kalshi_ids
    assert published["polymarket_count"] == len(poly_ids)
    assert published["kalshi_count"] == len(kalshi_ids)
    assert published["kalshi_host"] == KALSHI_PRIMARY
    # the dated file is a byte-identical copy and no temp files remain
    dated = [p for p in env.out_dir.iterdir() if p.name != "venue_markets_latest.json"]
    assert len(dated) == 1 and dated[0].suffix == ".json"
    assert dated[0].read_bytes() == env.latest.read_bytes()
    return published


def test_harness_blocks_real_network(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _install(tmp_path, monkeypatch, _valid_poly(), _valid_kalshi())
    with pytest.raises(UnexpectedRequest):
        socket.create_connection(("example.invalid", 443))
    with pytest.raises(UnexpectedRequest):
        socket.getaddrinfo("example.invalid", 443)


def test_main_success_replaces_latest_with_both_venues(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """Valid full run: both venues fetched through fakes and the real latest file replaced."""
    env = _install(tmp_path, monkeypatch, _valid_poly(), _valid_kalshi())
    code, out, _err = _run_main(capsys)

    assert env.poly_cursors == [None, "c2"]
    assert env.kalshi_cursors == [None, "k2"]
    published = _assert_success(env, code, POLY_IDS, KALSHI_IDS)
    assert "VALIDATION SUCCESSFUL" in out
    by_id = {m["market_id"]: m for m in published["polymarket"]}
    assert by_id["poly-control-senate"]["outcome_token_map"] == {
        "Yes": "poly-control-senate-yes",
        "No": "poly-control-senate-no",
    }
    assert all(s <= fvm.MIN_REQUEST_INTERVAL for s in env.sleeps)  # fake sleeps only


def test_main_validation_failure_preserves_latest(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """Real validate_dataset rejects a short dataset; the real latest file is untouched."""
    poly = _valid_poly()
    poly["c2"]["data"] = _poly_seats(60, 10)  # 72 Polymarket markets < 100
    env = _install(tmp_path, monkeypatch, poly, _valid_kalshi())
    code, _out, err = _run_main(capsys)

    assert env.poly_cursors == [None, "c2"]
    assert env.kalshi_cursors == [None, "k2"]
    assert code == 1
    assert "Polymarket has fewer than 100 markets (72)" in err
    _assert_preserved(env)


# ---------------------------------------------------------------------
# Polymarket pagination
# ---------------------------------------------------------------------

def _poly_set(key: str, field: str, value: object):
    return lambda poly, _k: poly[key].__setitem__(field, value)


def _poly_pop(key: str, field: str):
    return lambda poly, _k: poly[key].pop(field)


def _poly_replace(key: str, value: object):
    return lambda poly, _k: poly.__setitem__(key, value)


def _poly_cycle(poly: dict, _k: dict) -> None:
    poly["c2"]["next_cursor"] = "c3"
    poly["c3"] = {"data": _poly_seats(200, 5), "next_cursor": "c2"}


def _poly_repeated_page_adjacent(poly: dict, _k: dict) -> None:
    poly["c2"]["data"] = copy.deepcopy(poly[""]["data"])


def _poly_repeated_page_later(poly: dict, _k: dict) -> None:
    poly["c2"]["next_cursor"] = "c3"
    poly["c3"] = {"data": copy.deepcopy(poly[""]["data"]), "next_cursor": "LTE="}


def _reversed_records(records: list[dict]) -> list[dict]:
    return list(reversed(records))


def _repriced_records(records: list[dict]) -> list[dict]:
    records[0]["tokens"][0]["price"] = 0.99
    return records


def _remetadata_records(records: list[dict]) -> list[dict]:
    for record in records:
        record["description"] = "edited resolution text"
        record["end_date_iso"] = "2026-12-31T00:00:00Z"
    return records


def _reordered_and_repriced_records(records: list[dict]) -> list[dict]:
    return _repriced_records(_reversed_records(records))


def _poly_replay_adjacent(transform):
    """c2 replays the first page (same condition_ids) after `transform`."""
    def mutate(poly: dict, _k: dict) -> None:
        poly["c2"]["data"] = transform(copy.deepcopy(poly[""]["data"]))
    return mutate


def _poly_replay_non_adjacent(transform):
    """c2 stays distinct; c3 replays the first page after `transform` and ends with LTE=."""
    def mutate(poly: dict, _k: dict) -> None:
        poly["c2"]["next_cursor"] = "c3"
        poly["c3"] = {
            "data": transform(copy.deepcopy(poly[""]["data"])),
            "next_cursor": "LTE=",
        }
    return mutate


POLY_FAILURES = [
    pytest.param(_poly_pop("c2", "next_cursor"), [None, "c2"], "missing 'next_cursor'", id="missing-cursor"),
    pytest.param(_poly_set("", "next_cursor", None), [None], "'next_cursor' must be a non-empty string", id="null-cursor-first-page"),
    pytest.param(_poly_set("c2", "next_cursor", None), [None, "c2"], "'next_cursor' must be a non-empty string", id="null-cursor"),
    pytest.param(_poly_set("c2", "next_cursor", ""), [None, "c2"], "'next_cursor' must be a non-empty string", id="empty-cursor"),
    pytest.param(_poly_set("c2", "next_cursor", 5), [None, "c2"], "'next_cursor' must be a non-empty string", id="numeric-cursor"),
    pytest.param(_poly_set("c2", "next_cursor", True), [None, "c2"], "'next_cursor' must be a non-empty string", id="bool-cursor"),
    pytest.param(_poly_set("c2", "next_cursor", ["c3"]), [None, "c2"], "'next_cursor' must be a non-empty string", id="list-cursor"),
    pytest.param(_poly_set("c2", "data", None), [None, "c2"], "expected 'data' to be a list", id="data-null"),
    pytest.param(_poly_pop("c2", "data"), [None, "c2"], "expected dict with 'data'", id="data-missing"),
    pytest.param(_poly_replace("c2", []), [None, "c2"], "expected dict with 'data'", id="page-not-object"),
    pytest.param(_poly_set("c2", "next_cursor", "c2"), [None, "c2"], "failed to progress", id="consecutive-cursor-cycle"),
    pytest.param(_poly_cycle, [None, "c2", "c3"], "pagination loop detected: repeated cursor c2", id="nonconsecutive-cursor-cycle"),
    pytest.param(_poly_repeated_page_adjacent, [None, "c2"], "repeated page content", id="whole-page-repeated-under-new-cursor"),
    pytest.param(_poly_repeated_page_later, [None, "c2", "c3"], "repeated page content", id="whole-page-repeated-non-adjacent"),
    pytest.param(_poly_replay_adjacent(_reversed_records), [None, "c2"], "repeated page content", id="reordered-replay-adjacent"),
    pytest.param(_poly_replay_adjacent(_repriced_records), [None, "c2"], "repeated page content", id="changed-price-replay-adjacent"),
    pytest.param(_poly_replay_adjacent(_remetadata_records), [None, "c2"], "repeated page content", id="changed-metadata-replay-adjacent"),
    pytest.param(_poly_replay_non_adjacent(_reversed_records), [None, "c2", "c3"], "repeated page content", id="reordered-replay-non-adjacent"),
    pytest.param(_poly_replay_non_adjacent(_repriced_records), [None, "c2", "c3"], "repeated page content", id="changed-price-replay-non-adjacent"),
    pytest.param(_poly_replay_non_adjacent(_remetadata_records), [None, "c2", "c3"], "repeated page content", id="changed-metadata-replay-non-adjacent"),
    pytest.param(_poly_replay_non_adjacent(_reordered_and_repriced_records), [None, "c2", "c3"], "repeated page content", id="reordered-and-repriced-replay-non-adjacent"),
]


@pytest.mark.parametrize(("mutate", "poly_cursors", "reason"), POLY_FAILURES)
def test_main_polymarket_pagination_failure_preserves_latest(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    mutate,
    poly_cursors: list[str | None],
    reason: str,
) -> None:
    """One altered Polymarket response (after otherwise-valid data) fails the whole run."""
    poly, kalshi = _valid_poly(), _valid_kalshi()
    mutate(poly, kalshi)
    env = _install(tmp_path, monkeypatch, poly, kalshi)
    code, _out, err = _run_main(capsys)
    # Kalshi is fetched only after Polymarket succeeds
    _assert_failure(env, code, err, reason, poly_cursors, [])


def test_main_polymarket_partial_record_overlap_is_not_a_repeated_page(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """A record seen on two distinct pages is ordinary overlap: dedupe by id and succeed."""
    poly = _valid_poly()
    poly["c2"]["data"] = [copy.deepcopy(poly[""]["data"][0])] + _poly_seats(60, 60)
    env = _install(tmp_path, monkeypatch, poly, _valid_kalshi())
    code, _out, _err = _run_main(capsys)

    assert env.poly_cursors == [None, "c2"]
    _assert_success(env, code, POLY_IDS, KALSHI_IDS)


def test_main_polymarket_empty_terminal_page_succeeds(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """A valid empty terminal page ending in the LTE= sentinel completes normally."""
    poly = _valid_poly()
    poly["c2"]["next_cursor"] = "c3"
    poly["c3"] = {"data": [], "next_cursor": "LTE="}
    env = _install(tmp_path, monkeypatch, poly, _valid_kalshi())
    code, _out, _err = _run_main(capsys)

    assert env.poly_cursors == [None, "c2", "c3"]
    _assert_success(env, code, POLY_IDS, KALSHI_IDS)


def test_main_polymarket_reordered_partial_overlap_on_later_page_succeeds(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """Half of an earlier page re-served (reordered) alongside new records is ordinary overlap."""
    poly = _valid_poly()
    poly["c2"]["next_cursor"] = "c3"
    poly["c3"] = {
        "data": _reversed_records(copy.deepcopy(poly[""]["data"][:30])) + _poly_seats(120, 5),
        "next_cursor": "LTE=",
    }
    env = _install(tmp_path, monkeypatch, poly, _valid_kalshi())
    code, _out, _err = _run_main(capsys)

    assert env.poly_cursors == [None, "c2", "c3"]
    _assert_success(env, code, POLY_IDS | {f"poly-seat-{i}" for i in range(120, 125)}, KALSHI_IDS)


def _poly_closed(start: int, count: int) -> list[dict]:
    records = _poly_seats(start, count)
    for record in records:
        record["condition_id"] = record["condition_id"].replace("seat", "closed")
        record["closed"] = True
    return records


def test_main_polymarket_distinct_raw_pages_with_identical_eligible_records_succeed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """Identity is taken from the raw page, before eligibility filtering.

    Pages 1 and 2 differ only in their (ineligible, closed) records, so they are NOT a repeated
    page even though their eligible records are identical.
    """
    eligible = _poly_controls() + _poly_seats(0, 60)
    poly = {
        "": {"data": copy.deepcopy(eligible) + _poly_closed(900, 3), "next_cursor": "c2"},
        "c2": {"data": copy.deepcopy(eligible) + _poly_closed(903, 3), "next_cursor": "c3"},
        "c3": {"data": _poly_seats(60, 60), "next_cursor": "LTE="},
    }
    env = _install(tmp_path, monkeypatch, poly, _valid_kalshi())
    code, _out, _err = _run_main(capsys)

    assert env.poly_cursors == [None, "c2", "c3"]
    _assert_success(env, code, POLY_IDS, KALSHI_IDS)


# ---------------------------------------------------------------------
# Kalshi pagination
# ---------------------------------------------------------------------

def _kal_set(key: str, field: str, value: object):
    return lambda _p, kalshi: kalshi[key].__setitem__(field, value)


def _kal_pop(key: str, field: str):
    return lambda _p, kalshi: kalshi[key].pop(field)


def _kal_replace(key: str, value: object):
    return lambda _p, kalshi: kalshi.__setitem__(key, value)


def _kal_cycle(_p: dict, kalshi: dict) -> None:
    kalshi["k2"]["cursor"] = "k3"
    kalshi["k3"] = {"events": [], "cursor": "k2"}


KALSHI_FAILURES = [
    pytest.param(_kal_pop("k2", "cursor"), [None, "k2"], "missing 'cursor'", id="missing-cursor"),
    pytest.param(_kal_pop("", "cursor"), [None], "missing 'cursor'", id="missing-cursor-first-page"),
    pytest.param(_kal_set("k2", "cursor", None), [None, "k2"], "'cursor' must be a string, got NoneType", id="null-cursor"),
    pytest.param(_kal_set("", "cursor", None), [None], "'cursor' must be a string, got NoneType", id="null-cursor-first-page"),
    pytest.param(_kal_set("k2", "cursor", True), [None, "k2"], "'cursor' must be a string, got bool", id="bool-cursor"),
    pytest.param(_kal_set("k2", "cursor", 5), [None, "k2"], "'cursor' must be a string, got int", id="int-cursor"),
    pytest.param(_kal_set("k2", "cursor", 1.5), [None, "k2"], "'cursor' must be a string, got float", id="float-cursor"),
    pytest.param(_kal_pop("k2", "events"), [None, "k2"], "expected 'events' list", id="events-missing"),
    pytest.param(_kal_set("k2", "events", None), [None, "k2"], "expected 'events' list", id="events-null"),
    pytest.param(_kal_set("k2", "events", {}), [None, "k2"], "expected 'events' list", id="events-not-list"),
    pytest.param(_kal_replace("k2", []), [None, "k2"], "expected JSON object", id="page-not-object"),
    pytest.param(_kal_set("k2", "cursor", "k2"), [None, "k2"], "Kalshi pagination loop detected: repeated cursor k2", id="consecutive-cursor-cycle"),
    pytest.param(_kal_cycle, [None, "k2", "k3"], "Kalshi pagination loop detected: repeated cursor k2", id="nonconsecutive-cursor-cycle"),
]


@pytest.mark.parametrize(("mutate", "kalshi_cursors", "reason"), KALSHI_FAILURES)
def test_main_kalshi_pagination_failure_preserves_latest(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    mutate,
    kalshi_cursors: list[str | None],
    reason: str,
) -> None:
    """One altered Kalshi response, after a fully valid Polymarket fetch, fails the run."""
    poly, kalshi = _valid_poly(), _valid_kalshi()
    mutate(poly, kalshi)
    env = _install(tmp_path, monkeypatch, poly, kalshi)
    code, _out, err = _run_main(capsys)
    _assert_failure(env, code, err, reason, [None, "c2"], kalshi_cursors)


def test_main_kalshi_empty_page_with_cursor_continues(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """An empty events page with a nonempty cursor is not EOF; the next page is fetched."""
    kalshi = {
        "": {"events": [], "cursor": "k2"},
        "k2": {"events": _kalshi_controls() + _kalshi_seats(0, 120), "cursor": ""},
    }
    env = _install(tmp_path, monkeypatch, _valid_poly(), kalshi)
    code, _out, _err = _run_main(capsys)

    assert env.poly_cursors == [None, "c2"]
    assert env.kalshi_cursors == [None, "k2"]
    _assert_success(env, code, POLY_IDS, KALSHI_IDS)


def test_main_kalshi_explicit_empty_terminal_cursor_ends_traversal(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """The explicit empty-string cursor ends traversal: no extra request is made."""
    env = _install(tmp_path, monkeypatch, _valid_poly(), _valid_kalshi())
    code, _out, _err = _run_main(capsys)

    assert env.kalshi_cursors == [None, "k2"]
    _assert_success(env, code, POLY_IDS, KALSHI_IDS)
