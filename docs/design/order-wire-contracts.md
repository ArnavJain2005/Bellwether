# Order-path wire contracts: evidence, gates and checks (O1)

Oct 28, 2026 · Roadmap track O1 · Input to gate G-7, and to G-1 to G-6 ·
Status: **draft for Arnav's review** · Written against `main` at `efdb5a5`, rebased onto
`66bab31`

This note records, field by field, what [`docs/api/openapi.json`](../api/openapi.json)
and the committed recorded fixtures establish about the SIG wire contracts that the
order path would depend on ([PRD, section W](../PRD.md#w-api-wire-contract), OQ-W1 to
OQ-W11). It maps what is left to gates G-1 to G-7 and lists the real-platform checks that
only Arnav can run.

It is documentation only. It changes no code, no `shared/` schema, no risk setting, no
PRD question status and no gate status. It answers no question by inference and selects
no policy. Every check in section 3 is a proposal: none has been run.

**Bottom line.**

- **No account-side order evidence exists.** The repository holds no recorded order
  placement, cancellation, order item, account fill, per-order fill, position, P&L,
  settlement, transaction or `/account` response. The account-related recorded bodies are
  the tournament reads, whose `myBalance` and `isPendingEnrolment` are redacted
  placeholders, and one empty `status=all` orders page. Every account-unit question below
  therefore rests on documentation or is unknown.
- **Some things are documented:**
  - limit prices must be on the 0.005 tick in [0.005, 0.995], and off-tick values are
    rejected, not rounded;
  - market and book prices are YES-normalised;
  - fill and position *quantities* are signed: positive YES, negative NO;
  - the 502 `ORDER_STATUS_UNKNOWN`, 409 `REQUEST_IN_FLIGHT`, batch 503 and cancel-all
    503 retry rules are explicit.
- **Three documented statements bear on the NO-order request price (OQ-W3):**
  - placement responses report a "Canonical engine limit price in side-relative terms";
  - sell canonicalisation is written `sell yes q@p ≡ buy no q@(1−p)`;
  - multi-leg relationship validation uses the "YES-complement price" of NO limit prices.

  None of them is a statement about `OrderInput.price` for single or batch placement.
  Whether they are enough is Arnav's decision (D-1, section 4).
- **Undocumented and unobserved:**
  - the denomination of REST fill `price`, `avgFillPrice`, the placement `fillPrice` and
    `totalCost`, `Order.priceLimit`, and position `avgCost`, `currentPrice`,
    `marketValue`, `costBasis` and lot prices for NO holdings;
  - the granularity of fill and position quantities, and how a sell fill is signed (a
    fill row has no `action`);
  - how long SIG keeps an idempotency key, and the outcome behind a single-order 503, a
    "duplicate command" 409 or a timeout;
  - which scopes are "engine-routed";
  - whether `myBalance` is the cash orders are admitted against and whether it excludes
    resting orders;
  - whether an expired order can still fill.
- **Recorded public data shows every sampled SIG price on the 0.005 grid,** with at most
  three decimals (prices, books, the market list and the trade tape, Oct 9 to Oct 11).
  That is a bounded observation, not a guarantee, and it is not evidence about our fills.
- **No gate has evidence.** Nothing here passes G-1 to G-7. Section 2 says what each gate
  still needs; section 4 lists Arnav's decisions in dependency order.

## How to read this note

Every evidence row carries exactly one status:

| Status | Meaning |
| --- | --- |
| **documented** | The OpenAPI file explicitly says it, cited by a full JSON pointer. It is not necessarily true of this tournament or this key, and has not been observed. |
| **observed** | A named field in a committed, genuinely recorded response under `tests/fixtures/sig/recorded/`, with its capture, label and redaction status. One observation at one time; not a contract. |
| **proposed decision** | A choice put to Arnav, or a fail-closed behaviour proposed for later code. Not approved and not implemented. |
| **unknown** | Not settled by the spec or by recorded evidence, after checking the paths and schemas named in the row. Never resolved by inference here. |

"No statement found" means the named paths and schemas were read and none of them says
it. Where a documented statement and a bounded observation address different aspects of
one question, they are separate rows.

Code is cited only to show what depends on an answer, never as evidence of SIG
behaviour. Pydantic types, the internal tick and milli units, docstrings, field names and
OpenAPI `example` values are not evidence.

### Evidence base at `efdb5a5`

- **`main`.** The work started at `efdb5a57cf22ec99c4fe69f228e341fe6dde52bc` (PR #36,
  A3), the last known tip. While it was in progress, `main` moved to `66bab31` (PR #37,
  M2-B1: duplicate-batch detectors in the realtime feed). That change touches market-data
  recovery only: `engine/sig_client/realtime.py`, `realtime_topic.py`, `recovery.py`, their
  tests and `docs/design/market-data-recovery.md`. It adds no order-path, account or
  wire-contract evidence, and nothing below depends on it. Since the PRD's dated roadmap
  status (Oct 12, `2e3e464`), these have merged:
  - the A1 design note (#29);
  - the A2 account reads (#30, #33): `engine/sig_client/account_reads.py` and the read
    methods of `SigRestClient`;
  - the A3 `AccountSnapshot` (#36): `engine/account/snapshot.py`;
  - the M1 design note (#27), M2-A (#28) and M2-B1 (#37);
  - venue-market data work (#31, #34, #35).
- **What the engine does not have.** The only non-GET request it sends is
  `POST /realtime/token` (`engine/sig_client/realtime_token.py`). There is no write
  client, idempotency journal, `risk.check`, cancellation path, dry-run order path or
  `config/risk.yaml`. A2 keeps unresolved values as exact wire `Decimal`s and flags them
  in its unit-status table (`numeric_field_status` in `account_reads.py`). A3 has no
  ready status and never vouches for tournament cash, valuation or atomicity.
- **Recorded captures** (all read-only, run by Arnav with
  `engine/sig_client/record_fixtures.py`; see the
  [fixtures README](../../tests/fixtures/sig/README.md)):

  | Run | Captured (UTC) | Bodies used here |
  | --- | --- | --- |
  | `20261009T122244Z-fa008a` | Oct 9, 12:22 | tournament (redacted), markets page 1 (10 markets; redacted), prices for exchange 947, exchange book 947, market book 258 (depth 5; unmodified) |
  | `20261009T190027Z-fb88c9` | Oct 9, 19:00 | tournament (redacted), all 237 markets in 3 pages (redacted), prices, both books (unmodified), orders `status=all` page 1 (unmodified) |
  | `20261011T211201Z-12ab20` | Oct 11, 21:12 | tournament (redacted), market detail 258 (redacted), trade tape for exchange 947, 3 of at least 4 pages (unmodified) |

  In the redacted market bodies only `creator.id`, `creator.username` and one segment of
  `thumbnailUrl` were replaced (each body's `fixtures.json` entry lists the paths).
  Their price fields are as SIG sent them.
- **Never evidence here:**
  - the synthetic root-level fixtures `tests/fixtures/sig/*.json`, which are
    spec-derived;
  - the placeholders `$.myBalance` = `12345.67` and `$.isPendingEnrolment` = `true` in
    every recorded tournament body;
  - the public trade tape, as evidence about our own fills.
- **Never recorded:** any write response; any order item; account fills; per-order
  fills; positions; P&L; settlements; transactions; collateral; `/account`; any realtime
  message.

### OpenAPI citations used below

All pointers are into [`docs/api/openapi.json`](../api/openapi.json). Aliases are used in
the tables; field-level pointers are written out where a row depends on one field.

| Alias | JSON pointer | Content |
| --- | --- | --- |
| [INFO] | `#/info/description` | Error table, 502 note, Quickstart, pagination and coverage, Realtime overview, bot workflows, rate limits and retries, tick rule |
| [POST] | `#/paths/~1orders/post/description` | Single placement: idempotency, tick rule, default scope, netting, sell canonicalisation, self-trade prevention |
| [POST-200] | `#/paths/~1orders/post/responses/200/content/application~1json/schema` | Placement response: `orderId`, `open`, `remainingQuantity`, canonical `action`/`side`/`price`/`quantity`/`terminalReasonCode`, `quantityTraded`, `totalCost`, `fillPrice`, `all` |
| [POST-409], [POST-502], [POST-503], [POST-400] | `#/paths/~1orders/post/responses/409/description` (and `502`, `503`, `400`) | Placement error cases |
| [OIN] | `#/components/schemas/OrderInput` | Order fields shared by single, batch and multi-leg placement |
| [SOI-KEY] | `#/components/schemas/SingleOrderInput/allOf/1/properties/idempotencyKey/description` | Required key; replay and 409 rule |
| [BATCH] | `#/paths/~1orders~1batch/post/description` | Batch placement, completion and retry rules |
| [BATCH-502], [BATCH-503] | `#/paths/~1orders~1batch/post/responses/502/description` (and `503`) | Batch ambiguous outcomes |
| [BOI] | `#/components/schemas/BatchOrderInput` | Batch body: one key, 1 to 50 orders |
| [ML] | `#/paths/~1orders~1multi-leg/post/description` | Atomic multi-leg placement, YES-terms validation, resume after response loss |
| [MLD] | `#/components/schemas/MultiLegOrderData` | Multi-leg leg result, canonical fields |
| [ORDER] | `#/components/schemas/Order` | Order row: `quantity`, `priceLimit`, `open`, `expirationDate` |
| [ORDERS] | `#/paths/~1orders/get` | Order list: `status` filter, `tournamentId`, optional `coverage` |
| [ORDER1] | `#/paths/~1orders~1{id}/get/description` | One order; context from the order id |
| [DEL] | `#/paths/~1orders~1{id}/delete` | Cancel one order; echoes `tournamentId` |
| [CXL] | `#/paths/~1orders~1cancel-all/post` | Cancel-all scoping, 200 and 503 semantics |
| [CXLP] | `#/components/schemas/CancelAllPausedError/properties/error/properties/details/properties/cancelled/description` | Progress count on a paused cancel-all |
| [OSU] | `#/components/schemas/OrderStatusUnknownError` | The 502 body |
| [OFILLS] | `#/paths/~1orders~1{id}~1fills/get/responses/200/content/application~1json/schema` | One order's fills; echoes `tournamentId`; lifetime totals |
| [FILL] | `#/components/schemas/Fill` | REST fill row (tournament and default fills reads) |
| [TFILLS] | `#/paths/~1tournaments~1{slug}~1portfolio~1fills/get/description` | Tournament fills, newest first |
| [TPOS] | `#/paths/~1tournaments~1{slug}~1portfolio~1positions/get/description` | Tournament positions; FIFO lots; 409 |
| [TPOS-ROW] | `#/components/schemas/TournamentPortfolioPosition` | Tournament position row |
| [POS] | `#/components/schemas/Position` | Default-scope position row (for comparison only) |
| [TSET] | `#/components/schemas/TournamentSettlementEntry` | Tournament settlement row |
| [SET] | `#/components/schemas/SettlementEntry` | Default-scope settlement row (for comparison only) |
| [TPNL] | `#/paths/~1tournaments~1{slug}~1portfolio~1pnl/get/description` | Tournament account value and P&L; 409 |
| [TOUR] | `#/paths/~1tournaments~1{slug}/get` | Tournament metadata, `myBalance`, `isPendingEnrolment` |
| [ACCT] | `#/paths/~1account/get/description` | Default-context profile and balance |
| [PRICE] | `#/paths/~1exchanges~1{id}~1price/get/description` | One exchange's quote |
| [PRICES] | `#/paths/~1exchanges~1prices/get/description` | Bulk quotes |
| [XBOOK] | `#/paths/~1exchanges~1{id}~1orderbook/get/description` | Exchange book |
| [MBOOK] | `#/paths/~1markets~1{id}~1orderbook/get/description` | Market book |
| [TRADE] | `#/components/schemas/ExchangeTrade` | Public trade-tape row |
| [RT] | `#/paths/~1realtime~1token/post/description` | Realtime channels, books, `account_batch`, expiry |
| [AEE] | `#/components/schemas/AllExecutionEconomics` | ALL collateral economics of an execution |

### Recorded fixtures cited below

Paths are relative to `tests/fixtures/sig/recorded/`. Labels are those in each run's
`fixtures.json`.

| Alias | Bodies | Label |
| --- | --- | --- |
| [F-PRICES] | `20261009T122244Z-fa008a/bodies/03-exchange-prices-a1.json`, `20261009T190027Z-fb88c9/bodies/05-exchange-prices-a1.json` | recorded response, unmodified |
| [F-XBOOK] | `20261009T122244Z-fa008a/bodies/04-exchange-orderbook-947-a1.json`, `20261009T190027Z-fb88c9/bodies/06-exchange-orderbook-947-a1.json` | recorded response, unmodified |
| [F-MBOOK] | `20261009T122244Z-fa008a/bodies/05-market-orderbook-258-a1.json`, `20261009T190027Z-fb88c9/bodies/07-market-orderbook-258-a1.json` | recorded response, unmodified |
| [F-MKTS] | `20261009T190027Z-fb88c9/bodies/02-tournament-markets-p1-a1.json`, `03-tournament-markets-p2-a1.json`, `04-tournament-markets-p3-a1.json` | derived from recorded response, redacted (creator and thumbnail paths only) |
| [F-DETAIL] | `20261011T211201Z-12ab20/bodies/06-market-detail-258-a1.json` | derived from recorded response, redacted (creator and thumbnail paths only) |
| [F-TAPE] | `20261011T211201Z-12ab20/bodies/07-exchange-trades-947-p1-a1.json`, `08-exchange-trades-947-p2-a1.json`, `09-exchange-trades-947-p3-a1.json` | recorded response, unmodified |
| [F-ORDERS] | `20261009T190027Z-fb88c9/bodies/08-orders-p1-a1.json` | recorded response, unmodified |
| [F-TOUR] | `bodies/01-tournament-a1.json` in all three runs | derived from recorded response, redacted (`$.myBalance`, `$.isPendingEnrolment`) |

## 1. Evidence matrix: OQ-W1 to OQ-W11

Each question opens with the gates the PRD assigns it and the downstream code or
calculation that needs the answer. Row-specific dependencies are stated in the row.
"Fail-closed" says what later code must do until the row is resolved. It is a proposed
decision unless it restates an existing hard rule.

### OQ-W1: quote precision

**Gates:** G-7. **Needed by:**
- `price_to_ticks` and `price_to_milli` in `shared/schemas.py`, which refuse values off
  the 0.005 and 0.001 grids;
- `Quote.bid` and `Quote.ask` (integer milli);
- fair value v1 (R3);
- the executable-rounding input (OQ-W2);
- book handling in `engine/sig_client/realtime_wire.py`.

| Row | Claim or field | Status | Evidence | Remaining question; smallest resolver | Fail-closed until then |
| --- | --- | --- | --- | --- | --- |
| W1-a | SIG REST quote denomination: `latestPrice`, `bestBid`, `bestAsk`, `spread`, book level `price` | documented | [PRICE]: "All prices are YES-normalized (0–1 probability)". [XBOOK]: "Prices are YES-normalized (0–1)". [MBOOK]: "Returns YES-perspective orderbooks". [PRICES]: "Same per-exchange shape as `/exchanges/{id}/price`". `latestPrice`: "Latest YES trade price in the requested context ... never falls back to the global price" (`#/paths/~1exchanges~1{id}~1price/get/responses/200/content/application~1json/schema/properties/latestPrice/description`) | None for denomination | Not needed |
| W1-b | SIG REST quote precision and grid | unknown | No statement found. Level `price` (`#/components/schemas/ExchangeOrderbookLevel/properties/price`, `#/components/schemas/OrderbookLevel/properties/price`) and `#/components/schemas/BulkExchangePrice/properties/bestBid` are bare `number`s with no `multipleOf`. The tick rule in [INFO] and `#/components/schemas/OrderInput/properties/price/description` applies to "fresh limit-order prices", not to quotes | Are REST quotes always on the 0.005 grid, and with what maximum precision? A SIG statement (Q-S7, section 3.2) | Keep the exact `Decimal`. An off-grid SIG price is never rounded; the conversion fails for that value (as `price_to_ticks` does) and the market gets no executable price from it |
| W1-c | Bounded observation of SIG REST prices | observed | Every price field in [F-PRICES] (`$.data[*].latestPrice`, `bestBid`, `bestAsk`, `spread`), [F-XBOOK] (`$.bids[*].price`, `$.asks[*].price`, `$.bestBid`, `$.bestAsk`, `$.spread`), [F-MBOOK] (`$.exchanges[*].bids[*].price`, `$.exchanges[*].asks[*].price`, and the same under `$.contexts[*].orderbook`), [F-MKTS] (`$.data[*].exchanges[*].latestPrice`, 237 values, 0.035 to 0.965), [F-DETAIL] (`$.exchanges[*].latestPrice`) and [F-TAPE] (`$.data[*].price`, 150 values) is a multiple of 0.005 with at most three decimals. Book quantities and tape `size` are integers | Limits: books and quotes for one exchange (947) at depth 5, at three moments on Oct 9; the market list at one moment; no realtime message; nothing after Oct 11. Re-check with R7 (section 3.3) | As W1-b |
| W1-d | SIG realtime price precision | documented (shape); unknown (precision) | [RT]: books use "the same YES-normalized `{ price, quantity }` objects as the REST orderbooks"; `price` on `trades` and `fills` items "is the **YES-denominated** execution price in `[0, 1]`". No precision statement. No realtime message is recorded | Precision and grid of pushed books and trades. A recorded realtime batch (M3 work) or Q-S7 | As W1-b |
| W1-e | Polymarket and Kalshi quote precision | unknown | No committed evidence. The `shared/schemas.py` module docstring says Polymarket moves in 0.001 steps; that is an internal assumption, not evidence. `data/venue_markets/` records market identity, not quotes | Each venue's published per-market tick size and the precision of its quotes. Public read-only data captured under R2 (not SIG, and not part of O1's live checks) | A venue quote not representable in milli is dropped from fair value, never rounded (`price_to_milli` already refuses it). Not used by the order path directly |

### OQ-W2: executable rounding

**Gates:** G-6, G-7. **Needed by:** the future O2 conversion from a fair value
(`FairValue.value`, float milli, possibly off grid) or a target price into
`OrderIntent.price` (`OrderTick`, 1 to 199); the edge thresholds of any strategy.

| Row | Claim or field | Status | Evidence | Remaining question; smallest resolver | Fail-closed until then |
| --- | --- | --- | --- | --- | --- |
| W2-a | SIG does not round request prices | documented | "Fresh limit-order prices must be on the `0.005` tick between `0.005` and `0.995` ... Off-tick values return `400 VALIDATION_ERROR`" ([INFO]); the same in `#/components/schemas/OrderInput/properties/price/description`, [POST], [BATCH] and [ML]. Batch: "All orders are validated upfront — if any fail validation the entire request is rejected with 400 before any orders are placed" ([BATCH]) | None: the client must send an on-tick price | Never send a price not exactly `k/200` for an integer `k` from 1 to 199 |
| W2-b | Market-order encodings | documented | "`1` with `action: "buy"` and `0` with `action: "sell"` mean a market order; omit `price` for a market order" (`#/components/schemas/OrderInput/properties/price/description`). The schema allows 0 to 1 and `price` is not required (`#/components/schemas/OrderInput/required`) | None | Hard rule 4: never send 0, 1 or no price. `OrderTick` already excludes 0 and 200 |
| W2-c | Rounding direction for YES and NO buys and sells | proposed decision | No SIG statement exists or is needed: this is Bellwether's policy. Facts a policy must respect: (1) the tick set is closed under the YES complement (`k/200` ↔ `(200−k)/200`), so a rule written in YES ticks maps exactly to side-relative ticks whatever OQ-W3's answer; (2) rounding a YES price down is less aggressive for a YES buy, while for a NO buy (NO price `1 − YES`) rounding the YES price up is less aggressive; (3) a target that rounds outside [0.005, 0.995] has no limit order | Arnav chooses the direction per side and action (less aggressive, more aggressive, or refuse any off-grid target), and whether edge thresholds apply before or after rounding (D-6) | No conversion from an off-grid target. Dry run may log the target and both neighbouring ticks without choosing one |
| W2-d | Execution price relative to the limit | unknown | No statement found that a limit order never fills worse than its limit, or whether it fills at the resting order's price (price improvement). A market order "fills at best available price" ([POST]). `fillPrice` and `totalCost` have examples only ([POST-200]) | Is every fill at or better than the order's limit, on the order's own side? Q-S9, or X5 (one deliberate fill) | Commitment counts a resting or unknown buy at its limit times its quantity, and only as a bound if Arnav accepts that a limit is never exceeded (C1) |

### OQ-W3: NO-order request price

**Gates:** G-6, G-7. **Needed by:**
- the future O2 conversion of `OrderIntent.price` (internal YES ticks for both sides,
  `shared/schemas.py`) into the request `price` of single, batch and multi-leg placements,
  which all use [OIN];
- the commitment of our own unknown submissions (C1), which is computed from what we sent.

If the wire price is side-relative, a NO order is sent at `(200 − ticks)/200`; if it is
YES-denominated, at `ticks/200`. The two differ for every NO order except at 0.5, so a
wrong choice sends a materially different order, possibly a marketable one.

| Row | Claim or field | Status | Evidence | Remaining question; smallest resolver | Fail-closed until then |
| --- | --- | --- | --- | --- | --- |
| W3-a | `OrderInput.price` itself | unknown | "Limit price in [0, 1]. Limit prices must be on the 0.005 tick ..." (`#/components/schemas/OrderInput/properties/price/description`). No statement about `side`. The batch example has a NO buy at `0.3` (`#/components/schemas/BatchOrderInput/example`), which is an example, not evidence | Does a `side: "no"` request carry the NO price? Q-S8, or X2 (section 3.4) | No NO order is sent. O2 may model NO conversion only as an unresolved conversion that refuses to produce a wire price |
| W3-b | Canonical price in placement responses | documented | `price`: "Canonical engine limit price in side-relative terms. Present for engine-routed orders" (`#/paths/~1orders/post/responses/200/content/application~1json/schema/properties/price/description`). The same for multi-leg legs (`#/components/schemas/MultiLegOrderData/properties/price/description`). Batch items: "Freshly executed engine-routed items report the canonical `action`/`side`/`price`/`quantity`/`terminalReasonCode` as placed" ([BATCH]) | Says how the *response* states a placed NO order; not how the *request* was read. Also depends on OQ-W8 (only "engine-routed" orders carry it) | Proposed for O2: when the canonical fields are present, compare them with what was sent; any difference in `side`, `action`, `price` or `quantity` pauses the engine as a local/SIG mismatch |
| W3-c | Sell canonicalisation formula | documented | "a `SELL` that is flat, opposite the held side, or larger than the held quantity is canonicalized by the engine into its complement `BUY` (`sell yes q@p ≡ buy no q@(1−p)`)" ([POST]) | Writes the canonical NO buy's price as `1−p`, which is side-relative. It describes the canonical order, not the request field | As W3-a |
| W3-d | Multi-leg relationship validation | documented | "Price validation evaluates every leg in YES terms: YES limit prices are unchanged, NO limit prices use their YES-complement price" ([ML]; also `#/paths/~1orders~1multi-leg/post/requestBody/content/application~1json/schema/properties/relationshipConstraint/description`) | The most direct statement: a NO leg's limit price is not a YES price. Its context is multi-leg relationship validation, which we do not use (no ALL relationships) | As W3-a |
| W3-e | Observation | unknown | No order has been placed or recorded | X2 would show one order's canonical price, `priceLimit` and book level | As W3-a |
| W3-f | Whether W3-b to W3-d answer OQ-W3 | proposed decision | Three consistent documented statements, none on the request field of single or batch placement | Arnav decides whether these citations suffice for G-7, or require Q-S8 and/or X2 first (D-1) | As W3-a |

The PRD's internal unit is unaffected: `OrderIntent.price` stays YES-denominated ticks
whatever the answer. Only the boundary conversion depends on it.

### OQ-W4: REST fill units

**Gates:** G-3, G-7. **Needed by:**
- A2's `FillsWalk` and `OrderFillsWalk`, which keep these values exact and flagged
  (`numeric_field_status` entries for the fill rows: OQ-W4, OQ-W7);
- the internal `Fill` in `shared/schemas.py` (`price: Tick | None`, `quantity: float`),
  into which nothing converts yet;
- restart reconciliation (S1) and fills found on restart (OQ-E2);
- commitment of filled positions (C1); fees (PRD B).

| Row | Claim or field | Status | Evidence | Remaining question; smallest resolver | Fail-closed until then |
| --- | --- | --- | --- | --- | --- |
| W4-a | Fill `quantity` sign | documented | "Outcome-signed fill quantity; negative values indicate the NO side" (`#/components/schemas/Fill/properties/quantity/description`); the same on per-order fill rows (`#/paths/~1orders~1{id}~1fills/get/responses/200/content/application~1json/schema/properties/data/items/properties/quantity/description`) and `totalQuantityFilled` ("negative values indicate the NO side") | The sign follows the outcome, not the action. A fill row has no `action` field ([FILL] properties), so a YES buy and a YES sell both show positive quantity unless SIG signs sells otherwise. How is a fill that *reduces* a position signed? Q-S9; R4 if a sell fill exists; X6 | A fill is never used to change a held quantity without its order's `action` (from `GET /orders/{id}`) and a confirmed rule. Held quantity comes from positions, not from summed fills |
| W4-b | Fill `quantity` unit and granularity | unknown | `number`, no unit (`#/components/schemas/Fill/properties/quantity`). Order input is an integer: "the trading engine holds quantities as a signed 32-bit integer" (`#/components/schemas/OrderInput/properties/quantity/description`), a statement about order input only. Public tape `size` is an integer, "absolute value of the underlying signed quantity" (`#/components/schemas/ExchangeTrade/properties/size/description`), about public trades only | Can an account fill be fractional? Q-S9; R4 if fills exist | Keep exact; a non-integer fill quantity is flagged unexplained, never truncated |
| W4-c | Fill `price` denomination | unknown | No description on `#/components/schemas/Fill/properties/price` or on the per-order fill row price. Two related documented statements are about other feeds: realtime account fills are "**YES-denominated**" ([RT]); public tape `price` is the "YES-normalized fill price" (`#/components/schemas/ExchangeTrade/properties/price/description`). Neither is stated to apply to REST account fills | Is the REST fill price YES-denominated for NO fills? Q-S9; R4 (a NO fill, if any exists); X5 | Keep exact. No fill price is converted to ticks or used in cost, P&L or commitment |
| W4-d | Fill `price` null | unknown | Nullable (`#/components/schemas/Fill/properties/price`), meaning not documented | When is it null? Q-S9 | A null price is a gap: the fill's cost is unknown and its position stays unvalued |
| W4-e | Order-level fill totals: `avgFillPrice`, `totalQuantityFilled`; placement `fillPrice`, `totalCost`, `quantityTraded` | unknown (denomination, unit) | `avgFillPrice`: "Exact average fill price for the whole order, or null before the first fill" (`#/paths/~1orders~1{id}~1fills/get/responses/200/content/application~1json/schema/properties/avgFillPrice/description`). `fillPrice`, `totalCost` and `quantityTraded` have only examples ([POST-200]) | Denomination of `avgFillPrice` and `fillPrice` for NO orders; whether `totalCost` is cash debited (and includes any fee). Q-S9; X5 | Kept exact; not used as cash or cost |
| W4-f | `remainingQuantity` and canonical `quantity` on placement | documented | `remainingQuantity`: "Engine-authoritative unfilled quantity after admission resizing. Present for engine-routed orders"; `quantity`: "Canonical engine quantity as a positive integer" ([POST-200]) | "Admission resizing" is not explained (see W11-f) | A resized order (`remainingQuantity` plus `quantityTraded` not equal to what was sent) pauses the engine as a mismatch until explained |
| W4-g | Account fills versus the public trade tape | documented | Account fills: [TFILLS] ("all trade fills for the caller within this tournament"), [OFILLS], and `/portfolio/fills`. The tape (`GET /exchanges/{id}/trades`, [TRADE]) is market-wide and carries no order or account id | None | Our fills are never reconstructed from the tape. [F-TAPE] is never fill evidence |
| W4-h | Fees on fills | unknown | No fee field on [FILL]. A `fee` event type exists on tournament transactions (`#/components/schemas/TournamentTransaction/properties/event_type/enum/3`) | Are there trading fees? PRD B; R5; X5 | Edge and commitment assume nothing about fees; a fee seen in R5 or X5 is reported to Arnav |

### OQ-W5: position units

**Gates:** G-1, G-2, G-7. **Needed by:**
- A2's `PositionsRead`, which keeps rows exact and flagged (`numeric_field_status`
  entries for `TournamentPosition` and `PositionLot`: OQ-W5, OQ-W8, OQ-D1);
- the internal `Position` in `shared/schemas.py` (float fields);
- hard rule 5 (a sell never exceeds the held quantity);
- commitment at cost or value (OQ-C1) and equity (OQ-D1).

| Row | Claim or field | Status | Evidence | Remaining question; smallest resolver | Fail-closed until then |
| --- | --- | --- | --- | --- | --- |
| W5-a | `quantity` sign | documented | "Positive = YES shares, negative = NO shares" (`#/components/schemas/TournamentPortfolioPosition/properties/quantity/description`) | None for the sign | Not needed |
| W5-b | `quantity` unit, granularity and comparability with order `quantity` | unknown | `number`, no unit. Order input quantities are integer shares (`#/components/schemas/OrderInput/properties/quantity/description`) | Is a position quantity a whole number of shares, in the same unit as an order quantity? Q-S9; R3 if positions exist | Hard rule 5: no sell while the held quantity or its unit is not established |
| W5-c | `avgCost` | unknown | No description on the tournament row (`#/components/schemas/TournamentPortfolioPosition/properties/avgCost`). [POST] calls it "the surviving position's display average" | YES price or held-side price for a NO holding; whether a "display average" is usable for accounting. Q-S4, Q-S9; R3; X5 | Not used as cost |
| W5-d | `currentPrice` | documented (meaning); unknown (denomination) | "Tournament valuation price. Null only for zero-quantity rows; nonzero holdings without a valuation return 409" (`#/components/schemas/TournamentPortfolioPosition/properties/currentPrice/description`); "Prices use tournament valuation_price" ([TPOS]) | YES or held-side price for a NO holding; the mark source (OQ-D1). Q-S4; R3 | Not used as a mark |
| W5-e | `marketValue`, `costBasis`, `unrealizedPnl`, `unrealizedPnlPct`, `moneyEarned`, and the `summary` totals | unknown | No descriptions on the tournament row or summary. Only the default-scope row documents `moneyEarned`: "Realized cash from prior closes on this position" (`#/components/schemas/Position/properties/moneyEarned/description`), which is not carried over | Sign and unit for NO rows; whether `costBasis` is cash paid. Q-S4; R3 (relationships between fields, if rows exist) | Kept exact; not used in commitment or equity |
| W5-f | `lots[]`: `side`, `quantity`, `entryPrice` | unknown | `side` is a free string, lot `quantity` has no sign rule, `entryPrice` no denomination (`#/components/schemas/TournamentPortfolioPosition/properties/lots/items`). "FIFO lot breakdown" ([TPOS]); opposite-signed buys net "at FIFO lot cost" ([POST]) | How lots relate to the signed `quantity` after netting. Q-S10; R3; X5 | Not used |
| W5-g | Which rows are returned | unknown | "Returns the caller's open positions", yet rows carry `settled` and may have zero quantity ([TPOS], [TPOS-ROW]) | Are settled or flat rows listed? R3 | A row's presence is not taken to mean an open holding |
| W5-h | Tournament settlement `shares`, `avgEntryPrice`, `costBasis`, `realizedPnl`, `payout` | documented (`payout`); unknown (the rest) | `payout`: "Cash credited to the tournament virtual balance (or 0 for losses)" (`#/components/schemas/TournamentSettlementEntry/properties/payout/description`). The tournament `shares` has no description (`#/components/schemas/TournamentSettlementEntry/properties/shares`); the signed rule exists only on the default-scope row (`#/components/schemas/SettlementEntry/properties/shares/description`) | Sign and unit of tournament `shares`; denomination of `avgEntryPrice` for NO. Q-S4; R5 if settlements exist | `payout` may be recorded as cash credited; the rest is kept exact |
| W5-i | Missing valuation | documented | 409 "Tournament holdings exist without valuation prices" with `details.missingValuations` (`#/paths/~1tournaments~1{slug}~1portfolio~1positions/get/responses/409`, `#/paths/~1tournaments~1{slug}~1portfolio~1pnl/get/responses/409`) | Policy (OQ-D3) | A2 keeps it as `UnvaluedHoldingsRead`; nothing is valued |
| W5-j | Observation | unknown | Positions were never captured (synthetic fixtures only) | R3 | — |

### OQ-W6: `Order.priceLimit`, `Order.quantity` and the open remainder

**Gates:** G-3, G-7 (PRD); also used by G-1 (resting commitment) and G-4 (open-order
verification). **Needed by:**
- the `Order` and `OrdersPage` wire models and A2's `OrdersWalk`, which keep `priceLimit`
  exact (`numeric_field_status`: OQ-W6);
- the commitment of resting buys (OQ-C1, OQ-C2);
- matching an unknown submission against listed orders (OQ-E1).

| Row | Claim or field | Status | Evidence | Remaining question; smallest resolver | Fail-closed until then |
| --- | --- | --- | --- | --- | --- |
| W6-a | `priceLimit` denomination for YES and NO orders | unknown | Nullable `number` with no description (`#/components/schemas/Order/properties/priceLimit`). The placement response's canonical price is side-relative (W3-b), but nothing ties `priceLimit` to it | Q-S9; X2 (one NO order's `priceLimit` beside its canonical `price`) | A resting NO order's commitment cannot be computed from `priceLimit`. It is counted from our own journalled request (W3), or the order blocks readiness |
| W6-b | `priceLimit` null | unknown | Nullable, meaning not documented. `Order.quantity` mentions "legacy market orders, which never rest" | When is it null? Q-S9 | A listed order with a null `priceLimit` is unexplained and keeps the engine paused |
| W6-c | `quantity` and the open remainder | documented | "Always positive. Use `side` to determine YES/NO direction. While `open` is true this is the executable remainder; a closed order reports its unfilled remainder plus everything it filled. Orders whose remainder was discarded before they closed — legacy market orders ... and orders shrunk to fit a decreased balance — therefore report the reduced size rather than the quantity originally submitted" (`#/components/schemas/Order/properties/quantity/description`) | The original quantity is not recoverable from SIG; keep it in our journal. Type is `number`, while order input is an integer | Resting remainder = `quantity` while `open` is true |
| W6-d | `side` and `action` on listed orders after canonicalisation | unknown | [ORDER] does not say whether a canonicalised sell is listed as its canonical buy | Q-S10; only observable with a sell that is canonicalised, which Bellwether never sends | Hard rule 5 avoids the case. A listed order whose side or action differs from our journal is a mismatch |
| W6-e | `open` versus expiry | documented | `open`: "True if the order is still resting on the book and not expired. An order can have `open: true` yet be past its `expirationDate` — use `status=expired` to query that state" (`#/components/schemas/Order/properties/open/description`). `status=open`: "resting orders that have not expired"; `status=expired`: "legacy orders still stored as open but past their `expirationDate`, plus engine orders cancelled with reason `Expired`" (`#/paths/~1orders/get/parameters/0/schema/description`) | Whether an order past expiry can still fill (OQ-G1, section 2.4). Q-S13; X4 | Every order not shown closed counts as resting |
| W6-f | Fields an order row does not have | documented | [ORDER] lists `id`, `exchangeId`, `side`, `action`, `quantity`, `priceLimit`, `open`, `createdAt`, `expirationDate`: no idempotency key, tournament id, filled quantity or terminal reason (`#/components/schemas/Order/required`) | See W9-f | — |
| W6-g | Observation | observed | [F-ORDERS] (`status=all`, `tournamentId` sent): `$.data` is `[]`, `$.pagination.hasMore` is `false`, `$.pagination.nextCursor` is `null`; `$.coverage.complete` is `true` with an integer `$.coverage.projectedThroughSequence` | Limits: at Oct 9, 19:00 UTC no order of any status was listed for this key and tournament. Says nothing about order items, about later activity, or about scope (the response does not echo `tournamentId`) | — |

### OQ-W7: fills on the tick grid

**Gates:** G-7. **Needed by:** `Fill.price: Tick | None` in `shared/schemas.py`, which can
only hold on-grid prices. Any conversion into it must refuse off-grid values
(`price_to_ticks` raises), and the A2/A3 static test (A1 T46) keeps A2 and A3 from
converting at all.

| Row | Claim or field | Status | Evidence | Remaining question; smallest resolver | Fail-closed until then |
| --- | --- | --- | --- | --- | --- |
| W7-a | REST account fill prices on the 0.005 grid | unknown | No statement found on [FILL] or [OFILLS]; the tick rule is about fresh limit prices ([INFO]) | Q-S9; R4 or X5 give one or a few observations only | Fill prices stay exact `Decimal`s. A change to the `shared` `Fill.price` type needs Arnav's approval and is not proposed here |
| W7-b | `avgFillPrice` on the grid | unknown | "Exact average fill price" (W4-e). By arithmetic, a quantity-weighted average of on-grid prices is generally off grid, so even on-grid fills would not make `avgFillPrice` on grid | Whether it is quantity-weighted. Q-S9 | Never converted to ticks |
| W7-c | Public tape prices | observed | [F-TAPE] `$.data[*].price`: 150 trades on exchange 947, Oct 9 09:43 to 22:33 UTC, all on the grid. The run's `fixtures.json` also records that every `$.data[*].volume` equals the side price times `size`, computed in binary floating point (for example `0.7800000000000007` in `07-exchange-trades-947-p1-a1.json`) | Limits: public trades of one exchange, a truncated sample; not our fills. It also shows that some SIG numbers carry float noise, so exact parsing does not imply decimal-clean values | — |

### OQ-W8: routing, netting and sell conversion

**Gates:** G-1, G-7. **Needed by:**
- commitment (OQ-C1, OQ-C2, OQ-C5);
- hard rule 5;
- position reconciliation and the canonical-field checks proposed in W3-b.

| Row | Claim or field | Status | Evidence | Remaining question; smallest resolver | Fail-closed until then |
| --- | --- | --- | --- | --- | --- |
| W8-a | What "engine-routed" covers | unknown | Used without definition: "On engine-routed scopes" ([POST]); canonical fields "Present for engine-routed orders" ([POST-200], [MLD]); `/account` balance "engine-authoritative when routed" ([ACCT]); [INFO] mentions an "original-routing rollout". No read reports whether a scope is engine-routed | Is `midterm-elections` engine-routed for order placement? Q-S10; X1 (whether the canonical fields appear) | The netting and canonicalisation rules are assumed to apply, so sells stay forbidden unless fully backed (hard rule 5) |
| W8-b | Bounded engine indicators on reads | observed | [F-XBOOK] `$.asOf` and [F-MBOOK] `$.exchanges[*].asOf` (and `$.contexts[*].orderbook.exchanges[*].asOf`) are non-null (documented: null "when the book was read from the database", `#/components/schemas/BookVersion/description`). [F-ORDERS] carries `$.coverage` (documented: "Engine-backed responses include projection `coverage` metadata", `#/paths/~1orders/get/description`) | Limits: these show engine-backed *reads* on Oct 9. The spec does not equate engine-backed reporting with engine-routed placement, and says only that engine-backed responses include `coverage`, not that others omit it | As W8-a |
| W8-c | Netting of an opposite-signed buy | documented (rule); unknown (effects) | "On engine-routed scopes, an opposite-signed `BUY` nets against the existing position: it reduces or crosses the position at FIFO lot cost" ([POST]) | Its effect on the fill rows (sign and side), lots, `myBalance` and realised P&L. Q-S10; X5 then a netting buy (not proposed) | Commitment never nets an opposite-side buy against a holding (C5, proposed: summed). A buy that would net is still counted at full cost |
| W8-d | Sell conversion | documented | "a fully-backed `SELL` closes conventionally; a `SELL` that is flat, opposite the held side, or larger than the held quantity is canonicalized by the engine into its complement `BUY` (`sell yes q@p ≡ buy no q@(1−p)`), and the response's canonical `action`/`side`/`price`/`quantity` report the order as placed" ([POST]); `action` "may differ from the requested action when a sell is converted" ([POST-200]) | For a sell larger than the holding, the formula converts the whole quantity `q`; how that then nets is not stated. Q-S10 | Hard rule 5. If a response's canonical `action` is `buy` for a sent `sell`, the engine pauses |
| W8-e | A resting sell after the holding shrinks | unknown | Canonicalisation is documented at placement ("is canonicalized by the engine"). No statement found on whether a resting `SELL` is re-evaluated, resized, cancelled or converted when the holding later falls below its remainder | Q-S10. Also bears on OQ-C2, whose wording calls this case "documented" (section 2.1) | Resting sells are counted as potential buys of the complement at full remainder (a proposal for C2), or are not allowed to rest (OQ-G2) |
| W8-f | Self-trade prevention | documented | "an order that would cross your own resting order fills other counterparties, then its crossing remainder is cancelled with `terminalReasonCode: "SelfTradePrevented"`" ([POST]) | None for the rule | A placement whose `terminalReasonCode` is not null is reconciled before the next order |
| W8-g | Cash effect of ALL collateral | documented; not used | `effectiveEntryCost`: "Cash actually debited for the new fill after applying an eligible ALL collateral advance" ([AEE]); `all` is null "when no engine relationship quote was produced" ([POST-200]) | The PRD records no linked markets. R6 confirms that none applies | A non-null `all` on any placement pauses the engine as unexpected |

### OQ-W9: idempotency retention, scope and visibility

**Gates:** G-3, G-6, G-7. **Needed by:**
- the future idempotency journal (O2, hard rule 6);
- unknown-outcome resolution (OQ-E1);
- restart reconciliation (S1).

| Row | Claim or field | Status | Evidence | Remaining question; smallest resolver | Fail-closed until then |
| --- | --- | --- | --- | --- | --- |
| W9-a | A key is required, 1 to 255 characters, one per logical operation | documented | [SOI-KEY]; `#/components/schemas/BatchOrderInput/properties/idempotencyKey/description`; `#/paths/~1orders~1multi-leg/post/requestBody/content/application~1json/schema/properties/idempotencyKey/description`; [INFO] ("Generate one key per logical operation and reuse it only when retrying the identical payload") | None | Hard rule 6 |
| W9-b | Replay and conflict | documented | "An identical resolved payload replays the exact stored response; reuse with a different resolved payload returns 409" ([SOI-KEY]). Single: replay happens "before mutable settlement or market preflights" ([POST]). Batch: "The ordered, resolved batch is the request identity" ([BATCH]). Multi-leg: legs, scope, `relationshipConstraint` and organization "identify the retry" ([ML]) | A replay returns the *stored* response, which describes the original outcome, not the order's current state | A replay's result is reconciled with reads before trading resumes |
| W9-c | What "resolved" covers | unknown | Resolution of an omitted `tournamentId` is documented ([INFO], [BATCH]). Whether numeric forms (`0.4` versus `0.40`) or field order count as the same payload is not stated | Q-S11 | Proposed for O2: the journal stores the exact request bytes, and a retry resends them byte for byte, always with an explicit `tournamentId` |
| W9-d | Retention period | unknown | No statement found in [INFO], [SOI-KEY], [POST], [BATCH] or [ML] | How long a completed or incomplete key is kept. Q-S11 | A key whose age is beyond a bound Arnav sets (from SIG's answer) is resolved by reads only, never by resending (D-4, OQ-E1). With no answer, no bound exists and such a key keeps the engine paused |
| W9-e | Retries after a restart or a long outage | unknown | No statement found | Q-S11 | As W9-d |
| W9-f | Key visibility | documented (absence) | No order read returns the key ([ORDER], [ORDERS], [ORDER1], [OFILLS]); no endpoint looks orders up by key | None: the only key-based lookup is the identical retry | Matching by `exchangeId`, side, action, price, quantity and time is heuristic. It cannot tell two identical orders apart, and canonicalisation can change the side, action and price. A match is evidence only under the rule Arnav sets in OQ-E1 |
| W9-g | Key namespace | unknown | "Reusing a key from another authenticated organization conflicts" ([BATCH]). Whether single, batch and multi-leg share one namespace is not stated (DMM keys are "scoped per operation type", which is about DMM only) | Q-S11 | Keys are globally unique in our journal and never reused across endpoints |

### OQ-W10: ambiguous submissions and retries

**Gates:** G-3, G-6. **Needed by:**
- the O2 retry state machine (hard rule 6);
- the commitment of unknown submissions (OQ-C1);
- fault pauses (OQ-E3).

Cancellation rows are included because protective cancellation must stay available in
every pause (hard rule 3).

| Row | Response | Status | Evidence | Remaining question; smallest resolver | Fail-closed until then |
| --- | --- | --- | --- | --- | --- |
| W10-a | Single 502 `ORDER_STATUS_UNKNOWN` | documented | "the trading engine did not confirm whether the order was accepted. Check positions, or retry the identical order with the same idempotency key; that retry is safe and will not double-place" ([POST-502]); [INFO] error table and note; [OSU] message example | Bounded by retention (W9-d) | Same key, identical bytes; outcome unknown and counted at full size (C1) until a reply or reads resolve it |
| W10-b | Batch 502 (every item unknown) | documented | "Each result has status 502 and `data.code: ORDER_STATUS_UNKNOWN`. Check positions, or retry the identical ordered batch with the same idempotency key; that retry is safe and will not double-place" ([BATCH-502]) | As W10-a | As W10-a, per item |
| W10-c | 409 `REQUEST_IN_FLIGHT` | documented | "The same idempotency key is still executing — retry the identical request with the same key after 90 seconds" ([INFO] error table); "another request with the same idempotency key still owns its 90-second execution lease" ([INFO] rate limits); "Retry `REQUEST_IN_FLIGHT` with the same key after the lease interval" ([POST-409]) | What a repeated in-flight answer means. Q-S12 | Wait 90 s, retry identically; outcome unknown meanwhile |
| W10-d | Other 409s on placement | documented (cases); unknown (outcome) | "The idempotency key was reused with a different resolved payload, the trading engine reported a duplicate command, or ..." ([POST-409]). The code of the duplicate-command case is not given; `CONFLICT` is the generic code ([INFO]) | Was the earlier command placed? Q-S12 | A 409 other than `REQUEST_IN_FLIGHT` is an unknown outcome that pauses the engine. It is never retried under a new key |
| W10-e | Single 503 | documented (cases); unknown (outcome) | "the trading engine could not confirm the order (e.g. persistence backpressure or balance sync), or a new tournament membership is still syncing to the trading engine ...; retry shortly" ([POST-503]). [INFO]: `SERVICE_UNAVAILABLE` and `TX_CONFLICT` are "safe to retry with exponential backoff"; a 503 sent "before it runs when rate limiting is temporarily unavailable" means "the request was refused, not processed", but carries the same code | Whether an order behind "could not confirm" may have been placed, and how to tell the pre-run 503 apart. Q-S12 | Treated as unknown; same-key identical retry with backoff (as `CLAUDE.md` hard rule 6 says) |
| W10-f | Batch 503 | documented | "earlier orders in the batch may already have been placed, so do not treat 503 as proof that nothing executed ... retry the identical ordered batch with the same idempotency key — terminal items replay their stored per-item result and are not placed again" ([BATCH-503]) | None beyond retention | As documented; every unissued or transient item counts as unknown |
| W10-g | Batch 207 and completion | documented | "If any item returns 429 or 5xx, the outer batch remains incomplete; retry with the same key ... After a completed 207, resubmit failed items under a new batch key" ([BATCH]) | A 207 is complete only when no item has status 429 or 5xx | A failed item gets a new key only after a complete 207 shows it failed terminally |
| W10-h | Batch 429 (every item engine-rate-limited) | documented | "In the second case nothing was placed ... Retry the identical ordered batch with the same idempotency key after that wait" (`#/paths/~1orders~1batch/post/responses/429/description`); per item, "nothing was placed for it" ([BATCH]) | None | As documented |
| W10-i | Multi-leg 503 and response loss | documented (cases); unknown (outcome) | Four 503 causes, including "engine persistence backpressure while confirming" (`#/paths/~1orders~1multi-leg/post/responses/503/description`); "an incomplete claim after response loss resumes with the same engine key so the engine rehydrates the accepted command" ([ML]) | As W10-e | Same-key identical retry; unknown meanwhile |
| W10-j | Timeout, reset connection or no response | unknown | No statement found | Q-S12 | Unknown outcome: same-key identical retry within retention (unknown, W9-d), never a new key, engine paused until resolved (hard rule 6) |
| W10-k | 500 and 429 on placement | documented (codes); unknown (outcome) | `DATABASE_ERROR`, `INTERNAL_ERROR` (500), `RATE_LIMITED` (429, `Retry-After: 60`) ([INFO]). The spec does not say whether a placement answered 500 or 429 was executed | Q-S12 | Unknown outcome; same-key retry only |
| W10-l | Cancel one order: 503 and 409 | documented | 503: "Cancellation was not confirmed ... Retry — a retry of a completed cancel returns 409" (`#/paths/~1orders~1{id}/delete/responses/503/description`); 409: "already closed because it was filled or cancelled by a concurrent request" (`#/paths/~1orders~1{id}/delete/responses/409/description`); 200 echoes `tournamentId` (`#/paths/~1orders~1{id}/delete/responses/200/content/application~1json/schema/properties/tournamentId/description`) | A 409 does not say whether the order filled or was cancelled; read it | Retry until 200 or 409, then read the order and its fills |
| W10-m | Cancel-all: 200 and 503 | documented | 200: "Every targeted order is cancelled; none remains open" (`#/paths/~1orders~1cancel-all/post/responses/200/description`). 503: "`error.details.cancelled` reports how many orders this request confirmed cancelled; some targeted orders may still be open. Retry the same request until it returns 200 ... A 503 without `error.details.cancelled` was refused before anything was cancelled" (`#/paths/~1orders~1cancel-all/post/responses/503/description`); "The request may have cancelled at least this many" ([CXLP]) | Observed behaviour on the tournament (G-4, X3) | Retry the identical scoped request until 200; then verify with an all-pages `status=open` walk |

Cancels carry no idempotency key. Their retries are documented as safe because a cancel
can only remove orders. That does not make them work while our server or SIG's API is
unreachable (REQ-14).

### OQ-W11: tournament cash

**Gates:** G-1, G-3, G-7. **Needed by:**
- uncommitted cash and the minimum-cash ceiling (G-1);
- the readiness check (G-3);
- A2's `TournamentBalanceRead`;
- A3's `cash` candidate, which is never established (`snapshot.py`).

| Row | Claim or field | Status | Evidence | Remaining question; smallest resolver | Fail-closed until then |
| --- | --- | --- | --- | --- | --- |
| W11-a | `/account` balance scope | documented | "For an organization-bound key, `balance` comes from the organization's default tournament ... An unbound key retains the public-global cash balance" ([ACCT]); no parameters. Quickstart: "Use `GET /tournaments/{slug}` for an explicit tournament balance" ([INFO]) | None: it is never tournament cash | Never read for cash (A1 3.1) |
| W11-b | `myBalance` meaning | documented | "Caller's current or seed virtual cash balance in this tournament" (`#/paths/~1tournaments~1{slug}/get/responses/200/content/application~1json/schema/properties/myBalance`); "the authenticated member's current virtual balance for the tournament" (`#/paths/~1tournaments~1{slug}/get/description`); 503 "The authoritative tournament balance is temporarily unavailable" (`#/paths/~1tournaments~1{slug}/get/responses/503/description`) | The only explicitly tournament-scoped balance; a candidate, not established | A3 reports it as an unestablished candidate |
| W11-c | Enrolment | documented | `isPendingEnrolment` (`#/paths/~1tournaments~1{slug}/get/responses/200/content/application~1json/schema/properties/isPendingEnrolment`); a first order in an open tournament enrols the caller (`#/components/schemas/OrderInput/properties/tournamentId/description`); membership sync 503 ([POST-503]) | Whether the account is enrolled now, and when a seed becomes current. R1; Q-S5 | Pending or unknown enrolment keeps readiness failed |
| W11-d | Observation | unknown | [F-TOUR] `$.myBalance` and `$.isPendingEnrolment` are fixed placeholders in all three runs | R1 | — |
| W11-e | Admission cash | unknown | Placement can fail for "insufficient balance" ([POST-400]); no statement found that admission checks `myBalance` | Q-S1; no safe observation proves it (X1 informs, does not prove) | No cash figure is used as uncommitted cash (G-1) |
| W11-f | Reservations by resting orders | unknown | No statement found. Related phrases, not inferred from: orders "shrunk to fit a decreased balance" (`#/components/schemas/Order/properties/quantity/description`) and "admission resizing" ([POST-200] `remainingQuantity`) | OQ-C3. Q-S2; X1 | Every resting buy is counted as if not reserved by SIG (C3) |
| W11-g | Freshness and cross-read consistency | unknown | No as-of time on [TOUR]; no projection checkpoint ties it to orders or fills (A1 1.5, 1.6) | A1-N1, A1-N3; Q-S3 | No freshness claim (A3 already makes none) |
| W11-h | Other cash-like fields | documented; not used | Tournament P&L has no cash field ([TPNL]); history `cashValue` may be interpolated (A1 2.7); ALL economics only for linked markets ([AEE]) | None for O1 | Not used as cash |

## 2. Gate and decision map

The table says, for each gate, what this note documents, what needs Arnav's choice, what
needs genuine tournament evidence, and what later code and tests must prove. **No gate is
passed by this note.** A documented behaviour or a proposed check is not gate evidence.

| Gate | O1 documents (section 1) | Needs Arnav's choice | Needs tournament evidence (section 3) | Later implementation and tests must prove |
| --- | --- | --- | --- | --- |
| G-1 Commitment and classification | `myBalance` is the only scoped candidate (W11-b); no reservation statement (W11-f); netting and canonicalisation rules (W8-c, W8-d); cost and price denominations unknown (W4, W5, W6) | OQ-C1 to OQ-C5; the cash source (D-5); whether bounded evidence suffices | R1, X1 (reservation), R3/R4 if holdings exist | Commitment counts filled, resting and unresolved orders as defined; the classification file is committed and reviewed; ceilings use confirmed values only |
| G-2 Equity and loss trigger | `currentPrice` meaning; the 409 (W5-d, W5-i); `totalAccountValue` has no description (A1 2.5) | OQ-D1 to OQ-D5 | R3 (P&L and positions shapes); PRD B leaderboard valuation | The trigger latches, attempts cancellation, holds until reset; stale and missing marks follow the policy |
| G-3 Restart and readiness | Scope binding of each read (A1 1.2); no key in order reads (W9-f); ambiguous outcomes (W10); `projectedThroughSequence` is provenance only | OQ-E1 to OQ-E4; retention bound once known (W9-d) | R1 to R5 (shapes, coverage, pagination); Q-S3, Q-S11, Q-S12 | No order before tournament cash, positions, all open-order pages and fills are reconciled; every unknown key resolved or paused; no atomicity claimed |
| G-4 Outage protection and cancellation | Cancel-all and expiry semantics (W10-m, W6-e, section 2.4) | OQ-G1 lifetime, OQ-G2 allowed resting styles, the maximum resting commitment | X3 (cancel-all), X4 (expiry); Q-S13 | Bounds recorded with that evidence; until then resting orders are assumed live and fillable during an outage |
| G-5 Configuration changes | Nothing: G-5 has no wire dependency | OQ-F1 to OQ-F3 | None | Validation, versioning, audit log, impact assessment and the over-limit policy |
| G-6 Order path safety | Tick and market-order rules (W2-a, W2-b); retry rules (W10) | D-1 (NO price), D-6 (rounding), retry handling outside the documented cases | X2 if D-1 asks for it | Bypass test; limit-only on-tick; sell never above held; key saved before send; same-key retries; dry-run default; second-model review |
| G-7 API wire contract | Every OQ-W row's status (section 1) | Which citations count as answers (D-1, D-3) | Section 3, where a row names a check | Boundary conversion tests for every settled rule; every open row shown unused or fail-closed (section 2.7) |

### 2.1 G-1: OQ-C1 to OQ-C5

What each term of the proposed commitment
([PRD, working definitions](../PRD.md#working-definitions-proposed-not-yet-confirmed))
would need from the wire. This lists dependencies; it sets no rule and no limit.

| Term | Needs | Status of what it needs |
| --- | --- | --- |
| Open position "at cost" (OQ-C1) | Cash paid per position: `costBasis` or lots (W5-e, W5-f), or our own fills (W4) | Unknown for NO holdings; fills unknown |
| Open position "at current value" (OQ-C1) | A mark: `currentPrice` denomination (W5-d) and a valuation policy (OQ-D1) | Unknown; policy not chosen |
| Resting buy remainder | Remainder (W6-c, documented) times per-share cost: `priceLimit` denomination (W6-a), or our journalled request price (W3) | Remainder documented; price unknown |
| Resting sell (OQ-C2) | Whether a resting sell can become a buy after the holding shrinks (W8-e) | **Unknown.** OQ-C2 calls this "documented sell canonicalisation", but the spec documents canonicalisation at placement only. Rewording OQ-C2 is Arnav's call; this note does not edit it |
| Unknown submission | Our own journalled quantity and price (no SIG read), interpreted per W3 | Depends on D-1 |
| Reservation already deducted (OQ-C3) | W11-f | Unknown; X1 gives one bounded observation |
| Within-race netting (OQ-C5) | W8-c says how SIG nets *within one exchange*. It says nothing about a Democratic YES against a Republican YES, which are different exchanges and are not netted by SIG | PRD proposal: summed. A policy, not a wire question |

**Race and related-market group (OQ-C4).** The race ID comes from the market data (PRD
market facts). Group membership is a policy choice. The candidates are in OQ-C4: Senate
control with all Senate races; House control with all House races; same state; same
lean. If groups overlap, Arnav decides whether a market counts in full in every group it
belongs to. The classification must be a committed, reviewed file (G-1). R1 (S1-T6 race
mapping) supplies the race side only.

**Not activated.** The PRD's provisional ceilings remain provisional. Nothing here puts
them in configuration or code (hard rule 9).

### 2.2 G-2: OQ-D1 to OQ-D5

- **OQ-D1, equity formula and mark source.** Candidates:
  - `totalAccountValue` from tournament P&L (no description, so its parts are
    unknown);
  - our own cash plus positions at a chosen mark, which needs W5-d and W11.

  Either needs Arnav's choice and the W5 answers. The A3 snapshot is not proof of
  consistent valuation: its best status is `OBSERVED_UNRESOLVED`, and positions, P&L and
  balance are separate reads with no shared checkpoint.
- **OQ-D2, baseline.** A policy choice. `initialBalance` is observed as `100000` in
  [F-TOUR] (not a redacted field).
- **OQ-D3, stale or missing marks.** The 409 is documented for positions and P&L (W5-i)
  and is kept as a typed result by A2. Valuing at zero, at cost or pausing is Arnav's
  choice.
- **OQ-D4, loss reset.** Authority (proposed in the PRD: Arnav only) and semantics are
  policy. No wire dependency.
- **OQ-D5, election night.** Policy. No current requirement allows switching the trigger
  off.

### 2.3 G-3: OQ-E1 to OQ-E4 and tournament-scoped reconciliation

- **OQ-E1, unknown submissions.** The documented tools are:
  - the identical same-key retry (W10-a to W10-c, W10-f, W10-i);
  - reads, which cannot see keys (W9-f).

  What stays open:
  - retention (W9-d, W9-e) and the outcomes behind W10-d, W10-e, W10-j and W10-k;
  - what happens when neither retry nor reads settle an outcome. The engine stays paused
    and Arnav resolves it by hand (proposed).
- **OQ-E2, fills during downtime.** [TFILLS] is newest first with no time window, so a
  walk must reach fills older than the last reconciled one. Whether such fills are
  normal or pause for review is policy.
- **OQ-E3, fault-pause duration.** Policy; no wire dependency.
- **OQ-E4, blocker recovery.** M1 and M2 territory (G-8). O1 adds nothing beyond noting
  that order-path readiness also needs the market to be ready.
- **Scoped reads for readiness:**
  - cash: W11 (unestablished);
  - positions: [TPOS] (verified slug in the path, no echo);
  - all open-order pages: [ORDERS] with `tournamentId` (no echo; [OFILLS] and [DEL]
    echo per order);
  - fills: [TFILLS].

  Projection freshness is unknown: `projectedThroughSequence` is provenance, and the
  three tape pages of one walk reported different values (12ab20 `fixtures.json`). No
  cross-endpoint consistency is documented (A1-N1, A1-N3).
- **A restart alone recovers nothing.** Readiness needs these reads to agree with the
  journal.

### 2.4 G-4: section B, OQ-G1 and OQ-G2

Documented:

- Cancel-all with `{ "tournamentId": ... }` "uses that explicit accessible tournament"
  ([CXL] description). An empty body uses the default context, so a request must always
  name the tournament.
- It cancels "the caller's open, unexpired orders in that context, in batches of up to
  500". It returns 200 "once no targeted order remains open" and 503 if paused, with
  progress in `details.cancelled` ([CXL], [CXLP]).
- 409 means "The tournament is archived" (`#/paths/~1orders~1cancel-all/post/responses/409/description`).
- `expirationDate` is an "Optional future ISO 8601 datetime after which the limit order
  expires. Not supported for market orders"
  (`#/components/schemas/OrderInput/properties/expirationDate/description`).
- On the order row, "Reporting-backed orders preserve the engine order's expiration
  timestamp" (`#/components/schemas/Order/properties/expirationDate/description`).
- Engine orders that expire are "cancelled with reason `Expired`" (W6-e).
- "**Order expiry emits nothing.** Expired orders are filtered at read time"; books carry
  `nextExpiryAt` ([RT]).

Not documented:

- whether an order is excluded from *matching* at its expiry instant, rather than only
  filtered from reads, and with what delay;
- the minimum or maximum allowed expiry;
- whether expiry is honoured on this tournament;
- whether a legacy order "still stored as open but past their `expirationDate`" (the
  `status=expired` wording) can still fill.
  Cancel-all targets only *unexpired* open orders, so it may not remove such an order.

Consequences for G-4:

- Until X3, X4 and Q-S13 give evidence and Arnav decides, the PRD's assumption stands:
  resting orders stay live and can fill during an outage.
- A maximum resting lifetime is measurable only as the expiry Bellwether sets, and only
  if expired orders are shown not to fill.
- A maximum resting commitment needs W6-a or W3 to price resting orders.
- OQ-G2 (which order styles may rest at all) is Arnav's choice. "No resting orders" is
  one option, and so is "only with an expiry". Marketable orders whose remainder is
  cancelled at once are not outage protection (PRD).

### 2.5 G-5: OQ-F1 to OQ-F3

Loosening versus tightening (OQ-F1), positions or orders already over a new limit
(OQ-F2), and where versions and the audit log live (OQ-F3) are policy decisions with
later implementation tests. They have no wire dependency, and O1 makes no G-5 claim.

### 2.6 G-6: what O2 can and cannot build

| G-6 item | W answers it depends on | O2 can build now (with fake transports) | Blocked until |
| --- | --- | --- | --- |
| Limit-only, on-tick prices | W2-a, W2-b (documented); W2-c (policy); W3 (NO side) | The on-tick check and market-order refusal; YES-side conversion `ticks/200` | NO conversion: D-1. Rounding: D-6 |
| A sell never exceeds the held quantity | W5-a (documented sign); W5-b (unit); W8-d, W8-e | A sell path that always refuses | W5-b and the held-quantity source are settled (hard rule 5) |
| Durable idempotency key before send | None for the journal itself; W9-c for byte identity | Journal written and synced before send; byte-identical resend | Retention-based expiry of journal entries: W9-d |
| Ambiguous retry handling | W10-a to W10-c, W10-f to W10-i (documented) | Those cases exactly as documented | W10-d, W10-e, W10-j and W10-k outcomes: they stay unknown and paused |
| Dry-run default | None | Yes | — |
| `risk.check` bypass test | None | Yes | Commitment arithmetic waits for G-1 definitions (PRD O2 row) |
| Second-model review | None | Process | — |

This shows what the answers would unblock. It does not authorise starting O2, which
depends on O1 and the G-1 definitions in the PRD.

### 2.7 G-7: classification of the open rows

G-7 passes only when every row the order path, commitment, valuation or reconciliation
relies on is answered by a spec citation or logged real-platform evidence, and the rest
are shown unused or fail-closed.

| Class | Rows |
| --- | --- |
| **Documented and candidate answers** (Arnav accepts or rejects each as a citation) | W1-a, W2-a, W2-b, W3-b to W3-d (as an answer to OQ-W3: D-1), W4-a (sign only), W4-f, W4-g, W5-a, W5-d (meaning), W5-h (`payout`), W5-i, W6-c, W6-e, W6-f, W8-c and W8-d (rules), W8-f, W9-a, W9-b, W9-f, W10-a to W10-c, W10-f to W10-h, W10-l, W10-m, W11-a to W11-c |
| **Unanswered but unused** by the order path v1 | W1-e (used by R3 fair value; the order path never reads venue quotes), W5-g, W8-g (no linked markets; checked by R6), W11-h |
| **Observation rows** (bounded; they inform a question but answer none) | W1-c, W6-g, W7-c, W8-b. No observation exists for W3-e, W5-j or W11-d |
| **Unanswered and fail-closed** | W1-b, W1-d, W2-c (policy), W2-d, W3-a, W3-f, W4-b to W4-e, W4-h, W5-b, W5-c, W5-e, W5-f, W5-h (other fields), W6-a, W6-b, W6-d, W7-a, W7-b, W8-a, W8-e, W9-c to W9-e, W9-g, W10-d, W10-e, W10-i to W10-k, W11-e to W11-g |

**Boundary conversion tests, later, cover only settled rules:**

- on-tick validation: exact `k/200` for 1 to 199; refusal of 0, 1, off-tick values and
  no price;
- YES-side request price `ticks/200`;
- the NO-side price, only after D-1;
- exact `Decimal` formatting of the request, and byte-identical resend;
- the documented retry state machine (W10-a to W10-c, W10-f to W10-i);
- cancel-all always sent with `tournamentId`.

## 3. Arnav-only evidence runbook

**Nothing in this section has been run.** No agent runs any of it: agents never call SIG,
never log in to SIG and never see a key (`CLAUDE.md` hard rules 1, 2 and 8). The
read-only checks (3.3) can be run independently of the write checks (3.4). The write
checks need Arnav's separate review and decision before any of them is run.

### 3.0 Ground rules for every check

- **Keys.** Use the read-only key for section 3.3. Use the trade key only for section
  3.4, run by Arnav from his own machine. No key or header value appears in any artifact.
- **Scope.** Always use the slug `midterm-elections` on tournament paths and
  `tournamentId=bda92870-621e-47b0-bc3c-3602c5c26f55` on every request that takes one,
  including the cancel-all body. Never call a default-scope read (`/account`,
  `/portfolio/positions`, `/portfolio/pnl` and so on).
- **Budget.** Each check states its request count. All of them together stay far below
  80 reads and 25 writes per minute. Keep at least a few seconds between writes.
- **Artifact per check:**
  - UTC timestamp before and after each request;
  - method, path and query (no headers);
  - HTTP status, and `error.code` when present;
  - the response body, kept local;
  - pages read, `hasMore` and `nextCursor` presence, `coverage` as sent;
  - a note naming the check ID and the code commit used.

  Reads the existing recorder supports should go through
  `engine/sig_client/record_fixtures.py`, so their provenance matches the existing
  fixtures. Anything else is recorded by hand.
- **Redaction before anything is committed** (the R8 rules in A1 and the fixtures README
  apply):
  - balances stay local. What can be committed are relationships:
    - whether `myBalance` equals `initialBalance`;
    - its number of decimal places;
    - the exact difference between two reads;
  - `isPendingEnrolment` is recorded in Arnav's decision log, not in a fixture, unless
    he decides otherwise;
  - own order and fill IDs are replaced by consistent placeholders;
  - test order prices and quantities are chosen test values and can be kept. Where a
    question needs a field's precision or a relationship between fields, keep that
    precision or relationship rather than a coarser value;
  - where redaction would remove what the check is for, the note says so, and the value
    stays local.
- **One observation is one observation.** It shows what one key saw, for one order, at
  one time. It does not establish a general rule. Whether bounded evidence is enough for
  a gate is Arnav's decision, recorded in the PRD.

### 3.1 Reconciliation with A1's P1 to P6

| A1 check | Here | Change |
| --- | --- | --- |
| P1 (one read each of the tournament, positions, one fills page, P&L, one settlements page, `status=open` orders) | R1 to R5 | Split by question. Order and fill walks follow every page to `hasMore: false` within a stated cap, rather than one page, because G-3 needs all pages. `status=all` and `status=expired` are added. Per-order fills only when an order ID exists. Relationships between fields are recorded instead of raw values |
| P2 (key binding from the website) | R0 | Unchanged. The spec says participant keys are bound automatically ([INFO]); R0 records whether the website shows it |
| P3 (balance around one tiny resting buy) | X1 | It still informs OQ-C3 and OQ-W11 but cannot prove either (W11-e). Recording of the canonical response fields (W8-a), `GET /orders/{id}`, the book level and the cancel echo is added. Cancellation is by `DELETE /orders/{id}`, so X3 tests cancel-all separately |
| P4 (one minimal NO buy that fills) | X5 | Demoted to a separate real-trade decision, not a prerequisite of X1 to X4. Which side to fill is part of that decision. X2 covers the NO request price without a fill |
| P5 (per-order fills within P3) | X1, R4 | Folded in |
| P6 (collateral) | R6 | Unchanged, low priority |
| none | R7, X2, X3, X4 | New: quote precision sweep; NO request price and `priceLimit`; tournament-scoped cancel-all; expiry |

### 3.2 Questions for SIG versus bounded observations

Questions only SIG can answer with a guarantee: a bounded observation cannot establish
any of these. A1's Q-S1 to Q-S6 stand; this list adds the order-path questions. A written
answer is evidence only once Arnav records it, with its date, in the PRD. How such an
answer is recorded for G-7 is D-3.

| ID | Question | Rows |
| --- | --- | --- |
| Q-S1 to Q-S6 | As in [A1 9.2](account-state-contracts.md#92-questions-for-sig): admission cash, reservation, consistency, field denominations, pending enrolment, cursor stability | W11, W5, W4, W6 |
| Q-S7 | Are REST and realtime quotes and book levels always on the 0.005 grid, and with what maximum precision? | W1-b, W1-d |
| Q-S8 | For single and batch placement, does a `side: "no"` order's request `price` carry the NO price, as the multi-leg text and the canonical response imply? | W3-a, W3-f |
| Q-S9 | For NO orders: the denomination of REST fill `price`, `avgFillPrice`, `fillPrice`, `totalCost` and `priceLimit`. Are fills always on the tick and in whole shares? Can a limit fill worse than its limit? How is a fill that reduces a position signed? When is a fill price or `priceLimit` null? | W2-d, W4, W5-b, W6-a, W6-b, W7 |
| Q-S10 | Is `midterm-elections` engine-routed for placement? How do netting and canonical buys show in fills, lots and `myBalance`? What happens to a resting sell when the holding later shrinks? Are canonicalised orders listed with the canonical side and action? | W8, W6-d, W5-f |
| Q-S11 | How long are idempotency keys and stored responses kept, and does a replay survive SIG restarts? Do single, batch and multi-leg share one key namespace? What counts as the same "resolved payload"? | W9 |
| Q-S12 | After a single-order 503, a "duplicate command" 409, a 500, a 429 or a timeout, may the order have been placed, and is a same-key retry the right recovery? | W10-d, W10-e, W10-j, W10-k |
| Q-S13 | Is an order excluded from matching at its `expirationDate`, and with what delay? Is expiry honoured on this tournament, and what are the allowed values? Can a legacy order stored as open past its expiry fill? | G-4, W6-e |
| Q-S14 | PRD B: trading fees, and what happens to resting orders at the Nov 4 cutoff | W4-h, G-4 |

Facts a bounded observation can establish, for one case at one time:

- the shapes and presence of fields in our own responses;
- the enrolment flag now;
- whether this tournament's placement responses carry the canonical fields;
- one order's canonical price beside its `priceLimit`;
- one balance difference around one resting order;
- one cancel-all result followed by a full open-order walk;
- whether one order with an expiry was reported expired after it.

### 3.3 Read-only checks (priority order)

Each check is read-only and takes the read-only key unless it says otherwise. "Changes"
says how each possible result moves a question. None of them can move a question to
answered on its own.

**R0. Key binding (priority 1; no API call).**
- *Prerequisite:* none.
- *Do:* in the SIG website's API-key settings, note whether each key shows an
  organization binding and its scopes.
- *Artifact:* a dated note in the decision log. No key value and no screenshot of a key.
- *Changes:* explains why default-context reads are never used (W11-a).
- *Limits:* the website is not the API contract.
- *Stop:* nothing to clean up.

**R1. Tournament identity, enrolment and balance shape (priority 1).**
- *Prerequisite:* R0.
- *Requests:* `GET /tournaments/midterm-elections`, twice, at least 60 s apart (2 reads).
- *Inspect:* `id` and `slug` (must equal the configured pair), `status`,
  `initialBalance`, `currencyName`, `isPendingEnrolment`, `myBalance`.
- *Artifact:* the identity result; the enrolment flag (in the decision log); whether
  `myBalance` equals `initialBalance`, its decimal places, and the exact difference
  between the two reads. Raw balances stay local.
- *Changes:*
  - pending `true`: the next order would enrol the account (W11-c), so X1 to X4 begin
    with enrolment and may meet the membership-sync 503;
  - pending `false` and `myBalance` not equal to `initialBalance`: consistent with
    current rather than seed (W11-b);
  - a change between the two reads with no activity of ours: report it (A1-N3).
- *Limits:* says nothing about admission cash (W11-e), reservations (W11-f) or
  freshness.
- *Stop:* an identity mismatch stops every check.

**R2. Orders, all pages (priority 1).**
- *Prerequisite:* R1.
- *Requests:* `GET /orders` with `tournamentId`, `limit=200`, for:
  - `status=open`, walked until `hasMore: false` (cap 10 pages);
  - `status=all`, walked the same way;
  - `status=expired`, one page.

  At least 3 reads.
- *Inspect, per page:* `pagination`, `coverage` presence and values. Per item: field
  presence and types, and `priceLimit` decimal places (values local).
- *Artifact:* page records and item counts per status.
- *Changes:*
  - any item: first real order rows for W6 (shape only, unless Arnav keeps values), and
    an order ID for R4;
  - `coverage` absent: W8-b differs from Oct 9;
  - any open order: X1 to X4 must exclude it, and Arnav decides whether it should exist
    at all.
- *Limits:* the scope is not echoed, so it rests on the `tournamentId` sent.
- *Stop:* the page cap reached means the walk is incomplete; record that, never a
  partial count.

**R3. Positions and P&L (priority 2).**
- *Prerequisite:* R1.
- *Requests:* `GET /tournaments/midterm-elections/portfolio/positions` and
  `GET /tournaments/midterm-elections/portfolio/pnl?period=all` (2 reads).
- *Inspect:* the status (200, 403 with its `code`, or 409 with the count of
  `missingValuations`); the row count; per row, field presence, the sign and integrality
  of `quantity`, and whether `currentPrice` is null.
- *Artifact:* statuses, shapes, and only these relationships (computed locally, values
  not kept):
  - does `costBasis` equal `|quantity| × avgCost`;
  - does `marketValue` equal `|quantity| × currentPrice` or `|quantity| × (1 − currentPrice)`
    for a NO row;
  - does the sum of lot quantities equal `|quantity|`.
- *Changes:* each relationship that holds on a NO row is one observation for W5-c to
  W5-f. A 403 while pending informs A1-N4; a 409 informs OQ-D3.
- *Limits:* only rows that happen to exist. An empty list shows nothing about units.
- *Stop:* nothing to clean up.

**R4. Fills (priority 2; only if R2 found orders or R3 found holdings).**
- *Prerequisite:* R2.
- *Requests:*
  - `GET /tournaments/midterm-elections/portfolio/fills?limit=200`, walked until
    `hasMore: false` (cap 10 pages);
  - for one existing order ID: `GET /orders/{id}/fills?limit=200` (1 or more reads) and
    `GET /orders/{id}` (1 read).
- *Inspect:* per fill, `price` on the grid, its decimal places and null; `quantity`
  sign against `side`, and integrality; the order's `action` beside each fill's sign.
  Also the echoed `tournamentId`, `totalQuantityFilled`, `avgFillPrice`, `priceLimit`.
- *Artifact:* page records; per-fill booleans (on grid, integer, sign agrees with side);
  for a NO fill, whether `price` equals the order's `priceLimit` or its complement.
- *Changes:* one or a few observations for W4-a to W4-d, W6-a and W7-a. An echo of
  another tournament or null is a scope mismatch (A1 T17).
- *Limits:* fills of manual trades show the units of those trades only. Public tape rows
  are never used here.
- *Stop:* an incomplete walk is recorded as incomplete.

**R5. Settlements and fee transactions (priority 3).**
- *Prerequisite:* R1.
- *Requests:*
  - `GET /tournaments/midterm-elections/portfolio/settlements?limit=200`, walked to
    `hasMore: false` or `coverage.complete: false` (cap 5 pages);
  - `GET /tournaments/midterm-elections/portfolio/transactions?type=fee&limit=200`
    (1 page).
- *Inspect:* `coverage.complete` per page; whether any row exists; the presence of
  `shares`, `payout` and `type`; whether any `fee` event exists.
- *Changes:* PRD B (fees): a fee row is evidence that fees exist; no row in one page is
  not evidence that they do not. W5-h, if settlements exist.
- *Limits:* `complete: false` is documented as possible.
- *Stop:* nothing to clean up.

**R6. Collateral (priority 4).**
- *Requests:* `GET /portfolio/collateral?tournamentId=...` (1 read).
- *Changes:* an empty `data` at this time supports W8-g as unused.
- *Limits:* one time.

**R7. Quote precision sweep (priority 3).**
- *Requests:*
  - `GET /exchanges/prices?ids=...&tournamentId=...` for every exchange of the 237
    markets (3 reads of at most 100 IDs; the IDs come from the recorded market list or a
    fresh `GET /tournaments/midterm-elections/markets` walk of 3 reads);
  - `GET /markets/{id}/orderbook?tournamentId=...&depth=200` for about 5 markets,
    including one long shot and one near 0.5 (5 reads).
- *Inspect:* the count of off-grid values and the maximum decimal places for every price
  field; `asOf` null or not.
- *Artifact:* counts only, plus the request records. The bodies are public market data
  and can be committed as fixtures after the usual review.
- *Changes:* widens W1-c from one exchange to all of them at one time.
- *Limits:* REST only; one moment; no guarantee (Q-S7).
- *Stop:* nothing to clean up.

R1 to R5 also cover the coverage, 409 and projection checks: record `coverage` and
`projectedThroughSequence` exactly as sent and never compare them across endpoints
(A1 T43). Record each 409 body's shape. Never read `/account`.

### 3.4 Conditional write checks: separate review and execution

**Status: not approved.** These checks place and cancel real orders on the tournament.
Each needs Arnav's explicit decision before it is run, after reviewing this section on
its own. They are not prerequisites of each other except where stated. X5 is a real trade
and a distinct decision.

**Common design.**
- *Prerequisites:* R1 and R2 done; no open test order; Arnav present for the whole
  session; the trade key used from his own machine.
- *Size:* every order is `quantity: 1`. The most a buy of one share can cost is its
  price, which is below 1 SUSQie.
- *Price:* every price is on the 0.005 grid and far from the market. "Far" here means
  that the price would need a large move before it could match, not that it cannot
  match. **A far-from-market order can still fill,** including while the session is
  interrupted. A fill of one share bounds the money at risk, not the behaviour.
- *Keys:* each request's `idempotencyKey` is generated fresh, written down before
  sending, and reused only for an identical retry (practising hard rule 6).
- *Never:* an empty cancel-all body; a request without `tournamentId`; a market order; a
  sell (except optional X6).
- *Record:* the full response of every placement, including whether the canonical fields
  (`action`, `side`, `price`, `quantity`, `remainingQuantity`, `terminalReasonCode`) are
  present (W8-a).

**Choosing a price for a NO order (X2).** For a NO buy at request price `x` on an
exchange with YES best bid `b`:
- if SIG reads `x` as the NO price, the order rests while `x < 1 − b`;
- if SIG reads `x` as the YES price (NO price `1 − x`), it rests while `x > b`.

It is non-marketable under both readings only when `b < x < 1 − b`, which needs a
long-shot exchange (small `b`). The margins are `x − b` and `1 − b − x`. Example: with
`b = 0.05`, `x = 0.25` leaves margins of 0.20 and 0.70. `x = 0.5` cannot tell the two
readings apart. Arnav picks the exchange and `x` at the time, from a fresh book read, and
does not send if either margin is small.

**X1. Reservation and cash around one resting YES buy (priority 1 of 3.4).**
- *Informs:* OQ-C3 and W11, plus W8-a and W6.
- *Setup:* an exchange with a high YES price (best bid well above 0.5). A YES buy at an
  on-tick `x` far below the best bid, with no expiry.
- *Sequence:*
  1. R1 (balance A); R2 `status=open` walk (no test order).
  2. `POST /orders` (1 write).
  3. R1 (B); `GET /orders/{id}`; `GET /orders/{id}/fills` (expect the `tournamentId`
     echo and `totalQuantityFilled` 0); `GET /exchanges/{id}/orderbook?depth=200` (one
     more share at `x`).
  4. After about 60 s, R1 (C).
  5. `DELETE /orders/{id}` (1 write; record the `tournamentId` echo).
  6. R1 (D); `status=open` walk (no test order); `GET /orders/{id}` (`open: false`).

  About 12 reads and 2 writes.
- *Results:*
  - B = A − x, and D = A: one observation consistent with this resting order being
    deducted at its limit (OQ-C3);
  - B = A: no deduction visible at that moment. That could be no reservation or a lagging
    read, which C reduces but does not exclude;
  - anything else: unexplained, reported.
- *Limits:* it cannot show that admission uses `myBalance` (W11-e), and it says nothing
  about many orders or other times.
- *Abort:* the response is not `open: true` with `quantityTraded` 0, or any 5xx or
  timeout (see the abort plan).

**X2. NO request price and `priceLimit` (priority 2; only if D-1 asks for it).**
- *Informs:* OQ-W3 and OQ-W6.
- *Setup:* a long-shot exchange and an `x` chosen as above. A NO buy, quantity 1, no
  expiry.
- *Sequence:* R2 walk; `POST /orders`; `GET /orders/{id}`; book read at depth 200;
  `DELETE /orders/{id}`; R2 walk. About 5 reads and 2 writes.
- *Results:*
  - canonical `price` = `x`: the request was read side-relative; `1 − x`: read as a YES
    price;
  - `priceLimit` = `x` or `1 − x` gives its denomination (W6-a);
  - a YES-normalised book level at `1 − x` (if read side-relative) or `x` (if YES)
    corroborates;
  - canonical fields absent: placement not engine-routed in this response (W8-a); rely on
    `priceLimit` and the book.
- *Limits:* one order. Nothing about fills (W4) or positions (W5).
- *Abort:* any `quantityTraded` above 0 means a NO share is now held. Record it, do not
  sell, and Arnav decides whether to hold it to settlement.

**X3. Tournament-scoped cancel-all (priority 1 of 3.4; G-4).**
- *Informs:* PRD B (cancel-all in one write) and G-4.
- *Setup:* three far-from-market resting buys of quantity 1 on at least two exchanges,
  placed as three single orders (3 writes) or one batch (1 write, which also yields a
  batch response).
- *Sequence:*
  1. `status=open` walk (the three test orders listed; record their IDs).
  2. `POST /orders/cancel-all` with the body `{"tournamentId": "bda92870-621e-47b0-bc3c-3602c5c26f55"}`
     only (1 write).
  3. On 503: record `details.cancelled` (or its absence), wait for `Retry-After` or back
     off, and resend the identical request until 200. Stop after 5 attempts and escalate.
  4. `status=open` walk to `hasMore: false` (expect none of the three); `GET /orders/{id}`
     for each (`open: false`); `GET /orders/{id}/fills` for each (no fills); a book read.

  About 8 reads and 4 to 8 writes.
- *Results:*
  - 200 with `cancelled` 3 and an empty walk: one observation consistent with [CXL];
  - `cancelled` below 3 with 200: check fills and order states;
  - any test order still open after a 200: a contradiction. Cancel each with
    `DELETE /orders/{id}`, stop and report to SIG.
- *Limits:*
  - three orders, not hundreds;
  - a partial 503 cannot be produced on purpose;
  - nothing about an outage, or about orders in other contexts (do not create any to
    test this).
- *Abort:* the abort plan.

**X4. Expiry (priority 2 of 3.4; G-4, OQ-G1).**
- *Informs:* OQ-G1 (whether expiry is accepted and closes the order).
- *Setup:* one far-from-market buy of quantity 1 with `expirationDate` about 10 minutes
  ahead (UTC, ISO 8601 with milliseconds and `Z`).
- *Sequence:*
  1. Before expiry: the `POST /orders` response; `GET /orders/{id}` (the
     `expirationDate` echo, exact); `GET /markets/{id}/orderbook?tournamentId=...`
     (`nextExpiryAt`); `status=open` walk (listed).
  2. Two minutes after expiry: `status=open` walk (absent?); `status=expired` page
     (present?); `GET /orders/{id}` (`open`); a book read (depth gone?).
  3. Five minutes after expiry, if it is still listed open: cancel it (see the abort
     plan).

  About 8 reads and 1 or 2 writes.
- *Results:*
  - a 400 on placement: expiry rejected on this tournament (record the `code`);
  - listed as expired and closed after expiry: one observation that expiry closed this
    order within the observed delay;
  - still open past expiry: expiry is not a protection here.
- *Limits:*
  - **a far-from-market order not filling after expiry proves nothing about fill
    prevention**, because it would not have filled anyway;
  - the delay is bounded only by the read times;
  - nothing about load, scale or orders resting through our outage.

  The lifetime bound in G-4 needs Q-S13 as well.

**X5. One deliberate fill (separate real-trade decision; not a prerequisite).**
- *Informs:* W4, W5, W7, PRD B (fees) and W11 (balance change against cost).
- *Setup:* a marketable on-tick limit buy of quantity 1 at the current best ask of the
  side bought (never price 1), one side at a time. Arnav decides whether YES, NO or both,
  and on which exchange.
- *Sequence:* R1; R3; `POST /orders`; `GET /orders/{id}/fills`; the first page of R4;
  R3; R1; R5's transactions read with `type=trade,fee`.
- *Changes:* one real fill's `price`, `quantity` sign, `fillPrice`, `totalCost`, the
  position row and lots, the balance change, and any fee.
- *Limits:* one fill per side; no partial fills and nothing about fractional quantities.
  A share is then held, and closing it is another decision.

**X6. Close the X5 holding with a fully backed sell (optional, low priority).**
- *Informs:* W4-a (how a reducing fill is signed) and W8-d (a conventional close).
- *Do:* send it only if R3 shows the holding exactly. It is a sell, and is sized to the
  held quantity (hard rule 5).

**Abort and cleanup plan (every write check).**
1. *Triggers:*
   - any placement response other than the one expected;
   - any fill on an order meant to rest;
   - a 5xx, 409 or timeout on a placement;
   - a book move that brings a test order within a small margin;
   - loss of network or of the API.
2. *Unknown placement outcome:* resend the identical request with the same key once to
   learn the outcome (W10). Never send a new key.
3. *Cancel:* `POST /orders/cancel-all` with `{"tournamentId": "bda92870-621e-47b0-bc3c-3602c5c26f55"}`,
   repeated until 200 as documented. Then `DELETE /orders/{id}` for any test order still
   listed.
4. *Confirm:* a `status=open` walk to `hasMore: false` that lists no test order; each
   test order shows `open: false`; each test order's fills are recorded.
5. *If the API or network cannot be reached:*
   - nothing on our side protects the resting orders: they stay live and can fill;
   - retrying locally does nothing while SIG is unreachable;
   - try the same cancel-all from another network path;
   - if the SIG website offers cancellation of tournament orders (not verified), use it;
   - otherwise email SIG support with the order IDs and times;
   - record the time each order was last seen open. Exposure is bounded only by the
     orders' size (one share each).
6. *Close the session:* only after step 4 passes. Record the final balance relationship
   (R1).

## 4. Decisions for Arnav, in dependency order

None is selected here.

**1. Wire and cash evidence**
- **D-1.** Do W3-b to W3-d answer OQ-W3 for single and batch placement, or are Q-S8
  and/or X2 needed first?
- **D-2.** Run R0 to R7 now? Which of the section 3.0 redaction rules apply to
  committing their results?
- **D-3.** Ask SIG Q-S1 to Q-S14? How does a written SIG answer count for G-7, and where
  is it recorded?
- **D-4.** Run X1 to X4: whether, when, and with which exchanges and prices. Separately,
  X5 and X6.
- **D-5.** The source of tournament cash for G-1 (`myBalance` is the only candidate),
  after the evidence above.
- **D-6.** The executable rounding policy per side and action (W2-c), and whether edge
  thresholds apply before or after rounding.
- **D-7.** Whether to add the candidate questions to PRD section W:
  - **O1-N1:** how a reducing fill is signed, given that fills carry no `action`;
  - **O1-N2:** what happens to a resting sell after the holding shrinks;
  - **O1-N3:** whether a limit can fill worse than its limit;
  - **O1-N4:** the idempotency key namespace and what counts as the same "resolved
    payload";
  - **O1-N5:** the outcome of the "duplicate command" 409.

  Also whether to reword OQ-C2 (section 2.1).

**2. Commitment and grouping:** OQ-C1 (cost or value); OQ-C2 (resting sells); OQ-C3
(after X1 and Q-S2); OQ-C4 (groups and overlap); OQ-C5 (within-race netting).

**3. Equity and loss:** OQ-D1 to OQ-D5.

**4. Unknown-order recovery:**
- OQ-E1, including the handling of W10-d, W10-e, W10-j and W10-k, and a journal
  retention bound once Q-S11 is answered;
- OQ-E2.

**5. Outage bounds and order styles:** OQ-G1 (lifetime, after X4 and Q-S13); OQ-G2
(which styles may rest); the maximum resting commitment; OQ-E3.

**6. Configuration policy:** OQ-F1 to OQ-F3.

## 5. References

- [PRD](../PRD.md): sections W, B, C, D, E, F, G; gates G-1 to G-7; roadmap O1 and O2;
  provisional ceilings and working definitions.
- [A1 design note](account-state-contracts.md): scope binding (1.2), coverage (1.5),
  per-read matrix (2), cash (3), A1-N1 to A1-N5, Q-S1 to Q-S6, P1 to P6.
- [M1 design note](market-data-recovery.md): blockers and projection freshness (G-8).
- [`docs/api/openapi.json`](../api/openapi.json): every pointer above.
- [Fixtures README](../../tests/fixtures/sig/README.md) and each run's `fixtures.json`:
  labels, redactions and observations.
- Code on `main` at `efdb5a5` (unchanged in these files at `66bab31`), cited for
  dependencies only:
  - `shared/schemas.py`;
  - `engine/sig_client/account_reads.py`, `engine/sig_client/rest.py`,
    `engine/sig_client/wire.py`, `engine/sig_client/realtime_wire.py`,
    `engine/sig_client/realtime_token.py`;
  - `engine/account/snapshot.py`.
