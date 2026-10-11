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
- Query Polymarket's Gamma API (`https://gamma-api.polymarket.com/events/keyset`) with `tag_id=102289` (midterms) and `closed=false` using keyset pagination to fetch tag-filtered active 2026 midterm election events and nested markets (tag traversal yields tag-filtered results and does not claim to prove coverage of every 2026 election market on the venue).
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
- Distinct states covered by Senate and Governor markets (note: expected 35 Senate races [33 regular + Ohio and Florida specials] and 36 governor races).
- Sample questions (up to 20 per exclusion reason) to verify excluded markets.

### 3. Run Offline Regression Tests

All regression tests use synthetic mocks and offline fixtures (no live network access):

```bash
python -m pytest data/venue_markets/tests -v
python -m ruff check data/venue_markets
git diff --check
```

## Eligibility Evidence Rules

A market is kept only with affirmative evidence of a **2026 U.S. Senate, House, governor, chamber-control, or related midterm contest**. Both venues apply evidence checks in a structured order, and the first failing check becomes the exclusion reason (counted in the `exclusions` printed for each venue).

**Contest identity sources:** The question/title, slug, structured Kalshi ticker/series, parent event title, and parent event slug. Description and rules text is *incidental*: it can neither make a market eligible nor ineligible. Closing dates and `close_time` are never cycle evidence.

| Order | Check | Exclusion reason |
| --- | --- | --- |
| 1 | Market is strictly open/active (Polymarket `closed` is exactly `False`, `active` is exactly `True`; Kalshi status `open`/`active`) | `market_closed`, `market_inactive` |
| 2 | Not a resignation, appointment, confirmation, nomination, impeachment, retirement, or unrelated event | `unrelated_political_event` |
| 3 | Not a Lieutenant Governor contest ("lieutenant governor", "lt. governor", "ltgov", ...) | `lieutenant_governor_contest` |
| 4 | No foreign country/demonym or foreign political entity in the identity text (U.S. state names masked first). Foreign elections such as foreign parliament elections are excluded. If U.S. evidence is also present, the subject is contradictory | `foreign_election`, `conflicting_jurisdiction` |
| 5 | Not a state legislature contest ("state senate", "state house", "legislature", state supermajority, ...) | `state_legislative_contest` |
| 6 | At most one U.S. state named (multi-state comparisons such as "Florida or South Carolina" are classified as `other` or rejected if multi-seat) | `conflicting_state_context` |
| 7 | Primary identity names no year other than 2026. Any conflicting primary year (e.g. 2028 or 2024) is rejected | `non_2026_primary_contest` |
| 8 | Explicit 2026 election cycle evidence present (from question/slug/parent event for Polymarket, or title/year-bearing symbol for Kalshi) | `lacks_2026_contest_year` |
| 9 | An office (Senate, House, governor, Congress) and contest term (win, election, race, seat, control, majority, midterm, balance of power), or a recognised structured symbol | `lacks_election_evidence` |
| 10 | Positive U.S. jurisdiction or cycle evidence | `lacks_us_jurisdiction`, `ambiguous_jurisdiction` |

**Election-Cycle & Jurisdiction Evidence:**
- Explicit 2026 election cycle evidence is strictly required for all markets. A numeric tag ID (`tag_id=102289`) or recognized Kalshi series alone must **not** establish the year 2026.
- For Polymarket, 2026 evidence must be established from relevant market or parent-event identity text (question, market slug, parent event title, parent event slug). Source tag context (`tag_id=102289`) establishes U.S. jurisdiction when explicitly supplied by the fetcher, but does not waive the requirement for explicit 2026 cycle evidence. Note that Polymarket currently scans one selected Gamma tag (`tag_id=102289`); tag traversal yields tag-filtered results and does not claim complete coverage of all 2026 election markets across Polymarket.
- For Kalshi, recognized structured symbols establish office and U.S. jurisdiction, but 2026 cycle evidence is strictly required from relevant titles or recognized year-bearing market/event symbols. Arbitrary numeric suffixes, district numbers, and ambiguous components in a ticker must not establish a year. A cycle year is only extracted from a valid position following a recognized prefix.
- State-specific contest evidence takes precedence over broad chamber-control phrasing (e.g. "win the Senate" in Wyoming is classified as `senate`, not `control`). Genuine national control is reserved for chamber-level series (`CONTROLS`, `CONTROLH`).
- State extraction uses anchored, boundary-validated parsing so symbol substrings such as `WITHIN` in `KXSENATEWITHIN5` never falsely fabricate a state (like Wisconsin). If evidence is insufficient, state extraction returns unknown (`None`).
- Georgia and Washington count as valid U.S. states whenever the text names a Senate, governor, or House race, unless a foreign term is present. Country parliamentary elections (e.g. Georgia parliamentary election) remain excluded under `foreign_election`.
- Kept markets are classified as `senate`, `house`, `governor`, `control`, or `other` (seat counts, balance-of-power combos) using parent event context (`event_title`) and questions/titles. Saved Kalshi records retain `event_title` to guarantee fetch-time and validation-time classifications agree.
- Candidate and outcome labels (`yes_sub_title`, candidate `subtitle`) never establish jurisdiction or the election cycle, even when a label is exactly a state name or abbreviation (e.g. Washington). State extraction uses structured contest symbols and market/parent-event contest context. The targeted exclusion of Lieutenant Governor primary-office subtitles remains in place.
- Explicit U.S., United States, or federal Senate/House wording identifies federal seat-count contracts without requiring the particular phrase "in the U.S. Senate" or "U.S. congressional elections". Explicit state-legislature wording and recognised state-legislature series remain excluded.

**Recognised structured Kalshi symbols:** `CONTROLS`/`CONTROLH` and state-validated `SENATE<ST>`, `SENATEPARTY<ST>`, `GOV<ST>`/`GOVPARTY<ST>`, `HOUSE<ST><district|AL>` (optionally `KX`-prefixed, where `<ST>` is a real two-letter U.S. state code). Generic Kalshi SENATE/HOUSE/GOV prefixes are recognized by year extraction; they do not alone supply the office/jurisdiction evidence that the structured eligibility helper requires.

## Polymarket Binary Outcome Mappings

A market is kept only when outcome tokens hold **exactly two** valid records, each with a nonempty string `outcome` and nonempty string `token_id` (not null, blank, or placeholder such as `"None"`), with distinct labels (case-insensitive) and distinct token IDs. If either record is bad the whole market is rejected; bad records are never dropped to salvage the remaining mapping. Native labels are preserved and the label-token-price association is preserved regardless of array order. Reasons: `invalid_tokens`, `non_binary_tokens`, `invalid_token_mapping`.

## Output Schema

The output JSON file contains:

```json
{
  "fetched_at": "2026-10-09T11:00:00.000000+00:00",
  "kalshi_host": "api.elections.kalshi.com",
  "polymarket_count": 210,
  "kalshi_count": 676,
  "exclusion_samples": {
    "polymarket": {
      "foreign_election": ["Who will win the Brazilian Senate election in 2026?"]
    },
    "kalshi": {
      "foreign_election": ["UK General Election in 2026"]
    }
  },
  "polymarket": [
    {
      "venue": "polymarket",
      "market_id": "0x123... (Condition ID or Gamma Market ID)",
      "question": "Will the Democratic Party control the Senate after the 2026 Midterm elections?",
      "slug": "will-the-democratic-party-control-the-senate-after-the-2026-midterm-elections",
      "outcomes": ["Yes", "No"],
      "clob_token_ids": ["113287701...", "10711843..."],
      "outcome_token_map": {
        "Yes": "113287701...",
        "No": "10711843..."
      },
      "snapshot_prices": {
        "is_snapshot": true,
        "outcome_prices": [0.645, 0.355],
        "best_bid": 0.64,
        "best_ask": 0.65,
        "last_trade_price": 0.645
      },
      "end_date": "2026-11-04T04:59:00Z",
      "resolution_description": "...",
      "event_id": "102289",
      "event_title": "2026 U.S. Senate Elections",
      "event_slug": "2026-us-senate-elections",
      "race_type": "control"
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
      "event_title": "2026 U.S. Senate Elections",
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
      "filter_note": null,
      "race_type": "control"
    }
  ]
}
```

## Security & Network Policies

- **TLS / SSL**: Standard strict certificate verification is enforced. No `CERT_NONE` or legacy renegotiation flags.
- **Kalshi Fallback**: Tries `api.elections.kalshi.com` first, and gracefully falls back to `external-api.kalshi.com` on connection errors with jittered backoff.
- **Kalshi Connection Reuse**: Each fetch owns one verified TLS context and reuses healthy HTTPS connections per host. Every response body is fully read before reuse, including HTTP errors. Request, header, or body failures discard the connection; server-requested closure causes a fresh connection on the next attempt. All owned connections close when the fetch finishes or fails. `_kalshi_request()` remains a one-shot helper for diagnostics.
- **Rate Limiting**: Shared per-venue limiter enforces $\le 4$ requests per second across all request attempts, retries, and host fallbacks.
- **Failure Resilience**: Connection errors retry with exponential backoff and jitter. Failed runs preserve previous outputs byte-for-byte.
- **Bounded Recovery**: Connection reuse adds no retries. The existing initial attempt plus at most four retry rounds per page, immediate primary-to-fallback attempt, rate-limiter waits, timeouts and cursor retention remain in force. This reduces fresh TLS handshakes but cannot guarantee access when the network or upstream repeatedly resets connections.
- **Git Hygiene**: `data/venue_markets/output/` is git-ignored. Raw market data dumps are never committed.
