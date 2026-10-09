"""Offline regression and unit tests for data/venue_markets/fetch_venue_markets.py."""

from __future__ import annotations

import ast
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

    # Outside tag 102289, lacking year evidence is excluded
    senate_midterms = {
        "question": "Who controls the Senate after midterms?",
        "slug": "who-controls-senate-after-midterms",
        "closed": False,
        "active": True,
    }
    is_eligible, reason = is_2026_polymarket_market(senate_midterms, is_tag_102289=False)
    assert not is_eligible
    assert reason == "lacks_2026_contest_year"

    # In tag 102289, race question without year IS eligible
    is_eligible_in_tag, _ = is_2026_polymarket_market(senate_midterms, is_tag_102289=True)
    assert is_eligible_in_tag

    aoc_2028 = {
        "question": "Will Alexandria Ocasio-Cortez run for Senate in 2028?",
        "slug": "will-alexandria-ocasio-cortez-run-for-senate-in-2028",
        "description": "Resolves according to whether AOC runs for Senate in 2028.",
        "closed": False,
        "active": True,
    }
    is_eligible, reason = is_2026_polymarket_market(aoc_2028)
    assert not is_eligible
    assert reason == "non_2026_primary_contest"


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
        "title": "2026 U.S. Senate control",
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
        def __init__(self, data: object) -> None:
            self.data = data

        def open(self, _req: urllib.request.Request) -> io.BytesIO:
            return io.BytesIO(json.dumps(self.data).encode("utf-8"))

    # Valid market using Gamma format (clobTokenIds, outcomes)
    valid_items = [{
        "id": "1001",
        "title": "Texas Senate 2026",
        "slug": "texas-senate-2026",
        "markets": [{
            "id": "1001",
            "question": "Will Democrats win the U.S. Senate seat in Texas in 2026?",
            "slug": "democrats-texas-senate-2026",
            "closed": False,
            "active": True,
            "outcomes": json.dumps(["Yes", "No"]),
            "clobTokenIds": json.dumps(["tok_yes_123", "tok_no_456"]),
            "outcomePrices": json.dumps(["0.60", "0.40"]),
            "bestBid": 0.59,
            "bestAsk": 0.61,
            "lastTradePrice": 0.60,
            "endDate": "2026-11-04T00:00:00Z",
        }]
    }]
    markets, _excluded = fetch_polymarket_markets(limiter, opener=FakeOpener(valid_items))
    assert len(markets) == 1
    assert markets[0]["outcome_token_map"] == {"Yes": "tok_yes_123", "No": "tok_no_456"}
    assert markets[0]["event_title"] == "Texas Senate 2026"
    assert markets[0]["snapshot_prices"]["best_bid"] == 0.59

    # Invalid token mapping (duplicate labels)
    mismatched_items = [{
        "id": "1002",
        "title": "Texas Senate 2026",
        "markets": [{
            "id": "1002",
            "question": "Will Republicans win the U.S. Senate seat in Texas in 2026?",
            "closed": False,
            "active": True,
            "outcomes": json.dumps(["Yes", "Yes"]),
            "clobTokenIds": json.dumps(["tok_1", "tok_2"]),
        }]
    }]
    markets, _excluded = fetch_polymarket_markets(limiter, opener=FakeOpener(mismatched_items))
    assert len(markets) == 0

    # Invalid token mapping (null outcome label) - must reject entirely
    null_outcome_items = [{
        "id": "1003",
        "title": "Texas Senate 2026",
        "markets": [{
            "id": "1003",
            "question": "Will Democrats win the U.S. Senate seat in Texas in 2026?",
            "closed": False,
            "active": True,
            "outcomes": json.dumps([None, "No"]),
            "clobTokenIds": json.dumps(["tok_1", "tok_2"]),
        }]
    }]
    markets, _excluded = fetch_polymarket_markets(limiter, opener=FakeOpener(null_outcome_items))
    assert len(markets) == 0


# =====================================================================
# 4. Pagination & Cycle Detection Tests
# =====================================================================

def test_kalshi_pagination_cycle_detection() -> None:
    """Repeated pagination cursor must trigger FetchError to prevent infinite loops."""
    limiter = RateLimiter(interval=0.0)
    seen_calls: list[str] = []

    def mock_requester(_host: str, path: str) -> tuple[int, bytes]:
        seen_calls.append(path)
        body = json.dumps({
            "events": [{"series_ticker": "CONTROLS", "markets": []}],
            "cursor": "loop_cursor",
        }).encode("utf-8")
        return 200, body

    with pytest.raises(FetchError, match="loop detected"):
        fetch_kalshi_markets(limiter, requester=mock_requester)


def test_polymarket_unexpected_http_error_raises_fetch_error() -> None:
    """HTTP 500 must raise FetchError."""
    limiter = RateLimiter(interval=0.0)

    class FailingOpener:
        def open(self, req: urllib.request.Request) -> None:
            raise urllib.error.HTTPError(req.full_url, 500, "Internal Server Error", hdrs=None, fp=io.BytesIO(b"{}"))

    with pytest.raises(FetchError, match="Polymarket HTTP 500"), patch("time.sleep"):
        fetch_polymarket_markets(limiter, opener=FailingOpener())


# =====================================================================
# 5. Rate Limiting on Fallback & Retries
# =====================================================================

def test_rate_limiter_invoked_on_fallback_and_retries() -> None:
    """Rate limiter wait() must be invoked before EVERY request attempt."""
    mock_limiter = MagicMock(spec=RateLimiter)
    attempt_count = 0

    def mock_requester(_host: str, _path: str) -> tuple[int, bytes]:
        nonlocal attempt_count
        attempt_count += 1
        if attempt_count == 1:
            raise OSError("Connection reset by peer")
        return 200, json.dumps({"events": [], "cursor": ""}).encode("utf-8")

    with patch("time.sleep"):
        fetch_kalshi_markets(mock_limiter, requester=mock_requester)
    assert attempt_count == 2
    assert mock_limiter.wait.call_count == 2


# =====================================================================
# 6. End-to-End Harness: main() with fake transports and a temp output dir
# =====================================================================

CANARY = b'{"canary": "previous-good-run"}\n'
POLY_HOST = "gamma-api.polymarket.com"
KALSHI_PRIMARY = "api.elections.kalshi.com"


class UnexpectedRequest(BaseException):
    """Raised by the harness on any request the test did not script."""


def _poly_item(cid: str, question: str, slug: str) -> dict:
    return {
        "id": cid,
        "conditionId": f"0x{cid}",
        "question": question,
        "slug": slug,
        "closed": False,
        "active": True,
        "outcomes": json.dumps(["Yes", "No"]),
        "clobTokenIds": json.dumps([f"{cid}-yes", f"{cid}-no"]),
        "outcomePrices": json.dumps(["0.5", "0.5"]),
        "bestBid": 0.49,
        "bestAsk": 0.51,
        "lastTradePrice": 0.50,
        "endDate": "2026-11-03T00:00:00Z",
    }


def _poly_event(eid: str, title: str, slug: str, markets: list[dict]) -> dict:
    return {
        "id": eid,
        "title": title,
        "slug": slug,
        "markets": markets,
    }


def _poly_controls() -> list[dict]:
    return [
        _poly_item("poly-control-senate", "Will Democrats control the Senate in the 2026 U.S. midterms?", "dems-control-the-senate-2026-us-midterms"),
        _poly_item("poly-control-house", "Will Democrats control the House in the 2026 U.S. midterms?", "dems-control-the-house-2026-us-midterms"),
    ]


def _poly_seats(start: int, count: int) -> list[dict]:
    return [
        _poly_item(f"poly-seat-{i}", f"Will Democrats win U.S. Senate seat {i} in 2026?", f"us-senate-seat-{i}-2026")
        for i in range(start, start + count)
    ]


def _kalshi_controls() -> list[dict]:
    return [
        {
            "title": "2026 U.S. Senate Control",
            "series_ticker": "CONTROLS",
            "event_ticker": "CONTROLS-26",
            "markets": [{"ticker": "CONTROLS-26-D", "status": "open", "title": "2026 U.S. Senate Control"}],
        },
        {
            "title": "2026 U.S. House Control",
            "series_ticker": "CONTROLH",
            "event_ticker": "CONTROLH-26",
            "markets": [{"ticker": "CONTROLH-26-D", "status": "open", "title": "2026 U.S. House Control"}],
        },
    ]


def _kalshi_seats(start: int, count: int) -> list[dict]:
    return [
        {
            "title": f"2026 U.S. Senate seat {i}",
            "series_ticker": "SENATE",
            "event_ticker": f"SENATE-26-{i}",
            "markets": [{"ticker": f"SENATE-26-{i}", "status": "open", "title": f"2026 U.S. Senate seat {i}"}],
        }
        for i in range(start, start + count)
    ]


def _valid_poly() -> dict[int, list[dict]]:
    """Two pages: offset 0 has 100 events (100 markets), offset 100 has 22 events (22 markets)."""
    page0 = [
        _poly_event("ev-ctrl-s", "2026 U.S. Senate Control", "senate-control", [_poly_controls()[0]]),
        _poly_event("ev-ctrl-h", "2026 U.S. House Control", "house-control", [_poly_controls()[1]]),
    ] + [
        _poly_event(f"ev-seat-{i}", f"2026 U.S. Senate seat {i}", f"senate-seat-{i}", [_poly_seats(i, 1)[0]])
        for i in range(98)
    ]
    page1 = [
        _poly_event(f"ev-seat-{i}", f"2026 U.S. Senate seat {i}", f"senate-seat-{i}", [_poly_seats(i, 1)[0]])
        for i in range(98, 120)
    ]
    return {
        0: page0,
        100: page1,
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
        self.poly_offsets: list[int] = []
        self.kalshi_cursors: list[str | None] = []
        self.sleeps: list[float] = []
        self.now = 1000.0

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

    monkeypatch.setattr(fvm, "__file__", str(tmp_path / "fetch_venue_markets.py"))
    monkeypatch.setattr(sys, "argv", ["fetch_venue_markets.py"])
    monkeypatch.setattr(fvm, "time", types.SimpleNamespace(time=env.time, sleep=env.sleep))

    def fake_urlopen(req: urllib.request.Request, timeout: float | None = None) -> _FakeResponse:
        parsed = urllib.parse.urlparse(req.full_url)
        if parsed.netloc != POLY_HOST or parsed.path != "/events":
            raise UnexpectedRequest(f"unexpected Polymarket URL: {req.full_url}")
        qs = urllib.parse.parse_qs(parsed.query)
        offset = int(qs.get("offset", ["0"])[0])
        env.poly_offsets.append(offset)
        if offset not in env.poly:
            raise UnexpectedRequest(f"unscripted Polymarket offset: {offset}")
        return _FakeResponse(json.dumps(env.poly[offset]).encode("utf-8"))

    def fake_kalshi(host: str, path: str, _timeout: float = 25.0) -> tuple[int, bytes]:
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
    assert sorted(p.name for p in env.out_dir.iterdir()) == ["venue_markets_latest.json"]


def _assert_failure(
    env: _Env,
    code: object,
    err: str,
    reason: str,
    poly_offsets: list[int],
    kalshi_cursors: list[str | None],
) -> None:
    assert env.poly_offsets == poly_offsets
    assert env.kalshi_cursors == kalshi_cursors
    assert code == 1
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

    assert env.poly_offsets == [0, 100]
    assert env.kalshi_cursors == [None, "k2"]
    published = _assert_success(env, code, POLY_IDS, KALSHI_IDS)
    assert "VALIDATION SUCCESSFUL" in out
    by_id = {m["market_id"]: m for m in published["polymarket"]}
    assert by_id["poly-control-senate"]["outcome_token_map"] == {
        "Yes": "poly-control-senate-yes",
        "No": "poly-control-senate-no",
    }
    assert by_id["poly-control-senate"]["event_id"] == "ev-ctrl-s"
    assert by_id["poly-control-senate"]["event_title"] == "2026 U.S. Senate Control"
    assert by_id["poly-control-senate"]["snapshot_prices"]["best_bid"] == 0.49
    assert all(s <= fvm.MIN_REQUEST_INTERVAL for s in env.sleeps)


def test_main_validation_failure_preserves_latest(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """Real validate_dataset rejects a short dataset; the real latest file is untouched."""
    poly = {
        0: [
            _poly_event("ev-ctrl-s", "Senate Control", "senate-control", [_poly_controls()[0]]),
            _poly_event("ev-ctrl-h", "House Control", "house-control", [_poly_controls()[1]]),
        ] + [
            _poly_event(f"ev-seat-{i}", f"Senate seat {i}", f"senate-seat-{i}", [_poly_seats(i, 1)[0]])
            for i in range(50)
        ]
    }
    env = _install(tmp_path, monkeypatch, poly, _valid_kalshi())
    code, _out, err = _run_main(capsys)

    assert env.poly_offsets == [0]
    assert env.kalshi_cursors == [None, "k2"]
    assert code == 1
    assert "Polymarket has fewer than 100 markets (52)" in err
    _assert_preserved(env)


def test_main_polymarket_gamma_http_error_preserves_latest(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """Polymarket HTTP error raises FetchError and preserves latest."""
    env = _install(tmp_path, monkeypatch, {}, _valid_kalshi())

    def failing_urlopen(_req: urllib.request.Request, timeout: float | None = None) -> _FakeResponse:
        raise urllib.error.HTTPError("https://gamma-api.polymarket.com/events", 500, "Server Error", hdrs=None, fp=io.BytesIO(b"{}"))

    monkeypatch.setattr(urllib.request, "urlopen", failing_urlopen)
    code, _out, err = _run_main(capsys)
    _assert_failure(env, code, err, "Polymarket HTTP 500", [], [])


def test_main_polymarket_gamma_malformed_response_preserves_latest(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """Polymarket malformed response raises FetchError and preserves latest."""
    env = _install(tmp_path, monkeypatch, {}, _valid_kalshi())

    def malformed_urlopen(_req: urllib.request.Request, timeout: float | None = None) -> _FakeResponse:
        return _FakeResponse(b'"not a list or dict"')

    monkeypatch.setattr(urllib.request, "urlopen", malformed_urlopen)
    code, _out, err = _run_main(capsys)
    _assert_failure(env, code, err, "Polymarket malformed response", [], [])


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
    _assert_failure(env, code, err, reason, [0, 100], kalshi_cursors)


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

    assert env.poly_offsets == [0, 100]
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


# =====================================================================
# 7. Election eligibility (both venues)
# =====================================================================

def _poly_market(question: str, slug: str = "", description: str = "", **extra: object) -> dict:
    item: dict = {
        "question": question,
        "market_slug": slug,
        "description": description,
        "closed": False,
        "active": True,
    }
    item.update(extra)
    return item


POLY_ELIGIBLE = [
    pytest.param(_poly_market("Will Democrats win the U.S. Senate election in 2026?"), id="senate"),
    pytest.param(_poly_market("Will Republicans win the U.S. House majority in the 2026 midterms?"), id="house"),
    pytest.param(_poly_market("Who will win the 2026 Texas governor election?"), id="governor-state-context"),
    pytest.param(_poly_market("Who will win the 2026 Ohio gubernatorial race?"), id="gubernatorial"),
    pytest.param(_poly_market("Will Democrats control the Senate in the 2026 U.S. midterms?"), id="chamber-control-senate"),
    pytest.param(_poly_market("Will Republicans control the House in the 2026 U.S. midterms?"), id="chamber-control-house"),
    pytest.param(_poly_market("Will the Democrat win the Arizona Senate race in 2026?"), id="state-senate-race"),
    pytest.param(_poly_market("Will Republicans win the West Virginia Senate race in 2026?"), id="west-virginia"),
    pytest.param(_poly_market("Will Democrats win the Virginia Senate race in 2026?"), id="virginia"),
    pytest.param(_poly_market("Will Democrats win the U.S. Senate seat in Georgia in 2026?"), id="georgia-with-explicit-us"),
    # New kept cases from S1-T5-F2:
    pytest.param(
        _poly_market("Will the Democratic Party control the Senate after the 2026 Midterm elections?"),
        id="senate-control-midterms-kept",
    ),
    pytest.param(
        _poly_market("Will the Democrats win the Georgia Senate race in 2026?"),
        id="georgia-senate-race-kept",
    ),
    pytest.param(
        _poly_market("Will Democrats win the Georgia Senate race?"),
        id="race-without-year-kept-in-tag",
    ),
    pytest.param(
        _poly_market("Will Democrats win the U.S. Senate election?", "will-democrats-win-the-us-senate-election-2026"),
        id="year-from-slug",
    ),
    pytest.param(
        _poly_market(
            "Will Democrats control the U.S. Senate in 2026?",
            description="Unlike 2028 and 2024, this concerns only the 2026 election.",
        ),
        id="rules-mention-other-years-as-background",
    ),
]


@pytest.mark.parametrize("item", POLY_ELIGIBLE)
def test_polymarket_eligible_contests(item: dict) -> None:
    assert is_2026_polymarket_market(item) == (True, "verified_2026")


POLY_INELIGIBLE = [
    pytest.param(
        _poly_market("Who will win the Brazilian Senate election in 2026?"),
        "foreign_election",
        id="brazilian-senate-2026",
    ),
    pytest.param(
        _poly_market("Who will win the Georgia parliamentary election in 2026?"),
        "foreign_election",
        id="georgia-country-parliamentary-election",
    ),
    pytest.param(
        _poly_market("Who will win the Georgian parliamentary election in 2026?"),
        "foreign_election",
        id="georgian-parliamentary-election",
    ),
    pytest.param(
        _poly_market(
            "Who will win the U.S. Senate election in 2028?",
            description="Compare with the 2026 election results.",
        ),
        "non_2026_primary_contest",
        id="us-senate-2028-rules-mention-2026",
    ),
    pytest.param(
        _poly_market("Will Democrats win the U.S. Senate in 2026 and 2028?"),
        "non_2026_primary_contest",
        id="conflicting-primary-years",
    ),
    pytest.param(
        _poly_market("Will Democrats win the U.S. Senate in 2026?", "us-senate-2028"),
        "non_2026_primary_contest",
        id="slug-contradicts-question-year",
    ),
    pytest.param(
        _poly_market("Will the U.S. Senate vote on a Canada trade deal in 2026?"),
        "conflicting_jurisdiction",
        id="us-and-foreign-context",
    ),
    pytest.param(
        _poly_market("Closer Senate Race in 2026: Florida or South Carolina?"),
        "conflicting_state_context",
        id="multi-state-comparison",
    ),
    pytest.param(
        _poly_market("Will Democrats win the Texas state senate in 2026?"),
        "state_legislative_contest",
        id="state-legislature",
    ),
    pytest.param(
        _poly_market("Will the U.S. governor of Texas resign in 2026?"),
        "unrelated_political_event",
        id="resignation",
    ),
    pytest.param(
        _poly_market("Will Trump appoint a new U.S. Senate majority leader in 2026?"),
        "unrelated_political_event",
        id="appointment",
    ),
    pytest.param(
        _poly_market("Who will be confirmed as Secretary of State in 2026 in the U.S.?"),
        "unrelated_political_event",
        id="confirmation",
    ),
    pytest.param(
        _poly_market("Will the U.S. Senate pass the farm bill in 2026?"),
        "lacks_election_evidence",
        id="office-without-election",
    ),
    pytest.param(
        _poly_market("Will Trump be in the White House after the 2026 U.S. election?"),
        "lacks_election_evidence",
        id="white-house-is-not-the-house",
    ),
    pytest.param(
        _poly_market("Will the United States win the 2026 World Cup?"),
        "lacks_election_evidence",
        id="us-but-not-an-election",
    ),
    pytest.param(
        _poly_market("Will Democrats control the Senate in the 2026 U.S. midterms?", closed=True),
        "market_closed",
        id="closed",
    ),
    pytest.param(
        _poly_market("Will Democrats control the Senate in the 2026 U.S. midterms?", active=False),
        "market_inactive",
        id="inactive",
    ),
]


@pytest.mark.parametrize(("item", "reason"), POLY_INELIGIBLE)
def test_polymarket_ineligible_contests(item: dict, reason: str) -> None:
    assert is_2026_polymarket_market(item) == (False, reason)


def _kalshi_market(
    ticker: str,
    series: str,
    title: str,
    ev_title: str = "",
    rules: str = "",
    status: str = "open",
    **extra: object,
) -> tuple[dict, dict]:
    market: dict = {"ticker": ticker, "status": status, "title": title, "rules_primary": rules}
    market.update(extra)
    return market, {"title": ev_title, "series_ticker": series, "event_ticker": ticker.rsplit("-", 1)[0]}


KALSHI_ELIGIBLE = [
    pytest.param(("KXSENATE-26", "KXSENATE", "Will Democrats win the U.S. Senate in 2026?"), id="senate-title"),
    pytest.param(("HOUSE-26-A", "HOUSE", "Which party will win the U.S. House in 2026?"), id="house-title"),
    pytest.param(("KXGOV-26", "KXGOV", "Who will win the 2026 Ohio governor race?"), id="governor-state-context"),
    pytest.param(
        ("KXCTRL-26", "KXCTRL", "Will Republicans control the U.S. House after the 2026 midterms?"),
        id="chamber-control-title",
    ),
    pytest.param(("SENATEAZ-26-D", "SENATEAZ", "Democratic Party"), id="structured-senate-state"),
    pytest.param(("HOUSETX12-26-D", "HOUSETX12", "Democratic Party"), id="structured-house-district"),
    pytest.param(("GOVPARTYTX-26-D", "GOVPARTYTX", "Democratic Party"), id="structured-governor-state"),
    pytest.param(("CONTROLS-26-D", "CONTROLS", "Democratic Party"), id="structured-senate-control"),
    pytest.param(("KXCONTROLH-26-R", "KXCONTROLH", "Republican Party"), id="structured-house-control"),
    pytest.param(("CONTROLS-2026-D", "CONTROLS", "Yes"), id="structured-four-digit-year"),
    pytest.param(("CONTROLS-D", "CONTROLS", "Democratic Party"), id="structured-ticker-without-year"),
    pytest.param(("KXSENATE-26", "KXSENATE", "Will the Democrats win the Georgia Senate race in 2026?"), id="georgia-senate-race-kalshi"),
    pytest.param(
        ("KXSENATE-26", "KXSENATE", "Will Democrats win the U.S. Senate in 2026?", "", "Unlike in 2028, only 2026 counts."),
        id="rules-mention-other-year-as-background",
    ),
    pytest.param(
        ("KXSENATE-26-28", "KXSENATE", "Will Democrats win the U.S. Senate in 2026?"),
        id="seat-number-is-not-a-year",
    ),
]


@pytest.mark.parametrize("args", KALSHI_ELIGIBLE)
@pytest.mark.parametrize("primary_flag", [False, True])
def test_kalshi_eligible_contests(args: tuple, primary_flag: bool) -> None:
    market, event = _kalshi_market(*args)
    assert is_2026_kalshi_market(market, event, is_primary_match=primary_flag) == (True, None)


KALSHI_INELIGIBLE = [
    pytest.param(
        ("KXBRSENATE-26", "KXBRSENATE", "Who will win the Brazilian Senate election in 2026?"),
        "foreign_election",
        id="brazilian-senate-2026",
    ),
    pytest.param(
        ("KXGEPARL-26", "KXGEPARL", "Georgia parliamentary election in 2026"),
        "foreign_election",
        id="georgia-parliament-election",
    ),
    pytest.param(
        (
            "KXSENATE-28",
            "KXSENATE",
            "Who will win the U.S. Senate election in 2028?",
            "",
            "Compare with the 2026 election results.",
        ),
        "non_2026_primary_contest",
        id="us-senate-2028-rules-mention-2026",
    ),
    pytest.param(
        ("KXSENATE-28", "KXSENATE", "Will Democrats win the U.S. Senate in 2026?"),
        "non_2026_primary_contest",
        id="ticker-year-contradicts-title",
    ),
    pytest.param(
        ("CONTROLH-28-D", "CONTROLH", "Democratic Party"),
        "non_2026_primary_contest",
        id="structured-control-with-2028-ticker",
    ),
    pytest.param(
        ("SENATE-26-X", "SENATE", "Senate election 2026"),
        "lacks_us_jurisdiction",
        id="broad-senate-prefix-does-not-bypass",
    ),
    pytest.param(
        ("SENATEXX-26-D", "SENATEXX", "Democratic Party"),
        "lacks_election_evidence",
        id="invalid-state-abbreviation-not-structured",
    ),
    pytest.param(
        ("SENATEAZ-26-D", "SENATEAZ", "Australian Senate election 2026"),
        "conflicting_jurisdiction",
        id="structured-us-with-foreign-title",
    ),
    pytest.param(
        ("KXUKGEYEAR-26", "KXUKGEYEAR", "UK General Election in 2026", "UK General Election"),
        "foreign_election",
        id="uk-general-election",
    ),
    pytest.param(
        ("NBA-26", "NBA", "2026 NBA Finals winner"),
        "lacks_election_evidence",
        id="sports",
    ),
    pytest.param(
        ("KXSENATE-26", "KXSENATE", "Will the U.S. Senate majority leader resign in 2026?"),
        "unrelated_political_event",
        id="resignation",
    ),
    pytest.param(
        ("KXSENATE-26", "KXSENATE", "Closer Senate race 2026: Florida or South Carolina?"),
        "conflicting_state_context",
        id="multi-state-comparison",
    ),
    pytest.param(
        ("KXSENATE-26", "KXSENATE", "Will Democrats win the Texas state senate in 2026?"),
        "state_legislative_contest",
        id="state-legislature",
    ),
    pytest.param(
        ("KXSENATE-26", "KXSENATE", "Will the U.S. Senate pass the farm bill in 2026?"),
        "lacks_election_evidence",
        id="office-without-election",
    ),
    pytest.param(
        ("CONTROLS-26-D", "CONTROLS", "Democratic Party", "", "", "closed"),
        "market_inactive",
        id="closed-status",
    ),
    pytest.param(
        ("CONTROLS-26-D", "CONTROLS", "Democratic Party", "", "", ""),
        "market_inactive",
        id="missing-status",
    ),
]


@pytest.mark.parametrize(("args", "reason"), KALSHI_INELIGIBLE)
@pytest.mark.parametrize("primary_flag", [False, True])
def test_kalshi_ineligible_contests(args: tuple, reason: str, primary_flag: bool) -> None:
    extra = args[6] if len(args) > 6 else {}
    market, event = _kalshi_market(*args[:6], **extra)
    assert is_2026_kalshi_market(market, event, is_primary_match=primary_flag) == (False, reason)


# =====================================================================
# 8. Polymarket binary outcome-token mappings
# =====================================================================

class _PolyPageOpener:
    def __init__(self, items: list[dict]) -> None:
        self.items = items

    def open(self, _req: urllib.request.Request) -> io.BytesIO:
        events = [{"id": "ev-1", "title": "2026 Senate", "slug": "2026-senate", "markets": self.items}]
        return io.BytesIO(json.dumps(events).encode("utf-8"))


def _binary_market(tokens: object, cid: str = "bin-1") -> dict:
    return _poly_market(
        "Will Democrats win the U.S. Senate election in 2026?",
        "us-senate-2026",
        condition_id=cid,
        tokens=tokens,
    )


def _fetch_items(items: list[dict]) -> tuple[list[dict], dict[str, int]]:
    return fetch_polymarket_markets(RateLimiter(interval=0.0), opener=_PolyPageOpener(items))


_YES = {"outcome": "Yes", "token_id": "tok-yes", "price": 0.7}
_NO = {"outcome": "No", "token_id": "tok-no", "price": 0.3}

TOKEN_REJECTS = [
    pytest.param([], "non_binary_tokens", id="zero-tokens"),
    pytest.param([_YES], "non_binary_tokens", id="one-token"),
    pytest.param([_YES, _NO, {"outcome": "Maybe", "token_id": "tok-maybe"}], "non_binary_tokens", id="three-tokens"),
    pytest.param([_YES, _NO, dict(_YES, token_id="t3"), dict(_NO, token_id="t4")], "non_binary_tokens", id="four-tokens"),
    pytest.param(None, "invalid_tokens", id="tokens-null"),
    pytest.param("Yes/No", "invalid_tokens", id="tokens-not-a-list"),
    pytest.param([_YES, dict(_NO, outcome="Yes")], "invalid_token_mapping", id="duplicate-labels"),
    pytest.param([_YES, dict(_NO, outcome="yes")], "invalid_token_mapping", id="duplicate-labels-case-insensitive"),
    pytest.param([_YES, dict(_NO, token_id="tok-yes")], "invalid_token_mapping", id="duplicate-token-ids"),
    pytest.param([dict(_YES, outcome=None), _NO], "invalid_token_mapping", id="first-label-null"),
    pytest.param([_YES, dict(_NO, outcome=None)], "invalid_token_mapping", id="second-label-null"),
    pytest.param([_YES, dict(_NO, outcome="")], "invalid_token_mapping", id="blank-label"),
    pytest.param([_YES, dict(_NO, outcome="   ")], "invalid_token_mapping", id="whitespace-label"),
    pytest.param([_YES, dict(_NO, outcome="None")], "invalid_token_mapping", id="placeholder-label"),
    pytest.param([_YES, dict(_NO, outcome=7)], "invalid_token_mapping", id="non-string-label"),
    pytest.param([dict(_YES, token_id=None), _NO], "invalid_token_mapping", id="first-token-id-null"),
    pytest.param([_YES, dict(_NO, token_id=None)], "invalid_token_mapping", id="second-token-id-null"),
    pytest.param([_YES, dict(_NO, token_id="")], "invalid_token_mapping", id="blank-token-id"),
    pytest.param([_YES, dict(_NO, token_id=" ")], "invalid_token_mapping", id="whitespace-token-id"),
    pytest.param([_YES, dict(_NO, token_id="None")], "invalid_token_mapping", id="placeholder-token-id"),
    pytest.param([_YES, dict(_NO, token_id=123)], "invalid_token_mapping", id="non-string-token-id"),
    pytest.param([_YES, {"outcome": "No", "price": 0.3}], "invalid_token_mapping", id="missing-token-id"),
    pytest.param([_YES, {"token_id": "tok-no", "price": 0.3}], "invalid_token_mapping", id="missing-label"),
    pytest.param([_YES, "No"], "invalid_token_mapping", id="record-not-an-object"),
]


@pytest.mark.parametrize(("tokens", "reason"), TOKEN_REJECTS)
def test_polymarket_rejects_non_binary_or_malformed_token_mappings(tokens: object, reason: str) -> None:
    """The whole market is rejected; nothing is truncated or salvaged from a bad record."""
    markets, excluded = _fetch_items([_binary_market(copy.deepcopy(tokens))])
    assert markets == []
    assert excluded == {reason: 1}


def test_polymarket_accepts_exactly_two_valid_tokens() -> None:
    markets, excluded = _fetch_items([_binary_market(copy.deepcopy([_YES, _NO]))])
    assert excluded == {}
    assert len(markets) == 1
    market = markets[0]
    assert market["outcomes"] == ["Yes", "No"]
    assert market["clob_token_ids"] == ["tok-yes", "tok-no"]
    assert market["outcome_token_map"] == {"Yes": "tok-yes", "No": "tok-no"}
    assert market["snapshot_prices"]["outcome_prices"] == [0.7, 0.3]


def test_polymarket_reversed_token_order_keeps_label_token_price_association() -> None:
    markets, excluded = _fetch_items([_binary_market(copy.deepcopy([_NO, _YES]))])
    assert excluded == {}
    market = markets[0]
    assert market["outcomes"] == ["No", "Yes"]
    assert market["clob_token_ids"] == ["tok-no", "tok-yes"]
    assert market["outcome_token_map"] == {"No": "tok-no", "Yes": "tok-yes"}
    assert market["snapshot_prices"]["outcome_prices"] == [0.3, 0.7]
    prices = dict(zip(market["outcomes"], market["snapshot_prices"]["outcome_prices"], strict=True))
    assert prices == {"No": 0.3, "Yes": 0.7}


def test_polymarket_preserves_native_non_yes_no_labels() -> None:
    tokens = [
        {"outcome": "Democratic", "token_id": "tok-d", "price": 0.55},
        {"outcome": "Republican", "token_id": "tok-r", "price": 0.45},
    ]
    markets, excluded = _fetch_items([_binary_market(copy.deepcopy(tokens))])
    assert excluded == {}
    market = markets[0]
    assert market["outcomes"] == ["Democratic", "Republican"]
    assert market["outcome_token_map"] == {"Democratic": "tok-d", "Republican": "tok-r"}
    assert "Yes" not in market["outcome_token_map"] and "No" not in market["outcome_token_map"]


def test_polymarket_does_not_salvage_remaining_mapping_from_bad_record() -> None:
    """One malformed record rejects the market even though the other record is perfectly valid."""
    markets, excluded = _fetch_items([_binary_market([_YES, dict(_NO, token_id=None)])])
    assert markets == []
    assert excluded == {"invalid_token_mapping": 1}


# =====================================================================
# 9. Eligibility and mappings through the real main() harness
# =====================================================================

def _exclusions(out: str, venue: str) -> dict:
    (line,) = [ln for ln in out.splitlines() if ln.startswith("Found") and f"from {venue}" in ln]
    return ast.literal_eval(line.split("exclusions: ", 1)[1].rstrip(")"))


def _poly_noise() -> list[dict]:
    one = _poly_item("poly-bad-one-token", "Will Democrats win U.S. Senate seat 500 in 2026?", "us-senate-seat-500-2026")
    one["tokens"] = [
        {"outcome": "Yes", "token_id": "poly-bad-one-token-yes", "price": 0.5}
    ]
    three = _poly_item("poly-bad-three-tokens", "Will Democrats win U.S. Senate seat 501 in 2026?", "us-senate-seat-501-2026")
    three["tokens"] = [
        {"outcome": "Yes", "token_id": "poly-bad-three-tokens-yes", "price": 0.5},
        {"outcome": "No", "token_id": "poly-bad-three-tokens-no", "price": 0.5},
        {"outcome": "Maybe", "token_id": "poly-bad-three-tokens-maybe", "price": 0.0},
    ]
    null_label = _poly_item("poly-bad-null-label", "Will Democrats win U.S. Senate seat 502 in 2026?", "us-senate-seat-502-2026")
    null_label["tokens"] = [
        {"outcome": "Yes", "token_id": "poly-bad-null-label-yes", "price": 0.5},
        {"outcome": None, "token_id": "poly-bad-null-label-no", "price": 0.5},
    ]
    return [
        _poly_item("poly-bad-brazil", "Who will win the Brazilian Senate election in 2026?", "brazilian-senate-election-2026"),
        {
            **_poly_item("poly-bad-2028", "Who will win the U.S. Senate election in 2028?", "us-senate-election-2028"),
            "description": "Compare with the 2026 election results.",
        },
        one,
        three,
        null_label,
    ]


def _kalshi_noise() -> list[dict]:
    def event(ticker: str, series: str, title: str, rules: str = "", status: str = "open") -> dict:
        return {
            "title": title,
            "series_ticker": series,
            "event_ticker": ticker,
            "markets": [{"ticker": ticker, "status": status, "title": title, "rules_primary": rules}],
        }

    return [
        event("KXBRSENATE-26", "KXBRSENATE", "Who will win the Brazilian Senate election in 2026?"),
        event("KXSENATE-28", "KXSENATE", "Who will win the U.S. Senate election in 2028?", "Compare with the 2026 election results."),
        event("SENATE-26-X", "SENATE", "Senate election 2026"),
        event("KXSENATE-26-C", "KXSENATE", "Will Democrats win the U.S. Senate in 2026?", status="closed"),
    ]


def test_main_excludes_ineligible_markets_and_invalid_mappings_from_successful_output(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    poly, kalshi = _valid_poly(), _valid_kalshi()
    poly[100].append(_poly_event("ev-noise", "Noise", "noise", _poly_noise()))
    kalshi["k2"]["events"] = kalshi["k2"]["events"] + _kalshi_noise()
    env = _install(tmp_path, monkeypatch, poly, kalshi)
    code, out, _err = _run_main(capsys)

    assert env.poly_offsets == [0, 100]
    assert env.kalshi_cursors == [None, "k2"]
    published = _assert_success(env, code, POLY_IDS, KALSHI_IDS)
    assert all(len(m["outcomes"]) == 2 for m in published["polymarket"])
    assert _exclusions(out, "Polymarket") == {
        "foreign_election": 1,
        "non_2026_primary_contest": 1,
        "non_binary_tokens": 2,
        "invalid_token_mapping": 1,
    }
    assert _exclusions(out, "Kalshi") == {
        "foreign_election": 1,
        "non_2026_primary_contest": 1,
        "lacks_us_jurisdiction": 1,
        "market_inactive": 1,
    }


def _assert_validation_failure(env: _Env, code: object, err: str, reason: str) -> None:
    assert env.poly_offsets == [0, 100]
    assert env.kalshi_cursors == [None, "k2"]
    assert code == 1
    assert "VALIDATION FAILED ON NEW DATASET" in err
    assert reason in err
    _assert_preserved(env)


def test_main_invalid_mapping_removing_house_control_preserves_latest(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    poly = _valid_poly()
    # House control market with only 1 token
    poly[0][1]["markets"][0]["clobTokenIds"] = json.dumps(["only-one-tok"])
    poly[0][1]["markets"][0]["outcomes"] = json.dumps(["Yes"])
    env = _install(tmp_path, monkeypatch, poly, _valid_kalshi())
    code, _out, err = _run_main(capsys)
    _assert_validation_failure(env, code, err, "Polymarket missing House Control market")


def test_main_ineligible_senate_control_preserves_latest(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    poly = _valid_poly()
    # Senate control made ineligible by conflicting primary year 2028
    poly[0][0]["markets"][0]["question"] = "Will Democrats control the Senate in 2028?"
    poly[0][0]["markets"][0]["slug"] = "dems-control-the-senate-2028"
    env = _install(tmp_path, monkeypatch, poly, _valid_kalshi())
    code, _out, err = _run_main(capsys)
    _assert_validation_failure(env, code, err, "Polymarket missing Senate Control market")


def test_main_conflicting_kalshi_house_control_year_preserves_latest(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    kalshi = _valid_kalshi()
    house_event = kalshi[""]["events"][1]
    house_event["title"] = "2028 U.S. House Control"
    house_event["markets"][0]["title"] = "2026 U.S. House Control 2028"
    env = _install(tmp_path, monkeypatch, _valid_poly(), kalshi)
    code, _out, err = _run_main(capsys)
    _assert_validation_failure(env, code, err, "Kalshi missing House Control market")
