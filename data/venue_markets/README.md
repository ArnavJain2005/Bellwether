# Venue Markets Ingestion (`data/venue_markets`)

Script to fetch and normalize 2026 US midterm election markets from Polymarket and Kalshi into a unified dataset for contract matching against SIG's Super Market platform.

## Requirements

- Python 3.11+
- Standard library only (no `pip install` required)

## Usage

### 1. Fetch Markets

Run the fetcher to query Polymarket and Kalshi public APIs:

```bash
python data/venue_markets/fetch_venue_markets.py
```

This will:
- Query Polymarket's Gamma API for open midterm, Senate, and House race markets (`tag_id` 102289, 100199, 100344).
- Query Kalshi's Trade API v2 for open election events and markets (Senate, House, governor, chamber control series).
- Save results to:
  - `data/venue_markets/output/venue_markets_<YYYY-MM-DD>.json`
  - `data/venue_markets/output/venue_markets_latest.json`
- Automatically run validation checks at the end of the run.

### 2. Validate Existing Output

To validate an existing run without re-fetching:

```bash
python data/venue_markets/fetch_venue_markets.py --validate
```

The validation report inspects:
- Total market counts per venue (asserts $\ge 100$ per venue).
- Market counts categorized by race type (`senate`, `house`, `governor`, `control`, `other`).
- ID integrity (checks for duplicate or missing IDs).
- Presence of chamber control markets (Senate Control and House Control on both venues).
- Distinct Senate and Gubernatorial races covered by each venue.

## Output Schema

The output JSON file contains:

```json
{
  "fetched_at": "2026-10-07T11:59:00.000000+00:00",
  "kalshi_host": "api.elections.kalshi.com",
  "polymarket_count": 2100,
  "kalshi_count": 676,
  "polymarket": [
    {
      "venue": "polymarket",
      "market_id": "562793",
      "question": "Will the Democratic Party control the Senate after the 2026 Midterm elections?",
      "slug": "will-the-democratic-party-control-the-senate-after-the-2026-midterm-elections",
      "outcomes": ["Yes", "No"],
      "clob_token_ids": ["113287701...", "10711843..."],
      "snapshot_prices": {
        "is_snapshot": true,
        "outcome_prices": [0.645, 0.355],
        "best_bid": 0.64,
        "best_ask": 0.65,
        "last_trade_price": 0.65
      },
      "end_date": "2026-11-04T04:59:00Z",
      "resolution_description": "...",
      "event_id": "32224",
      "event_title": "Which party will win the Senate in 2026?",
      "event_slug": "which-party-will-win-the-senate-in-2026"
    }
  ],
  "kalshi": [
    {
      "venue": "kalshi",
      "market_ticker": "CONTROLS-2026-D",
      "event_ticker": "CONTROLS-2026",
      "series_ticker": "CONTROLS",
      "title": "Will Democrats win the U.S. Senate in 2026?",
      "subtitle": "Democratic Party",
      "snapshot_prices": {
        "is_snapshot": true,
        "yes_bid_dollars": "0.6200",
        "yes_ask_dollars": "0.6300",
        "last_price_dollars": "0.6200",
        "yes_bid": null,
        "yes_ask": null,
        "last_price": null,
        "yes_bid_cents": 62,
        "yes_ask_cents": 63,
        "last_price_cents": 62
      },
      "close_time": "2027-02-01T15:00:00Z",
      "rules_text": "...",
      "filter_note": null
    }
  ]
}
```

## Security & Network Policies

- **TLS / SSL**: Standard strict certificate verification is enforced. No `CERT_NONE` or legacy renegotiation flags.
- **Kalshi Fallback**: Tries `api.elections.kalshi.com` first, and gracefully falls back to `external-api.kalshi.com` on connection errors with jittered backoff.
- **Rate Limiting**: Sequential requests throttled to $\le 4$ requests per second per venue.
- **Git Hygiene**: `data/venue_markets/output/` is git-ignored. Raw market data dumps are never committed.
