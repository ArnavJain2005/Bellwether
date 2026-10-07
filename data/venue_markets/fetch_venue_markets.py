"""Fetch 2026 US midterm election markets from Polymarket and Kalshi.

Saves active markets to data/venue_markets/output/venue_markets_<date>.json
and data/venue_markets/output/venue_markets_latest.json.

Standard library only (Python 3.11+).
"""

from __future__ import annotations

import argparse
import http.client
import json
import os
import random
import re
import ssl
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

USER_AGENT = "bellwether/0.1 (research)"
MIN_REQUEST_INTERVAL = 0.3  # at most 4 requests per second per venue


class FetchError(Exception):
    """Raised when venue market fetching or pagination fails."""


# Regex matching primary target series on Kalshi
KALSHI_TARGET_SERIES_RE = re.compile(
    r"^(?:KX)?(?:CONTROLS|CONTROLH|SENATE.*|GOVPARTY.*|HOUSE(?:PARTY)?[-_]?[A-Z]{2}[-_]?(?:\d+|AL).*)$",
    re.IGNORECASE,
)

US_STATES = [
    "alabama", "alaska", "arizona", "arkansas", "california", "colorado",
    "connecticut", "delaware", "florida", "georgia", "hawaii", "idaho",
    "illinois", "indiana", "iowa", "kansas", "kentucky", "louisiana",
    "maine", "maryland", "massachusetts", "michigan", "minnesota",
    "mississippi", "missouri", "montana", "nebraska", "nevada",
    "new hampshire", "new jersey", "new mexico", "new york",
    "north carolina", "north dakota", "ohio", "oklahoma", "oregon",
    "pennsylvania", "rhode island", "south carolina", "south dakota",
    "tennessee", "texas", "utah", "vermont", "virginia", "washington",
    "west virginia", "wisconsin", "wyoming",
]

# Longer state names must be tested before substrings (e.g. "west virginia" before "virginia")
US_STATES_BY_LENGTH = sorted(US_STATES, key=len, reverse=True)

STATE_ABBRS = {
    "AL": "alabama", "AK": "alaska", "AZ": "arizona", "AR": "arkansas",
    "CA": "california", "CO": "colorado", "CT": "connecticut", "DE": "delaware",
    "FL": "florida", "GA": "georgia", "HI": "hawaii", "ID": "idaho",
    "IL": "illinois", "IN": "indiana", "IA": "iowa", "KS": "kansas",
    "KY": "kentucky", "LA": "louisiana", "ME": "maine", "MD": "maryland",
    "MA": "massachusetts", "MI": "michigan", "MN": "minnesota", "MS": "mississippi",
    "MO": "missouri", "MT": "montana", "NE": "nebraska", "NV": "nevada",
    "NH": "new hampshire", "NJ": "new jersey", "NM": "new mexico", "NY": "new york",
    "NC": "north carolina", "ND": "north dakota", "OH": "ohio", "OK": "oklahoma",
    "OR": "oregon", "PA": "pennsylvania", "RI": "rhode island", "SC": "south carolina",
    "SD": "south dakota", "TN": "tennessee", "TX": "texas", "UT": "utah",
    "VT": "vermont", "VA": "virginia", "WA": "washington", "WV": "west virginia",
    "WI": "wisconsin", "WY": "wyoming",
}


def _safe_parse_json(text: str | None, default: Any = None) -> Any:
    if not text:
        return default
    try:
        return json.loads(text)
    except (json.JSONDecodeError, TypeError):
        return default


def _safe_float(val: Any) -> float | None:
    if val is None:
        return None
    try:
        return float(val)
    except (ValueError, TypeError):
        return None


def _to_cents(dollars_val: Any, native_cents: Any = None) -> int | None:
    if native_cents is not None:
        try:
            return int(native_cents)
        except (ValueError, TypeError):
            pass
    if dollars_val is not None:
        try:
            return round(float(dollars_val) * 100)
        except (ValueError, TypeError):
            pass
    return None


class RateLimiter:
    """Enforces sequential requests at most `1 / interval` per second."""

    def __init__(self, interval: float = MIN_REQUEST_INTERVAL) -> None:
        self.interval = interval
        self.last_request_time = 0.0

    def wait(self) -> None:
        now = time.time()
        elapsed = now - self.last_request_time
        if elapsed < self.interval:
            time.sleep(self.interval - elapsed)
        self.last_request_time = time.time()


# =====================================================================
# State & Race Classification
# =====================================================================

def extract_states_from_text(text: str) -> list[str]:
    """Find all US states mentioned as whole words in text, handling multi-word states first."""
    # Replace hyphens with spaces to properly handle slugs and hyphenated text (e.g. 'west-virginia')
    masked = text.lower().replace("-", " ")
    found: list[str] = []
    for state in US_STATES_BY_LENGTH:
        pattern = rf"\b{re.escape(state)}\b"
        if re.search(pattern, masked):
            found.append(state)
            # Mask out the matched state so substrings like 'virginia' inside 'west virginia' cannot re-match
            masked = re.sub(pattern, " ", masked)
    return found


def extract_race_state(venue: str, market: dict[str, Any], race_type: str) -> str | None:
    """Extract US state associated with a race, rejecting multi-state comparisons."""
    if race_type not in ("senate", "governor"):
        return None

    if venue == "kalshi":
        series = (market.get("series_ticker") or "").upper()
        ticker = (market.get("market_ticker") or "").upper()
        for prefix in ("GOVPARTY", "KXGOVPARTY", "SENATEPARTY-", "SENATEPARTY", "SENATE", "KXSENATE"):
            if series.startswith(prefix):
                abbr = series[len(prefix):][:2]
                if abbr in STATE_ABBRS:
                    return STATE_ABBRS[abbr]
            if ticker.startswith(prefix):
                abbr = ticker[len(prefix):][:2]
                if abbr in STATE_ABBRS:
                    return STATE_ABBRS[abbr]

    # Text extraction for Polymarket / Kalshi title fallback
    if venue == "polymarket":
        text = f"{market.get('question', '')} {market.get('slug', '')}"
    else:
        text = f"{market.get('title', '')} {market.get('subtitle', '')}"

    states = extract_states_from_text(text)
    # If comparison between 2 or more states (e.g. 'Florida or South Carolina'), return None
    if len(states) == 1:
        return states[0]
    if len(states) > 1:
        return None

    # Check 2-letter abbreviation word boundaries
    upper_text = text.upper()
    for abbr, full in STATE_ABBRS.items():
        if re.search(rf"\b{abbr}\b", upper_text):
            return full

    return None


def classify_race(venue: str, market: dict[str, Any]) -> str:
    """Classify a market as 'senate', 'house', 'governor', 'control', or 'other'."""
    if venue == "polymarket":
        q = (market.get("question") or "").lower()
        slug = (market.get("slug") or "").lower()
        text = f"{q} {slug}"
        # Multi-state comparisons are classified as 'other'
        states = extract_states_from_text(text)
        if len(states) >= 2 or "closer" in text:
            return "other"
        if any(k in text for k in ["control the senate", "control the house", "balance of power"]):
            return "control"
        if "governor" in text:
            return "governor"
        if "senate" in text:
            return "senate"
        if "house" in text:
            return "house"
        return "other"

    # Kalshi
    ticker = (market.get("market_ticker") or "").upper()
    series = (market.get("series_ticker") or "").upper()
    title = (market.get("title") or "").lower()
    text = f"{ticker} {series} {title}"

    if any(k in series for k in ["CONTROLS", "CONTROLH"]) or any(k in text for k in ["win the senate", "win the house", "senate control", "house control"]):
        return "control"
    if series.startswith("GOVPARTY") or "governor" in title:
        return "governor"
    if series.startswith("SENATE") or "senate" in title:
        return "senate"
    if series.startswith("HOUSE") or "house" in title or re.search(r"\b[a-z]{2}-\d{2}\b", title):
        return "house"
    return "other"


# =====================================================================
# Market Eligibility Verification
# =====================================================================

def is_2026_polymarket_market(item: dict[str, Any]) -> tuple[bool, str]:
    """Verify whether a Polymarket record belongs to the 2026 US midterm election cycle."""
    if item.get("closed") is not False:
        return False, "market_closed"
    if item.get("active") is not True:
        return False, "market_inactive"

    q = (item.get("question") or "").lower()
    slug = (item.get("market_slug") or "").lower()
    desc = (item.get("description") or "").lower()
    combined_text = f"{q} {slug} {desc}"

    # Explicit exclusion for other election years or conflicting years
    if "2028" in combined_text and "2026" in combined_text:
        return False, "conflicting_year_evidence"
    if ("2028" in combined_text or "2024" in combined_text) and "2026" not in combined_text:
        return False, "non_2026_cycle"

    # Negative keyword checks for non-midterm/unrelated politics
    if any(k in q for k in ["steps down", "confirmed as secretary", "confirmed as member", "resigns", "resignation"]):
        return False, "unrelated_political_event"

    # Affirmative evidence of the relevant 2026 US election contest is required
    # Year 2026 must be explicitly mentioned in textual content (closing year alone is insufficient)
    if "2026" not in combined_text and "midterm" not in combined_text:
        return False, "lacks_2026_cycle_evidence"

    election_keywords = ["senate", "house", "governor", "election", "midterm", "elections", "gubernatorial", "control"]
    if not any(k in combined_text for k in election_keywords):
        return False, "lacks_election_evidence"

    return True, "verified_2026"


def is_2026_kalshi_market(m: dict[str, Any], ev: dict[str, Any], is_primary_match: bool) -> tuple[bool, str | None]:
    """Verify whether a Kalshi market belongs to the 2026 US midterm election cycle."""
    m_status = (m.get("status") or "").lower()
    if m_status and m_status not in ("open", "active"):
        return False, "market_inactive"

    m_ticker = (m.get("ticker") or "").upper()
    m_series = (m.get("series_ticker") or ev.get("series_ticker") or "").upper()
    m_title = (m.get("title") or ev.get("title") or "")
    ev_title = (ev.get("title") or "")
    rules_text = (m.get("rules_primary") or "") + " " + (m.get("rules_secondary") or "")
    search_text = f"{m_ticker} {m_series} {m_title} {ev_title} {rules_text}".lower()

    # Reject foreign elections or non-election bets
    if any(k in search_text for k in ["uk general", "chancellor of germany", "cabinet leave", "federal reserve board of", "reconciliation bill"]):
        return False, "foreign_election"

    # Require explicit 2026 evidence for ALL matches (including primary matches)
    if "2026" not in search_text:
        return False, "lacks_2026_cycle_evidence"

    if "2028" in search_text and "2026" in search_text:
        return False, "conflicting_year_evidence"
    if ("2028" in search_text or "2024" in search_text) and "2026" not in search_text:
        return False, "non_2026_cycle"

    return True, None


# =====================================================================
# Polymarket Fetcher
# =====================================================================

def fetch_polymarket_markets(
    rate_limiter: RateLimiter,
    base_url: str = "https://clob.polymarket.com/markets",
    opener: Any = None,
) -> tuple[list[dict[str, Any]], dict[str, int]]:
    """Fetch active 2026 midterm markets from Polymarket CLOB API."""
    print("Fetching Polymarket markets...", flush=True)
    markets_by_id: dict[str, dict[str, Any]] = {}
    excluded_counts: dict[str, int] = {}
    seen_cursors: set[str] = set()

    next_cursor = ""
    while next_cursor != "LTE=":
        if next_cursor in seen_cursors:
            raise FetchError(f"Polymarket pagination loop detected: repeated cursor {next_cursor}")
        if next_cursor:
            seen_cursors.add(next_cursor)

        params: dict[str, Any] = {"limit": 1000}
        if next_cursor:
            params["next_cursor"] = next_cursor

        url = f"{base_url}?{urllib.parse.urlencode(params)}"
        req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})

        data = None
        for attempt in range(5):
            rate_limiter.wait()
            try:
                if opener is not None:
                    resp_obj = opener.open(req)
                    body_bytes = resp_obj.read()
                else:
                    with urllib.request.urlopen(req, timeout=15) as resp:
                        body_bytes = resp.read()
                data = json.loads(body_bytes.decode("utf-8"))
                break
            except urllib.error.HTTPError as e:
                if attempt < 4:
                    time.sleep((2 ** attempt) + random.uniform(0, 1))
                else:
                    raise FetchError(f"Polymarket HTTP {e.code} at cursor {next_cursor}: {e}") from e
            except Exception as e:
                if attempt < 4:
                    time.sleep((2 ** attempt) + random.uniform(0, 1))
                else:
                    raise FetchError(f"Polymarket request failed at cursor {next_cursor}: {e}") from e

        if not isinstance(data, dict) or "data" not in data:
            raise FetchError(f"Polymarket malformed response at cursor {next_cursor}: expected dict with 'data'")

        markets = data.get("data") or []
        for item in markets:
            m_id = item.get("condition_id")
            if not isinstance(m_id, str) or not m_id.strip() or m_id == "None":
                excluded_counts["missing_id"] = excluded_counts.get("missing_id", 0) + 1
                continue

            is_eligible, reason = is_2026_polymarket_market(item)
            if not is_eligible:
                excluded_counts[reason] = excluded_counts.get(reason, 0) + 1
                continue

            tokens = item.get("tokens") or []
            if not isinstance(tokens, list):
                excluded_counts["invalid_tokens"] = excluded_counts.get("invalid_tokens", 0) + 1
                continue

            outcomes = []
            clob_tokens = []
            prices = []
            for t in tokens:
                outcome = str(t.get("outcome", "")).strip()
                tid = str(t.get("token_id", "")).strip()
                if outcome and tid and tid != "None":
                    outcomes.append(outcome)
                    clob_tokens.append(tid)
                    prices.append(_safe_float(t.get("price")))

            if not outcomes or not clob_tokens or len(outcomes) != len(clob_tokens) or len(set(outcomes)) != len(outcomes) or len(set(clob_tokens)) != len(clob_tokens):
                excluded_counts["invalid_token_mapping"] = excluded_counts.get("invalid_token_mapping", 0) + 1
                continue

            outcome_token_map = dict(zip(outcomes, clob_tokens))

            snapshot_prices = {
                "is_snapshot": True,
                "outcome_prices": prices,
                "best_bid": None,
                "best_ask": None,
                "last_trade_price": None,
            }

            market_record = {
                "venue": "polymarket",
                "market_id": m_id,
                "question": item.get("question"),
                "slug": item.get("market_slug"),
                "outcomes": outcomes,
                "clob_token_ids": clob_tokens,
                "outcome_token_map": outcome_token_map,
                "snapshot_prices": snapshot_prices,
                "end_date": item.get("end_date_iso"),
                "resolution_description": item.get("description"),
                "event_id": None,
                "event_title": None,
                "event_slug": None,
            }
            markets_by_id[m_id] = market_record

        returned_cursor = data.get("next_cursor")
        if not returned_cursor or returned_cursor == next_cursor:
            if returned_cursor != "LTE=":
                raise FetchError(f"Polymarket pagination failed to progress. Cursor stuck at: {returned_cursor}")
            break
        next_cursor = returned_cursor

    return list(markets_by_id.values()), excluded_counts


# =====================================================================
# Kalshi Fetcher
# =====================================================================

def _kalshi_request(
    host: str,
    path: str,
    rate_limiter: RateLimiter | None = None,
    timeout: float = 25.0,
) -> tuple[int, bytes]:
    # Rate limiter applied before EVERY request attempt
    if rate_limiter:
        rate_limiter.wait()

    ctx = ssl.create_default_context()
    conn = http.client.HTTPSConnection(host, 443, context=ctx, timeout=timeout)
    try:
        headers = {
            "User-Agent": USER_AGENT,
            "Accept": "application/json",
            "Connection": "close",
        }
        conn.request("GET", path, headers=headers)
        resp = conn.getresponse()
        data = resp.read()
        return resp.status, data
    finally:
        conn.close()


def fetch_kalshi_markets(
    rate_limiter: RateLimiter,
    primary_host: str = "api.elections.kalshi.com",
    fallback_host: str = "external-api.kalshi.com",
    requester: Any = None,
) -> tuple[str, list[dict[str, Any]], dict[str, int]]:
    """Fetch active 2026 midterm markets from Kalshi Trade API v2."""
    print("Fetching Kalshi markets...", flush=True)
    active_host = primary_host
    actual_serving_host = primary_host

    markets_by_ticker: dict[str, dict[str, Any]] = {}
    excluded_counts: dict[str, int] = {}
    seen_cursors: set[str] = set()

    cursor = ""
    page = 0
    limit = 200

    while True:
        # Detect pagination cursor loop
        if cursor and cursor in seen_cursors:
            raise FetchError(f"Kalshi pagination loop detected: repeated cursor {cursor}")
        if cursor:
            seen_cursors.add(cursor)

        query_params: dict[str, Any] = {
            "status": "open",
            "limit": limit,
            "with_nested_markets": "true",
        }
        if cursor:
            query_params["cursor"] = cursor
        path = f"/trade-api/v2/events?{urllib.parse.urlencode(query_params)}"

        page_data: dict[str, Any] | None = None
        for attempt in range(5):  # 1 initial + up to 4 retries
            try:
                if requester is not None:
                    status, body = requester(active_host, path)
                else:
                    status, body = _kalshi_request(active_host, path, rate_limiter=rate_limiter)

                if status == 200:
                    page_data = json.loads(body.decode("utf-8"))
                    actual_serving_host = active_host
                    break
                print(f"  Kalshi HTTP {status} from {active_host} on attempt {attempt + 1}", flush=True)
            except (http.client.HTTPException, ssl.SSLError, OSError, TimeoutError) as e:
                # Connection error: fall back immediately if on primary host
                if active_host == primary_host:
                    print(f"  Connection error on {primary_host} ({e}); falling back to {fallback_host}", flush=True)
                    active_host = fallback_host
                    actual_serving_host = fallback_host
                    # Immediate attempt on fallback host with rate limiter
                    try:
                        if requester is not None:
                            status, body = requester(active_host, path)
                        else:
                            status, body = _kalshi_request(active_host, path, rate_limiter=rate_limiter)
                        if status == 200:
                            page_data = json.loads(body.decode("utf-8"))
                            break
                    except (http.client.HTTPException, ssl.SSLError, OSError, TimeoutError) as fb_err:
                        e = fb_err
                if attempt == 4:
                    raise FetchError(f"Exhausted 4 retries on {active_host} for page {page + 1}: {e}") from e

                backoff = 1.0 * (1.8 ** attempt) + random.uniform(0.2, 0.5)
                print(f"  Kalshi connection reset ({e}); retrying in {backoff:.2f}s (retry {attempt + 1}/4)...", flush=True)
                time.sleep(backoff)

        if page_data is None:
            raise FetchError(f"Failed to retrieve Kalshi page {page + 1} after retries")

        events = page_data.get("events")
        if not isinstance(events, list):
            raise FetchError(f"Kalshi malformed response on page {page + 1}: expected 'events' list")

        new_cursor = page_data.get("cursor") or ""
        page += 1

        for ev in events:
            series_ticker = ev.get("series_ticker") or ""
            ev_title = ev.get("title") or ""
            ev_subtitle = ev.get("sub_title") or ""
            ev_ticker = ev.get("event_ticker") or ""

            for m in ev.get("markets") or []:
                m_ticker = m.get("ticker") or ""
                if not m_ticker:
                    excluded_counts["missing_market_ticker"] = excluded_counts.get("missing_market_ticker", 0) + 1
                    continue

                m_series = m.get("series_ticker") or series_ticker
                m_title = m.get("title") or ev_title

                is_primary_match = bool(
                    KALSHI_TARGET_SERIES_RE.match(m_series)
                    or KALSHI_TARGET_SERIES_RE.match(m_ticker)
                )

                is_eligible, filter_note = is_2026_kalshi_market(m, ev, is_primary_match)
                if not is_eligible:
                    reason = "inactive_or_non_2026"
                    excluded_counts[reason] = excluded_counts.get(reason, 0) + 1
                    continue

                rules_primary = m.get("rules_primary") or ""
                rules_secondary = m.get("rules_secondary") or ""
                rules_text = f"{rules_primary}\n{rules_secondary}".strip()

                yes_bid_dollars = m.get("yes_bid_dollars")
                yes_ask_dollars = m.get("yes_ask_dollars")
                last_price_dollars = m.get("last_price_dollars")
                yes_bid = m.get("yes_bid")
                yes_ask = m.get("yes_ask")
                last_price = m.get("last_price")

                snapshot_prices = {
                    "is_snapshot": True,
                    "yes_bid_dollars": yes_bid_dollars,
                    "yes_ask_dollars": yes_ask_dollars,
                    "last_price_dollars": last_price_dollars,
                    "yes_bid": yes_bid,
                    "yes_ask": yes_ask,
                    "last_price": last_price,
                    "yes_bid_cents": _to_cents(yes_bid_dollars, yes_bid),
                    "yes_ask_cents": _to_cents(yes_ask_dollars, yes_ask),
                    "last_price_cents": _to_cents(last_price_dollars, last_price),
                }

                subtitle = m.get("yes_sub_title") or m.get("subtitle") or ev_subtitle

                market_record = {
                    "venue": "kalshi",
                    "market_ticker": m_ticker,
                    "event_ticker": ev_ticker,
                    "series_ticker": m_series,
                    "title": m_title,
                    "subtitle": subtitle,
                    "snapshot_prices": snapshot_prices,
                    "close_time": m.get("close_time"),
                    "rules_text": rules_text,
                    "filter_note": filter_note,
                }
                markets_by_ticker[m_ticker] = market_record

        if page % 5 == 0 or not new_cursor or not events:
            print(f"  Kalshi page {page}: total kept markets so far: {len(markets_by_ticker)}", flush=True)

        if not new_cursor or not events:
            break
        cursor = new_cursor

    print(f"Kalshi complete. Served by {actual_serving_host}. Total markets: {len(markets_by_ticker)}", flush=True)
    return actual_serving_host, list(markets_by_ticker.values()), excluded_counts


# =====================================================================
# Validation & Atomic Output
# =====================================================================

def validate_dataset(data: dict[str, Any]) -> list[str]:
    """Inspect dataset and return a list of failure descriptions, or empty list if valid."""
    errors: list[str] = []
    polymarket_markets = data.get("polymarket", [])
    kalshi_markets = data.get("kalshi", [])

    if len(polymarket_markets) < 100:
        errors.append(f"Polymarket has fewer than 100 markets ({len(polymarket_markets)})")
    if len(kalshi_markets) < 100:
        errors.append(f"Kalshi has fewer than 100 markets ({len(kalshi_markets)})")

    # ID integrity
    poly_ids = [m.get("market_id") for m in polymarket_markets]
    if any(not m_id for m_id in poly_ids):
        errors.append("Polymarket has markets missing market_id")
    if len(poly_ids) != len(set(filter(None, poly_ids))):
        errors.append("Polymarket has duplicate market_ids")

    kalshi_ids = [m.get("market_ticker") for m in kalshi_markets]
    if any(not t for t in kalshi_ids):
        errors.append("Kalshi has markets missing market_ticker")
    if len(kalshi_ids) != len(set(filter(None, kalshi_ids))):
        errors.append("Kalshi has duplicate market_tickers")

    # Chamber control checks
    poly_has_senate_ctrl = any("senate" in (m.get("question") or "").lower() and classify_race("polymarket", m) == "control" for m in polymarket_markets)
    poly_has_house_ctrl = any("house" in (m.get("question") or "").lower() and classify_race("polymarket", m) == "control" for m in polymarket_markets)
    kalshi_has_senate_ctrl = any(("CONTROLS" in (m.get("series_ticker") or "") or "senate" in (m.get("title") or "").lower()) and classify_race("kalshi", m) == "control" for m in kalshi_markets)
    kalshi_has_house_ctrl = any(("CONTROLH" in (m.get("series_ticker") or "") or "house" in (m.get("title") or "").lower()) and classify_race("kalshi", m) == "control" for m in kalshi_markets)

    if not poly_has_senate_ctrl:
        errors.append("Polymarket missing Senate Control market")
    if not poly_has_house_ctrl:
        errors.append("Polymarket missing House Control market")
    if not kalshi_has_senate_ctrl:
        errors.append("Kalshi missing Senate Control market")
    if not kalshi_has_house_ctrl:
        errors.append("Kalshi missing House Control market")

    return errors


def publish_output(dated_file: Path, latest_file: Path, output_payload: dict[str, Any]) -> None:
    """Atomically write and replace output files through temporary files."""
    dated_file.parent.mkdir(parents=True, exist_ok=True)
    serialized = json.dumps(output_payload, indent=2)

    tmp_dated = dated_file.with_suffix(".tmp")
    tmp_latest = latest_file.with_suffix(".tmp")

    try:
        with open(tmp_dated, "w", encoding="utf-8") as f:
            f.write(serialized)
        with open(tmp_latest, "w", encoding="utf-8") as f:
            f.write(serialized)

        # Atomic replacement
        os.replace(tmp_dated, dated_file)
        os.replace(tmp_latest, latest_file)
    finally:
        if tmp_dated.exists():
            tmp_dated.unlink()
        if tmp_latest.exists():
            tmp_latest.unlink()


def run_validation(latest_path: Path) -> int:
    """Validate and summarize the latest output file."""
    if not latest_path.exists():
        print(f"Error: {latest_path} does not exist. Run fetch first.", file=sys.stderr)
        return 1

    with open(latest_path, "r", encoding="utf-8") as f:
        data = json.load(f)

    fetched_at = data.get("fetched_at")
    kalshi_host = data.get("kalshi_host")
    polymarket_markets = data.get("polymarket", [])
    kalshi_markets = data.get("kalshi", [])

    print("=" * 70)
    print("VENUE MARKETS VALIDATION REPORT")
    print("=" * 70)
    print(f"Fetched At:   {fetched_at}")
    print(f"Kalshi Host:  {kalshi_host}")
    print(f"Total Counts: Polymarket: {len(polymarket_markets)}, Kalshi: {len(kalshi_markets)}")
    print("-" * 70)

    # 1. Duplicate & Missing IDs
    poly_ids = [m.get("market_id") for m in polymarket_markets]
    poly_missing = sum(1 for m_id in poly_ids if not m_id)
    poly_dupes = len(poly_ids) - len(set(filter(None, poly_ids)))

    kalshi_ids = [m.get("market_ticker") for m in kalshi_markets]
    kalshi_missing = sum(1 for t in kalshi_ids if not t)
    kalshi_dupes = len(kalshi_ids) - len(set(filter(None, kalshi_ids)))

    print("ID INTEGRITY:")
    print(f"  Polymarket missing IDs: {poly_missing}, duplicates: {poly_dupes}")
    print(f"  Kalshi missing IDs:     {kalshi_missing}, duplicates: {kalshi_dupes}")
    print("-" * 70)

    # 2. Race Type Counts
    poly_types: dict[str, int] = {}
    for m in polymarket_markets:
        rtype = classify_race("polymarket", m)
        poly_types[rtype] = poly_types.get(rtype, 0) + 1

    kalshi_types: dict[str, int] = {}
    for m in kalshi_markets:
        rtype = classify_race("kalshi", m)
        kalshi_types[rtype] = kalshi_types.get(rtype, 0) + 1

    print("MARKET COUNTS BY RACE TYPE:")
    for rtype in ["senate", "house", "governor", "control", "other"]:
        print(f"  {rtype.capitalize():<10} -> Polymarket: {poly_types.get(rtype, 0):<5} | Kalshi: {kalshi_types.get(rtype, 0):<5}")
    print("-" * 70)

    # 3. Chamber Control Presence
    poly_has_senate_ctrl = any("senate" in (m.get("question") or "").lower() and classify_race("polymarket", m) == "control" for m in polymarket_markets)
    poly_has_house_ctrl = any("house" in (m.get("question") or "").lower() and classify_race("polymarket", m) == "control" for m in polymarket_markets)
    kalshi_has_senate_ctrl = any(("CONTROLS" in (m.get("series_ticker") or "") or "senate" in (m.get("title") or "").lower()) and classify_race("kalshi", m) == "control" for m in kalshi_markets)
    kalshi_has_house_ctrl = any(("CONTROLH" in (m.get("series_ticker") or "") or "house" in (m.get("title") or "").lower()) and classify_race("kalshi", m) == "control" for m in kalshi_markets)

    print("CHAMBER CONTROL MARKETS:")
    print(f"  Polymarket Senate Control Present: {'YES' if poly_has_senate_ctrl else 'NO'}")
    print(f"  Polymarket House Control Present:  {'YES' if poly_has_house_ctrl else 'NO'}")
    print(f"  Kalshi Senate Control Present:     {'YES' if kalshi_has_senate_ctrl else 'NO'}")
    print(f"  Kalshi House Control Present:      {'YES' if kalshi_has_house_ctrl else 'NO'}")
    print("-" * 70)

    # 4. Distinct State Coverage
    poly_senate_states = {
        extract_race_state("polymarket", m, "senate")
        for m in polymarket_markets
        if classify_race("polymarket", m) == "senate"
    } - {None}

    poly_gov_states = {
        extract_race_state("polymarket", m, "governor")
        for m in polymarket_markets
        if classify_race("polymarket", m) == "governor"
    } - {None}

    kalshi_senate_states = {
        extract_race_state("kalshi", m, "senate")
        for m in kalshi_markets
        if classify_race("kalshi", m) == "senate"
    } - {None}

    kalshi_gov_states = {
        extract_race_state("kalshi", m, "governor")
        for m in kalshi_markets
        if classify_race("kalshi", m) == "governor"
    } - {None}

    print("DISTINCT STATE COVERAGE:")
    print(f"  Polymarket States with Senate Markets:   {len(poly_senate_states)}")
    print(f"  Polymarket States with Governor Markets: {len(poly_gov_states)}")
    print(f"  Kalshi States with Senate Markets:       {len(kalshi_senate_states)}")
    print(f"  Kalshi States with Governor Markets:     {len(kalshi_gov_states)}")
    print("  (Note: Expected 33 Class 2 Senate seats and 36 Gubernatorial seats in 2026)")
    print("=" * 70)

    errors = validate_dataset(data)
    if errors:
        print("VALIDATION FAILURES DETECTED:", file=sys.stderr)
        for err in errors:
            print(f"  - {err}", file=sys.stderr)
        return 1

    print("VALIDATION SUCCESSFUL: All checks passed.")
    return 0


# =====================================================================
# Main CLI
# =====================================================================

def main() -> None:
    parser = argparse.ArgumentParser(description="Fetch 2026 US midterm election markets from Polymarket and Kalshi.")
    parser.add_argument("--validate", action="store_true", help="Validate existing latest output without re-fetching.")
    args = parser.parse_args()

    base_dir = Path(__file__).resolve().parent
    output_dir = base_dir / "output"
    latest_file = output_dir / "venue_markets_latest.json"

    if args.validate:
        sys.exit(run_validation(latest_file))

    today_str = datetime.now(UTC).strftime("%Y-%m-%d")
    dated_file = output_dir / f"venue_markets_{today_str}.json"

    rate_limiter = RateLimiter(MIN_REQUEST_INTERVAL)
    try:
        polymarket_markets, poly_excluded = fetch_polymarket_markets(rate_limiter)
        kalshi_host, kalshi_markets, kalshi_excluded = fetch_kalshi_markets(rate_limiter)
    except Exception as e:  # noqa: BLE001
        print(f"\nFETCH FAILED: {e}", file=sys.stderr, flush=True)
        print("Previous latest output preserved byte-for-byte; no output updated.", file=sys.stderr, flush=True)
        sys.exit(1)

    print(f"Found {len(polymarket_markets)} eligible markets from Polymarket (exclusions: {poly_excluded})", flush=True)
    print(f"Found {len(kalshi_markets)} eligible markets from Kalshi (exclusions: {kalshi_excluded})", flush=True)

    output_payload = {
        "fetched_at": datetime.now(UTC).isoformat(),
        "kalshi_host": kalshi_host,
        "polymarket_count": len(polymarket_markets),
        "kalshi_count": len(kalshi_markets),
        "polymarket": polymarket_markets,
        "kalshi": kalshi_markets,
    }

    errors = validate_dataset(output_payload)
    if errors:
        print("\nVALIDATION FAILED ON NEW DATASET:", file=sys.stderr, flush=True)
        for err in errors:
            print(f"  - {err}", file=sys.stderr, flush=True)
        print("Previous latest output preserved byte-for-byte; no output updated.", file=sys.stderr, flush=True)
        sys.exit(1)

    publish_output(dated_file, latest_file, output_payload)
    print(f"Saved {len(polymarket_markets) + len(kalshi_markets)} markets to {dated_file}", flush=True)
    print(f"Atomically updated {latest_file}", flush=True)

    print("\nRunning validation on published dataset...", flush=True)
    sys.exit(run_validation(latest_file))


if __name__ == "__main__":
    main()
