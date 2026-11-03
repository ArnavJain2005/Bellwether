# MAP-ALL report

Run 2026-11-04. Venue lists fetched from the public Kalshi and Polymarket endpoints with the
functions in `data/venue_markets/fetch_venue_markets.py` (Kalshi 4455 open markets, Polymarket
6325). No SIG call, no keys. Commands: `bot.mapping.load_map` (no error), `python -m bot.refs_check`
(exit 0), `ruff check .`, `pytest`.

## Summary counts

| | Before | After |
| --- | --- | --- |
| Approved contracts loaded by `load_map` (non-excluded races) | 112 | 202 |
| Fully approved races (both contracts) | 56 | 101 |
| Races in map.yaml | 95 | 112 |
| SIG contracts in map.yaml | 190 | 224 |
| SIG contracts still not in map.yaml | 47 | 13 |

- House races in map.yaml approved: 28 races, 56 contracts (0 left unapproved).
- House races added with `approved: true`: 17 races, 34 contracts.
- Excluded races (11 races, 22 contracts) and every previously approved line: unchanged.
  Only `approved: false` -> `true` changed on existing lines (56 lines, all House).

## How a contract was verified

Kalshi rules text had to name the same state and district (House), same state (Senate),
the same party, and the term beginning in 2027 (House and Senate) or "pursuant to the 2026
election" (governor). All 28 existing House contracts and all 34 new ones match this exactly,
and the ticker agrees with the SIG race. Exactly one open Kalshi market in the fetched list
satisfies the rules for each new contract.

The House rules read "the House member sworn in for XX-NN for the term beginning in 2027 is a
member of the <Party>". They do not use the words "November 2026 general election"; the term
beginning in 2027 is what the November 2026 general election fills. Senate contracts approved in
MAP-APPROVE-1 use the same swearing-in wording, so this is consistent with that approval.
MN-02 D reads "Democratic (DFL) Party": same party, approved.

New House races use Kalshi series `KXHOUSERACE` (tickers `KXHOUSERACE-<ST><DD>-26-D|R`); the
older races use `HOUSE<ST><N>-26-D|R`.

Polymarket tokens: set only when exactly one open market asks "Will the <Party> Party win the
<ST-DD> House seat?" (the "Yes" token). Null when none, or when a second party-winner
market also exists (ambiguous). New contracts without a token: FL-09 D, KY-04 D, NY-13 R,
FL-20 R, MN-05 R. Existing tokens were not touched.

## Added races (approved)

house:CA-48, CO-04, FL-09, IA-02, KY-04, NY-13, SC-01, TX-23, AL-02, FL-07, FL-16, FL-20,
FL-22, MN-01, MN-05, TN-05, VA-05.

## refs_check

No contract showed a closed market or "venues disagree", so nothing was unapproved. One
contract shows `no_quote`: `senate:SD D` (Kalshi and Polymarket both have no bid, ask 0.003 /
0.001). It was approved before this task, so it is left as is; Arnav may want to look at it.

## Left out (13 SIG contracts)

- governor:RI I (880): no Kalshi party-winner market for an Independent; race already in map (D, R).
- senate:MT I (965): same reason; race excluded already in map.
- senate:NE I (969): same reason; race excluded already in map.
- senate:KY D, R (955, 956): the only Kalshi markets with Kentucky Senate rules sit under tickers
  `SENATELA-26-D/R` (ticker says Louisiana, rules and title say Kentucky). Ticker/rules conflict,
  so ambiguous.
- senate:LA D, R (957, 958): Kalshi `KXSENATELA-26NOV-D/R` match on rules, but Louisiana is on
  the project's runoff-risk list (`RISKY_STATES` in `bot/make_map.py`, such races are
  excluded), and the old `SENATELA-26` tickers are mislabelled Kentucky. Needs Arnav.
- governor:KS D, R (992, 993): no Kalshi party-winner market; only candidate, vote-share and
  combined governor+senate markets.
- senate:FL D, R (1043, 1044): no Kalshi party-winner market for the special election; only
  candidate, margin and combo markets.
- governor:NH D, R (1061, 1062): no Kalshi party-winner market; only vote-share and combo markets.

`bot/map_review.md` was not regenerated (`make_map.py` refuses to run over approvals).
