# Account-state contracts and snapshot design (A1)

Oct 12, 2026 · Roadmap track A1 · Input to gates G-1, G-2, G-3 and G-7 ·
Status: **draft for independent design review**

This note inventories the SIG account reads the engine could use, records what the API
spec, the recorded fixtures and the code on `main` establish about each one, and
proposes the shape of the raw reads (A2) and the `AccountSnapshot` (A3). It is a design
only. It changes no code, no `shared/` schema, no risk setting and no gate status. It
selects no risk limit, valuation policy, bootstrap policy or activation setting
([PRD, roadmap](../PRD.md#roadmap)).

**Bottom line.**

- **No read gives established tournament cash today.** `GET /account` takes no
  `tournamentId` and reports a default-context balance, so it is never used as
  tournament cash. The only explicitly addressed candidate is `myBalance` on
  `GET /tournaments/{slug}`. It is documented as the caller's "current or seed" balance
  in that tournament. Three things are undocumented: whether it excludes cash held by
  resting buy orders, whether it is current at the instant it is served, and what it is
  while enrolment is pending (it may be a seed). OQ-W11 and OQ-C3 stay open.
- **Of the reads that can be pinned to the tournament, only three echo their scope:**
  the tournament read, per-order fills and the collateral rows. Positions, P&L and
  settlements rest on a verified slug in the path. Orders and `/portfolio/fills` rest on
  the `tournamentId` sent.
- **Units are partly documented.** Position and fill quantities have a documented sign
  convention: positive YES, negative NO. No price, cost or value field on a REST
  position, fill or order states its denomination. That leaves OQ-W4 to OQ-W7 open for
  every field the commitment, valuation or reconciliation logic would need.
- **No read carries a server-side as-of time, and nothing ties reads together.**
  Balance, positions and P&L have no projection metadata. Orders and fills carry a
  `projectedThroughSequence` that is provenance, not a freshness proof. An
  `AccountSnapshot` built from these reads is a set of separate observations, and it
  says so.
- **The A3 snapshot therefore has no "ready" state.** It reports what it observed, what
  is missing, what contradicts and what is unresolved. Account readiness for trading is a
  later engine decision (S1) that also needs the open questions answered.

The smallest useful next slice, A2-A (section 8), adds three things, all read-only and
without units:

- a fresh tournament-balance read;
- the tournament positions read, with the documented 409 kept as a typed result;
- per-page provenance for the existing open-orders walk.

## How to read this note

Every statement carries one of these labels where it matters (as in the
[M1 note](market-data-recovery.md#how-to-read-this-note)):

| Label | Meaning |
| --- | --- |
| **[Documented]** | Stated by `docs/api/openapi.json`, cited by JSON pointer. Not necessarily observed on the tournament. |
| **[Observed]** | Seen in a committed recorded fixture under `tests/fixtures/sig/recorded/`. An observation is not a contract. |
| **[Code]** | What the code on `main` (`3a7fc15`) does, cited as `path:line`. |
| **[Proposed]** | A policy or mechanism this note proposes. Not implemented and not approved. |
| **[Unknown]** | Not settled by the spec or by evidence. Never resolved by inference here; mapped to a PRD question where one exists. |

Code references are `file:line` on `main` at `3a7fc15`. The short forms `rest.py`,
`wire.py`, `errors.py`, `rate_limiter.py`, `readiness.py` and `recovery.py` are files in
`engine/sig_client/`.

**Scope:** REST reads of our own account state in the configured tournament
(`midterm-elections`). Also covered are the default-context counterparts where comparing
them clarifies scope, and the `user:{profile_id}` realtime channel as context only.
**Out of scope:**

- market-data recovery (M1, M2);
- the socket adapter (M3);
- every write endpoint, including order placement and cancellation (O1, O2);
- an account-channel implementation;
- risk, valuation, bootstrap and activation policy (G-1, G-2, G-3, G-11 decisions are
  Arnav's).

### Evidence available

- **[Documented]** The whole OpenAPI file. "Documented" means the spec states it, not
  that it has been seen on the tournament ([PRD, document status](../PRD.md#document-status)).
- **[Observed]** Three read-only captures by Arnav (`recorded/20261009T122244Z-fa008a`,
  `recorded/20261009T190027Z-fb88c9` and `recorded/20261011T211201Z-12ab20`).
  - **The tournament read:** all three captures, but `$.myBalance` and
    `$.isPendingEnrolment` are **redacted to fixed placeholders** (`12345.67`, `true`)
    in every committed body, so they say nothing about the account
    ([fixtures README](../../tests/fixtures/sig/README.md)).
  - **The open-orders read:** one capture, `status=all`, a single empty page with
    `coverage` present.
  - **Never captured:** positions, fills, P&L, settlements, transactions, collateral,
    per-order fills or `/account`.
- **[Code]** The read client in `rest.py` and the wire models in `wire.py`. The only
  account reads it implements are `GET /tournaments/{slug}` and `GET /orders`.

### OpenAPI citations used below

All pointers are into [`docs/api/openapi.json`](../api/openapi.json). Field-level
pointers are written out in full where a claim depends on them.

| Alias | JSON pointer | Content |
| --- | --- | --- |
| [INFO] | `#/info/description` | Authentication, key binding, scopes, error table, default-scope rules ("Position monitoring"), pagination and coverage contract, rate limits |
| [SEC] | `#/components/securitySchemes/bearerAuth` | Bearer or `X-API-Key` header; applied globally by `#/security` |
| [ACCT] | `#/paths/~1account/get` | Profile and default-context balance |
| [TLIST] | `#/paths/~1tournaments/get` | Accessible tournaments with `myBalance`, `joinedAt`, `isPendingEnrolment` |
| [TOUR] | `#/paths/~1tournaments~1{slug}/get` | Tournament metadata and `myBalance`, `isPendingEnrolment` |
| [TPOS] | `#/paths/~1tournaments~1{slug}~1portfolio~1positions/get` | Tournament positions; 409 for holdings without a valuation price |
| [TPOS-ROW] | `#/components/schemas/TournamentPortfolioPosition` | Position row: signed `quantity`, `avgCost`, `currentPrice`, `lots` |
| [MISSVAL] | `#/components/schemas/MissingTournamentValuation` | The 409 body: `details.missingValuations[]` |
| [TFILLS] | `#/paths/~1tournaments~1{slug}~1portfolio~1fills/get` | Tournament fills, newest first, cursor pages, optional `coverage` |
| [FILL] | `#/components/schemas/Fill` | Fill row: outcome-signed `quantity`, `price`, `side` |
| [TPNL] | `#/paths/~1tournaments~1{slug}~1portfolio~1pnl/get` | Tournament account value and P&L; 409 as for positions |
| [TSETTLE] | `#/paths/~1tournaments~1{slug}~1portfolio~1settlements/get` | Our settlement entries; `coverage.complete` can be false |
| [TCOV] | `#/components/schemas/TournamentEconomicCommandCoverage` | "hard 20-page budget" completeness flag |
| [TTX] | `#/paths/~1tournaments~1{slug}~1portfolio~1transactions/get` | Cash and trade audit trail |
| [THIST] | `#/paths/~1tournaments~1{slug}~1portfolio~1history/get` | Value series with `cashValue`; snapshots plus a live valuation |
| [ORDERS] | `#/paths/~1orders/get` | Order list with `tournamentId` filter, optional `coverage` |
| [ORDER] | `#/components/schemas/Order` | Order row: positive `quantity`, `priceLimit`, `open`, `expirationDate` |
| [ORDER1] | `#/paths/~1orders~1{id}/get` | One order; context from the order id |
| [OFILLS] | `#/paths/~1orders~1{id}~1fills/get` | One order's fills; echoes `tournamentId`; lifetime totals |
| [COLL] | `#/paths/~1portfolio~1collateral/get` | ALL collateral components; echoes `tournamentId` per row |
| [PPOS], [PPNL], [PSETTLE], [PTX], [PHIST] | `#/paths/~1portfolio~1positions/get` and siblings | Default-scope portfolio reads; no `tournamentId` parameter |
| [PFILLS] | `#/paths/~1portfolio~1fills/get` | Fills with an explicit `tournamentId` parameter |
| [OIN-T] | `#/components/schemas/OrderInput/properties/tournamentId/description` | A first order in an open tournament enrols the caller |
| [PLACE] | `#/paths/~1orders/post` | Netting, sell canonicalisation, "insufficient balance", admission sync 503 (cited for context; no write is proposed) |
| [TPAG] | `#/components/schemas/TournamentPagination` | `limit`, `hasMore`, `nextCursor` (nullable), all required |
| [RT] | `#/paths/~1realtime~1token/post/description` | `user:{profile_id}` channel and `account_batch` arrays |

## 1. Common facts for every account read

### 1.1 Authentication and key binding

- **[Documented]** Every endpoint requires an API key, sent as
  `Authorization: Bearer <key>` or `X-API-Key: <key>` ([INFO], [SEC]). No account
  operation overrides the global `#/security` requirement. Reads need the `read` scope:
  "`GET` — market data, prices, orderbooks, positions, fills, transaction history"
  ([INFO]).
- **[Documented]** "Keys owned by non-platform organization members are automatically
  bound to that organization" ([INFO]). Binding decides what an omitted context means:
  an organization-bound key resolves account and portfolio reads to "the organization's
  default tournament ... falling back to its Ongoing Play tournament when none is
  designated, so the default may be finite"; an unbound key reads the public global
  context ([INFO], "Position monitoring").
- **[Unknown]** Whether Bellwether's key is organization-bound, and which tournament is
  its organization's default. Neither is exposed by any read. This is exactly why an
  omitted or default context can never be accepted as the configured tournament
  (OQ-W11).
- **[Code]** The client sends the key per request in the `Authorization` header only,
  refuses injected credential headers, never follows redirects and never puts response
  text into errors or logs (`rest.py:19`-`28`, `rest.py:403`-`406`, `errors.py:3`-`13`).

### 1.2 Tournament binding, by kind of evidence

The configured tournament is a slug and UUID pair (`engine/config.py:63`), which the client
checks against `GET /tournaments/{slug}` before any scoped read (`rest.py:7`-`11`,
`rest.py:493`). Each account read binds to it in one of four ways:

| Kind | Reads | What it shows |
| --- | --- | --- |
| **Echo** | [TOUR] (`id`, `slug`); [OFILLS] (`tournamentId`, nullable); [COLL] rows (`tournamentId`, nullable); [PTX] rows (`tournamentId`, default scope only) | The response names its scope, so the client can compare it with the configuration |
| **Verified slug in the path** | [TPOS], [TFILLS], [TPNL], [TSETTLE], [TTX], [THIST] | The path names the tournament; the slug was checked against the UUID once. Rows carry no tournament id |
| **UUID parameter sent, no echo** | [ORDERS], [PFILLS] | Scope rests on the `tournamentId` sent, as for the trade tape. ([COLL] also takes the parameter, but its rows echo it) |
| **Default context** | [ACCT], [PPOS], [PPNL], [PSETTLE], [PTX], [PHIST]; and [ORDERS], [PFILLS], [COLL] when `tournamentId` is omitted | **Never tournament evidence.** Resolves to the key's default or the global context ([INFO]) |

- **[Documented]** [ORDER1] and [OFILLS]: "The order ID determines its trading
  context" (`#/paths/~1orders~1{id}/get/description`). [ORDER] itself has no tournament
  field, so `GET /orders/{id}` cannot show which tournament an order is in. Only its
  fills read can (`#/paths/~1orders~1{id}~1fills/get/responses/200/content/application~1json/schema/properties/tournamentId`).
- **[Documented]** The default-scope [PTX] row has `tournamentId`
  (`#/components/schemas/Transaction/properties/tournamentId`), but the tournament
  [TTX] row does not (`#/components/schemas/TournamentTransaction`). Comparing the two
  shows that per-row scope echo is a property of the default-scope schema only.

### 1.3 Required, optional, nullable and extra fields

- **[Documented]** No account response schema sets `additionalProperties`. The twelve
  places in the file that set it to `false` are all request bodies or relationship
  expressions, none an account response. Extra fields are therefore allowed by the
  spec.
- **[Code]** Wire models ignore unknown fields (`wire.py:11`-`12`, `wire.py:76`), as
  they must for an open schema.
- **[Observed]** No extra field on the tournament read in any capture. The orders page
  had exactly `data`, `pagination` and `coverage`.
- Per-read required, optional and nullable fields are listed in section 2.
- **[Documented]** `coverage` is optional (not in `required`) on [ORDERS], [TFILLS] and
  [PFILLS], and required on [OFILLS], [TSETTLE], [TTX], [PSETTLE] and [PTX]. Where it is
  optional it is not nullable. Example:
  `#/paths/~1orders/get/responses/200/content/application~1json/schema/required`
  lists only `data` and `pagination`.

### 1.4 Errors common to every read

- **[Documented]** All errors share `{ error: { code, message, details? } }`, and
  clients should "Branch on `code` (stable), not on `message`" ([INFO]). The per-endpoint
  error schemas are `#/components/schemas/TournamentError`, `PortfolioError`,
  `OrderError` and `MeError`; their `details` is free-form.
- **[Documented]** **Any** key-authenticated endpoint may answer 401 (key codes), 403
  (`INSUFFICIENT_SCOPES`, `ACCOUNT_BANNED`, `RESIDENCE_UPDATE_REQUIRED`, `FORBIDDEN`),
  429 `RATE_LIMITED` with `Retry-After: 60`, or 503 with `Retry-After` "before it runs
  when rate limiting is temporarily unavailable" ([INFO]). So 503 is possible on reads
  whose own `responses` list none ([TSETTLE], [TTX], [PPOS], [PSETTLE], [PTX]).
- **[Documented]** 409 occurs on account reads only for **holdings without a valuation
  price**, on [TPOS], [TPNL] and [PPNL]. The 409 body is [MISSVAL] (or
  `#/components/schemas/PortfolioMissingTournamentValuation`): `error.code` is the enum
  `CONFLICT`, and
  `#/components/schemas/MissingTournamentValuation/properties/error/properties/details/properties/missingValuations`
  is a required array of `{ exchangeId, marketId, marketTitle, settlementOption }`.
  `409 REQUEST_IN_FLIGHT` is an order-placement code and does not apply to these reads.
- **[Code]** Reads retry 429, 503 and a fixed list of transport failures only, within one
  deadline per logical read, through a shared account limiter (`rest.py:158`-`167`,
  `rest.py:1084`, `rate_limiter.py:35`-`36`, `rate_limiter.py:118`). Every other status
  raises at once. A 409 becomes
  `SigConflictError`, 403 becomes `SigAuthError`, and only a documented `code` survives:
  `details` is discarded (`rest.py:240`-`248`, `rest.py:1147`-`1179`). **The
  `missingValuations` list of a 409 is therefore lost today.**

### 1.5 Pagination and coverage

- **[Documented]** Every list response is "a bounded page ... with `pagination.hasMore`
  and an opaque `pagination.nextCursor`". "Where present, the `coverage` object reports
  `complete` for the requested window and `projectedThroughSequence` — the newest engine
  event reflected by the read models at response time" ([INFO]).
- **[Documented]** Two different coverage contracts apply to account reads:
  - **Projection coverage** on [ORDERS], [TFILLS], [PFILLS] and [OFILLS]. `complete` is
    "Always true because cursor pagination exposes the complete ... history" (an enum
    `[true]`). `projectedThroughSequence` is the "Latest trading-engine sequence
    reflected by the reporting projection, or null when no checkpoint exists"
    (`#/paths/~1tournaments~1{slug}~1portfolio~1fills/get/responses/200/content/application~1json/schema/properties/coverage`).
  - **Economic-command coverage** on [TSETTLE], [TTX], [PSETTLE] and [PTX]: only
    `complete`. It is "False when the economic-command history walk reached its hard
    20-page budget before it found enough matching events or exhausted the log"
    (`#/components/schemas/TournamentEconomicCommandCoverage/properties/complete/description`).
    **A page can legitimately be incomplete.**
- **[Documented]** Positions and P&L are single objects with no pagination and no
  coverage. Balance has neither.
- **[Unknown]** Three things are undocumented:
  - how a cursor behaves when new fills, orders or settlements are written during a
    newest-first walk;
  - whether a page that reports `complete: false` can be continued with its cursor;
  - how `projectedThroughSequence` relates to the state behind balance, positions or
    P&L.

  The M1 note found the same projection-freshness gap for the trade tape
  ([M1, 3.4](market-data-recovery.md#34-what-the-recorded-sample-shows-and-does-not-show)).
- **[Code]** `_collect` follows `nextCursor` while `hasMore`. It fails on a repeated
  cursor, a cursor on the last page, a duplicate item key or the page cap, and never
  returns a partial list (`rest.py:965`-`1009`; cap `DEFAULT_MAX_PAGES = 50`,
  `rest.py:145`).

### 1.6 Time

- **[Documented]** "Timestamps are ISO-8601 UTC" ([INFO]). No account read documents an
  as-of or served-at time for balance, positions or P&L. The only engine version on
  account reads is `projectedThroughSequence`.
- **[Proposed]** Observation times are therefore always our own: the local monotonic
  clock to order our own requests, and local UTC for display. They say nothing about
  SIG's state at any instant (as for `LifecycleObservation`, `recovery.py:162`).

## 2. Contract evidence matrix

One subsection per read. "Fields" lists required fields, with `?` for optional and
`|null` for nullable, as stated by the 200 schema at the cited pointer.

### 2.1 `GET /account` [ACCT]

| Aspect | Evidence |
| --- | --- |
| Binding | **[Documented]** No parameters. "For an organization-bound key, `balance` comes from the organization's default tournament: enrolled members receive that tournament's balance (engine-authoritative when routed), while an organization member pending enrolment receives its initial virtual balance. An unbound key retains the public-global cash balance" (`#/paths/~1account/get/description`). No scope echo: the body names no tournament |
| Fields | `id`, `username`\|null, `email`\|null, `createdAt`, `avatarUrl`\|null, `bio`\|null, `balance`\|null, all required (`#/paths/~1account/get/responses/200/content/application~1json/schema/required`) |
| Cash | `balance`: "Default-tournament balance for an organization-bound key; public-global cash balance for an unbound key" (`#/paths/~1account/get/responses/200/content/application~1json/schema/properties/balance`). It is nullable, and the meaning of null is undocumented |
| Errors | 403 (cannot access its resolved default tournament), 404 (profile or default tournament not found), 500, 503 ("authoritative default tournament or public-global balance is temporarily unavailable"); schema `MeError` |
| Observed | Never captured |
| Code | Not implemented |
| Use | **[Proposed] Not used** by A2 or A3. Its balance is not tournament cash (OQ-W11). It also carries personal fields (`email`, `username`, `bio`) that the engine does not need |

### 2.2 `GET /tournaments/{slug}` [TOUR] and `GET /tournaments` [TLIST]

| Aspect | Evidence |
| --- | --- |
| Binding | **[Documented]** Path slug; the body echoes `id` (UUID) and `slug`. "Returns metadata and the authenticated member's current virtual balance for the tournament" (`#/paths/~1tournaments~1{slug}/get/description`) |
| Fields | `id`, `slug`, `name`, `description`\|null, `status`, `startDate`\|null, `endDate`\|null, `initialBalance`, `currencyName`, `memberCount`, `marketCount`, `myBalance`, `isPendingEnrolment`, all required and only the three marked nullable (`#/paths/~1tournaments~1{slug}/get/responses/200/content/application~1json/schema/required`) |
| Cash | `myBalance` (number, not nullable): "Caller's current or seed virtual cash balance in this tournament" (`#/paths/~1tournaments~1{slug}/get/responses/200/content/application~1json/schema/properties/myBalance`) |
| Enrolment | `isPendingEnrolment`: "True when the caller can access an open tournament, or a private tournament of an organization they own or administer, but has not yet been enrolled" (`#/paths/~1tournaments~1{slug}/get/responses/200/content/application~1json/schema/properties/isPendingEnrolment`). A first order enrols the caller ([OIN-T]). Just after enrolment, orders may get 503 because "a new tournament membership is still syncing to the trading engine" (`#/paths/~1orders/post/responses/503/description`) |
| Denomination | `currencyName` names the tournament currency. **[Observed]** `"SUSQies"`, `initialBalance: 100000`, `status: "active"`, start `2026-10-01T16:00:00.000Z`, end `2026-11-04T17:00:00.000Z` in all three captures |
| Errors | 400, 403 ("Not a member of this tournament", but 200 for an open tournament), 404, 500, 503 ("The authoritative tournament balance is temporarily unavailable") |
| Observed | Three captures. `myBalance` and `isPendingEnrolment` are **redacted placeholders**. No extra fields |
| Code | `Tournament` model with `myBalance: WireNumber` (exact `Decimal`) and `isPendingEnrolment: bool` (`wire.py:83`-`96`). `get_tournament` reads afresh, re-checks slug and UUID, and replaces the cached result (`rest.py:474`-`482`). `verify_tournament` reads only when nothing is cached and otherwise **returns the cached `Tournament`** (`rest.py:484`-`491`) |
| [TLIST] | Same balance fields per row plus `joinedAt`\|null (`#/components/schemas/TournamentSummary/properties/joinedAt`), with offset pagination. It includes "open organization tournaments before first-action enrolment" (`#/paths/~1tournaments/get/description`). **[Proposed]** Not used in A2: [TOUR] gives the same balance with an identity check. How `joinedAt` relates to enrolment is undocumented (A1-N4) |

### 2.3 `GET /tournaments/{slug}/portfolio/positions` [TPOS]

| Aspect | Evidence |
| --- | --- |
| Binding | **[Documented]** Verified slug in the path; rows carry no tournament id. "Prices use tournament valuation_price" (`#/paths/~1tournaments~1{slug}~1portfolio~1positions/get/description`) |
| Fields | `positions[]` and `summary{ totalMarketValue, totalCostBasis, totalUnrealizedPnl }`, both required. Rows: `exchangeId`, `marketId`, `marketTitle`, `option`\|null, `settled`, `quantity`, `avgCost`, `currentPrice`\|null, `marketValue`, `costBasis`, `unrealizedPnl`, `unrealizedPnlPct`, `moneyEarned`, `lots[]{ lotId, side, quantity, entryPrice, openedAt }`, all required (`#/components/schemas/TournamentPortfolioPosition/required`) |
| Quantity | **[Documented]** "Positive = YES shares, negative = NO shares" (`#/components/schemas/TournamentPortfolioPosition/properties/quantity/description`). That fixes the sign. Fractional shares and granularity are undocumented |
| Price and value | `currentPrice`: "Tournament valuation price. Null only for zero-quantity rows; nonzero holdings without a valuation return 409" (`#/components/schemas/TournamentPortfolioPosition/properties/currentPrice/description`). **[Unknown]** The denomination of `avgCost`, `currentPrice` and `lots[].entryPrice` (YES price or held-side price), and the units and signs of `marketValue`, `costBasis`, `unrealizedPnl` and `moneyEarned` for a NO holding: none has a description on the tournament row (OQ-W5). `unrealizedPnlPct` scale is undocumented |
| Lots | `lots[].side` is a free string, not an enum; lot `quantity` has no sign rule. **[Unknown]** How lots relate to the signed `quantity` after the documented FIFO netting of an opposite-signed buy (`#/paths/~1orders/post/description`; OQ-W8) |
| Rows included | **[Unknown]** "Returns the caller's open positions", yet rows carry `settled` and may have zero quantity (`currentPrice` null "only for zero-quantity rows"). Whether settled or flat rows appear is undocumented |
| Pagination | None: one object with no coverage. **[Unknown]** whether the list is ever truncated |
| Errors | 403 ("Not a member of this tournament"), 404, **409** "Tournament holdings exist without valuation prices" ([MISSVAL], `#/paths/~1tournaments~1{slug}~1portfolio~1positions/get/responses/409`), 500, 503 ("The authoritative tournament portfolio is temporarily unavailable") |
| Default counterpart | [PPOS] has no `tournamentId` and "resolves this endpoint to its organization's default tournament" (`#/paths/~1portfolio~1positions/get/description`). Its row's `currentPrice` is "Global price for unbound keys or tournament valuation price for organization-bound keys" (`#/components/schemas/Position/properties/currentPrice/description`). **Never used** |
| Observed / Code | Never captured; not implemented |

### 2.4 `GET /tournaments/{slug}/portfolio/fills` [TFILLS] and `GET /portfolio/fills` [PFILLS]

| Aspect | Evidence |
| --- | --- |
| Binding | **[Documented]** [TFILLS]: verified slug in the path. [PFILLS]: explicit `tournamentId` parameter (`#/paths/~1portfolio~1fills/get/parameters/4`); omitted means the default context. Neither echoes scope. Fills "require membership in the requested accessible tournament" ([INFO]) |
| Fields | `data[]` ([FILL]: `id`, `orderId`\|null, `exchangeId`, `marketId`, `price`\|null, `quantity`, `side` ∈ {`yes`,`no`}, `filledAt`, all required), `pagination` ([TPAG] `#/components/schemas/TournamentPagination`: `limit`, `hasMore`, `nextCursor`\|null), `coverage?` (`#/paths/~1tournaments~1{slug}~1portfolio~1fills/get/responses/200/content/application~1json/schema/properties/coverage`) |
| Quantity | **[Documented]** "Outcome-signed fill quantity; negative values indicate the NO side" (`#/components/schemas/Fill/properties/quantity/description`), alongside a separate `side` enum. **[Unknown]** Unit and granularity (OQ-W4) |
| Price | **[Documented]** For the **realtime** `account_batch` fills only: "`price` on every item in `trades` and `fills` is the **YES-denominated** execution price" ([RT]). The REST [FILL] `price` has no description and no range (`#/components/schemas/Fill/properties/price`). **[Unknown]** the REST fill price denomination, whether it is always on the 0.005 grid, and what a null price means (OQ-W4, OQ-W7). Not inferred from the realtime statement |
| Order | "newest first" (`#/paths/~1tournaments~1{slug}~1portfolio~1fills/get/description`). No time-window parameter, so every walk starts at the newest fill |
| Pagination | Limit max 200 (`#/paths/~1tournaments~1{slug}~1portfolio~1fills/get/parameters/3`); projection coverage (1.5) |
| Errors | 400, 403, 404, 500, 503 ("Authoritative reporting is temporarily unavailable") |
| Observed / Code | Never captured; not implemented. `shared/schemas.py:246` defines a domain `Fill` with integer tick prices and a float quantity. That is an internal type, not wire evidence, and A2 does not convert into it (OQ-W7) |

### 2.5 `GET /tournaments/{slug}/portfolio/pnl` [TPNL]

| Aspect | Evidence |
| --- | --- |
| Binding | **[Documented]** Verified slug in the path. "All figures are scoped to the tournament's virtual balance and tournament valuation prices" (`#/paths/~1tournaments~1{slug}~1portfolio~1pnl/get/description`) |
| Fields | `period`, `periodStart`\|null, `periodEnd`, `periodPnl`\|null, `unrealizedPnl`, `totalAccountValue`, `totalHoldingsValue`, `totalCostBasis`, `roi`\|null, `sharpe`\|null, all required |
| Cash and value | `totalAccountValue` and `totalHoldingsValue` are numbers with no description (`#/paths/~1tournaments~1{slug}~1portfolio~1pnl/get/responses/200/content/application~1json/schema/properties/totalAccountValue`). **There is no cash field.** **[Unknown]** whether `totalAccountValue − totalHoldingsValue` equals `myBalance` or any cash figure; this note does not derive cash from it (OQ-W11, OQ-D1) |
| Period | `period` defaults to `quarter` (`#/paths/~1tournaments~1{slug}~1portfolio~1pnl/get/parameters/1`). "For `period=all`, ROI is computed relative to the tournament's initial virtual balance" |
| Errors | 400, 403, 404, **409** (as for positions: [MISSVAL], `#/paths/~1tournaments~1{slug}~1portfolio~1pnl/get/responses/409`), 500, 503 |
| Default counterpart | [PPNL] resolves to the default tournament; its 409 body is `#/components/schemas/PortfolioMissingTournamentValuation`. **Never used** |
| Observed / Code | Never captured; not implemented |

### 2.6 `GET /tournaments/{slug}/portfolio/settlements` [TSETTLE]

| Aspect | Evidence |
| --- | --- |
| Binding | **[Documented]** Verified slug in the path. "Payouts apply to the tournament virtual balance" (`#/paths/~1tournaments~1{slug}~1portfolio~1settlements/get/description`) |
| Fields | `data[]` (`#/components/schemas/TournamentSettlementEntry`: `id`, `type`\|null ∈ {win, loss, refund, correction}, `createdAt`\|null, `marketId`\|null, `marketTitle`\|null, `exchangeId`\|null, `settlementOption`\|null, `settlementOutcome`\|null, `shares`\|null, `avgEntryPrice`\|null, `costBasis`\|null, `payout`, `realizedPnl`\|null), `pagination`, `coverage` ([TCOV]), all required |
| Quantity | **[Documented] only on the default-scope row:** "Signed share count from metadata (positive = YES, negative = NO)" (`#/components/schemas/SettlementEntry/properties/shares/description`). The tournament row's `shares` has **no description** (`#/components/schemas/TournamentSettlementEntry/properties/shares`). **[Unknown]** for the tournament read; the default-scope description is not carried over (OQ-W5) |
| Cash | `payout`: "Cash credited to the tournament virtual balance (or 0 for losses)" (`#/components/schemas/TournamentSettlementEntry/properties/payout/description`) |
| Pagination | Limit max 200 (`#/paths/~1tournaments~1{slug}~1portfolio~1settlements/get/parameters/1`). **`coverage.complete` can be false** (1.5) |
| Errors | 400, 403, 404, 500 listed (`#/paths/~1tournaments~1{slug}~1portfolio~1settlements/get/responses`). No 503 is listed, but the global 503 applies (1.4) |
| Observed / Code | Never captured; not implemented |

### 2.7 Other tournament portfolio reads: [TTX] and [THIST]

- **[TTX] transactions.** A cursor-paginated "audit trail of the caller's trades and
  transactions", newest first, with economic-command coverage. Rows have `event_type` ∈
  {trade, settlement, deposit, fee, market_creation}, `amount`\|null ("Cash amount
  (non-trade events only)" on the default-scope row) and `transactionType`\|null. A
  `fee` event type exists ([PRD, B](../PRD.md#b-platform-behaviour): trading fees are
  unknown). **[Proposed]** Not in A2. It is the natural source for a later cash
  reconciliation, once the units of trades are settled (OQ-W4, OQ-W5).
- **[THIST] history.** The value series "from tournament snapshots plus a live
  tournament valuation". Each point has `cashValue`\|null and `holdingsValue`\|null, and
  sub-daily points may be `interpolated`
  (`#/components/schemas/TournamentPortfolioHistoryPoint/properties/cashValue`).
  - **[Unknown]** which point is the live one, and whether its `cashValue` equals
    `myBalance`.
  - **[Proposed]** Not used as a cash source. It is derived and possibly interpolated,
    and adopting it would be a valuation-policy choice (OQ-D1).
- **Not covered:** `GET /tournaments/{slug}/me/smart-score`, which is for monitoring
  only.

### 2.8 `GET /orders` [ORDERS] and `GET /orders/{id}` [ORDER1]

| Aspect | Evidence |
| --- | --- |
| Binding | **[Documented]** `tournamentId` query parameter: "Filter to orders placed in a specific accessible tournament. Omit to use the organization's default tournament ... or public global orders" (`#/paths/~1orders/get/parameters/5`). Rows carry no tournament id ([ORDER]). [ORDER1] takes its context from the order id |
| Fields | [ORDER]: `id`, `exchangeId`, `side` ∈ {yes, no}, `action` ∈ {buy, sell}, `quantity`, `priceLimit`\|null, `open`, `createdAt`, `expirationDate`\|null, all required (`#/components/schemas/Order/required`). **No idempotency key, no filled quantity, no terminal reason and no tournament id** |
| Quantity | **[Documented]** "Always positive. Use `side` to determine YES/NO direction. While `open` is true this is the executable remainder; a closed order reports its unfilled remainder plus everything it filled. Orders whose remainder was discarded before they closed — legacy market orders ... and orders shrunk to fit a decreased balance — therefore report the reduced size" (`#/components/schemas/Order/properties/quantity/description`). The original submitted size is not recoverable |
| Price | `priceLimit` has no description or range: **[Unknown]** denomination for NO orders (OQ-W6). **[Code]** kept exact and unbounded (`wire.py:262`) |
| Status | `status` defaults to `open`: "resting orders that have not expired" (`#/paths/~1orders/get/parameters/0/schema/description`). But the row's `open` field says "An order can have `open: true` yet be past its `expirationDate`" (`#/components/schemas/Order/properties/open/description`). **[Unknown]** whether a `status=open` walk can return such a row |
| Pagination | Limit max 200 (`#/paths/~1orders/get/parameters/3`). Projection coverage is optional (`#/paths/~1orders/get/responses/200/content/application~1json/schema/properties/coverage`) |
| Errors | [ORDERS]: 400, 500, 503. [ORDER1]: 400, 403 ("belongs to another user or is outside the API key's organization context"), 404, 500, 503 |
| Observed | One capture: `status=all`, `tournamentId` sent, one page `data: []`, `hasMore: false`, `nextCursor: null`, `coverage.complete: true`, integer `projectedThroughSequence`. **No order item and no cursor-following observed** ([fixtures README](../../tests/fixtures/sig/README.md)) |
| Code | `list_orders_page` always sends `tournamentId` (`rest.py:766`) and notes that the scope cannot be echo-checked (`rest.py:753`). `list_all_orders` returns a bare `list[Order]` and **drops each page's `coverage`** (`rest.py:783`-`807`), unlike the trade walk, which keeps it per page (`rest.py:209`-`225`). `OrdersPage.coverage` defaults to None and **accepts an explicit null** (`wire.py:282`), whereas the trade model rejects one (`wire.py:393`-`398`). [ORDER1] is not implemented |

### 2.9 `GET /orders/{id}/fills` [OFILLS]

| Aspect | Evidence |
| --- | --- |
| Binding | **[Documented]** Context from the order id. The body **echoes** `tournamentId`: "Tournament this order belongs to, or null for global orders" (`#/paths/~1orders~1{id}~1fills/get/responses/200/content/application~1json/schema/properties/tournamentId`) |
| Fields | `orderId`, `exchangeId`, `tournamentId`\|null, `data[]{ id, price`\|null`, quantity, side, filledAt }`, `pagination`, `coverage` (required here), `totalQuantityFilled`, `avgFillPrice`\|null, all required |
| Quantity and price | Row `quantity` is outcome-signed, as in [FILL]. `totalQuantityFilled` is "Exact outcome-signed filled quantity for the whole order, not only this page" (`#/paths/~1orders~1{id}~1fills/get/responses/200/content/application~1json/schema/properties/totalQuantityFilled/description`). `avgFillPrice` is "Exact average fill price for the whole order, or null before the first fill". Price denomination: **[Unknown]** (OQ-W4) |
| Errors | 400, 403, 404, 500, 503 |
| Observed / Code | Never captured; not implemented |
| Use | **[Proposed]** On demand (A2-B), as the only read that can confirm one order's tournament by echo |

### 2.10 `GET /portfolio/collateral` [COLL]

| Aspect | Evidence |
| --- | --- |
| Binding | **[Documented]** Optional `tournamentId` (`#/paths/~1portfolio~1collateral/get/parameters/0`, format uuid); rows echo `tournamentId`\|null, "Null for the global trading scope" (`#/components/schemas/PortfolioCollateral/properties/tournamentId`). "collateral only requires readable scope" ([INFO]) |
| Fields | `data[]{ profileId, tournamentId`\|null`, outstandingAdvance (≥ 0), componentId, coveredExchangeIds[], relationshipIds[], guaranteedPayoutFloor`\|null` (≥ 0), diagnostic`\|null` }`, all required. No pagination |
| Meaning | "component-level ALL collateral advances and guaranteed payout floors" (`#/paths/~1portfolio~1collateral/get/description`). `guaranteedPayoutFloor` is the "Minimum component payout when the engine can evaluate every allowed joint outcome; null when unavailable" (`#/components/schemas/PortfolioCollateral/properties/guaranteedPayoutFloor/description`) |
| Relevance | The PRD records no composite, multi-outcome or linked markets in this tournament, so ALL collateral never applies ([PRD, competition constraints](../PRD.md#competition-constraints)). That is a fact from the Oct 2 dump, not a guarantee |
| Errors | 400, 403, 404, 500, 503 |
| Use | **[Proposed]** Not in A2. Never a cash source (section 3.4). Optional real-platform check P6 |

### 2.11 Realtime `user:{profile_id}` (context only)

**[Documented]** The account channel's `account_batch` carries `fills`, `orderUpdates`,
`settlements`, `refunds` and `collateralChanges`, with `delivery.revision` and
`previousRevision` ([RT]). Realtime "is a **best-effort low-latency feed**, not the
financial source of truth" ([RT]), and polling `/portfolio/*` in a loop is discouraged
([INFO]). Its fill `price` is documented as YES-denominated. Whether REST fills use the
same denomination is not stated (2.4).

**[Proposed]** No account-channel work in A2 or A3. Without it, the only way A3 can see
a change between reads is to read again (5.4).

## 3. Cash and collateral

### 3.1 `/account` is never tournament cash

**[Proposed]** No A2 method reads `GET /account`, and no A3 field is filled from it.
The reasons:

- it has no `tournamentId` parameter and no scope echo (2.1);
- what it returns depends on key binding and the organization's default, neither of
  which is observable (1.1);
- a numerically equal `balance` and `myBalance` would still not show the same scope.

If Arnav ever reads it for diagnosis, the value is labelled default-context and stays
out of every account computation (OQ-W11).

### 3.2 `myBalance` as a candidate

`GET /tournaments/{slug}` is the only read that is explicitly addressed to the
configured tournament, echoes its identity and returns a balance. It is a **candidate**
source of tournament cash, not an established one.

| Question | Status |
| --- | --- |
| Belongs to the configured tournament? | **Partly evidenced.** The path names the slug and the body echoes slug and UUID, which the client checks (`rest.py:493`-`515`). What is *not* evidenced is that the value is the balance the trading engine admits orders against. The spec says "current or seed". `/account` hints at an "engine-authoritative when routed" balance, but for `/account` only (OQ-W11, OQ-W8) |
| Current, or a seed? | **[Documented]** It may be a seed: "current or seed virtual cash balance". **[Unknown]** For a pending member, whether the seed equals `initialBalance`. `/account` says a pending member receives "its initial virtual balance", but that is a statement about `/account` (2.1). And when exactly does the value stop being a seed: at the first order, or after the membership sync (`#/paths/~1orders/post/responses/503/description`)? |
| Excludes resting buy commitments? | **[Unknown]** (OQ-C3). The only related statement is that resting orders can be "shrunk to fit a decreased balance" ([ORDER] `quantity`). That would be consistent with resting orders *not* being fully reserved, but it does not say so, and this note does not infer it |
| Fresh at the instant served? | **[Unknown]** No as-of time and no projection checkpoint (1.6). **[Documented]** A 503 is returned when "The authoritative tournament balance is temporarily unavailable". The spec does not say whether a 200's value comes from that source, or how current it is |
| Units | The tournament's virtual cash, named by `currencyName` (observed `"SUSQies"`). Exact `Decimal`; fractional values allowed (the example is `847.5`) |
| Can be negative? | **[Unknown]**. Not constrained by the schema; kept as sent |

### 3.3 Fresh observation versus cached verification

**[Code]** `verify_tournament()` returns the cached `Tournament` from the client's most
recent successful tournament read, and reads only when nothing is cached
(`rest.py:474`-`491`). Its `myBalance` is the balance at the time of that read, which
may be hours old.

**[Proposed]** The rules:

1. A cash observation in A3 comes **only** from a `GET /tournaments/{slug}` read made
   for that snapshot (A2's `read_tournament_balance`, section 4.2). It is never taken
   from the verification cache, even when the cache holds a `Tournament` object.
2. Each observation keeps its own provenance (4.1): local start and finish times, the
   attempt count and the identity check result. The snapshot records whether the balance
   read was taken **before** or **after** the other reads, and re-reads it at the end
   (5.4).
3. Verification remains a one-time identity check with a permanent mismatch latch
   (`rest.py:507`-`515`). A fresh balance read also re-checks identity
   (`rest.py:480`). A mismatch there latches as well and fails the snapshot.

### 3.4 Collateral is not spendable cash

**[Proposed]** Neither `outstandingAdvance` nor `guaranteedPayoutFloor` (2.10) is used
as, added to or subtracted from cash. Their documented meanings:

- an advance is collateral the engine has advanced to a relationship component;
- a payout floor is a lower bound on a component's payout over joint outcomes.

Neither statement says the amount can be spent on a new order. The default-scope
transaction row lists `ALL_COLLATERAL_ADVANCE`, `ALL_COLLATERAL_REPAYMENT` and
`ALL_COLLATERAL_RELEASE` as transaction types
(`#/components/schemas/Transaction/properties/transactionType/description`). Advances
may therefore move cash balances, but the spec does not say how.

**Missing evidence:**

- whether this tournament has any ALL component for our account (expected: none);
- whether `myBalance` is net of outstanding advances;
- what an advance does to "insufficient balance" admission (`#/paths/~1orders/post/responses/400/description`).

None of this is needed while no linked markets exist. Check P6 (section 9.3) would
confirm the empty set.

### 3.5 What is missing before any cash figure can be trusted

| Missing evidence | Question | Who closes it |
| --- | --- | --- |
| That `myBalance` is the balance orders are admitted against, for this key and tournament | OQ-W11 | SIG answer (Q-S1) or a logged check by Arnav (P3) |
| Whether resting buys are already deducted from `myBalance` | OQ-C3 | P3 (tiny resting order far from the market) |
| Our current enrolment state, and the seed-to-current transition | OQ-W11 | P1 (read-only), P3 |
| Whether a fill or settlement is reflected in `myBalance` before, with or after positions | none yet; candidate A1-N3 | SIG answer (Q-S3) |
| Fees | PRD section B (fees) | First fill, P4 |

Until these close, A3 reports `myBalance` as an **unestablished candidate** (5.2), and
nothing downstream may treat it as uncommitted cash (G-1).

## 4. Proposed A2: raw tournament-scoped reads

### 4.1 Principles [Proposed]

- **R1. Read only.** GET endpoints only, through the existing `SigRestClient`
  machinery: one logical-read deadline, retries on 429, 503 and the listed transport
  failures only, the shared account `RateLimiter`, no redirects, and the key in the
  header only (`rest.py:30`-`56`, `rest.py:1024`-`1129`). No new transport and no write
  method.
- **R2. Tournament-bound or not at all.** Every A2 method uses a verified slug path or
  sends the configured `tournamentId`. No A2 method can produce a default-context read.
  Where SIG echoes scope, the echo is checked and a mismatch fails the read. A tournament
  identity mismatch also latches for the client's life, as today (`rest.py:507`-`515`).
- **R3. Exact values, no units.** New wire models follow `wire.py`: `Decimal` numbers
  exactly as sent, strict types, documented nullability, unknown fields ignored, and
  timestamps kept as strings. Ranges are applied only where documented. No price, cost
  or value field is bounded to [0, 1], because none is documented as YES-normalized on
  these REST reads (2.3, 2.4).
- **R4. No domain conversion.** A2 returns wire models only. Nothing converts to
  `shared/schemas.py` `Fill` or `Position` (`shared/schemas.py:246`,
  `shared/schemas.py:268`), to ticks or to milli, while OQ-W4 to OQ-W7 are open. No
  `shared/` change.
- **R5. Unresolved units travel with the data.** A static table in the engine (not in
  `shared/`) maps each numeric wire field to either its documenting pointer or the open
  PRD question. A3 copies the entries for fields present in a snapshot (5.3).
- **R6. Provenance per read and per page.** Every result carries a `ReadProvenance`:
  - route template and request path (both built from configuration);
  - the allowlisted parameters sent (`tournamentId`, `status`, `limit`, `period`,
    `type`, and whether a cursor was sent);
  - the scope-evidence kind (1.2);
  - attempt count and HTTP status;
  - `started_mono` and `finished_mono` (local monotonic), and `finished_utc` (local
    wall clock, display only);
  - page index;
  - `coverage` exactly as sent, or `omitted`.

  Provenance holds no response text beyond the documented coverage values.
- **R7. Documented outcomes are values, not lost errors.** A documented 409 for missing
  valuations becomes a typed result carrying the validated ids (4.3). Other statuses
  keep today's behaviour: 403 raises `SigAuthError` with its documented code preserved
  (`FORBIDDEN` and `INSUFFICIENT_SCOPES` stay distinguishable through `code`). 404 and
  others raise as now.
- **R8. Redaction unchanged.** Errors and logs carry no response values: not balances,
  quantities, ids, titles or cursors (`errors.py:3`-`13`). Result objects hold values
  for engine use only. Tests use synthetic fixtures. Recorded account bodies are never
  committed without Arnav's review and redaction, as the recorder rules already require
  (`engine/sig_client/RECORDER.md:240`-`247`).
- **R9. Walks are complete or explicitly not.** A cursor walk returns every page under
  `_collect`'s rules (`rest.py:965`). Where the spec documents that a page can be
  incomplete (economic-command coverage), the walk result is marked incomplete with the
  page index. It is never presented as complete, and never silently truncated.

### 4.2 Proposed methods

The **minimal set** is A2-A: the next slice, section 8. A2-B is listed so that A3's
shape is complete. Method names are proposals.

| Slice | Method | Endpoint | Result | Notes |
| --- | --- | --- | --- | --- |
| A2-A | `read_tournament_balance()` | [TOUR] | `TournamentBalanceRead { tournament: Tournament, provenance }` | Always a fresh read; re-checks identity; never served from `verify_tournament`'s cache (3.3) |
| A2-A | `read_tournament_positions()` | [TPOS] | `PositionsRead { rows, summary, provenance }` **or** `UnvaluedHoldingsRead { missing, provenance }` | 409 handling as in 4.3; one request, no pagination |
| A2-A | `read_open_orders()` | [ORDERS] `status=open` | `OrdersWalk { orders, pages: tuple[PageRecord] }` | Wraps `list_orders_page`. Keeps every page's `coverage` and provenance. Rejects an explicit `coverage: null` (the spec makes it optional, not nullable), matching `wire.py:393` |
| A2-B | `read_tournament_fills()` | [TFILLS] | `FillsWalk { fills, pages }` | Full newest-first walk with a page cap; duplicate fill id fails (`_collect`) |
| A2-B | `read_tournament_pnl(period="all")` | [TPNL] | `PnlRead` **or** `UnvaluedHoldingsRead` | Always sends `period` explicitly, never relying on the `quarter` default |
| A2-B | `read_tournament_settlements()` | [TSETTLE] | `SettlementsWalk { entries, pages, complete }` | `complete` is false if any page reported `coverage.complete: false` (R9) |
| A2-B | `read_order_fills(order_id)` | [OFILLS] | `OrderFillsWalk { order_id, echoed_tournament_id, fills, totals, pages }` | Checks the echoed `tournamentId` equals the configured UUID; a null or other value raises `SigContextMismatchError` |

**Not proposed:**

- [ACCT] (3.1);
- every default-scope `/portfolio/*` read;
- [PFILLS], because [TFILLS] covers it with the same slug binding as the other reads.
  Their equivalence is unknown, so A2 uses one and never mixes them;
- [TLIST], [TTX], [THIST], [COLL] and smart score.

### 4.3 The 409 "missing valuation" result

**[Proposed]** On a 409 from [TPOS] or [TPNL], A2 validates the body against [MISSVAL]:
`error.code == "CONFLICT"` and a schema-valid `details.missingValuations`. Each item's
`exchangeId` and `marketId` must be digit strings, as the client already requires of
ids (`rest.py:301`).

- **If valid:** A2 returns `UnvaluedHoldingsRead` holding the `(exchange_id,
  market_id)` pairs. `marketTitle` and `settlementOption` are dropped, since they are
  free text the engine does not need.
- **If invalid** (another code, or a malformed or empty list): A2 raises
  `SigConflictError` as today.

The pairs are result data and are never put in an error or log line (R8). This keeps
the documented signal "holdings exist without valuation prices" visible to A3 (5.5)
instead of collapsing it into a generic conflict.

### 4.4 Rate cost

Under the existing 80-read budget (`rate_limiter.py:35`):

- **A2-A** is 1 + 1 + ⌈open orders / 200⌉ reads, normally 3.
- **A2-B** adds 1 for P&L, ⌈fills / 200⌉ and ⌈settlements / 200⌉, plus one per order
  whose fills are read.

Fills and settlements have no time filter, so their walks grow with history. Each walk
keeps a page cap and fails closed above it. A later incremental "read until a known id"
scheme would need cursor stability under inserts, which is undocumented (A1-N2), so it
is not proposed.

## 5. Proposed A3: `AccountSnapshot`

### 5.1 What it is and is not [Proposed]

`AccountSnapshot` is an engine-internal, immutable record assembled from A2 results for
the configured tournament. It is not a `shared/` schema: readiness and account schemas
need Arnav's approval ([PRD, shared contracts](../PRD.md#shared-contracts)). It follows
the shape of `MarketReadiness`: a status, one human-readable reason per failing
condition with its question ids, local evaluation times, and a `does_not_cover` text
(`readiness.py:33`-`37`, `readiness.py:80`-`109`).

It **never claims**:

- **Atomicity.** Its reads are separate observations at different times (as in M1,
  principle E4).
- **Valuation consistency.** P&L and positions values are reported as sent, never
  reconciled into an equity figure (OQ-D1, OQ-D3).
- **Projection freshness.** Coverage checkpoints are kept, never compared across
  endpoints and never called fresh.
- **Restart or recovery readiness.** That needs the order journal (O2) and the
  readiness check (S1, G-3).
- **Market readiness.** That is `MarketReadiness`. The two are combined only by the
  engine readiness check (S1).

**There is no READY status.** The best possible status is `OBSERVED_UNRESOLVED`. This
mirrors M2-A, where no production market is ever ready while D-3 and D-4 are undecided
(`recovery.py:16`-`21`).

### 5.2 Fields

```text
AccountSnapshot
  tournament            configured slug + UUID
  status                UNAVAILABLE | INCOMPLETE | CONTRADICTED | OBSERVED_UNRESOLVED
  reasons               tuple[str]       one line per failing condition, with OQ / A1-N ids
  assembled_from_mono   first read's started_mono
  assembled_to_mono     last read's finished_mono
  evaluated_utc         local wall clock, display only
  reads                 tuple[ReadProvenance]   every read used, in order (4.1 R6)

  identity              VERIFIED_THIS_ASSEMBLY | MISMATCH
                        from the fresh balance read(s), never from the verification cache

  enrolment
    reported            pending | enrolled | not read      (isPendingEnrolment as sent)
    portfolio_access    member | not member (403 FORBIDDEN) | other error | not read
    state               PENDING | ENROLLED_REPORTED | CONTRADICTED | UNKNOWN

  cash
    value               Decimal as sent (myBalance) | None
    currency            currencyName as sent
    basis               SEED_WHILE_PENDING | REPORTED | UNAVAILABLE
    first_read, last_read   provenance of the opening and closing balance reads
    changed_during_assembly bool | None
    source_established  False   (OQ-W11; fixed until Arnav records evidence)
    excludes_resting_orders UNKNOWN (OQ-C3; fixed until answered)

  positions
    state               OBSERVED | UNVALUED | NOT_MEMBER | UNAVAILABLE | NOT_READ
    rows                raw wire rows as sent (empty unless OBSERVED)
    unvalued            tuple[(exchange_id, market_id)]   from the 409 (4.3)
    unresolved_fields   field -> OQ id (5.3)

  open_orders
    state               COMPLETE_WALK | UNAVAILABLE | NOT_READ
    orders              raw rows; pages: per-page coverage as sent
    scope_evidence      UUID_PARAM_SENT (no echo)
    unresolved_fields   priceLimit -> OQ-W6
    note                "Order rows carry no idempotency key or tournament id" (OQ-E1)

  fills                 (A2-B) COMPLETE_WALK | UNAVAILABLE | NOT_READ; raw rows; pages
  valuation             (A2-B) REPORTED_AS_SENT | MISSING_VALUATIONS | UNAVAILABLE | NOT_READ
    candidate_equity    totalAccountValue as sent, labelled "candidate, OQ-D1"; unused
  settlements           (A2-B) COMPLETE | INCOMPLETE_BY_SIG_BUDGET | UNAVAILABLE | NOT_READ

  findings              tuple[Finding{ check_id, kind: CONTRADICTION | UNEXPLAINED,
                                       components, question_ids }]
  vouches_for           tuple[str]   e.g. "every open-order page was read"
  cannot_vouch_for      tuple[str]   always includes the fixed list in 5.6
  does_not_cover        fixed text (5.6)
```

The status is the first that applies:

1. `UNAVAILABLE` if the identity check failed, or the balance, positions or open-orders
   read ended in an error. A positions 403 `FORBIDDEN` (recorded as `NOT_MEMBER`) and a
   documented 409 (recorded as `UNVALUED`) are outcomes, not errors;
2. `CONTRADICTED` if any finding is a contradiction;
3. `INCOMPLETE` if any read component is `NOT_READ`, `NOT_MEMBER`, `UNVALUED`,
   `INCOMPLETE_BY_SIG_BUDGET` or `MISSING_VALUATIONS`, or the cash changed during
   assembly;
4. otherwise `OBSERVED_UNRESOLVED`.

A pending enrolment therefore never looks like "no positions": positions are
`NOT_MEMBER` or `NOT_READ`, not an empty `OBSERVED` list (5.5).

### 5.3 Unresolved-value flags

Every numeric field that A3 exposes carries its status from the R5 table. The status is
never dropped when the value is passed on.

| Field | Documented | Unresolved |
| --- | --- | --- |
| `myBalance` | Tournament virtual cash; "current or seed" | Reservation (OQ-C3), source (OQ-W11), seed value while pending |
| Position `quantity` | Sign: positive YES, negative NO | Granularity; lots relation (OQ-W5, OQ-W8) |
| Position `avgCost`, `currentPrice`, `lots[].entryPrice` | `currentPrice` is the tournament valuation price | Denomination (OQ-W5); mark source (OQ-D1) |
| Position `marketValue`, `costBasis`, `unrealizedPnl`, `moneyEarned` | None on the tournament row | Units and signs for NO (OQ-W5) |
| Fill `quantity` | Outcome-signed, negative NO | Unit (OQ-W4) |
| Fill `price` | None on REST (YES only for realtime) | Denomination and grid (OQ-W4, OQ-W7) |
| Order `quantity` | Positive; open means remainder | None for open orders |
| Order `priceLimit` | None | Denomination (OQ-W6) |
| P&L `totalAccountValue` and the other P&L fields | Tournament-scoped | Meaning as equity (OQ-D1); behaviour with stale marks (OQ-D3) |
| Settlement `shares` | None on the tournament row | Sign and unit (OQ-W5) |
| Settlement `payout` | Cash credited to the tournament balance | Timing relative to `myBalance` (A1-N3) |

### 5.4 Observation times and the closing re-read

- **[Proposed]** A3 reads the balance **first and last**. Equal values set
  `changed_during_assembly = False`. That shows only that no change was observed in that
  one value at those two instants; it is **not** atomicity, and it says nothing about
  positions or orders between the reads. Different values set it to `True`, and the
  status becomes `INCOMPLETE` with the reason "balance changed during assembly; read
  again". A3 does not retry by itself; the caller decides when to assemble again.
- **[Proposed]** Open-order pages, fill pages and settlement pages each keep their
  `projectedThroughSequence` as sent. A3 does not compare them with one another, with
  the market topics' sequences, or with anything else. Engine sequence bounds "are
  provenance, not the topic gap counter" ([INFO]), and the meaning across endpoints is
  undocumented (A1-N1).
- **[Proposed]** A snapshot's age is reported as `now_mono − assembled_to_mono` by
  whoever consumes it. A3 sets no maximum age: that is policy for S1.

### 5.5 Consistency checks

Each check produces a finding with its question ids. Contradictions make the snapshot
`CONTRADICTED`; unexplained findings are reported but do not change the status.

| Id | Check | Kind | Basis |
| --- | --- | --- | --- |
| K1 | Every read in `reads` is tournament-bound (no default-context kind) | Contradiction (programming error) | 1.2; R2 |
| K2 | Fresh balance reads echo the configured slug and UUID | `UNAVAILABLE` on mismatch, latched | `rest.py:493` |
| K3 | `isPendingEnrolment = true`, but a portfolio read returned 200 with rows | Contradiction | [TOUR] description vs [TPOS] 403 "Not a member"; A1-N4 |
| K4 | `isPendingEnrolment = false`, but a portfolio read returned 403 `FORBIDDEN` | Contradiction | Same |
| K5 | `isPendingEnrolment = true` and `myBalance ≠ initialBalance` | Unexplained | "seed" is not documented to equal `initialBalance` on [TOUR] (3.2) |
| K6 | A position row has `currentPrice = null` and `quantity ≠ 0` | Contradiction | `#/components/schemas/TournamentPortfolioPosition/properties/currentPrice/description` |
| K7 | A position row's lot sides or lot quantities disagree with the sign or size of `quantity` | Unexplained | Lot semantics undocumented (OQ-W5, OQ-W8) |
| K8 | A fill's `quantity` sign disagrees with its `side` | Contradiction | `#/components/schemas/Fill/properties/quantity/description` |
| K9 | An order in a `status=open` walk has `open = false` | Contradiction | `#/paths/~1orders/get/parameters/0/schema/description` |
| K10 | An order in a `status=open` walk has `expirationDate` at or before the local clock | Unexplained | [ORDER] `open` description; local-clock skew unknown |
| K11 | Positions returned 409 while P&L returned 200, or the reverse | Unexplained | Different reads at different times; reported, not reconciled |
| K12 | An order-fills read echoes a `tournamentId` other than the configured UUID, or null | `SigContextMismatchError` in A2 | `#/paths/~1orders~1{id}~1fills/get/responses/200/content/application~1json/schema/properties/tournamentId` |
| K13 | The balance changed between the first and last read | `INCOMPLETE` (not a finding) | 5.4 |

**Not checked in A3**, because the units are unresolved: positions against fills,
fills against orders, cash against settlements or fills, and summary totals against row
sums. A3 lists each one under `cannot_vouch_for`, with OQ-W4, OQ-W5 and OQ-W8.

### 5.6 What it can and cannot vouch for

`vouches_for` holds only statements A3 checked, each of the form "this read completed
under these rules". For example:

- "the tournament identity was verified by a read made during this assembly";
- "every page of the open-orders walk was read, with no repeated cursor or duplicate
  id";
- "positions were reported without a valuation conflict".

`cannot_vouch_for` always includes:

- that `myBalance` is cash available for new orders (OQ-W11, OQ-C3);
- that the reads describe one instant (no atomicity);
- that any read reflects every engine event up to its time (projection freshness,
  A1-N1);
- the units or denomination of any price, cost or value field (OQ-W4 to OQ-W7);
- that positions, fills, orders and cash agree with one another;
- that no order or fill exists that the engine did not place (OQ-E1, OQ-E2; needs O2);
- anything that happened before this process started (restart recovery, G-3);
- market readiness of any market.

`does_not_cover` (fixed text) reads: "Account observations only. This snapshot is not a
readiness result. It says nothing about market readiness, unresolved idempotency keys,
risk, configuration, reference data or rate budget, and never authorizes trading on its
own."

### 5.7 Examples

1. **Pending enrolment.** `isPendingEnrolment = true`, `myBalance = 100000`, and
   positions returns 403 `FORBIDDEN`. The result: cash `SEED_WHILE_PENDING`,
   enrolment `PENDING`, positions `NOT_MEMBER`, status `INCOMPLETE`, and the reason
   "not enrolled: positions not readable; first order enrols (OQ-W11)". It is not an
   empty portfolio.
2. **Missing valuation.** Positions returns 409 with two `(exchange, market)` pairs.
   The result: positions `UNVALUED` with both pairs, valuation `MISSING_VALUATIONS` if
   P&L also returns 409, status `INCOMPLETE`, and the reasons cite OQ-D3. Nothing values
   the holdings at a default.
3. **Incomplete orders.** The open-orders walk hits its page cap, or SIG repeats a
   cursor. A2 raises (no partial list), and A3 sets open orders `UNAVAILABLE` and status
   `UNAVAILABLE`. A walk is never marked complete on the strength of `coverage.complete`
   alone: completeness is the walk reaching `hasMore: false`.
4. **Contradiction.** `isPendingEnrolment = false`, but fills returns 403 `FORBIDDEN`
   (K4): status `CONTRADICTED`, reasons cite A1-N4.
5. **Change during assembly.** The opening balance read gives `X` and the closing read
   gives `Y ≠ X`. The result is `INCOMPLETE` with `changed_during_assembly = True`. All
   other components are kept as observed, for logging only.

## 6. Mapping unresolved contracts to PRD questions

| Unresolved | PRD question | Gate |
| --- | --- | --- |
| Which read gives tournament cash; `/account` scope; seed while pending | OQ-W11 | G-1, G-3, G-7 |
| Whether `myBalance` excludes resting buys | OQ-C3 | G-1 |
| REST fill price denomination; fill quantity unit | OQ-W4 | G-3, G-7 |
| Position price, cost and value denomination; lots; tournament settlement `shares` sign | OQ-W5 | G-1, G-2, G-7 |
| `priceLimit` denomination | OQ-W6 | G-3, G-7 |
| Fills on the 0.005 grid; null fill price | OQ-W7 | G-7 |
| Engine routing ("engine-authoritative when routed"); netting and lots | OQ-W8 | G-1, G-7 |
| Equity from `totalAccountValue` or our own valuation | OQ-D1 | G-2 |
| The 409 for holdings without a valuation price | OQ-D3 | G-2 |
| Orders carry no idempotency key: matching an unknown-outcome submission | OQ-E1 (with OQ-W9, OQ-W10) | G-3 |
| Fills found on restart | OQ-E2 | G-3 |
| `fee` transaction type exists; whether fees apply | B, "Are there trading fees?" | none |
| Leaderboard valuation versus tournament P&L | B, leaderboard valuation | G-2 |

**Candidate additions** (not added to the PRD by this task; for Arnav to accept,
reword or reject):

- **A1-N1** (G-3): What does `projectedThroughSequence` on orders and fills guarantee
  relative to balance, positions and P&L, which carry no checkpoint? Can any two account
  reads be shown to reflect the same engine state?
- **A1-N2** (G-3): Are fills, orders and settlements cursors stable under concurrent
  inserts during a newest-first walk? Can a page with `coverage.complete: false` be
  continued?
- **A1-N3** (G-1, G-3): Are a fill, a settlement payout and the resulting `myBalance`
  change visible together, or can a read see one without the other?
- **A1-N4** (G-3): While enrolment is pending, do tournament portfolio reads return 403
  `FORBIDDEN` or an empty 200? Is `joinedAt` on [TLIST] the enrolment time?
- **A1-N5** (G-3): Can a `status=open` walk return an order whose `expirationDate` has
  passed, and does such an order still fill?

## 7. Offline acceptance-test matrix (future A2 and A3)

All tests run offline. They use `httpx.MockTransport` with synthetic, spec-derived
fixtures (labelled as such, as in `tests/fixtures/sig/README.md`), the injected clock,
and the shared limiter. None calls SIG.

| Id | Layer | Setup | Expected |
| --- | --- | --- | --- |
| T1 | A2 | `read_tournament_balance` twice | Two requests (never served from the verification cache); each result's provenance has distinct times; identity checked on both |
| T2 | A2 | Balance read returns another slug or UUID | `SigContextMismatchError`; latched for later reads |
| T3 | A2 | `myBalance` is `847.5`, `1e2`, a 20-digit decimal or negative | Kept as the exact `Decimal` sent; not rounded or rejected (no documented range) |
| T4 | A2 | `myBalance` or `isPendingEnrolment` missing, null or a string | `SigResponseError` naming the field location only |
| T5 | A2 | Every A2 method | Request path and params contain the configured slug or `tournamentId`; no method can be called without it; no `/account` or `/portfolio/*` default path is ever requested |
| T6 | A2 | Positions 200 with rows, fractional and negative quantities, null `currentPrice` on a zero row, extra unknown fields | Rows kept as sent; extras ignored; no conversion to `shared` types |
| T7 | A2 | Positions 409 with a valid `missingValuations` | `UnvaluedHoldingsRead` with the digit-string pairs; titles dropped; nothing logged |
| T8 | A2 | Positions 409 with another code, an empty list, a non-digit id or no `details` | `SigConflictError`; no partial result |
| T9 | A2 | Positions 403 `FORBIDDEN` versus 403 `INSUFFICIENT_SCOPES` | `SigAuthError`, with `code` preserved and distinguishable |
| T10 | A2 | Positions 503 with Retry-After, then 200 | Retried within one deadline; `Retry-After` respected; limiter shared |
| T11 | A2 | Settlements or transactions 503 (undocumented per endpoint, documented globally) | Retried the same way (1.4) |
| T12 | A2 | Open-orders walk over three pages, each with different `coverage` | Every page's coverage kept in order; orders complete |
| T13 | A2 | Orders page with `coverage: null` | `SigResponseError` (optional, not nullable) |
| T14 | A2 | Orders page with `coverage` omitted | Recorded as `omitted`; walk continues |
| T15 | A2 | Repeated cursor, duplicate order id, cursor on the last page, page cap reached | `SigPaginationError`; no partial list |
| T16 | A2 | Settlements page with `coverage.complete: false` | Walk result `complete = False` with the page index; never presented as complete |
| T17 | A2 | Order fills echo another `tournamentId`, or null | `SigContextMismatchError` |
| T18 | A2 | Any error carrying balance, ids or titles in `message` or `details` | None of that text in the exception or the logs (redaction test, as `test_sig_redaction.py`) |
| T19 | A2 | P&L read | `period=all` sent explicitly |
| T20 | A3 | Pending, seed balance, positions 403 | Example 5.7.1: `INCOMPLETE`, positions `NOT_MEMBER`, not an empty `OBSERVED` list |
| T21 | A3 | Positions 409 | Example 5.7.2: `UNVALUED` with ids; status `INCOMPLETE`; no value invented |
| T22 | A3 | Orders walk failed | `UNAVAILABLE`; no orders component claims completeness |
| T23 | A3 | K3, K4, K6, K8, K9 each injected | `CONTRADICTED` with the check id and question ids in `reasons` |
| T24 | A3 | K5, K7, K10, K11 each injected | Finding of kind `UNEXPLAINED`; status unchanged by it |
| T25 | A3 | First balance read `X`, last read `Y ≠ X` | `INCOMPLETE`, `changed_during_assembly = True` |
| T26 | A3 | First and last balance equal, all reads clean | `OBSERVED_UNRESOLVED`, never `READY`; the enum has no ready member |
| T27 | A3 | Any snapshot | `cash.source_established is False` and `excludes_resting_orders == UNKNOWN`; the 5.6 list is in `cannot_vouch_for`; `does_not_cover` present |
| T28 | A3 | Any snapshot | Coverage checkpoints present and never compared (no code path reads two endpoints' checkpoints together) |
| T29 | A3 | A `Tournament` taken from `verify_tournament`'s cache is offered as the cash source | Rejected: cash provenance must be a read inside this assembly |
| T30 | A3 | A snapshot built with a market `MarketReadiness` present | No field of either is derived from the other; account status has no effect on market status, and the reverse |
| T31 | Both | Static check | No A2/A3 module imports `shared.schemas` `Fill` or `Position`, or converts to ticks or milli |

## 8. Next bounded implementation slice: A2-A

**Scope** (engine and tests only):

1. `read_tournament_balance()`: a fresh [TOUR] read returning
   `TournamentBalanceRead` with provenance (T1 to T5).
2. `read_tournament_positions()`: [TPOS] with wire models for the position row, its lots
   and the summary, and the typed 409 result (T6 to T10).
3. `read_open_orders()`: an `OrdersWalk` with per-page coverage and provenance; an
   explicit `coverage: null` rejected (T12 to T15). The existing `list_all_orders` keeps
   its current signature, so `realtime.py` and the recorder are unaffected.
4. `ReadProvenance` and the R5 unit-status table, both engine-internal.

**Out of the slice:** fills, P&L, settlements, order fills (A2-B), `AccountSnapshot`
(A3), any `shared/` change, any account channel, any write, and any recorder change.

**Prerequisites:**

- Arnav's review of this note (the A2 dependency in the roadmap).
- No new external evidence. A2-A keeps every unresolved value exact and flagged, so it
  does not wait for OQ-W4 to OQ-W11. The real-platform checks in 9.3 make A2-A's
  fixtures realistic but do not block it.
- A decision that A2-A may add wire models to `engine/sig_client/wire.py` and methods to
  `SigRestClient`. Both are inside `engine/`, which Claude Code owns, but the read
  client's documented scope ("eight documented GET endpoints", `rest.py:3`) grows.

**Then:** A2-B, then A3. A3 depends on A2-B only for the fills, valuation and
settlements components. An A3 built on A2-A alone would report those as `NOT_READ` and
stay `INCOMPLETE`.

## 9. Remaining questions and checks

### 9.1 Decisions for Arnav (none is selected here)

- **D-A1.** Accept or change the A2/A3 shape, including the minimal A2-A set and that
  [TFILLS] (not [PFILLS]) is the fills read.
- **D-A2.** Whether A1-N1 to A1-N5 join PRD section W or E.
- **D-A3.** The cash source for G-1. This note only shows that `myBalance` is the sole
  candidate. Adopting it needs the evidence in 3.5, and the decision belongs in the PRD.
- Valuation policy (OQ-D1, OQ-D3), bootstrap and restart policy (OQ-E1, OQ-E2) and
  every risk limit stay with Arnav and are not touched by A2 or A3.

### 9.2 Questions for SIG

- **Q-S1** (OQ-W11): Is `myBalance` on `GET /tournaments/{slug}` the balance that order
  admission checks for that tournament, for an organization-bound and an unbound key?
- **Q-S2** (OQ-C3): Does `myBalance` exclude cash held by resting buy orders? If not,
  how is "insufficient balance" decided for a new order while others rest?
- **Q-S3** (A1-N1, A1-N3): Do balance, positions, P&L, orders and fills read one
  consistent state, or separate projections? What does `projectedThroughSequence`
  guarantee across them?
- **Q-S4** (OQ-W4, OQ-W5, OQ-W6): Are REST fill `price`, position `avgCost`,
  `currentPrice`, `lots[].entryPrice` and order `priceLimit` YES-denominated for NO
  rows? What are the units and signs of `marketValue`, `costBasis` and the tournament
  settlement `shares`?
- **Q-S5** (A1-N4): While enrolment is pending, what do tournament portfolio reads
  return, and what is the seed balance?
- **Q-S6** (A1-N2): Are account read cursors stable under concurrent writes, and can a
  `coverage.complete: false` page be continued?

### 9.3 Real-platform checks Arnav would run himself

None is run by an agent. Each is listed with its rate cost.

- **No writes:** P1 and P6 are read-only, and P2 needs no API call.
- **Writes:** P3 and P4 place orders, so they belong with the G-4 session Arnav already
  plans ([PRD, acceptance gates](../PRD.md#acceptance-gates)). P3 uses a tiny order
  priced far from the market. P4 deliberately fills a minimal quantity, which is a real
  trade and Arnav's decision.
- **Reads inside P3:** P5.
- **Enrolment:** if P1 shows pending enrolment, the first order in P3 or P4 also enrols
  the account ([OIN-T]).

Captured bodies are reviewed and redacted before anything is committed (R8).

| Id | Check | Kind | Closes or informs |
| --- | --- | --- | --- |
| P1 | One read each of [TOUR], [TPOS], [TFILLS] (one page), [TPNL] `period=all`, [TSETTLE] (one page) and [ORDERS] `status=open`, recording status codes, shapes, the presence of `coverage`, and the unredacted `isPendingEnrolment` (kept local) | Read-only, about 6 reads | Enrolment state; 403-vs-200 while pending (A1-N4); first observed positions and P&L shapes; fixtures for A2 |
| P2 | Record from the SIG website's API-key settings whether the key is organization-bound, if shown; no API call | No API call | Key binding (1.1); why `/account` is not used |
| P3 | `myBalance` before placing, while resting, and after cancelling one tiny resting buy far from the market (with P1's reads around it) | Writes (2), reads | OQ-C3; OQ-W11 (balance moves with tournament orders); seed-to-current transition if pending |
| P4 | One minimal NO buy that fills (marketable, so a real trade), then [TFILLS], [OFILLS], [TPOS], [ORDERS] `status=all` and [TOUR] | Writes (1), reads | OQ-W4, OQ-W5, OQ-W6, OQ-W7 for one example; fees (PRD section B); K8 and K6 in practice |
| P5 | Within P3: one [OFILLS] or [ORDER1] read for the placed order | Reads | Echoed `tournamentId` (K12); order-row fields for a real order |
| P6 | One `GET /portfolio/collateral?tournamentId=<configured>` read | Read-only, 1 read | That no ALL collateral exists for our account (3.4) |

## 10. References

- [PRD](../PRD.md): A1 to A3 roadmap rows, REQ-11, G-1, G-2, G-3, G-7, open
  questions C, D, E and W.
- [M1 design note](market-data-recovery.md): evidence labels, principles E3 and E4, and
  the `MarketReadiness` shape.
- [`docs/api/openapi.json`](../api/openapi.json): all pointers above.
- [Fixtures README](../../tests/fixtures/sig/README.md): what is recorded and redacted.
- Code on `main` `3a7fc15`: `engine/sig_client/rest.py`, `engine/sig_client/wire.py`,
  `engine/sig_client/errors.py`, `engine/sig_client/rate_limiter.py`,
  `engine/sig_client/readiness.py`, `engine/sig_client/recovery.py`, `engine/config.py`,
  `shared/schemas.py`.
