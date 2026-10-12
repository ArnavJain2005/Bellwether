# Market-data recovery design (M1)

Oct 12, 2026 · Roadmap track M1 · Input to gate G-8 and open question OQ-E4 ·
Status: **draft for independent design review**

This note designs how a realtime market that the feed has marked not ready could be
shown safe to use again. It is a design only. It changes no code, no `shared/` schema
and no risk setting, and it does not pass gate G-8: approval of this note is one of
G-8's conditions, not the gate itself ([PRD, acceptance gates](../PRD.md#acceptance-gates)).

**Bottom line.** With the documented API and the evidence recorded so far, **no
realtime blocker can be cleared automatically with proof.** One outcome can be shown:
a settlement signal can be *confirmed* by the market-state read, which moves the
market to a terminal state that is never ready again. Every other blocker stays open,
and the affected market stays not ready for the life of the feed, as it does today.
Section 10 lists the exact guarantees that are missing as questions for SIG, and
section 11 lists the policy decisions that are Arnav's. The smallest useful M2 slice
(section 9) builds readiness results, recovery fencing and fail-closed lifecycle checks
without clearing anything.

## How to read this note

Every statement carries one of these labels where it matters:

| Label | Meaning |
| --- | --- |
| **[Documented]** | Stated by `docs/api/openapi.json`, cited by JSON pointer. Not necessarily observed on the tournament. |
| **[Observed]** | Seen in a committed recorded fixture under `tests/fixtures/sig/recorded/`. An observation is not a contract. |
| **[Code]** | What the code on `main` (`3e0fe27`) does, cited as `path:line`. |
| **[Proposed]** | A policy or mechanism this note proposes. Not implemented and not approved. |
| **[Unknown]** | Not settled by the spec or by evidence. Never resolved by inference here. |

Code references are `file:line` on `main` at `3e0fe27`. Short forms `realtime.py`,
`realtime_topic.py`, `realtime_wire.py`, `rest.py` and `wire.py` are files in
`engine/sig_client/`.

Scope: the tournament market topics (`tournament:{tournament_id}:market:{market_id}`)
and the REST reads that could recover them. Out of scope: account state (A1 to A3,
including the `user:{profile_id}` channel, fills, positions and our own settlements),
the socket adapter (M3), the order path (O1, O2), and Antigravity's venue coverage and
race-mapping work (S1-T5, R1).

### OpenAPI citations used below

All pointers are into [`docs/api/openapi.json`](../api/openapi.json).

| Alias | JSON pointer | Content |
| --- | --- | --- |
| [INFO] | `#/info/description` | Pagination and coverage contract; realtime overview; book-sync procedure |
| [RT] | `#/paths/~1realtime~1token/post/description` | Channel matrix, `market_batch`, revisions, `resyncRequired`, versioned books |
| [MKT] | `#/paths/~1markets~1{id}/get` | Market detail in an explicit tournament scope |
| [MKT-S] | `#/components/schemas/Market` | `status`, `settledWith`, `settledOn`, `contexts` |
| [CTX] | `#/components/schemas/MarketPricingContext` | Per-context `status`, `settledWith`, `settledOn` |
| [TMKTS] | `#/paths/~1tournaments~1{slug}~1markets/get` | Tournament market list (status per market) |
| [TMKT] | `#/paths/~1tournaments~1{slug}~1markets~1{id}/get` | Tournament market detail with embedded books |
| [MOB] | `#/paths/~1markets~1{id}~1orderbook/get` | Market orderbook, per-exchange `asOf` |
| [EOB] | `#/paths/~1exchanges~1{id}~1orderbook/get` | Exchange orderbook, `asOf` |
| [BV] | `#/components/schemas/BookVersion` | `{ sequence, at }`, null for database reads |
| [TAPE] | `#/paths/~1exchanges~1{id}~1trades/get` | Trade tape: `from` (parameters/2), `to` (parameters/3), opaque `cursor` (parameters/5) |
| [TAPE-COV] | `#/paths/~1exchanges~1{id}~1trades/get/responses/200/content/application~1json/schema/properties/coverage` | `complete`, `projectedThroughSequence` |
| [TRADE] | `#/components/schemas/ExchangeTrade` | `id`, `createdAt`, `price`, `size`, `side`, `volume`; no `sequence` |
| [PRICE] | `#/paths/~1exchanges~1{id}~1price/get` | `latestPrice`, best bid and ask; no version |
| [PH-COV] | `#/paths/~1exchanges~1{id}~1price-history/get/responses/200/content/application~1json/schema/properties/coverage/properties/projectedThroughSequence` | "complete through" wording (candles only) |
| [ORD-COV] | `#/paths/~1orders/get/responses/200/content/application~1json/schema/properties/coverage` | Same projection metadata on orders |
| [SETTLE] | `#/paths/~1tournaments~1{slug}~1portfolio~1settlements/get` | Our own settlement entries (an account read, A1) |

## 1. Existing behaviour

### 1.1 What "ready" means today

[Code] `RealtimeMarketFeed.is_ready(market_id)` (`engine/sig_client/realtime.py:416`) is
`_ready_now` (`engine/sig_client/realtime.py:576`): the feed is not closing, the market's
`ready` flag is set, **no blocker is outstanding**, the token is valid on the clock, and
no held book's known `nextExpiryAt` has passed. The `ready` flag is set only at the end
of a REST orderbook resync (`engine/sig_client/realtime.py:660`) when the resync answered
the latest trigger (`market.wanted == request`), the topic is subscribed, the token is
valid, no blocker is outstanding and the feed is not closing
(`engine/sig_client/realtime.py:649`-`659`). It is withdrawn, with a `MarketStale` event,
by any trigger (`_set_stale`, `engine/sig_client/realtime.py:552`), always before any book,
trade or settlement from the triggering message is published
(`engine/sig_client/realtime.py:539`-`548`).

### 1.2 Triggers and blockers

Every trigger is a `StaleReason` (`engine/sig_client/realtime_topic.py:98`). Only the six in
`BLOCKING_REASONS` (`engine/sig_client/realtime_topic.py:123`) become blockers.

| Trigger (`StaleReason`) | Raised by [Code] | Blocker? | What recovers it today [Code] |
| --- | --- | --- | --- |
| `SUBSCRIBED` | every SUBSCRIBED status, first join or re-join (`realtime.py:528`) | no | REST orderbook resync |
| `CHANNEL_ERROR`, `CHANNEL_TIMED_OUT`, `CHANNEL_CLOSED` | channel status (`realtime.py:531`) | no | re-join, then resync |
| `SOCKET_ERROR`, `SOCKET_CLOSED` | socket event, every market (`realtime.py:514`) | no | re-join, then resync |
| `TOKEN_REFRESH` | successful refresh, every market (`realtime.py:723`) | no | resync |
| `TOKEN_EXPIRED` | lifetime ran out (`realtime.py:733`) | no | refresh, then resync |
| `BOOK_DIRTY_EVENT` | standalone `book_dirty` (`realtime_topic.py:249`) | no | resync |
| `BOOK_EXPIRY` | a held book's `nextExpiryAt` passed (`realtime.py:537`, `realtime.py:757`, `realtime.py:644`) | no | a resync whose read is shown to follow the expiry (1.3) |
| `STOPPED` | `run()` ending (`realtime.py:463`) | no | nothing: the feed is over |
| `MARKET_SETTLED` | `marketSettled[]` item or standalone `market_settled` (`realtime_topic.py:322`, `realtime_topic.py:255`) | **yes** | nothing |
| `NULL_TRADE_SEQUENCE` | a trade with null `id` and `sequence` (`realtime_topic.py:326`) | **yes** | nothing |
| `REVISION_GAP` | `previousRevision` above the last accepted revision (`realtime_topic.py:312`) | **yes** | nothing |
| `RESYNC_REQUIRED` | a batch flagged `resyncRequired` (`realtime_topic.py:305`) | **yes** | nothing |
| `MALFORMED` | invalid JSON or shape, wrong market or tournament, bad book clock (`realtime_topic.py:275`-`295`) | **yes** | nothing |
| `MISSING_BOOK` | a named exchange without its book (`realtime_topic.py:329`) | **yes** | nothing |

[Code] Blockers are only ever added (`realtime.py:540`, `realtime.py:554`); no code path
removes one. A blocked market therefore stays not ready for the life of the feed, and
every later resync logs that it "stays not ready" (`realtime.py:651`).

### 1.3 Book-only recovery and expiry (kept as is)

[Code] The non-blocking triggers are recovered by one REST market orderbook read at
depth 200 (`realtime.py:614`), applied by `MarketTopic.apply_snapshot`
(`realtime_topic.py:397`). The rules this design preserves unchanged:

- A versioned REST book replaces a held book only when strictly newer; an unversioned one
  replaces it unless a push landed after the read started (`realtime_topic.py:462`).
- A side holding exactly `depth` levels is marked incomplete (`realtime_topic.py:449`).
- **Expiry fails closed.** A held book whose known `nextExpiryAt` has passed keeps the
  market not ready (`realtime.py:582`, `realtime_topic.py:484`). A REST read clears it
  only when shown to have been made at or after the expiry: by SIG's own `asOf.at` for a
  versioned book, or, for an unversioned book, only through an operator-configured bound
  `expiry_skew_margin_s` on how far the local clock may run ahead of SIG's
  (`realtime_topic.py:509`). The bound defaults to `None` (unknown,
  `realtime.py:300`), and then an unversioned read never clears a known expiry. A read
  that replaces a book without covering its expiry carries the expiry forward
  (`realtime_topic.py:441`).
- A read that completes after a held book expired re-triggers `BOOK_EXPIRY` instead of
  publishing ready (`realtime.py:644`).

These are book-level rules. Nothing in this note relaxes them, and no recovery read
proposed here is a book read or touches expiry state.

### 1.4 Durable lost-state blockers

The six blockers stand for information an orderbook read cannot supply:

- `MARKET_SETTLED`: the market's lifecycle changed. [Documented] treat the signal "as an
  invalidation hint and refetch the authoritative market state" ([RT]).
- `NULL_TRADE_SEQUENCE`: a trade that "cannot be ordered against other reads"; "the client
  must resynchronize that exchange's price and trade tape over REST" ([RT]). [Code] none of
  that batch's trades are emitted (`realtime_topic.py:333`).
- `REVISION_GAP`, `RESYNC_REQUIRED`, `MALFORMED`, `MISSING_BOOK`: batch content was missed
  or discarded, so a settlement or an unsequenced trade may be hidden in it
  (`realtime_topic.py:120`-`132`).

### 1.5 Restart discards state; it does not prove recovery

[Code] A new `RealtimeMarketFeed` builds new `_Market` and `MarketTopic` objects
(`realtime.py:386`): no blockers, no last revision, no trade-id memory. Its first
SUBSCRIBED status triggers an orderbook resync, and the market is published ready when
that read completes. **A restart therefore clears every blocker without any evidence.**
Nothing missed before or during the restart is reconciled. This is exactly the
behaviour the PRD rules out as recovery ([PRD, market-data blockers](../PRD.md#risk-management)).

### 1.6 Weaknesses found while reviewing the existing behaviour

These are facts about `main`, recorded so that M2 and its reviewers do not miss them.
None is changed by this note.

1. **Unobserved windows are book-only.** Reconnects, socket and channel errors, token
   refresh and expiry are recovered by an orderbook read alone. [Documented] Delivery "has
   no replay buffer" ([INFO]). A batch missed during the disconnection is detected only
   when a *later* batch on the same topic arrives with a higher `previousRevision`
   ([RT]). On a quiet topic nothing reveals the loss.
2. **Initial readiness reads no market state.** A market that settled or closed before
   the subscription is published ready after its first orderbook read.
3. **Standalone events carry no revision.** [Documented] "Some admin and settlement
   actions still send `book_dirty`, `market_settled` ... individually, with the same
   payload as the matching batch item" ([INFO], [RT]). [Code] Those payloads have no
   `delivery` (`realtime_wire.py:166`, `realtime_wire.py:175`). [Unknown] whether the same
   settlement is also carried in a revisioned `market_batch`. If it is not, a missed
   standalone `market_settled` cannot be detected by any revision check.
4. **A duplicate batch's arrays are ignored**, including `marketSettled`
   (`realtime_topic.py:309`). That is the documented rule ([RT]). [Unknown] whether a topic's
   revision can ever go backwards (for example across a SIG deploy). If it did, every later
   batch would look like a duplicate, and its trades and settlements would be dropped with
   no blocker. Books would still apply, because they apply by version
   (`realtime_topic.py:298`).
5. **A gap batch's own arrays are discarded**, settlements included
   (`realtime_topic.py:314`-`316`). This is safe today, because `REVISION_GAP` blocks
   anyway.
6. **Market closure has no realtime signal.** [Documented] `marketSettled` is sent "when a
   market scope settles or refunds" ([RT]); nothing is documented for `open` → `closed`.
   [Unknown] whether SIG rejects orders on a closed or settled market, and with which code.
7. **Trade de-duplication memory is bounded** at 4,096 ids per topic
   (`realtime_topic.py:93`, `realtime_topic.py:382`).

## 2. Evidence matrix

### 2.1 Principles [Proposed]

- **E1. Evidence is blocker-specific.** A read clears only the blocker whose loss it
  covers. An orderbook read never clears a blocker (as today), and a restart never does.
- **E2. All four properties or nothing.** Evidence must be in scope (the configured
  tournament, checked by the client where SIG echoes it), complete (every page of a fixed
  window), fresh (shown to reflect every event the blocker may have hidden) and consistent
  (agreeing with what the feed itself received). Each property must rest on a documented
  guarantee or logged evidence, never on inference.
- **E3. Unproven reads may only restrict.** A read whose freshness is not established can
  add a blocker or confirm a terminal state. It can never clear a blocker or publish ready.
- **E4. No atomic snapshot across endpoints.** Reads of different endpoints, and pages of
  one walk, are separate observations at different times. Nothing here treats them as one
  snapshot.

### 2.2 Candidate authoritative reads

| Read | Client method [Code] | Scope evidence | Freshness metadata | Completeness metadata |
| --- | --- | --- | --- | --- |
| Market detail [MKT] | `get_market` (`rest.py:566`) | Sends `tournamentId`; checks exactly one tournament context naming the configured slug and UUID, and that the top-level status, settlement and prices agree with it (`rest.py:587`) | **None.** No version, no `asOf`. `settledOn` is a settlement timestamp, not a read time ([MKT-S], [CTX]) | Single object |
| Tournament market list [TMKTS] | `list_all_tournament_markets` (`rest.py:544`) | Addressed by slug after the client verifies slug and UUID; rows carry no tournament echo | None | Cursor walk; pages are separate reads (E4) |
| Tournament market detail [TMKT] | none | Addressed by slug | Per-exchange book `asOf` [BV]; **no version for `status`** | Single object |
| Market orderbook [MOB] | `get_market_orderbook` (`rest.py:671`) | One tournament context, checked | Per-exchange `asOf` [BV], comparable with pushed books | Depth-capped sides |
| Exchange price [PRICE] | none (bulk: `get_exchange_prices`, `rest.py:613`) | `tournamentId` sent | None | Single object |
| Trade tape [TAPE] | `list_all_exchange_trades` (`rest.py:888`) | `tournamentId` sent; **no tournament echo** | Per-page `coverage.projectedThroughSequence`, "latest trading-engine sequence reflected by the reporting projection" ([TAPE-COV]); [INFO]: "at response time" | Per-page `coverage.complete`, "always true because cursor pagination exposes the full requested trade history without window truncation" ([TAPE-COV]) |
| Our settlements [SETTLE] | none | Tournament slug | `coverage.complete` only, about a 20-page walk budget | Only *our* entries; an account read (A1) |

[Code] The tape walk returns all pages or an error, never a partial tape
(`rest.py:888`); it rejects `coverage.complete: false` (`rest.py:869`), a repeated cursor,
an item on two pages, a page cap (`rest.py:965`), and a page whose market or window
differs from the first (`rest.py:910`). It keeps each page's coverage exactly as sent
(`rest.py:209`).

### 2.3 Per blocker

#### `MARKET_SETTLED`

- **May have lost:** nothing about the signal itself, which was received; the market's
  lifecycle in the tournament scope changed to settled or refunded.
- **Candidate read:** market detail [MKT] with the configured `tournamentId`; field
  `contexts[0].status`, `settledWith`, `settledOn` ([CTX]). The market list [TMKTS] carries
  the same fields without a context echo.
- **Scope:** checked by the client (`rest.py:587`).
- **Completeness:** a single object.
- **Freshness:** [Unknown] whether market detail reflects a settlement no later than the
  `marketSettled` signal (read-after-signal consistency). No version exists to show it.
- **Consistency:** a read showing `status: settled` with the signal's `settledWith`
  agrees with the signal. [Unknown] whether the two `settledWith` values use the same
  label (outcome label or `REFUND`, [RT]; [MKT-S] gives `"YES"` as its example).
- **Unproven:** freshness; label equality; whether a settled market can be corrected or
  reopened ([SETTLE] lists a `correction` type, which concerns our entries, not market
  state).
- **Verdict [Proposed]:** never cleared to ready. A read showing `settled` with an equal
  `settledWith` *confirms* the blocker as terminal (the market is never ready again). A
  read showing `open`, or a different label, is inconclusive: it cannot tell a stale read
  from a reversal, so the blocker stays open.

#### `NULL_TRADE_SEQUENCE`

- **May have lost:** the trade's order relative to other events, and its identity: with
  `id` null it cannot be matched or de-duplicated. [Code] every other trade in that
  batch was dropped too (`realtime_topic.py:333`). The batch's books were applied, so the
  book state is not lost (`realtime_topic.py:298`).
- **Candidate reads:** the trade tape [TAPE] and the exchange price [PRICE], which [RT]
  names.
- **Scope:** `tournamentId` sent; the tape echoes no tournament (`rest.py:9`-`11`).
- **Completeness:** a full cursor walk of a fixed window.
- **Freshness:** none usable. The trade has no sequence, so no projection checkpoint can
  be compared with it, whatever Q5 says about tape ids.
- **Consistency:** none. [Unknown] whether such a fill appears on the tape, under what
  `id`, and whether its `createdAt` equals the realtime `executedAt`. Matching by price,
  size and time would be inference.
- **Unproven:** everything needed. [PRICE] carries no version either.
- **Verdict:** cannot be cleared. Stays open.

#### `REVISION_GAP`

- **May have lost:** the content of the missed revisions (last accepted, `previousRevision`]
  and, because the gap batch's arrays are discarded, of (`previousRevision`, `revision`]:
  trades, `marketSettled` and book signals. Books are recovered by the orderbook read.
- **Candidate reads:** market state for settlement (as for `MARKET_SETTLED`); trade tape
  for trades.
- **Scope:** as above.
- **Completeness:** the tape needs a window that covers the lost revisions. [Unknown] the
  time span of those revisions: batches carry no time bounds, `delivery` carries engine
  sequence bounds that are documented as "provenance, not a topic-local gap counter"
  ([RT]) and are deliberately not modelled (`realtime_wire.py:108`-`111`), and local receive
  times do not bound them because "delayed batches can be delivered later" ([RT]).
- **Freshness:** settlement: none (as above). Trades: see section 3; no documented
  comparison links the lost revisions to `projectedThroughSequence`.
- **Consistency:** trades recovered from REST could be matched to realtime trades by `id`
  ([RT]: the same id the tape gives), but the lost trades were never received, so there
  is nothing to match.
- **Unproven:** the window, settlement freshness, projection freshness.
- **Verdict:** cannot be cleared. Stays open.

#### `RESYNC_REQUIRED`

- **May have lost:** [Documented] a `resyncRequired` batch keeps "its `books`, then as many
  of its `trades` as fit, in order, and no `bookDirty` or `marketSettled`" ([RT]): every
  settlement signal in its window and an unknown tail of its trades.
- **Candidate reads, scope, completeness, freshness, consistency:** as for `REVISION_GAP`
  over (`previousRevision`, `revision`]. The partial trades it carried are not emitted
  (`realtime_topic.py:305`-`308`) and do not prove the tail.
- **Verdict:** cannot be cleared. Stays open.

#### `MALFORMED`

- **May have lost:** the whole message. Two cases [Code]: `delivery` parsed (its revision
  is accepted when newer, `realtime_topic.py:291`-`295`), so the lost window is known; or
  `delivery` did not parse (`realtime_topic.py:283`-`285`), so the message cannot be
  attributed to any revisions. Malformed standalone `book_dirty` and `market_settled`
  payloads also land here (`realtime_topic.py:252`, `realtime_topic.py:258`); a malformed
  standalone `market_settled` may be a missed settlement.
- **Candidate reads:** as for `REVISION_GAP`, plus market state for the standalone case.
- **Unproven:** as for `REVISION_GAP`; for an unattributable message, even the window.
- **Verdict:** cannot be cleared. Stays open.

#### `MISSING_BOOK`

- **May have lost:** the batch was received and parsed but broke the documented rule that
  every exchange named by `trades` or `bookDirty` comes with its book ([RT]). [Code] its
  trades are discarded; its settlements are still reported and raise `MARKET_SETTLED`
  (`realtime_topic.py:322`-`332`). Lost: the trades of (`previousRevision`, `revision`]
  and trust in the batch.
- **Candidate reads:** orderbook (books, already done by the resync); trade tape.
- **Unproven:** as for `REVISION_GAP` (trades only).
- **Verdict:** cannot be cleared. Stays open.

### 2.4 Summary

| Blocker | Best documented evidence | Clearable today? | Missing (section 10) |
| --- | --- | --- | --- |
| `MARKET_SETTLED` | Market detail showing the same settlement | **No**; confirmable as terminal | Q1, Q2, Q9 |
| `NULL_TRADE_SEQUENCE` | Tape and price | **No** | Q3, Q4, Q5, Q6, Q11 |
| `REVISION_GAP` | Market detail; tape over an unknown window | **No** | Q1, Q3, Q4, Q6, Q7, Q8 |
| `RESYNC_REQUIRED` | As for a gap | **No** | as above, Q12 |
| `MALFORMED` | As for a gap; unattributable if `delivery` did not parse | **No** | as above |
| `MISSING_BOOK` | Tape | **No** | Q3, Q4, Q6, Q7 |

## 3. Freshness and checkpoints

### 3.1 The counters are different things

| Counter | Where | Scope | Documented meaning | Documented comparisons |
| --- | --- | --- | --- | --- |
| `delivery.revision`, `previousRevision` | every `market_batch` | one topic | The batch covers the topic's revisions after `previousRevision` through `revision`; duplicate and gap rules ([RT]) | Only with other revisions of the same topic |
| `delivery.sourceSequenceFrom`, `...Through` | every `market_batch` | engine | "Provenance, not a topic-local gap counter" ([RT]); not modelled [Code] | None |
| Book `asOf { sequence, at }` | pushed books; REST books [MOB], [EOB], [TMKT] | one exchange | "The last engine event applied to the book" and "the engine clock when the book was read" ([BV]); null for a database read | Pushed and REST books of the same exchange: "compare by `sequence`, then `at`" ([MOB], [EOB], [RT]) |
| Trade `sequence` | realtime `trades[]` | engine | "Its engine event sequence" ([RT]) | None documented. [RT] says a pushed book "can be newer than the batch's own trades", which suggests an ordering but does not define a comparison |
| Trade `id` | realtime `trades[]`; tape [TRADE] | trade | "`id` (a string) and `sequence` (a number) identify the match: its engine event sequence, which is also the same id `GET /exchanges/{id}/trades` gives the trade, so one trade seen on both can be deduplicated and ordered" ([RT]) | Identity, for de-duplication. The sentence can be read as saying the tape `id` *is* the sequence, but it does not say so plainly, and the tape has no `sequence` field ([TRADE]). Treated as identity only until Q5 is answered |
| `projectedThroughSequence` | `coverage` on tape pages [TAPE-COV], orders [ORD-COV], fills | reporting projection | "Latest trading-engine sequence reflected by the reporting projection" ([TAPE-COV]); "the newest engine event reflected by the read models at response time" ([INFO]) | None documented |
| Cursors | every paginated read | one walk | "Opaque" ([INFO], [TAPE] parameters/5) | None. Opaque; never parsed, ordered or compared |

[Proposed] Comparisons are made only where the table says they are documented: topic
revisions with revisions of the same topic, and book versions with book versions of the
same exchange. Everything else stays uncompared until SIG documents the relationship.
[Code] The feed already follows this: sequences are never compared with revisions
(`realtime_topic.py:40`-`42`), and `projectedThroughSequence` is never compared with any
realtime value (`rest.py:13`-`17`).

### 3.2 What a projection checkpoint would have to guarantee

To show from a trade-tape walk that no trade is missing, all of these would need to be
documented [Unknown for each]:

1. `projectedThroughSequence` and realtime trade `sequence` (or book `asOf.sequence`, or
   `delivery.sourceSequenceThrough`) are values of one comparable counter (Q3).
2. A checkpoint `N` means the tape is **complete through** `N`: every trade with sequence
   at or below `N` is on it. [PH-COV] uses exactly that wording for candles ("complete
   through", the lower of two positions); [TAPE-COV] says only "reflected by", which does
   not state prefix completeness (Q4).
3. Which trades the window must contain: the time span or sequence span of the revisions
   at issue (Q8), and whether `createdAt` on the tape can differ from `executedAt` in the
   realtime trade (Q6).

None of these is documented, so no tape walk is used as recovery evidence in M2's first
slice.

### 3.3 A past window does not certify freshness

- [Documented] `coverage.complete` is "always true because cursor pagination exposes the
  full requested trade history without window truncation" ([TAPE-COV]). It is about
  pagination, not about whether the projection has caught up.
- [Documented] `projectedThroughSequence` is a value "at response time" ([INFO]). Each
  page of a walk is read at a different time; a walk is not a snapshot (E4).
- A window whose `to` lies in the past can still gain rows: a trade executed inside it
  but projected late appears on a later read. A past `until` therefore says nothing about
  freshness on its own, and neither does a complete walk of it.

### 3.4 What the recorded sample shows and does not show

[Observed] Capture `20261011T211201Z-12ab20` read the tape of exchange 947 over the fixed
window `[2026-10-01T00:00:00.000Z, 2026-10-10T00:00:00.000Z)`, page limit 50, three pages,
between about 21:12:22Z and 21:12:34Z on Oct 11:

- **It is truncated.** All three pages report `hasMore: true`; the third page's cursor was
  never followed. Nothing is known about older trades in the window
  ([fixture README](../../tests/fixtures/sig/README.md)).
- Each page's `coverage` is `complete: true` with `projectedThroughSequence` `133949521`,
  `133950141` and `133950511` in page order. **The checkpoint advanced during the walk**
  (by 990 in about eight seconds). That shows the projection moving while the walk ran.
  It does not show that the projection had caught up with the engine for this exchange,
  and it does not show what the checkpoint counts.
- The 150 trade `id`s are digit strings from `114373466` (newest, `2026-10-09T22:33:16.692Z`)
  down to `108107998` (oldest, `2026-10-09T09:43:11.765Z`), decreasing with `createdAt` in
  this sample. Book versions for the same exchange read on Oct 9 were in the same range:
  `109392279` (`2026-10-09T12:22:56`, capture `fa008a`) and `112607913` (`19:00:47`,
  capture `fb88c9`), and the orders page read just after the latter reported
  `projectedThroughSequence` `112608652`.
- These magnitudes are **consistent with** one engine-wide counter shared by trade ids,
  book versions and checkpoints. Consistency in a few samples is not a documented
  comparison, and this note does not use it (Q3, Q5).

### 3.5 Clocks

[Code] The feed uses the local monotonic clock to order its own requests and the local
wall clock only through the expiry rules in 1.3. [Proposed] Recovery evidence never
compares a local wall-clock time with a SIG timestamp (`asOf.at`, `executedAt`,
`createdAt`, `settledOn`, `marketSettled.at`). Local times may order *our* actions
(a read started after a blocker was raised); they never show what SIG had done by then.
[Unknown] whether `asOf.at` (the engine clock) and the reporting timestamps share a clock
(Q15). The existing expiry behaviour, including the unknown skew bound, is unchanged.

## 4. Events during recovery

### 4.1 Model [Proposed]

- **Blocker instance.** Each blocker becomes a record, not a set member: a local id, its
  `StaleReason`, the local monotonic time it was raised, the market's generation at that
  moment, the revision interval it concerns when known (none for an unattributable
  `MALFORMED`), the triggering item when one exists (for example the `marketSettled`
  payload), its state (`open` or `terminal`) and the evidence collected for it. Two
  settlements are two instances. `blockers(market_id)` keeps returning the set of open
  reasons, so existing callers are unaffected.
- **Generation.** Each market has a counter, starting at 0, raised synchronously by every
  event in 4.2. It generalizes the existing fence for book resyncs (`request =
  market.wanted`, `realtime.py:609`, checked at `realtime.py:649`).
- **Recovery attempt.** One per market at a time. It records the generation and the set of
  open blocker instances when it starts, then makes its reads. It may commit only under
  4.3. An attempt that loses the race is discarded, and the next one starts from scratch
  (under the bounds in section 5).

### 4.2 What raises the generation [Proposed]

| Event | Why it invalidates an attempt |
| --- | --- |
| Any new blocker instance, including one carried by a duplicate or late batch (4.4) | The attempt's target set is out of date |
| SUBSCRIBED (re-join), socket or channel error or close | A new unobserved window has opened (1.6, item 1) |
| Token refresh or expiry | Documented to require resynchronization ([RT]) |
| A lifecycle read showing a status different from the last one seen | The market's state changed under the attempt |
| A suspected revision anomaly (4.4) | The topic's continuity is in doubt |
| `aclose()` | Nothing may commit after closing begins (as today, `realtime.py:617`, `realtime.py:658`) |

Not raised by: a normal, contiguous batch with sequenced trades and complete books; a
standalone `book_dirty`; a book expiry. These change books, and books are governed by the
existing book-readiness rules, which are checked again before any ready is published.

### 4.3 Commit conditions [Proposed]

A recovery attempt may change a blocker instance only if, checked synchronously in one
step with no `await` between the check and the change:

1. the feed is not closing and the attempt was not cancelled or past its deadline;
2. the market's generation equals the attempt's starting generation;
3. the instance is still `open`;
4. every read in the attempt was **started** after the instance was raised and after the
   attempt began (ordering of our own requests on the monotonic clock; no claim about
   SIG's clock), and none is reused from an earlier attempt;
5. every read succeeded and passed the client's scope checks;
6. the blocker-specific predicate (section 2) holds using only documented comparisons.

Committing changes the blocker instance only. It never sets `ready` directly. Readiness is
then re-evaluated from scratch by the existing rule: subscription, token, every book
version and expiry rule, and no open blocker. [Code] The existing book resync already
refuses ready when any blocker is outstanding (`realtime.py:651`).

**In M2's first slice the only change a commit can make is `open` → `terminal` for a
`MARKET_SETTLED` instance confirmed by market state.** No predicate that clears a blocker
is implemented until section 10's questions are answered or Arnav decides otherwise
(section 11).

### 4.4 How concurrent events supersede recovery [Proposed]

- **Concurrent pushes.** Books keep applying by version while an attempt runs, as today.
  A push that raises a blocker raises the generation; the attempt cannot commit.
- **New blockers.** As above. The new instance is included in the next attempt.
- **Reconnects.** Every SUBSCRIBED raises the generation and opens an unobserved window
  (decision D-1 in section 11 settles whether that window blocks).
- **Revision resets.** [Unknown] whether they occur (Q7). None is detectable from
  documented fields: the next batches simply look like duplicates. Two fail-closed
  detectors are proposed for duplicate batches, whose arrays are otherwise ignored:
  a `marketSettled` item or a null-sequence trade in a duplicate raises the corresponding
  blocker (redundant but harmless after a genuine retried send, which repeats content
  already seen); a sequenced trade `id` in a duplicate that the topic has never seen
  raises a new reason, `REVISION_ANOMALY`. A genuine retried send repeats trades
  already accepted, so an unseen id appears only when the original batch was discarded
  (the market is then already blocked) or the id has left the 4,096-id memory (a false
  positive that blocks, never one that clears).
- **Closing.** Two meanings:
  - *Feed closing* (`aclose()`): the attempt is cancelled, nothing commits, and nothing
    is published, as for book resyncs today (`realtime.py:617`).
  - *Market closing*: [Documented] no realtime signal exists for `open` → `closed` (1.6,
    item 6). A lifecycle read showing `closed` or `settled` raises a new terminal reason,
    `LIFECYCLE_NOT_OPEN`, and raises the generation.

### 4.5 Obsolete responses [Proposed]

A response belonging to an attempt that can no longer commit is never used to publish
ready or to change a blocker instance. Under E3 it may still restrict: if it shows the
market `settled` or `closed`, it raises `LIFECYCLE_NOT_OPEN` (a new instance, which raises
the generation again). It is logged with its attempt id and generation.

### 4.6 No snapshot across endpoints

Market detail, the orderbook and the tape are read separately (E4). [TMKT] returns status
and books in one response, but nothing documents that its `status` was read at its books'
`asOf`. A single response is therefore not treated as a consistent snapshot either.

## 5. Recovery bounds [Proposed]

Values marked *configurable* are client policy for Arnav to set; this note does not choose
them.

- **Fixed windows.** Any tape walk passes both `since` and `until` (as fixed instants).
  [Code] An omitted `until` means "now" and the walk fails if it moves between pages
  (`rest.py:888`-`904`). No tape walk runs in M2's first slice (3.2).
- **Page caps.** A walk that hits `max_pages` (default 50, `rest.py:145`) fails without a
  partial result. The blocker stays open; the attempt is retried later with the same cap.
  A cap is never raised automatically to make a walk succeed.
- **Duplicate ids and cursors.** [Code] A repeated cursor, an item on two pages, a page
  with a different market or window, or a page reporting `complete: false` fails the walk
  (`rest.py:965`, `rest.py:869`). The attempt fails; the blocker stays open.
- **Missing data.** An omitted `coverage`, a null `projectedThroughSequence`, a null trade
  `price`, a null `latestPrice` or a missing context is absent evidence. It never satisfies
  a predicate.
- **503 and 429.** [Code] Each logical read already retries 429 and 503 within its own
  deadline, honours `Retry-After` as a floor and imposes 429 cooldowns on the shared
  limiter (`rest.py:30`-`56`). A read that still fails fails the attempt. [Documented] 503 on
  [TAPE] means "authoritative reporting is temporarily unavailable"; on [MOB] "the
  authoritative market orderbook is temporarily unavailable".
- **Deadlines.** [Code] The client has a deadline per logical read but none per
  collection (`rest.py:36`-`38`). Each recovery attempt therefore gets its own overall
  deadline (*configurable*) on the injected monotonic clock. An attempt past its deadline
  is cancelled and commits nothing.
- **Cancellation.** Cancelling an attempt (deadline, `aclose()`, superseding generation)
  propagates `CancelledError`; nothing is committed and no event is published.
- **Shared rate budget.** All reads share the account limiter (80 of 100 reads a minute).
  [Code] The feed already gates orderbook resyncs at 40 starts a minute
  (`realtime.py:291`). Recovery reads get their own gate (*configurable*, like the resync
  gate) so that they cannot starve account reconciliation or resyncs, and the shared
  limiter still has no priorities. Note the scale: after a reconnect the existing resync
  alone needs one read per market (237 for every market). Where a lifecycle check covers
  many markets, it uses the market list [TMKTS] (three pages for 237 markets) rather than
  one market-detail read per market.
- **Retry scheduling.** One attempt per market at a time; triggers during an attempt
  coalesce into one follow-up (as for resyncs). Failed attempts back off exponentially with
  jitter and a floor from any `Retry-After` (*configurable* base and cap). After a
  *configurable* number of failed attempts the market is reported as `escalated` in its
  readiness result: still not ready, still retried at the capped interval, and visible to
  the operator. Escalation never clears anything.
- **Partial or stale evidence never clears a blocker.** This follows from E2, E3 and 4.3.
- **Expiry and clock skew are unchanged.** No recovery read is a book read. A recovery
  attempt that completes after a held book's `nextExpiryAt` may still commit its blocker
  change (its evidence does not concern that book), but readiness is not published until
  a book read covers the expiry under the existing rules, which with the skew bound unknown
  means a versioned read made at or after it (example F-6).

## 6. Engine readiness

### 6.1 Proposed per-market result [Proposed]

An engine-internal, immutable `MarketReadiness` value, built on request from the feed's
state. It lives in `engine/`; no `shared/` schema is proposed (the PRD reserves readiness
schemas for Arnav's approval).

| Field | Content |
| --- | --- |
| `market_id` | The market |
| `status` | `READY`, `NOT_READY` or `TERMINAL` |
| `generation` | The market's generation when evaluated |
| `evaluated_mono`, `evaluated_utc` | Local monotonic time (for ordering) and local UTC time (for display only) |
| `reasons` | Human-readable reasons, one per failing condition, for example "blocker: revision gap (revisions 812–815) open; missing Q1, Q3, Q4" |
| `transport` | Subscribed; token valid |
| `books` | Per exchange: version or null, source, complete, expiry known, outstanding expiry |
| `continuity` | Last accepted revision; open unobserved windows (cause and time) |
| `lifecycle` | Last lifecycle read: status, `settledWith`, `settledOn` as sent, when our read started and finished (monotonic), its generation, and the fixed note "unversioned" |
| `blockers` | Every instance: reason, raised at, generation, revision interval, state, evidence collected, missing guarantees (question ids) |
| `escalated` | True after the configured number of failed attempts |
| `does_not_cover` | The fixed statement that this result says nothing about account state, orders, risk, configuration or reference data |

- `READY` exactly when today's `_ready_now` holds, no blocker instance is open or
  terminal, a lifecycle read made in the current generation showed the market `open`
  (decision D-3), and the unobserved-window policy (D-1) is met.
- `TERMINAL` when any instance is terminal (settlement confirmed, or the market was
  seen closed or settled). Recovery stops for that market.
- `NOT_READY` otherwise.

### 6.2 Its role in engine readiness

- The PRD's readiness check requires "every market to be traded is ready in the realtime
  feed with no outstanding blocker" ([PRD, risk management](../PRD.md#risk-management)).
  `MarketReadiness` is the evidence for that one item. It is necessary for a new order in
  that market and never sufficient.
- It does not claim, and must not be read as claiming, tournament cash, positions, open
  orders, unresolved idempotency keys, local-versus-SIG agreement, reference-data
  freshness, configuration validity or rate budget. Those are A1 to A3, O2, S1 and C1.
  **Feed readiness alone never authorizes trading.**
- [Proposed, for O2 and S1] The order path re-reads the market's readiness immediately
  before sending, and refuses if the generation differs from the one its decision used.
- A `READY` result means "no loss detected, plus a recent lifecycle read showing the
  market open". It does not mean nothing was lost: delivery is best-effort ([RT]), and a
  missed standalone `market_settled` is undetectable (1.6, item 3).

### 6.3 Protective cancellation stays available

Protective cancellation is not gated on `MarketReadiness`. A market that is `NOT_READY`,
`TERMINAL` or escalated may still have resting orders, and cancelling them must remain
possible, scoped to the configured tournament, within the rate budget and audited
(`CLAUDE.md` hard rule 3). Nothing in this design blocks a cancel, and a cancel path never
places or enlarges an order. No cancel path exists yet.

## 7. Failure examples

Times are seconds on the local monotonic clock. `g` is the market's generation.

**F-1. Late projection.** t=0 a batch carries a trade with null `sequence`;
`NULL_TRADE_SEQUENCE` opens, g=1. t=5 a hypothetical tape walk over a fixed past window
finishes: three pages, `hasMore` false on the last, `projectedThroughSequence` rising page
by page. The fill is not on it. t=40 the same window, read again, contains one more row.
The first walk was complete and its window was in the past, yet it was not fresh. Nothing
could have told the two apart, which is why no tape walk clears anything (3.2, 3.3).

**F-2. Settlement during a lifecycle read.** g=4, a `REVISION_GAP` instance is open.
t=0 attempt A starts (g=4) and requests market detail. t=0.3 a batch carries
`marketSettled`; `MARKET_SETTLED` opens, g=5. t=0.6 the response arrives showing `open`.
A cannot commit (g changed). Under E3 an `open` read restricts nothing, so it is only
logged. t=2 attempt B starts (g=5); t=2.4 market detail shows `settled` with the same
`settledWith`; B commits `MARKET_SETTLED` → `terminal`. Result: `TERMINAL`. The
`REVISION_GAP` instance stays open; it no longer matters for trading, but it is never
recorded as cleared.

**F-3. Truncated history.** A tape walk of a busy exchange reads three pages, each with
`hasMore: true`, and reaches its page cap (as the recorded sample did under a three-page
cap, 3.4). The client raises a pagination error and returns no partial tape. The attempt
fails, the blocker stays open, and the next attempt waits for the backoff.

**F-4. Restart.** t=0 `REVISION_GAP` opens on market 258. t=60 the process restarts.
*Today:* the new feed has no blockers; at t=63 its first orderbook read publishes 258
ready. Nothing was reconciled (1.5). *Proposed (D-4):* the open instance and the last
accepted revision are persisted and reloaded, so 258 is still blocked after the restart;
the downtime is recorded as an unobserved window, and the first batch after the restart
either shows continuity (`previousRevision` at or below the persisted revision) or opens a
new `REVISION_GAP`.

**F-5. Stale generation after a reconnect.** g=7, attempt A (g=7) requests market detail.
t=1 the socket closes; every market's g rises (258 to g=8). t=3 the re-join; resyncs and
a new attempt B (g=8) start. t=4 A's response arrives showing `settled`. A cannot commit,
but under 4.5 it restricts: `LIFECYCLE_NOT_OPEN` opens (g=9), which also makes B's commit
impossible. The next attempt confirms it. If A's response had shown `open`, it would have
been logged and ignored.

**F-6. Expiry crossed during a recovery read.** A pushed book for exchange 947 carries
`nextExpiryAt` E. At t=0 (before E) an attempt requests market detail; at t=2 the local
clock passes E and `BOOK_EXPIRY` triggers a book resync; at t=3 the market-detail response
arrives. The attempt may commit its blocker change (expiry does not raise the generation),
but readiness is not published: the expired book is still held. The resync's book read is
unversioned and, with `expiry_skew_margin_s` unset, cannot cover E, so the market stays
not ready and is read again. Only a versioned read whose `asOf.at` is at or after E clears
it (`realtime_topic.py:509`).

**F-7. Shutdown during an attempt.** An attempt is waiting for a tape page when
`aclose()` is called. The attempt is cancelled; nothing is committed, persisted or
published, and every market reports not ready from the moment `aclose()` returns, as today
(`realtime.py:466`, `realtime.py:576`).

**F-8. Revision reset (hypothetical, Q7).** Last accepted revision 5,000. SIG redeploys and
the topic restarts at revision 1. *Today:* every batch is a duplicate; its books apply, its
trades and settlements are silently ignored, and the market stays ready. *Proposed:* a
duplicate carrying `marketSettled`, a null-sequence trade, or a trade id never seen opens a
blocker (4.4). A reset during a period with no trades and no settlement remains
undetectable.

**F-9. Reconnect on a quiet topic.** Last accepted revision 40. t=0 the socket closes; a
settlement batch (revision 41) is published and lost. t=5 the re-join; the orderbook read
succeeds. *Today:* the market is ready at t=6 and stays ready while no batch arrives.
*Proposed:* the lifecycle read in the new generation may show `settled` (restricts), or
`open` if market detail lags (Q1); the window stays open under D-1 until a contiguous batch
or the policy allows otherwise.

**F-10. `resyncRequired` hides a settlement.** A batch for revisions 90–95 arrives flagged
`resyncRequired`, with books and some trades only. `RESYNC_REQUIRED` opens. A lifecycle
read shows `settled`: `LIFECYCLE_NOT_OPEN` opens and the market is `TERMINAL`. A lifecycle
read shows `open`: inconclusive (Q1, Q12); the market stays blocked.

**F-11. A missed standalone settlement (Q10).** An admin settles the market with a
standalone `market_settled` event, which the socket drops. No revision moves, so nothing
is detected. The next periodic lifecycle read (proposed for M2-B) is the only detector, and
whether it reflects the settlement is Q1.

**F-12. Rate limiting during recovery.** An attempt's market-detail read receives 429 with
`Retry-After: 60`. The client imposes the cooldown on the shared limiter and retries
within its deadline; the deadline expires first, so the read fails. The attempt fails; the
next one waits at least 60 s. No blocker changes, and the market stays not ready.

## 8. Offline acceptance-test matrix

Tests use the existing fake transport and harness (`tests/sig_client/realtime_kit.py`) and
`httpx.MockTransport` REST responses, with the injected monotonic and wall clocks. No live
calls. "A" marks tests for M2's first slice; "later" marks tests that need a later slice
or an answer from SIG.

| ID | Scenario | Expected | Slice |
| --- | --- | --- | --- |
| AT-01 | Each blocking reason, then any number of successful orderbook resyncs | Market never ready; blocker open; result `NOT_READY` with the reason | A |
| AT-02 | Each blocking reason, then a feed restart with no durable ledger | The new feed's result lists an unobserved window with cause "feed start" and says no blocker history is held; nothing in it reads as recovery | A |
| AT-03 | Same, with the durable ledger | Blocker still open after restart; market not ready | later (D-4) |
| AT-04 | `MARKET_SETTLED`, market detail shows `settled` with equal `settledWith` | Instance `terminal`; result `TERMINAL`; never ready again, even after more resyncs | A |
| AT-05 | `MARKET_SETTLED`, market detail shows `open` | Instance stays `open`; result `NOT_READY` with "inconclusive (Q1)" | A |
| AT-06 | `MARKET_SETTLED`, market detail shows `settled` with a different label | Stays `open`; reason names the label mismatch (Q2) | A |
| AT-07 | Second `marketSettled` while the first attempt's read is in flight | First attempt cannot commit; a new attempt covers both instances | A |
| AT-08 | Any new blocker during an attempt | Generation rises; the in-flight response changes nothing (except 4.5 restrictions) | A |
| AT-09 | Reconnect (SUBSCRIBED) during an attempt | Same as AT-08 | A |
| AT-10 | Token refresh and token expiry during an attempt | Same as AT-08 | A |
| AT-11 | Obsolete response showing `settled` or `closed` | `LIFECYCLE_NOT_OPEN` opens; nothing cleared; no ready published | A |
| AT-12 | `aclose()` during an attempt | Attempt cancelled; nothing committed or published; not ready | A |
| AT-13 | Market detail 503 or 429 until the attempt deadline | Attempt fails; blocker open; next attempt waits for backoff and any `Retry-After` | A |
| AT-14 | Market detail with a context for another tournament | Scope error; attempt fails; nothing committed | A |
| AT-15 | First subscription: lifecycle read shows `closed` or `settled` | `LIFECYCLE_NOT_OPEN`; never ready | A |
| AT-16 | First subscription: lifecycle read fails until deadline | Not ready until a lifecycle read succeeds | A |
| AT-17 | Book expiry crossed while a recovery read is in flight, skew bound unset | Blocker commit allowed if otherwise valid; no ready until a versioned read at or after the expiry | A |
| AT-18 | Same, with an unversioned book read and a configured bound | Existing `_covers` rule decides; recovery code does not change the outcome | A |
| AT-19 | Readiness result content for every state | Every failing condition has a human-readable reason and its question ids; `does_not_cover` present | A |
| AT-20 | Duplicate batch carrying `marketSettled` | `MARKET_SETTLED` opens | later (M2-B) |
| AT-21 | Duplicate batch carrying a null-sequence trade | `NULL_TRADE_SEQUENCE` opens | later (M2-B) |
| AT-22 | Duplicate batch carrying an unseen trade id | `REVISION_ANOMALY` opens; an id evicted from memory also opens it (false positive accepted) | later (M2-B) |
| AT-23 | Tape walk hits the page cap, repeats a cursor or an item, or reports `complete: false` | Attempt fails; blocker open | later (Q3, Q4) |
| AT-24 | Tape walk complete over a past window, then a later read returns an extra row (late projection) | First walk never cleared anything | later (Q3, Q4) |
| AT-25 | `NULL_TRADE_SEQUENCE` with any combination of tape and price reads | Never cleared while Q3 to Q6 and Q11 are open | A |
| AT-26 | Periodic lifecycle sweep finds a market `closed` | `LIFECYCLE_NOT_OPEN`; result `TERMINAL` | later (M2-B) |
| AT-27 | Recovery gate saturated | Attempts wait for the gate; resync gate and shared limiter behaviour unchanged | A |
| AT-28 | Escalation after the configured number of failures | `escalated` true; still not ready; retries continue at the capped interval | A |

G-8 also requires the concrete socket adapter to pass the transport contract tests (M3);
that is outside M1 and M2.

## 9. M2 plan

### 9.1 Smallest slice: M2-A

Within `engine/sig_client/` only; no `shared/` change, no new REST endpoint, no order
code, no live call.

1. Blocker instances replacing the internal `set[StaleReason]`, keeping
   `blockers(market_id)` unchanged for callers.
2. The per-market generation and the commit rule of 4.3, generalizing the existing
   `wanted` fence.
3. A recovery attempt runner: one per market, its own read gate, an overall deadline,
   backoff, escalation and cancellation (section 5).
4. Lifecycle reads with the existing `get_market` (`rest.py:566`): before the first ready
   in each generation (D-3), and in every attempt. They may only restrict: `settled` or
   `closed` opens `LIFECYCLE_NOT_OPEN`; `settled` with an equal label confirms an open
   `MARKET_SETTLED` instance as `terminal`.
5. `MarketReadiness` (6.1) and a `readiness(market_id)` accessor; `is_ready` becomes
   `readiness(market_id).status is READY`.
6. Tests AT-01, AT-02, AT-04 to AT-19, AT-25, AT-27 and AT-28.

M2-A clears no blocker. It makes the existing "blocked for the life of the feed"
behaviour explicit and explained, detects settled and closed markets that the feed misses
today (1.6, items 2 and 6), and builds the fencing that any future clearing rule needs.
It reads no trade tape: no predicate can use a tape walk yet (3.2), so the reads would
spend budget for nothing.

### 9.2 Prerequisites for M2-A

- This note reviewed and approved by Arnav, with the independent review.
- Decisions D-3 (initial lifecycle read required) and D-5 (no manual clearing in M2), or
  Arnav's alternatives.
- Values for the configurable bounds (recovery read gate, attempt deadline, backoff,
  escalation count), set by Arnav.
- Confirmation that `MarketReadiness` stays engine-internal for now.
- Nothing from SIG is needed for M2-A, and M3 is not needed (fake transport).

### 9.3 Later slices (not proposed for implementation yet)

- **M2-B:** duplicate-batch detectors (4.4) and a periodic lifecycle sweep with the market
  list [TMKTS] (*configurable* interval).
- **M2-C:** a durable blocker ledger and last accepted revision per topic (D-4). Needs a
  storage decision; SQLite storage is not started.
- **M2-D:** clearing predicates, only for guarantees SIG documents (section 10) or
  policies Arnav adopts (section 11), each with its own tests (AT-23, AT-24 and new ones).

## 10. Missing guarantees: questions for SIG

Each question names what it would unblock. Until it is answered by documentation or logged
evidence, the affected blocker stays open.

- **Q1 (settlement freshness).** Does `GET /markets/{id}?tournamentId=` (and the tournament
  market list and detail) reflect a tournament settlement no later than the `marketSettled`
  signal is published? Is there any version or watermark for market status?
- **Q2 (settlement labels).** Is `marketSettled.settledWith` always equal to the market
  detail's `settledWith` for the same settlement?
- **Q3 (one counter).** Are realtime trade `sequence`, book `asOf.sequence`,
  `delivery.sourceSequenceFrom/Through` and `coverage.projectedThroughSequence` values of
  one comparable engine sequence? Per engine, per tournament or per exchange?
- **Q4 (prefix completeness).** On `GET /exchanges/{id}/trades`, does
  `projectedThroughSequence` = N mean every trade with sequence at or below N is on the
  tape ("complete through", as stated for price history), or only that event N has been
  projected?
- **Q5 (trade identity).** The realtime prose calls the trade's engine event sequence
  "the same id `GET /exchanges/{id}/trades` gives the trade". Is a tape `id` always the
  decimal form of the realtime `sequence`? Can tape rows be ordered by `id`?
- **Q6 (timestamps).** Is a tape row's `createdAt` the realtime trade's `executedAt`? What is
  the longest delay between execution and a trade appearing on the tape?
- **Q7 (revisions).** Is a topic's `delivery.revision` strictly increasing for the life of
  the topic, including across SIG deploys and restarts? If it can reset, how is a client
  told?
- **Q8 (lost windows).** How should a client find the time or sequence span of a missed
  revision range, so that it knows which tape window to read?
- **Q9 (settlement after-effects).** Can a tournament settlement be corrected or reversed so
  that the market reopens? Is a client told?
- **Q10 (standalone events).** Is every standalone `market_settled` also carried in a
  revisioned `market_batch` on the tournament market topic? If not, how can a client detect
  a missed one?
- **Q11 (null-sequence fills).** Does a fill recorded without a match event appear on the
  tape? Under what `id`, and is it covered by `projectedThroughSequence`?
- **Q12 (`resyncRequired`).** Is there any way to obtain the `marketSettled` and trades a
  `resyncRequired` batch dropped, other than the REST reads above?
- **Q13 (closure).** Is there any realtime signal when a market moves from `open` to
  `closed`? Which error does an order on a closed or settled market receive?
- **Q14 (redelivery).** After a client reconnects, are batches it missed ever re-sent?
- **Q15 (clocks).** Do `asOf.at` (engine clock) and `createdAt`, `executedAt`, `settledOn`
  come from the same clock?

## 11. Decisions for Arnav

Each is a policy choice, not a platform fact. This note proposes no answer as adopted;
where a recommendation is given, it is labelled.

- **D-1 (unobserved windows).** After a reconnect, socket or channel error, token refresh
  or expiry, or the first subscription, must a market stay not ready until a contiguous
  batch (one whose `previousRevision` is at or below the last accepted revision) confirms
  continuity? *Strict* closes item 1 of 1.6 for active markets but leaves quiet markets not
  ready after every reconnect and every token refresh (about every three hours). *As today*
  accepts that a loss in the window is detected only when a later batch arrives.
- **D-2 (trade-stream continuity).** Does any trading consumer depend on SIG's trade
  stream being complete (for example last-trade marks or lag measurement used in
  decisions)? If none does, `NULL_TRADE_SEQUENCE` and the trade part of lost windows could
  stop blocking new trading, which needs a PRD change. Until decided, they block.
- **D-3 (initial lifecycle trust).** Accept a lifecycle read showing `open`, made after the
  subscription, as the initial-state evidence for readiness, knowing its freshness is
  unproven (Q1)? Without it no market can ever be ready, because initial state always rests
  on that read. *Recommendation:* accept it for initial state only, as M2-A does.
- **D-4 (durable ledger).** Persist open blocker instances and the last accepted revision
  across restarts? Then a blocked market stays blocked for the rest of the competition
  unless a clearing rule exists. Without persistence, a restart clears blockers with no
  evidence.
- **D-5 (manual clearing).** Should an operator ever be able to clear a blocker? If so, it
  would be a controlled, audited command (track C1), never automatic. M2 proposes none.
- **D-6 (settlement standard for lost windows).** If D-3 is accepted, should the same
  standard (a lifecycle read showing `open`, made after the window) also settle the
  *settlement* part of a lost window? The trade part would still need D-2 or Q3 to Q8.
  This would make lost-window blockers clearable with residual risk; it is not proposed as
  adopted.

## 12. References

- [`docs/PRD.md`](../PRD.md): M1, M2, M3 in the roadmap; G-8; OQ-E4; risk management.
- [`docs/api/openapi.json`](../api/openapi.json): pointers in the table at the top.
- Realtime feed: [`engine/sig_client/realtime.py`](../../engine/sig_client/realtime.py),
  [`engine/sig_client/realtime_topic.py`](../../engine/sig_client/realtime_topic.py),
  [`engine/sig_client/realtime_wire.py`](../../engine/sig_client/realtime_wire.py).
- REST client and wire models: [`engine/sig_client/rest.py`](../../engine/sig_client/rest.py),
  [`engine/sig_client/wire.py`](../../engine/sig_client/wire.py).
- Recorded fixtures: [`tests/fixtures/sig/README.md`](../../tests/fixtures/sig/README.md);
  market detail and tape pages in
  `tests/fixtures/sig/recorded/20261011T211201Z-12ab20/bodies/`; book versions in
  `tests/fixtures/sig/recorded/20261009T122244Z-fa008a/bodies/` and
  `tests/fixtures/sig/recorded/20261009T190027Z-fb88c9/bodies/`.
