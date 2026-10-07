"""Fetch 2026 US midterm election markets from Polymarket and Kalshi.

Saves active markets to data/venue_markets/output/venue_markets_<date>.json
and data/venue_markets/output/venue_markets_latest.json.

Standard library only (Python 3.11+).
"""

from __future__ import annotations

import argparse
import http.client
import json
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
    """Enforces sequential requests at most `max_rate` per second."""

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
# Polymarket Fetcher
# =====================================================================

def fetch_polymarket_markets(rate_limiter: RateLimiter) -> list[dict[str, Any]]:
    """Fetch active 2026 midterm markets from Polymarket Gamma API."""
    print("Fetching Polymarket markets...", flush=True)
    tags = [102289, 100199, 100344]  # midterms, senate, house races
    markets_by_id: dict[str, dict[str, Any]] = {}

    for tag_id in tags:
        offset = 0
        limit = 100
        while True:
            rate_limiter.wait()
            params = urllib.parse.urlencode({
                "tag_id": tag_id,
                "closed": "false",
                "limit": limit,
                "offset": offset,
            })
            url = f"https://gamma-api.polymarket.com/markets?{params}"
            req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})

            try:
                with urllib.request.urlopen(req, timeout=15) as resp:
                    data = json.loads(resp.read().decode("utf-8"))
            except urllib.error.HTTPError as e:
                # Gamma API returns 422 when offset exceeds max depth (e.g. 2000)
                if e.code == 422:
                    break
                print(f"  Polymarket HTTP error {e.code} for tag {tag_id} offset {offset}: {e}", flush=True)
                break
            except (urllib.error.URLError, TimeoutError, OSError) as e:
                print(f"  Polymarket fetch error for tag {tag_id} offset {offset}: {e}", flush=True)
                break

            if not data or not isinstance(data, list):
                break

            for item in data:
                m_id = str(item.get("id", ""))
                if not m_id:
                    continue

                parent_events = item.get("events") or []
                parent_event = parent_events[0] if parent_events else {}

                outcomes_raw = item.get("outcomes")
                outcomes = _safe_parse_json(outcomes_raw, default=[]) if isinstance(outcomes_raw, str) else (outcomes_raw or [])

                tokens_raw = item.get("clobTokenIds")
                clob_tokens = _safe_parse_json(tokens_raw, default=[]) if isinstance(tokens_raw, str) else (tokens_raw or [])

                prices_raw = item.get("outcomePrices")
                outcome_prices = _safe_parse_json(prices_raw, default=[]) if isinstance(prices_raw, str) else (prices_raw or [])
                parsed_prices = [_safe_float(p) for p in outcome_prices] if isinstance(outcome_prices, list) else []

                snapshot_prices = {
                    "is_snapshot": True,
                    "outcome_prices": parsed_prices,
                    "best_bid": _safe_float(item.get("bestBid")),
                    "best_ask": _safe_float(item.get("bestAsk")),
                    "last_trade_price": _safe_float(item.get("lastTradePrice")),
                }

                market_record = {
                    "venue": "polymarket",
                    "market_id": m_id,
                    "question": item.get("question"),
                    "slug": item.get("slug"),
                    "outcomes": outcomes,
                    "clob_token_ids": clob_tokens,
                    "snapshot_prices": snapshot_prices,
                    "end_date": item.get("endDate"),
                    "resolution_description": item.get("description"),
                    "event_id": str(parent_event.get("id")) if parent_event.get("id") is not None else None,
                    "event_title": parent_event.get("title"),
                    "event_slug": parent_event.get("slug"),
                }
                markets_by_id[m_id] = market_record

            offset += len(data)
            if len(data) < limit:
                break

        print(f"  Tag {tag_id} complete. Total unique Polymarket markets so far: {len(markets_by_id)}", flush=True)

    return list(markets_by_id.values())


# =====================================================================
# Kalshi Fetcher
# =====================================================================

def _kalshi_request(host: str, path: str, timeout: float = 25.0) -> tuple[int, bytes]:
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


def fetch_kalshi_markets(rate_limiter: RateLimiter) -> tuple[str, list[dict[str, Any]]]:
    """Fetch active 2026 midterm markets from Kalshi Trade API v2."""
    print("Fetching Kalshi markets...", flush=True)
    primary_host = "api.elections.kalshi.com"
    fallback_host = "external-api.kalshi.com"
    active_host = primary_host
    actual_serving_host = primary_host

    markets_by_ticker: dict[str, dict[str, Any]] = {}
    cursor = ""
    page = 0
    limit = 200

    while True:
        rate_limiter.wait()
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
                status, body = _kalshi_request(active_host, path)
                if status == 200:
                    page_data = json.loads(body.decode("utf-8"))
                    actual_serving_host = active_host
                    break
                print(f"  Kalshi HTTP {status} from {active_host} on attempt {attempt + 1}", flush=True)
            except (http.client.HTTPException, ssl.SSLError, OSError, TimeoutError) as e:
                # Connection error: if on primary host, switch immediately to fallback
                if active_host == primary_host:
                    print(f"  Connection error on {primary_host} ({e}); falling back to {fallback_host}", flush=True)
                    active_host = fallback_host
                    actual_serving_host = fallback_host
                    # Immediate try on fallback host without consuming a retry
                    try:
                        status, body = _kalshi_request(active_host, path)
                        if status == 200:
                            page_data = json.loads(body.decode("utf-8"))
                            break
                    except (http.client.HTTPException, ssl.SSLError, OSError, TimeoutError) as fb_err:
                        e = fb_err
                if attempt == 4:
                    print(f"  Exhausted 4 retries on {active_host} for page {page + 1}: {e}", flush=True)
                    break
                backoff = 1.0 * (1.8 ** attempt) + random.uniform(0.2, 0.5)
                print(f"  Kalshi connection reset ({e}); retrying in {backoff:.2f}s (retry {attempt + 1}/4)...", flush=True)
                time.sleep(backoff)

        if page_data is None:
            print(f"  Failed to retrieve Kalshi page {page + 1} after retries. Stopping pagination.", flush=True)
            break

        events = page_data.get("events") or []
        new_cursor = page_data.get("cursor") or ""
        page += 1

        for ev in events:
            series_ticker = ev.get("series_ticker") or ""
            category = ev.get("category") or ""
            ev_title = ev.get("title") or ""
            ev_subtitle = ev.get("sub_title") or ""
            ev_ticker = ev.get("event_ticker") or ""

            for m in ev.get("markets") or []:
                m_ticker = m.get("ticker") or ""
                if not m_ticker:
                    continue

                m_series = m.get("series_ticker") or series_ticker
                m_title = m.get("title") or ev_title
                search_text = f"{m_ticker} {m_title} {ev_title} {ev_subtitle} {m_series}".lower()

                is_primary_match = bool(
                    KALSHI_TARGET_SERIES_RE.match(m_series)
                    or KALSHI_TARGET_SERIES_RE.match(m_ticker)
                )

                filter_note: str | None = None
                if not is_primary_match:
                    if category in ("Elections", "Politics") and "2026" in search_text:
                        filter_note = f"Included from {category} category mentioning 2026 (series: {m_series})"
                    else:
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

        if not new_cursor or not events or new_cursor == cursor:
            break
        cursor = new_cursor

    print(f"Kalshi complete. Served by {actual_serving_host}. Total markets: {len(markets_by_ticker)}", flush=True)
    return actual_serving_host, list(markets_by_ticker.values())


# =====================================================================
# Validation & Analysis
# =====================================================================

def classify_race(venue: str, market: dict[str, Any]) -> str:
    """Classify a market as 'senate', 'house', 'governor', 'control', or 'other'."""
    if venue == "polymarket":
        q = (market.get("question") or "").lower()
        slug = (market.get("slug") or "").lower()
        text = f"{q} {slug}"
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


def extract_race_state(venue: str, market: dict[str, Any], race_type: str) -> str | None:
    """Extract US state associated with a race."""
    if race_type not in ("senate", "governor"):
        return None

    if venue == "kalshi":
        series = (market.get("series_ticker") or "").upper()
        ticker = (market.get("market_ticker") or "").upper()
        # Look for 2-letter state abbreviation in series
        for prefix in ("GOVPARTY", "KXGOVPARTY", "SENATEPARTY-", "SENATEPARTY", "SENATE", "KXSENATE"):
            if series.startswith(prefix):
                abbr = series[len(prefix):][:2]
                if abbr in STATE_ABBRS:
                    return STATE_ABBRS[abbr]
            if ticker.startswith(prefix):
                abbr = ticker[len(prefix):][:2]
                if abbr in STATE_ABBRS:
                    return STATE_ABBRS[abbr]

    # Polymarket / text fallback
    text = ""
    if venue == "polymarket":
        text = f"{market.get('question', '')} {market.get('slug', '')}".lower()
    else:
        text = f"{market.get('title', '')} {market.get('subtitle', '')}".lower()

    for state in US_STATES:
        if state in text:
            return state

    for abbr, full in STATE_ABBRS.items():
        if re.search(rf"\b{abbr}\b", text.upper()):
            return full

    return None


def run_validation(latest_path: Path) -> int:
    """Validate and summarize the latest output file."""
    if not latest_path.exists():
        print(f"Error: {latest_path} does not exist. Run fetch first.")
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
    kalshi_has_senate_ctrl = any("CONTROLS" in (m.get("series_ticker") or "") or "senate" in (m.get("title") or "").lower() and classify_race("kalshi", m) == "control" for m in kalshi_markets)
    kalshi_has_house_ctrl = any("CONTROLH" in (m.get("series_ticker") or "") or "house" in (m.get("title") or "").lower() and classify_race("kalshi", m) == "control" for m in kalshi_markets)

    print("CHAMBER CONTROL MARKETS:")
    print(f"  Polymarket Senate Control Present: {'YES' if poly_has_senate_ctrl else 'NO'}")
    print(f"  Polymarket House Control Present:  {'YES' if poly_has_house_ctrl else 'NO'}")
    print(f"  Kalshi Senate Control Present:     {'YES' if kalshi_has_senate_ctrl else 'NO'}")
    print(f"  Kalshi House Control Present:      {'YES' if kalshi_has_house_ctrl else 'NO'}")
    print("-" * 70)

    # 4. Distinct Races Count
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

    print("DISTINCT RACES COVERAGE:")
    print(f"  Polymarket Distinct Senate Races:   {len(poly_senate_states)} (states: {', '.join(sorted(poly_senate_states))[:60]}...)")
    print(f"  Polymarket Distinct Governor Races: {len(poly_gov_states)} (states: {', '.join(sorted(poly_gov_states))[:60]}...)")
    print(f"  Kalshi Distinct Senate Races:       {len(kalshi_senate_states)} (states: {', '.join(sorted(kalshi_senate_states))[:60]}...)")
    print(f"  Kalshi Distinct Governor Races:     {len(kalshi_gov_states)} (states: {', '.join(sorted(kalshi_gov_states))[:60]}...)")
    print("=" * 70)

    # Sanity checks
    if len(polymarket_markets) < 100 or len(kalshi_markets) < 100:
        print("FAIL: One or both venues have fewer than 100 markets!")
        return 1
    if not (poly_has_senate_ctrl and poly_has_house_ctrl and kalshi_has_senate_ctrl and kalshi_has_house_ctrl):
        print("FAIL: Chamber control markets missing!")
        return 1

    print("VALIDATION SUCCESSFUL: All checks passed.")
    return 0


# =====================================================================
# Main
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

    output_dir.mkdir(parents=True, exist_ok=True)
    today_str = datetime.now(UTC).strftime("%Y-%m-%d")
    dated_file = output_dir / f"venue_markets_{today_str}.json"

    rate_limiter = RateLimiter(MIN_REQUEST_INTERVAL)
    polymarket_markets = fetch_polymarket_markets(rate_limiter)
    kalshi_host, kalshi_markets = fetch_kalshi_markets(rate_limiter)

    print(f"Found {len(polymarket_markets)} markets from Polymarket", flush=True)
    print(f"Found {len(kalshi_markets)} markets from Kalshi", flush=True)

    output_payload = {
        "fetched_at": datetime.now(UTC).isoformat(),
        "kalshi_host": kalshi_host,
        "polymarket_count": len(polymarket_markets),
        "kalshi_count": len(kalshi_markets),
        "polymarket": polymarket_markets,
        "kalshi": kalshi_markets,
    }

    serialized = json.dumps(output_payload, indent=2)
    with open(dated_file, "w", encoding="utf-8") as f:
        f.write(serialized)
    with open(latest_file, "w", encoding="utf-8") as f:
        f.write(serialized)

    print(f"Saved {len(polymarket_markets) + len(kalshi_markets)} markets to {dated_file}", flush=True)
    print(f"Updated {latest_file}", flush=True)

    print("\nRunning validation on fetched dataset...", flush=True)
    sys.exit(run_validation(latest_file))


if __name__ == "__main__":
    main()
