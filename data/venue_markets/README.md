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
- Query Polymarket's CLOB API (`https://clob.polymarket.com/markets`) using cursor-based pagination to exhaustively fetch all active 2026 election cycle markets.
- Query Kalshi's Trade API v2 for open election events and markets (Senate, House, governor, chamber control series).
- Save results atomically (temporary file + atomic rename) to:
  - `data/venue_markets/output/venue_markets_<YYYY-MM-DD>.json`
  - `data/venue_markets/output/venue_markets_latest.json`
- Automatically run validation checks at the end of the run. On any fetch or validation error, the process exits non-zero and preserves the existing `venue_markets_latest.json` byte-for-byte.

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
- Distinct states covered by Senate and Governor markets.

### 3. Run Offline Regression Tests

All regression tests use synthetic mocks and offline fixtures (no live network access):

```bash
python -m pytest data/venue_markets/tests -v
python -m ruff check data/venue_markets
git diff --check
```

## Eligibility Evidence Rules

A market is kept only with affirmative evidence of a **2026 U.S. Senate, House, governor or chamber-control contest**. Both venues apply the same checks in this order, and the first failing check becomes the exclusion reason (counted in the `exclusions` printed for each venue).

**What counts as the contest identity.** Only the question/title, slug, structured Kalshi ticker/series and the event title. Description and rules text is *incidental*: it can neither make a market eligible nor ineligible. So a 2028 contest whose rules say "compare with the 2026 results" is rejected, and a genuine 2026 contest whose rules mention 2028 as background is kept. Closing dates and `close_time` are never cycle evidence.

| Order | Check | Exclusion reason |
| --- | --- | --- |
| 1 | Market is open/active (Polymarket `closed is False`, `active is True`; Kalshi status `open`/`active`) | `market_closed`, `market_inactive` |
| 2 | Not a resignation, appointment, confirmation, nomination, impeachment, retirement or cabinet/other unrelated event | `unrelated_political_event` |
| 3 | No foreign country/demonym in the identity text (U.S. state names are masked first so "New Mexico" is not "Mexico"). If U.S. evidence is also present the subject is contradictory | `foreign_election`, `conflicting_jurisdiction` |
| 4 | Not a state legislature contest ("state senate", "state house", "legislature", ...) | `state_legislative_contest` |
| 5 | At most one U.S. state named (multi-state comparisons such as "Florida or South Carolina" are not one contest) | `conflicting_state_context` |
| 6 | Identity names 2026 and no other year 2020-2039 (a ticker year such as `-26` counts; a second number such as a seat number does not). Any conflicting primary year is rejected | `lacks_2026_contest_year`, `non_2026_primary_contest` |
| 7 | An office (Senate, House, governor/gubernatorial; the White House is not the House) **and** a contest term (win, election, race, seat, control, majority, midterm, ...), or a recognised structured symbol | `lacks_election_evidence` |
| 8 | Positive U.S. jurisdiction, from explicit wording ("U.S.", "US", "USA", "United States"), an unambiguous U.S. state name, or a recognised structured symbol | `lacks_us_jurisdiction`, `ambiguous_jurisdiction` |

Generic words ("senate", "governor", "control") alone are never enough. "Georgia" and "Washington" are ambiguous (country/city) and need explicit U.S. wording. State abbreviations are never inferred from prose such as "in".

**Recognised structured Kalshi symbols** (no broad prefix bypasses the checks above, apart from replacing the office/contest and jurisdiction evidence): `CONTROLS`/`CONTROLH` (optionally `KX`-prefixed) for chamber control, and `SENATE<ST>`, `SENATEPARTY<ST>`, `GOV<ST>`/`GOVPARTY<ST>`, `HOUSE<ST><district|AL>` where `<ST>` is a real two-letter U.S. state code. The cycle year must still come from the identity text or from the year token of the ticker, and foreign/legislature/conflict checks still apply.

**Limitations.**
- Evidence is textual and deliberately conservative. A genuine market whose question has neither explicit U.S. wording nor a state name (for example "Will Democrats control the Senate after the 2026 midterms?") is excluded as `lacks_us_jurisdiction`; review the exclusion counts of a real fetch.
- A year other than 2026 anywhere in the question/slug/title rejects the market, even when it is only background (e.g. "the seat held since 2022").
- The foreign-term list is finite; an unlisted country is not detected unless U.S. evidence is absent.
- Party names ("Democrats"/"Republicans") and the word "midterms" are not accepted as U.S. evidence by themselves.
- Primary elections are not distinguished from general elections.
- A state named alongside "House" (e.g. "Texas House") is read as a federal House contest unless a legislature phrase is present.

## Polymarket Binary Outcome Mappings

A market is kept only when `tokens` holds **exactly two** records, each with a nonempty string `outcome` and a nonempty string `token_id` (not null, blank or a placeholder such as `"None"`), with distinct labels (case-insensitive) and distinct token IDs. If either record is bad the whole market is rejected; a bad record is never dropped to salvage the other, and three outcomes are never truncated to two. Native labels are preserved (no `Yes`/`No` is invented) and the label, token ID and price stay associated whatever the array order. Reasons: `invalid_tokens` (not a list), `non_binary_tokens` (count is not two), `invalid_token_mapping` (malformed or duplicated).

## Output Schema

The output JSON file contains:

```json
{
  "fetched_at": "2026-10-07T11:59:00.000000+00:00",
  "kalshi_host": "api.elections.kalshi.com",
  "polymarket_count": 210,
  "kalshi_count": 676,
  "polymarket": [
    {
      "venue": "polymarket",
      "market_id": "0x123... (Condition ID)",
      "question": "Will the Democratic Party control the U.S. Senate after the 2026 midterm elections?",
      "slug": "will-the-democratic-party-control-the-us-senate-after-the-2026-midterm-elections",
      "outcomes": ["Yes", "No"],
      "clob_token_ids": ["113287701...", "10711843..."],
      "outcome_token_map": {
        "Yes": "113287701...",
        "No": "10711843..."
      },
      "snapshot_prices": {
        "is_snapshot": true,
        "outcome_prices": [0.645, 0.355],
        "best_bid": null,
        "best_ask": null,
        "last_trade_price": null
      },
      "end_date": "2026-11-04T04:59:00Z",
      "resolution_description": "...",
      "event_id": null,
      "event_title": null,
      "event_slug": null
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

**Note on Identifiers and Metadata**:
- **Polymarket IDs**: We strictly use the `condition_id` as the primary `market_id` instead of the legacy numerical Gamma ID. Token IDs are extracted precisely from the CLOB API outcome map to ensure fidelity.
- **Missing Parent-Event Metadata**: Because the CLOB API flattens markets, parent event bindings (`event_id`, `event_slug`, `event_title`) are completely absent (`null`).
- **Snapshot Prices**: The prices captured are one-time `snapshot_prices` provided by the API list endpoint. They are NOT live, executable quotes (`best_bid` and `best_ask` are absent).

## Security & Network Policies

- **TLS / SSL**: Standard strict certificate verification is enforced. No `CERT_NONE` or legacy renegotiation flags.
- **Kalshi Fallback**: Tries `api.elections.kalshi.com` first, and gracefully falls back to `external-api.kalshi.com` on connection errors with jittered backoff.
- **Rate Limiting**: Shared per-venue limiter enforces $\le 4$ requests per second across all request attempts, retries, and host fallbacks.
- **Failure Resilience**: Pagination cursor cycles and unexpected HTTP 422 errors raise explicit errors. Failed runs preserve previous outputs byte-for-byte.
- **Git Hygiene**: `data/venue_markets/output/` is git-ignored. Raw market data dumps are never committed.
