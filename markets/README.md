# `markets/`: R1 contract classification and venue mapping

Roadmap track **R1** (legacy S1-T6). These files classify every SIG contract in a
provenanced inventory and record, separately for Polymarket and Kalshi, whether a
settlement-equivalent counterpart has been **verified by a human reviewer**.

> R1 supplies classification evidence toward G-1. It does not pass G-1, does not decide
> commitment or related-market-group policy (OQ-C4), does not answer any section W
> question, and does not activate fair value or trading. Every `fair_value_eligible` flag
> is `false`, and the validator rejects `true` while no confidence rule is approved.

Commands are in [`data/r1_mapping/README.md`](../data/r1_mapping/README.md). The latest
generated status is [`R1_REPORT.md`](R1_REPORT.md).

## Relationship to the election-week bot map (3 Nov 2026)

The bot's [`map.yaml`](../bot/map.yaml) is a narrower, independently approved
**trading selection**, not the R1 settlement-equivalence ledger. Its `approved: true`
flag permits a contract in the election-week bot's map; it is not an R1
`verified_match` decision for Kalshi or Polymarket. The bot map records proposed
venue identifiers but does not contain reviewed SIG Info-tab text, criterion-by-criterion
comparisons or R1 reviewer provenance. Do not copy its approvals into
`r1_mapping.json` without the R1 evidence and review workflow below.

| Recorded state | Count |
| --- | ---: |
| SIG contracts in the 9 Oct R1 inventory | 237 |
| Those contracts represented in the bot map | 190 (95 races) |
| Bot contracts flagged `approved: true` | 114 (57 races; none in an excluded race) |
| Bot contracts flagged `approved: false` | 76 |
| R1 inventory contracts absent from the bot map | 47 |
| R1 reviewed SIG Info-tab texts | 0 |
| R1 verified Polymarket / Kalshi matches | 0 / 0 |

The 114 approved bot entries all match an R1 market and exchange ID. Of these,
114 carry a Kalshi ticker and 78 carry a Polymarket token ID; those identifiers are
**candidate reference links**, not R1 equivalence findings. The 10 excluded bot
races contain no approved contracts. These counts describe committed files at
[`af48722b`](https://github.com/ArnavJain2005/Bellwether/commit/af48722b78d3372ef27c5ece5c304d48ee876659),
not a current SIG inventory or a new human review. The generated
[`R1_REPORT.md`](R1_REPORT.md) remains the source of truth for formal R1 status.

## Files

| File | Written by | What it holds |
| --- | --- | --- |
| `r1_sources.json` | hand (Arnav approves) | Pinned inputs: the SIG inventory (recorded fixture pages, hashes, completeness basis, limits), each frozen venue output (file, SHA-256, counts, S1-T5 validation result, per-venue coverage), and policies: authorized reviewers, OQ-C4 status, fair-value confidence rule, race-identity source. |
| `r1_mapping.json` | `init`, then reviewers | One record per SIG contract: SIG identity, race identity, OQ-C4 group proposals, one decision per venue, history. |
| `sig_resolution_evidence.json` | `init`, then the person capturing Info tabs | One entry per SIG contract: status of its verbatim Info-tab resolution text. |
| `sig_info_tab/<market_id>.md` | the person capturing Info tabs | Verbatim Info-tab text (none exist yet). |
| `r1_candidates.json` | `candidates` (generated) | Algorithmic venue suggestions and near misses with reasons. Never evidence. |
| `R1_REPORT.md` | `report` (generated) | Coverage report and missing-input manifest keyed by SIG market ID. |

All files are deterministic: records are ordered by numeric SIG market ID, one reviewed
field per line, so a new snapshot or a review produces a readable diff. Raw venue dumps,
SIG captures and account material are never committed here.

## Record format (`r1_mapping.json`)

```jsonc
{
  "sig": {"market_id": "258", "exchange_id": "947", "option": "YES",
          "title": "Will the Democratic Party win the Georgia Senate?",
          "settlement_date": "2026-11-04T17:00:00.000Z"},
  "identity": {
    "status": "unresolved",              // or "verified"
    "reason": "...",
    "proposed": {"basis": "sig_title_grammar", "shape": "statewide", "office": "senate",
                 "state": "GA", "district": null, "party": "D",
                 "election_type": "unverified", "cycle": "unverified",
                 "race_key": "senate:GA"},
    "authoritative": null                // {race_id, stage_id?, source, reviewed_by, reviewed_at, ...}
  },
  "groups": {"status": "unresolved_oq_c4", "approved": [],
             "proposals": ["chamber:senate", "state:GA", "party-outcome:D"]},
  "venues": {
    "polymarket": {"decision": "unresolved", "reason_codes": [...],
                   "venue_snapshot": "venue-20261011T183701Z", "match": null,
                   "confidence": null, "discrepancies": [],
                   "review": {"status": "pending", "reviewer": null, "reviewed_at": null},
                   "fair_value_eligible": false},
    "kalshi": { ... }
  },
  "history": [{"at": "2026-10-28", "by": "...", "change": "created", "detail": "..."}]
}
```

- **SIG contract.** Each SIG market in the inventory has exactly one exchange with option
  `YES`; that exchange is the contract. A NO position is the complement of the same
  exchange, so there is no separate NO record.
- **Race identity.** `proposed` is read from the title only (three recorded shapes). It is
  a proposal, not a race ID: titles carry no race ID, stage ID, cycle or regular/special
  flag. `race_key` follows from the fields (`senate:GA`, `house:AL-02`, `senate_control`;
  a special election gets `:special`, so it never shares a key with the regular race).
  `status` becomes `verified` only with an `authoritative` block from a reviewed source,
  which must also settle `election_type` and `cycle`.
- **Groups (OQ-C4).** `proposals` lists every PRD OQ-C4 candidate the contract would join
  (a contract can be in several). Nothing chooses a definition. `approved` stays empty
  until Arnav decides OQ-C4 in `r1_sources.json`.
- **Decisions**, one per venue, independent of each other:
  - `unresolved` (default): `reason_codes`, each defined in `reason_code_definitions` or
    explained in a `reason`.
  - `no_match_in_reviewed_scope`: `searched_scope.sources` (each `kind`, `ref`,
    `complete_for_scope`; a complete non-snapshot source also needs `evidence`,
    `reviewed_by`, `reviewed_at`) and `coverage_limits`. Absence from a frozen snapshot
    whose coverage is incomplete is **not** a no-match.
  - `verified_match`: `match` (Polymarket `market_id`, `event_id`, `question`,
    `outcome_label` `Yes`, `token_id`, `end_date`, `rules_sha256`; Kalshi `market_ticker`,
    `event_ticker`, `series_ticker`, `title`, `side` `yes`, `close_time`, `rules_sha256`),
    `equivalence` for every criterion below, `confidence` (`high`/`medium`), and an
    approved `review`.
- **`discrepancies`**: `{code, detail, status: open|resolved, resolution}`. Any open
  discrepancy blocks `verified_match` and fair value.
- **`shared_outcome_exception`** (`{id, reason, reviewed_by}`): the only way two SIG
  contracts may map to the same venue outcome; every record sharing it must carry the
  same exception.
- **`history`**: append an entry (`at`, `by`, `change`, `detail`) for every change.

## Equivalence criteria (verified_match)

Each criterion records `status`, `sig_evidence`, `venue_evidence` and `note`. Strict
criteria must be `same`; `resolution_source` and `timing` may be `different_explained`
with a note that explains the difference against both rules.

| Criterion | Compare | Strict |
| --- | --- | --- |
| `event_and_office` | Same election and office (Senate, governor, House, chamber control) | yes |
| `jurisdiction` | State and district, including at-large | yes |
| `election_cycle_and_term` | Cycle and term (for example 2026 election, term from 2027) | yes |
| `election_type` | Regular versus special | yes |
| `outcome_subject` | Party versus a named candidate or "an independent" | yes |
| `outcome_polarity` | SIG YES equals venue Yes on a same-party question | yes |
| `proposition` | Wins the election, sworn in, inaugurated, controls chamber, vote share | yes |
| `settlement_condition` | What exactly decides YES, including runoffs and replacements | yes |
| `resolution_source` | Resolution authority and source, certification | explained |
| `timing` | Cutoffs, certification, venue close versus SIG settlement | explained |

A shared party label, a similar question, a ticker pattern or a nearby price is never
enough. Venue metadata is never rewritten to create a match (for example, Kalshi
`SENATELA-26-*` rows describe Kentucky): record a discrepancy and fail closed.

## Reviewer workflow

1. **Capture SIG Info-tab text** (Arnav, by an authorized process; no agent logs in to
   SIG). Save it verbatim to `sig_info_tab/<market_id>.md`, then in
   `sig_resolution_evidence.json` set `text_path`, `text_sha256` (SHA-256 of the UTF-8
   text with line endings normalised to LF), `source` (for example "SIG Info tab, market
   258 / exchange 947"), `captured_by`, `captured_at`, and `status: captured_unreviewed`.
   After review set `reviewed_by`, `reviewed_at`, `status: reviewed`.
2. **Race identity.** From a reviewed source of SIG race and stage IDs, fill
   `identity.authoritative`, set `proposed.election_type` and `proposed.cycle`, and set
   `status: verified`. Sibling contracts must share one race ID.
3. **Venue decisions.** Read `r1_candidates.json` for the contract, open the venue
   record in the frozen output named by `venue_snapshot`, and compare every criterion
   against the SIG text. Copy the `match` fields exactly from the snapshot; the validator
   re-checks them, the token's outcome and the party and district.
4. **Run** `python -m data.r1_mapping validate`, then `report`, and commit both.
5. **Fair value** stays `false` until Arnav approves a confidence rule in
   `r1_sources.json`. The proposal there is not policy.

Only names in `policies.authorized_reviewers` can approve evidence, identities or
decisions. Agents may draft records but never approve them.
