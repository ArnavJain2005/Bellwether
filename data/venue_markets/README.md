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

**Contest identity sources:** The question/title, slug, structured Kalshi ticker/series, parent event title, and parent event slug. For the three bounded House party-winner paths (see "House Admission Grammar & Verification"), a Polymarket market's own complete opening resolution clause may supply the missing cycle, a Kalshi `KXHOUSERACE` market's complete decisive rules sentence must corroborate its contract, and a Kalshi per-district series (`HOUSE<ST><N>`/`KXHOUSE<ST><N>`) market's decisive rules sentence must corroborate its outcome. Otherwise, description and rules text is *incidental*: it can neither make a market eligible nor ineligible. Closing dates and `close_time` are never cycle evidence.

| Order | Check | Exclusion reason |
| --- | --- | --- |
| 1 | Market is strictly open/active (Polymarket `closed` is exactly `False`, `active` is exactly `True`; Kalshi status `open`/`active`) | `market_closed`, `market_inactive` |
| 2 | Not a resignation, appointment, confirmation, nomination, impeachment, retirement, or unrelated event | `unrelated_political_event` |
| 3 | Not a Lieutenant Governor contest ("lieutenant governor", "lt. governor", "ltgov", ...) | `lieutenant_governor_contest` |
| 4 | No foreign country/demonym or foreign political entity in the identity text (U.S. state names masked first). Foreign elections such as foreign parliament elections are excluded. If U.S. evidence is also present, the subject is contradictory | `foreign_election`, `conflicting_jurisdiction` |
| 5 | Not a state legislature contest ("state senate", "state house", "legislature", state supermajority, ...) | `state_legislative_contest` |
| 6 | At most one U.S. state named (multi-state comparisons such as "Florida or South Carolina" are classified as `other` or rejected if multi-seat) | `conflicting_state_context` |
| 7 | Primary identity names no year other than 2026. Any conflicting primary year (e.g. 2028 or 2024) is rejected | `non_2026_primary_contest` |
| 8 | Explicit 2026 election cycle evidence present (from question/slug/parent event; for Polymarket House party-winner contracts, the market's own complete opening resolution clause; for Kalshi, title/year-bearing symbol) | `lacks_2026_contest_year` |
| 9 | An office (Senate, House, governor, Congress) and contest term (win, election, race, seat, control, majority, midterm, balance of power), or a recognised structured symbol | `lacks_election_evidence` |
| 10 | Positive U.S. jurisdiction or cycle evidence | `lacks_us_jurisdiction`, `ambiguous_jurisdiction` |

**Election-Cycle & Jurisdiction Evidence:**
- Explicit 2026 election cycle evidence is strictly required for all markets. A numeric tag ID (`tag_id=102289`) or recognized Kalshi series alone must **not** establish the year 2026.
    - For Polymarket, 2026 evidence must be established from relevant market or parent-event identity text (question, market slug, parent event title, parent event slug). As a narrow repair (S1-T5-F6), when primary identity omits the election year and contains no conflicting year, a House party-winner market is admitted only if its question, slug, parent event and complete opening resolution clause parse under the supported grammar and agree on one district; anything else is excluded with a specific reason. Source tag context (`tag_id=102289`) establishes U.S. jurisdiction when explicitly supplied by the fetcher, but does not waive the requirement for explicit 2026 cycle evidence. Note that Polymarket currently scans one selected Gamma tag (`tag_id=102289`); tag traversal yields tag-filtered results and does not claim complete coverage of all 2026 election markets across Polymarket (e.g. California Governor event 57096 is a demonstrated tag omission, whereas CA-22 was observed under tag 102289). Recorded ME-02 event 726881 markets (Republican 3006482 and Democratic 3006483) retain their recorded conflicting slug (`me-01`) and are excluded under `mismatched_district`.
- For Kalshi, recognized structured symbols establish office and U.S. jurisdiction, but 2026 cycle evidence is strictly required from relevant titles or recognized year-bearing market/event symbols. Generic series `KXHOUSERACE` is admitted only through explicit market/event/series relationships, the cycle position (`-26`), a `D`/`R` party suffix, the recorded title shape and a complete supported decisive rules sentence that names the same party and district (see "House Admission Grammar & Verification"). Market records may omit `series_ticker` when the parent explicitly supplies it, but contradictory explicit fields are rejected. Legitimate candidate outcome labels (`yes_sub_title`) are preserved when the contract is a party winner. Reference-data ingestion of baseline 2026 election markets (such as candidate vote-share or popular-vote series like `KXVOTEGENERAL`) is preserved outside this narrow party-winner admission path; party-winner mapping and fair-value eligibility remain separate gates. Note: audited `SENATELA-26` records identify Kentucky contests in their title/event metadata, not a Louisiana cycle exception.
- State-specific contest evidence takes precedence over broad chamber-control phrasing (e.g. "win the Senate" in Wyoming is classified as `senate`, not `control`). Genuine national control is reserved for chamber-level series (`CONTROLS`, `CONTROLH`).
- State extraction uses anchored, boundary-validated parsing so symbol substrings such as `WITHIN` in `KXSENATEWITHIN5` never falsely fabricate a state (like Wisconsin). If evidence is insufficient, state extraction returns unknown (`None`).
- Georgia and Washington count as valid U.S. states whenever the text names a Senate, governor, or House race, unless a foreign term is present. Country parliamentary elections (e.g. Georgia parliamentary election) remain excluded under `foreign_election`.
- Kept markets are classified as `senate`, `house`, `governor`, `control`, or `other` (seat counts, balance-of-power combos) using parent event context (`event_title`) and questions/titles. Saved Kalshi records retain `event_title` to guarantee fetch-time and validation-time classifications agree.
- Candidate and outcome labels (`yes_sub_title`, candidate `subtitle`) never establish jurisdiction or the election cycle, even when a label is exactly a state name or abbreviation (e.g. Washington). State extraction uses structured contest symbols and market/parent-event contest context. The targeted exclusion of Lieutenant Governor primary-office subtitles remains in place.
- Explicit U.S., United States, or federal Senate/House wording identifies federal seat-count contracts without requiring the particular phrase "in the U.S. Senate" or "U.S. congressional elections". Explicit state-legislature wording and recognised state-legislature series remain excluded.

**Recognised structured Kalshi symbols:** `CONTROLS`/`CONTROLH` and state-validated `SENATE<ST>`, `SENATEPARTY<ST>`, `GOV<ST>`/`GOVPARTY<ST>`, `HOUSE<ST><district|AL>` (per-district series admitted only through their own rules; see "Kalshi per-district series"), and corroborated generic `KXHOUSERACE-<ST><dist>-26-[D|R]` (optionally `KX`-prefixed, where `<ST>` is a real two-letter U.S. state code). Generic Kalshi SENATE/HOUSE/GOV prefixes are recognized by year extraction; they do not alone supply the office/jurisdiction evidence that the structured eligibility helper requires.

## House Admission Grammar & Verification (S1-T5-F6 / R1 / R2 / R3 / C1)

Three bounded paths admit 2026 House party-winner contracts that the baseline rules exclude for `lacks_2026_contest_year` or require explicit rules corroboration (Polymarket party-winner, Kalshi generic `KXHOUSERACE`, and Kalshi per-district series `HOUSE<ST><N>`/`KXHOUSE<ST><N>`). Each path accepts a decisive resolution proposition only when one supported shape consumes it **completely**. Unknown, ambiguous, contradictory or partially consumed shapes are excluded with a reason; nothing falls back to keyword presence. Parsing works on text with zero-width characters removed, curly apostrophes straightened and whitespace collapsed. Extracted fields are held in `HouseResolutionEvidence` (shape, subject, office, state, district, party, polarity, payout, election year, term year).

### District references
Supported, as one complete reference:
- Upper-case code `<ST>-<N>` with one or two digits (`AL-02`, `AL-2`; normalised to `AL`, `2`) or `<ST>-AL` for at-large. `<ST>` must be a real state code; district 0 is rejected.
- `<State>'s <N><st|nd|rd|th> congressional district` with the correct suffix for `N` (`West Virginia's 2nd congressional district`).
- `<State>'s at-large congressional district`.

State names are matched longest first, so `West Virginia` never also yields `Virginia`. Not supported (fail closed): word ordinals (`second`), `district 2`, `congressional district 2`, `the second district of California`, lower-case codes inside clauses.

### Kalshi `KXHOUSERACE`
- Relationships: `event.series_ticker == "KXHOUSERACE"`, explicit `market.event_ticker` equal to the parent's, and `market.series_ticker` absent (as recorded upstream) or `KXHOUSERACE`. Event ticker `KXHOUSERACE-<ST><DD|AL>-26`; market suffix `D`/`R` (`DEM`/`REP` accepted).
- Title (recorded shape): `Will <Democratic|Republican> win the House race for <ST>-<DD>?`, agreeing with the suffix party and ticker district. Titles naming a candidate, turnout, vote share, margin or percentage are rejected.
- Decisive sentence: the first sentence of `rules_primary`, framed `If <condition>, then the market resolves to Yes.` (a `No` payout is contradictory). Supported conditions:
  - **K1 (recorded; 697 of 706 cached contracts):** `the House member sworn in for <district> for the term beginning in 2027 is a member of the <Democratic|Republican> Party` (`Party`/`party`). `Democratic (DFL) Party` (recorded on `HOUSEMN2-26-D`) is read as Democratic only for Minnesota districts; any other `(DFL)` label is rejected as `unsupported_party_label`. This parser is shared by both Kalshi paths.
  - **K2 (synthetic control, not observed upstream):** `the <Democratic|Republican> Party wins the <district> House <race|seat> in the 2026 election`.
- The district must come from the condition itself and equal the ticker district; the title and parent title/subtitle may only corroborate it. The term year must be 2027 (K1) and the election year 2026 (K2); a 2027 term never excuses another election year.
- Background: every remaining `rules_primary` sentence and every `rules_secondary` sentence must be a supported background shape (see "Background acceptance"); any other sentence is rejected.

### Kalshi per-district series (`HOUSE<ST><N>`, `KXHOUSE<ST><N>`)
Before this repair these contracts were admitted on ticker symbol and year alone; their rules were never read. A recorded named-candidate contract (`HOUSEAKAL-26-BHIL`, "If Bill Hill is sworn in for AK-AL ...") was admitted as a House reference that way. They are now admitted only through their own rules:
- Relationships: the parent's `series_ticker` is the per-district series, `market.event_ticker` equals the parent's, and `market.series_ticker` is absent (as recorded) or equal. Event ticker `<series>-26`; any other cycle is rejected.
- Decisive sentence: the complete K1 sentence above, naming Democratic or Republican. It alone establishes the claimed outcome, and its district must equal the series district.
  - A party other than Democratic or Republican (`member of the Independent party`, recorded on `HOUSEIA1-26-I`) is rejected as `unsupported_party_outcome`.
  - Named persons and unreviewed wording (`If a member of the <party> party is sworn in for AK-AL ...`, recorded on `HOUSEAKAL-26-D/R`) are rejected as `unsupported_resolution_subject`.
- Minnesota DFL: `Democratic (DFL) Party` counts as Democratic only for a Minnesota district (see K1).
- Background: same allowlist as `KXHOUSERACE`.
- Corroboration only: the ticker suffix, the title and the parent title/subtitle must agree with the rules' party and district; party is never inferred from them.
  - Suffixes: `D`/`DEM` or `R`/`REP`/`GOP` (`DEM`/`GOP` recorded on NC-11).
  - Recorded title shapes: `Will Democratic|a Democrat|Democrats|Republican|a Republican|Republicans win the House race for <ST>-<N>?`.
- Fresh-snapshot effect: 154 per-district contracts, 150 admitted. All 58 D/R contracts in the 29 SIG districts on this path are retained. Excluded: `HOUSEIA1-26-I`, `HOUSEAKAL-26-BHIL`, `HOUSEAKAL-26-D`, `HOUSEAKAL-26-R`.
- Admission is a reference-data identity check. It does **not** establish settlement equivalence with SIG: every SIG-to-venue pairing remains unknown until SIG's Info-tab rules are captured and reviewed.

### Polymarket
- Applies only when the baseline identity check returns `lacks_2026_contest_year` (no conflicting year) and the question mentions the House; other records keep their baseline reason.
- Question (recorded shape): `Will the <Democratic|Republican> Party win the <ST>-<DD> House seat?`. A present slug must be `will-the-<party>-party-win-the-<st>-<dd>-house-seat[-<digits>]` with the same party and district. Parent title/slug district references must agree; parent primary/nominee events are rejected.
- Opening clause: the market's own `description` (or saved `resolution_description`); a parent event description is never borrowed. Its first sentence must be completely:
  `This market will resolve according to the party of the candidate who wins <seat> in the U.S. House of Representatives in the 2026 midterm elections[, using certified totals from every county in the district].`
  where `<seat>` is `the <ST>-<DD> congressional district seat` or `<State>'s <Nth> congressional district seat`. This generic party-of-the-winner form needs no Yes payout sentence; the party comes from the question.
- Rejected: negated resolution, any inserted payout (`resolve to No/Yes according to`), named-subject `resolve to Yes if <person> wins` overrides, other offices, primary/other election kinds, other years, multiple districts, county/precinct/city restrictions (including `every county except ...`) and any other unconsumed tail.
- Background: every later sentence must be one of the five recorded boilerplate sentences (election date, party determination x2, consensus of credible reporting, FEC on ambiguity); any other sentence is rejected. No additional Polymarket background shape is supported.

### Background acceptance
Sentences after the decisive proposition are not parsed for meaning, so they are accepted only in explicitly supported shapes, each compared with the complete normalised sentence:
- **Recorded boilerplate:** the five Polymarket sentences listed above and the two Kalshi `rules_secondary` sentences. All 92 cached Polymarket party contracts and all 706 cached `KXHOUSERACE` contracts carry exactly these sentences (cached `rules_primary` is always a single sentence).
- **Reviewed synthetic shape (Kalshi only):** `The <Democratic|Republican> Party also nominated a candidate in this race.` It names no outcome, payout, condition, district or year, and the resolving party is fixed by the decisive sentence, which is checked first, so it can neither create nor erase a party contradiction. It has not been observed upstream.

Every other sentence is rejected as `unsupported_background_sentence`, whatever its wording. There is no keyword list: for example `The preceding statement is inverted.` and `The affirmative outcome occurs upon a Republican victory.` are rejected because they are unknown. A new shape must be complete, documented here, reviewed for neutrality and covered by tests before it is accepted.

### Rejection reasons on these paths
| Reason | Meaning |
| --- | --- |
| `insufficient_resolution_evidence` | Kalshi rules missing or first sentence not the `If ..., then the market resolves to ...` frame |
| `unsupported_resolution_clause` | Recognised frame, but the proposition is not a supported shape |
| `unsupported_resolution_subject` | Kalshi condition is not about the district's House member or House race winner (e.g. a mayor, a named person) |
| `missing_resolution_district` | Kalshi condition names no district |
| `unsupported_district_reference` | District slot is not a supported complete reference |
| `mismatched_district` | Districts disagree, or several districts are named |
| `contradictory_party_winner_rules` | Negation, No/inserted payout, other or combined parties, party disagreement |
| `not_party_winner_proposition` | Candidate, turnout, vote-share or otherwise non-party proposition |
| `unconsumed_resolution_condition` | Extra qualifier left inside the decisive proposition |
| `unsupported_background_sentence` | A sentence after the decisive proposition that is not a supported background shape |
| `sub_district_resolution_scope` | Polymarket clause restricted to counties, precincts or cities |
| `unsupported_market_identity` | Polymarket slug or question district outside the supported shapes |
| `non_2026_primary_contest` | Primary contest, or a non-2026 election / non-2027 term year |
| `unsupported_party_outcome` | Kalshi per-district rules name a party other than Democratic or Republican |
| `unsupported_party_label` | A `(DFL)` label outside a Minnesota district, or not attached to Democratic |
| `arbitrary_ticker_suffix` | Kalshi market suffix is not a recognised party code |

### Remaining limitations
- The grammar covers the recorded wording plus documented synthetic controls only. If a venue rewords its rules or boilerplate, affected records are excluded (fail closed) and must be reviewed before the grammar is extended.
- Background acceptance is an explicit allowlist of complete sentences. Rewording, punctuation changes or new neutral sentences are excluded until reviewed and added. Omitting recorded boilerplate is not rejected: a description consisting of the opening clause alone, or of the clause and a subset of the recorded sentences, is admitted.
- Kalshi K2 and the Polymarket county-aggregation tail are synthetic controls with no upstream example.
- Only the recorded per-district series shape (`HOUSE<ST><N>`, `KXHOUSE<ST><N>`) is routed through rules checks. Other structured House symbols still use the baseline symbol path, so this does not make federal House admission safe in general. State legislative chambers (such as statewide `KXTXHOUSE`, `KXTXHOUSE-26-D`/`-R`, "Who will win the Texas House of Representatives?" and titles naming `<State> House of Representatives`) are strictly rejected under `state_legislative_contest` and classified as `other` (resolved in S1-T5-F7).
- The Alaska `If a member of the <party> party is sworn in ...` wording and Independent outcomes stay excluded until a reviewed grammar supports them.
- Admission is reference-data ingestion only. Settlement equivalence with SIG, whole-venue completeness and fair-value eligibility remain separate, unaccepted gates; fair-value eligibility stays disabled.

### Test History & Explanation
- **Baseline 82-case suite:** Recorded 10 passes and 72 failures. Those 72 failures comprised 71 positive admission cases across unmatched House districts and 1 negative vote-share case. The failures were not all rejections for `lacks_2026_contest_year`.
- **83-case suite:** Added negative candidate-suffix control `test_kxhouserace_candidate_only_contract_rejected`.
- **S1-T5-F6-R1 94-case suite:** Corrected confirmed defect cases A–D on Polymarket and E–J on Kalshi, retained parent subtitle "In 2028" rejection, and restored the baseline reference-data ingestion assertion for vote-share contracts.
- **S1-T5-F6-R2 101-case suite:** Corrected cycle distinctions, party background mentions, primary rejections, relationship validation, and ME-02 slug conflict preservation.
- **S1-T5-F6-R3 112-case suite:** Added payout, negation, named-candidate, party-combination, election-versus-term, named-state district, question-combination, district-combination and county-aggregation cases, but still matched fragments within arbitrary text.
- **S1-T5-F6-C1 (this revision):** Replaced R3's fragment matching with the complete-consumption grammar above. Ten reviewer probes (synthetic mutations of recorded AL-02 records) failed against R3: Kalshi named-person affiliation, missing decisive district, mayoral subject, `unless turnout exceeds 50%` and `but loses the seat` conditions; Polymarket `will not resolve according to`, `will resolve to No according to`, named candidate with a party-of-winner distractor and `every county except Mobile County`; and a valid West Virginia full-name district wrongly rejected because a separate scan found Virginia. All ten are now regression tests and pass. Cached replay also showed R3 falsely rejecting 20 recorded Kalshi contracts outside the 17 target races (the five cached Oregon districts OR-01–OR-04 and OR-06, where `OR` was read as the word "or", and the five cached at-large districts DE, ND, SD, VT and WY); they are admitted now. Retained R3 cases keep their intent; corrections are listed at the top of the retained-case section of `test_house_admission_regressions.py` (recorded title shape for Kalshi fixtures, a reviewed background shape instead of R3's "mentioned only as background" sentence, the recorded description instead of R3's shortened party-determination sentence, and specific reason assertions).
- **S1-T5-F6-C1 background allowlist:** C1 first screened unknown background sentences with a keyword list, which admitted resolution-changing sentences it did not anticipate (`The preceding statement is inverted.`, `The affirmative outcome occurs upon a Republican victory.`, appended to recorded Democratic contracts on both venues; 8 regressions failed). Background is now an explicit allowlist and those sentences are rejected. Tests that expected keyword-derived reasons (`unconsumed_resolution_condition`, `non_2026_primary_contest`, `mismatched_district`) for unknown background now expect `unsupported_background_sentence`. R3's shortened Polymarket sentence `A candidate's party will be determined by their ballot-listed affiliation.` is a different party-determination rule (it drops the identifiable-affiliation timing and the caucus rule), so it is rejected rather than allowlisted; the 17-district positive control now uses the recorded description and a separate test pins the rejection.
- **S1-T5-F6-C2 (Kalshi per-district hardening):** Closed symbol-only admission loophole on per-district series (`HOUSE<ST><N>`, `KXHOUSE<ST><N>`). Contracts now require decisive K1 rule corroboration, matching party ticker suffix (`-D`/`-R`), title, and parent event. Added 36 regression tests in `test_kalshi_per_district_admission.py` covering positive decisive admission, Minnesota DFL party recognition, non-Minnesota DFL rejection, independent/third-party rejection (`HOUSEIA1-26-I`), named-candidate rejection (`HOUSEAKAL-26-BHIL`), and inverted member-first syntax rejection (`HOUSEAKAL-26-D/R`). Repaired legacy `HOUSETX12-26-D` fixture in `test_fetch_venue_markets.py` with canonical K1 decisive rules and realistic title, preserving test assertions while reflecting rules-based corroboration. Total venue test suite increased from 615 to 651 tests (all passing).
- **S1-T5-F7 (State legislative chamber exclusion):** Closed defect where statewide state legislative chambers (specifically Kalshi `KXTXHOUSE-26-R`/`-D` and questions naming `<State> House of Representatives`) were admitted as federal House references. Removed loose `(?P<st>[A-Z]{2})HOUSE` pattern from structured contest regexes, added targeted symbol check `_is_kalshi_state_chamber_symbol()` for `(?:KX)?<ST>HOUSE` series/tickers, and enhanced `_is_state_legislative_contest()` with state House of Representatives chamber pattern. Distinguishes the actual contest proposition from parent event titles, incidental federal wording, and incidental district codes (such as `tx-15` in slugs or "TX-15 is also on the ballot"), ensuring state-chamber winner/control propositions fail closed as `state_legislative_contest` and `other`. For state-chamber seat counts, federal identity must be established directly by the contest proposition itself (e.g. "Texas House of Representatives seats in the 2026 U.S. House elections") or by a recognized Kalshi federal seat-count series (`KXVAHOUSEDEM`, `KXTXHOUSEDEMSEATS`, `KXCAHOUSEDEM`); parent event federal wording and incidental district targets cannot convert a question about state chamber seats into a federal contest. Genuine district contest targets (`_DISTRICT_CONTEST_TARGET_RE`) only preserve races when the proposition itself did not name a state chamber, while genuine district contracts (`HOUSETX15`, `KXHOUSERACE`) remain admitted. Added 21 regression tests in `test_defect_regressions.py` covering recorded and synthetic state legislative chamber markets, parent event title isolation, incidental federal wording fail-closed boundaries, district slug/mention boundaries, state chamber seat-count parent-event isolation, incidental district distractor exclusion, and federal seat-count preservation. Total venue test suite increased from 651 to 672 tests (all passing); combined test suite: 2,552 passed.

### Backup Metadata Reconciliation
Measured on disk (`size`, raw SHA-256; every file below uses CRLF line endings except where noted):
- `s1_t5_f6_r2_pre_edit_backup`: `fetch_venue_markets.py` 79,535 bytes `c31f6af3e6bda474d879b3d7396dad15924bd38e986147d7fb83b4262c6996e0` (LF-normalised 77,745 bytes); `test_house_admission_regressions.py` 29,182 bytes `da60ef1445fe1d109eb762abb17e0212a584489abebca685502356220dbe23a9` (LF-normalised 28,547); `README.md` 15,556 bytes `174b73237ff519021ade40209ddada43503fef67b4992a3f5fd8164f1ce14b3f` (LF-normalised 15,372). These are byte-for-byte identical to the R1 exports.
- `s1_t5_f6_r3_pre_edit_backup`: 82,394 / 43,574 / 18,663 bytes (`cab3d8f0...`, `656e8e09...`, `de1a2abf...`), byte-for-byte identical to the R2 exports.
- `s1_t5_f6_r1_pre_edit_backup`: 72,343 / 17,710 / 13,728 bytes (`f0bd016c...`, `628feee8...`, `1a903b28...`); the test file there uses LF.

Corrections: the R3 report labelled the 79,535 / 29,182 / 15,556-byte files as LF, but they are CRLF. The R2 report's 81,180 / 15,310 / 37,298-byte figures match no measured backup or export in either line-ending form, so R3's explanation (an intermediate CRLF copy) is not supported by the files; their origin is undetermined. No backup was modified.

### Cached Record Coverage Replay
Offline replay of the raw cached October 11-12 responses (no network access):
- Polymarket: 90 of the 92 D/R contracts across the 46 SIG House races admitted; ME-02 markets 3006482 and 3006483 stay excluded (`mismatched_district`) because their recorded slugs name `me-01`. No slug is rewritten and no ID is hard-coded.
- Kalshi: all 34 D/R contracts across the 17 previously missing races admitted; across all 706 cached `KXHOUSERACE` contracts, 700 D/R contracts are admitted, 5 candidate contracts are excluded (`arbitrary_ticker_suffix`) and 1 finalized contract is inactive.

### Full Replay & Verification (Fresh & Cached Records)
Offline replay across 138,432 total records (125,388 fresh Kalshi, 11,934 fresh Polymarket, 706 cached Kalshi, 404 cached Polymarket):
- **Total admission changes:** Exactly 4 changes across all 138,432 records (all in fresh Kalshi; 0 in Polymarket, 0 in cached Kalshi):
  - `HOUSEIA1-26-I`: Excluded (`unsupported_party_outcome`) due to Independent outcome.
  - `HOUSEAKAL-26-BHIL`: Excluded (`unsupported_resolution_subject`) due to named-candidate swearing-in condition.
  - `HOUSEAKAL-26-D` and `HOUSEAKAL-26-R`: Excluded (`unsupported_resolution_subject`) due to inverted member-first syntax.
- **SIG-Scope D/R Contracts:** Exactly 58 of 58 remain admitted (`True, None`), all corroborated through their own decisive rules.
- **Prior Exclusions:** ME-02 (`mismatched_district`) and Polymarket AK-AL/WA-08 (`unsupported_background_sentence`) persist as excluded.

### Request Accounting Reconciliation
- Total initiated network attempts in F6: 7 (against a 6-attempt budget); all 7 failed due to local Windows TLS trust environment issues (5 diagnostic script attempts + 1 `certifi` Python attempt + 1 `Invoke-RestMethod` PowerShell attempt). The F5 evidence ledger is separate and is not combined with this budget.
- R3 and C1 made zero network requests; verification runs offline against cached artifacts and local tests.

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
