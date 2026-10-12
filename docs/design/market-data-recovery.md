# Market-data recovery design (M1)

Oct 12, 2026 · Roadmap track M1 · Input to gate G-8 and open question OQ-E4 ·
Status: **draft for independent design review** · Revised Oct 12 (M1-F1): continuity
fence, unknown history across restarts, lifetime guards and the observe/commit step,
blocker observability and quarantine

This note designs how a realtime market that the feed has marked not ready could be
shown safe to use again. It is a design only. It changes no code, no `shared/` schema
and no risk setting, and it does not pass gate G-8: approval of this note is one of
G-8's conditions, not the gate itself ([PRD, acceptance gates](../PRD.md#acceptance-gates)).

**Bottom line.** With the documented API and the evidence recorded so far, **no
realtime blocker can be cleared automatically with proof.** One outcome can be shown:
a settlement signal can be *confirmed* by the market-state read. Under local policy
the market is then quarantined: it stays blocked and is never ready again in this
engine, which is not a claim that SIG cannot correct or reopen it. Every other blocker
stays open, and the affected market stays not ready, as it does today. Blockers must
also survive restarts: a process that cannot show its market history treats it as
unknown and is never ready (section 4.2). Section 10 lists the exact guarantees that
are missing as questions for SIG, and section 11 lists the policy decisions that are
Arnav's. The smallest useful M2 slice (section 9) builds readiness results, recovery
fencing, history tracking and fail-closed lifecycle checks without clearing anything.

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
Section 4.2 proposes how blockers survive a new feed object, and how a process that
cannot show its history stays not ready.

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
  add a blocker, confirm a settlement or quarantine a market. It can never clear a
  blocker or publish ready.
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
  `settledWith` *confirms* the blocker, and the market is quarantined under local policy
  (4.1): the instance stays blocking and visible, and the market is never ready again in
  this engine. That is not proof that SIG cannot correct or reopen it (Q9). A
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
| `MARKET_SETTLED` | Market detail showing the same settlement | **No**; confirmable, then quarantined (local policy) | Q1, Q2, Q9 |
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
  payload), its state and the evidence collected for it. Two settlements are two
  instances. The state is `open` or `quarantined`. **Both states block.** No state means
  "cleared" in M2-A, because nothing clears (4.6).
- **Quarantine is local policy.** A quarantined instance records that this engine has seen
  enough to stop treating the market as tradable and stops recovering it. It is **not**
  evidence that SIG cannot correct, reverse or reopen the market (Q9). Nothing lifts a
  quarantine automatically; whether an operator ever may is decision D-5.
- **Recovery ledger.** Blocker instances, unobserved windows (4.3), the last accepted
  revision per topic and the market's history state (4.2) live in a `RecoveryLedger` that
  the engine owns and passes to the feed. The ledger outlives any one feed object: a
  replacement feed in the same process is given the same ledger and inherits everything in
  it. In M2-A the ledger is in memory only; persistence is a later slice (M2-C, D-4).
- **Generation.** Each market has a counter in the ledger, starting at 0, raised by the
  events in 4.4. It generalizes the existing fence for book resyncs (`request =
  market.wanted`, `realtime.py:609`, checked at `realtime.py:649`).
- **Recovery attempt.** One per market at a time. It records the generation, the set of
  blocker instances it targets and its own deadline when it starts, then makes its reads.
  Its responses are applied only through the observe/commit step (4.6).

**Blocker observability: a reviewed compatibility change.** `blockers(market_id)` keeps its
signature and return type (`frozenset[StaleReason]`). In M2-A it returns the reason of
**every open and every quarantined instance**, so a quarantined settlement stays visible as
`MARKET_SETTLED`. M2-A also adds new blocking reasons: `HISTORY_UNKNOWN` (4.2) and
`LIFECYCLE_NOT_OPEN` (4.6); M2-B adds `REVISION_ANOMALY` (4.7). Callers are **not**
unaffected:

- A feed started without retained history reports `{HISTORY_UNKNOWN}` and is never ready,
  where today it reports no blocker and becomes ready after its first orderbook read.
- Code that enumerates `StaleReason` or `BLOCKING_REASONS` sees new members.
- Existing tests that expect a market to become ready after its initial resync must
  construct the feed with an explicit bootstrap ledger (4.2).

[Code] No module in `engine/` calls `blockers()` today; only the feed defines it
(`realtime.py:420`). This change needs the reviewer's approval as part of M2-A.

### 4.2 History, bootstrap and restart [Proposed]

**Unknown history.** A market's history is *unknown* when the ledger cannot show what
happened to it before the current feed observed it: which blockers were raised and are
unresolved, and which revision was last accepted. A market with unknown history carries a
`HISTORY_UNKNOWN` instance, which blocks like any other and which nothing in M2-A resolves:
not a lifecycle read showing `open`, not an orderbook resync, not a contiguous batch.

Every ledger has an origin, set when it is created:

| Origin | When | History of each market |
| --- | --- | --- |
| `unknown` | The default: a new ledger with nothing loaded, which is what every new process has in M2-A | Unknown: `HISTORY_UNKNOWN` on every market, never ready |
| `bootstrap` | An explicit `BootstrapDeclaration` that history begins now and that no earlier history exists | Begins at the first subscription; the initial-state evidence it requires is decision D-3 |
| `loaded` | A durable ledger reloaded at start (M2-C, D-4) | Whatever the loaded ledger holds, including open and quarantined instances and fences |

Three situations are kept apart:

- **Initial bootstrap** (D-3): history starts here, deliberately, under an explicit
  declaration. There is no earlier revision to compare with, so the first subscription is
  not an unobserved window with a fence; its acceptance is whatever D-3 decides.
- **Reconnect and in-process restart recovery** (D-1): history is known. A socket or channel
  break, a token refresh or expiry, or a replacement feed object sharing the ledger opens an
  unobserved window with a captured fence (4.3). Retained blockers stay; nothing about the
  new feed object resets them.
- **Restart without retained history**: a new process with an `unknown` ledger. This is not
  bootstrap, because the process cannot show that nothing came before it. Every market is
  `HISTORY_UNKNOWN`.

**Why M2-A is safe without persistence.** In production, M2-A creates only `unknown`
ledgers. So every new process reports every market not ready, whatever its lifecycle reads
and book resyncs show; and within a process, every replacement feed shares the ledger and
inherits its blockers. A fresh feed object can therefore neither forget a known blocker nor
produce `READY` from an empty history. The cost is that M2-A never produces `READY` in
production; that is acceptable because no order path exists and trading is not activated.
`BootstrapDeclaration` exists in M2-A only so that tests can exercise the ready path. No
module in `engine/` creates one until Arnav decides D-3 and D-4, and an M2-A test checks
that none does. Using it in tests is not an adoption of D-3.

### 4.3 Revision continuity across an unobserved window [Proposed]

This predicate is what D-1 would rely on. It is defined here so that it is correct whatever
D-1 decides.

**Epoch assumption.** Revisions of one topic are compared only with each other, and only
under the assumption that SIG's revisions for that topic increase strictly and never reset,
across SIG deploys and across our reconnects and restarts (Q7). No documented field names
an epoch, so the assumption cannot be checked; the detectors in 4.7 open
`REVISION_ANOMALY` when evidence contradicts it. For a fence carried across a restart
(M2-C), the assumption also covers the ledger: the persisted value is the revision this
engine last accepted.

**Fence.** When an unobserved window opens (socket or channel error or close, a SUBSCRIBED
re-join, token refresh or expiry, a replacement feed sharing the ledger), the feed
captures, synchronously and in the same step that handles the event, the window's fence:
`F` = the topic's last accepted revision in the ledger at that instant, with the window's
id and the generation. If no batch has ever been accepted for the topic, there is no fence
(`F` absent).

**Clean advancing batch.** A `market_batch` processed after the fence was captured that is
well-formed, is not flagged `resyncRequired`, names no exchange without its book, and is
accepted by the existing rule *without a gap*: `revision` is greater than the last accepted
revision at its arrival, and `previousRevision` is at or below it.

**Confirmation.** A window with fence `F` is confirmed through revision `R` when the
topic's last accepted revision has advanced from `F` to `R` (`R > F`) through clean
advancing batches only, all processed after the fence was captured. Then every revision in
(`F`, `R`] was carried by a well-formed, complete batch that this feed processed.

**What never confirms a window:**

- **A duplicate.** A batch whose `revision` is at or below the last accepted revision
  advances nothing, whatever its `previousRevision`. Counterexample to the earlier wording:
  last accepted 40, a socket close captures `F` = 40, and after the re-join a retried batch
  with `previousRevision` 39 and `revision` 40 arrives. Its `previousRevision` is at or below
  40, but it is old content and says nothing about revisions published after the
  disconnection. It does not confirm the window (AT-30).
- A batch that advances the revision but is not clean (`resyncRequired`, malformed with a
  parsed `delivery`, missing a named book): the window stays unconfirmed, and the batch
  raises its own blocker as today.
- A gap (`previousRevision` above the last accepted revision): `REVISION_GAP` opens, as
  today; the window stays unconfirmed.
- Anything when there is no fence: with `F` absent, revisions cannot show what the window
  missed.

**What a confirmation covers, and what it does not.** It covers only the revisioned
`market_batch` stream of that topic, for revisions (`F`, `R`]. It does **not** show that
standalone events sent during the window were delivered: standalone `market_settled` and
`book_dirty` carry no revision (1.6, item 3; Q10). It says nothing about revisions above
`R`, about content of non-clean batches (each has its own blocker), or about the account
topic. It is void if the epoch assumption is false.

### 4.4 What raises the generation [Proposed]

The generation is raised synchronously, in the same step as the event, and never by an
await.

| Event | Why it invalidates an attempt |
| --- | --- |
| A new blocker instance from the realtime topic, including one carried by a duplicate or late batch (4.7) | The attempt's target set is out of date |
| An unobserved window opening (4.3) | The market's continuity changed under the attempt |
| A commit or restriction by the observe/commit step that adds or changes an instance (4.6) | Raised once, *after* that step has applied its own changes |

Not raised by: a normal, clean batch with sequenced trades and complete books; a standalone
`book_dirty`; a book expiry. These change books, and books are governed by the existing
book-readiness rules, which are checked again before any ready is published. A lifecycle
read does not raise the generation by itself; only the instances it adds or changes do,
through step 5 of 4.6, so it cannot invalidate its own transaction.

### 4.5 Lifetime guards [Proposed]

**Readiness ends when closing begins.** [Code] `aclose()` sets the closing flag before it
awaits `transport.close()` (`realtime.py:468`-`469`), and `_ready_now` checks that flag first
(`realtime.py:578`), so `is_ready` is already false while `transport.close()` is pending.
M2-A keeps that order: `readiness(market_id)` checks closing before anything else and
reports `NOT_READY` with the reason "feed closing" from the moment `aclose()` begins.

**Guard.** An attempt is *live* while the feed is not closing, the attempt has not been
cancelled and the monotonic clock is before the attempt's deadline. Every effect of an
attempt passes the guard first, synchronously, in the observe/commit step (4.6). If the
guard fails, the response is dropped: no blocker instance, ledger entry, generation, fence,
lifecycle observation or event is created or changed. A log line is the only trace.

- **Closing** (`aclose()` begins): every attempt is cancelled, and nothing is applied,
  including a response already received but not yet passed through the step.
- **Cancellation** for any other reason: the same.
- **Deadline expiry**: the same, including a response that arrived in time but reaches
  the step after the deadline.
- **Supersession is not cancellation.** An attempt whose generation is no longer current
  is not cancelled. If it completes while live, its response goes through the step as a
  *superseded* response, which may only restrict (4.6). A cancelled attempt may not even
  restrict.

**The existing teardown.** [Code] After closing, `run()`'s `finally` clears `subscribed` and
calls `_set_stale(STOPPED)` for every market (`realtime.py:459`-`463`). That emits
`MarketStale` only for markets whose `ready` flag was still set, and `STOPPED` is not a
blocking reason. It withdraws readiness that the closing flag has already withdrawn. M2-A
leaves it unchanged and adds nothing to it: no ledger write, blocker change or recovery
effect happens after closing begins.

### 4.6 The observe/commit step [Proposed]

Every response of a recovery attempt is applied by one synchronous step, with no `await`
anywhere inside it:

1. **Guard.** If the attempt is not live (4.5), drop the response and stop.
2. **Classify.** The response is *current* if the market's generation equals the attempt's
   starting generation, and *superseded* otherwise.
3. **Decide**, without changing anything yet:
   - *Restriction*, current or superseded: if the tournament context shows `settled` or
     `closed`, and the market has no quarantined `LIFECYCLE_NOT_OPEN` instance, decide to
     add one in state `quarantined`. A response showing `open` restricts nothing.
   - *Confirmation*, current only, under conditions 1 to 6 below: for each targeted open
     `MARKET_SETTLED` instance whose `settledWith` equals the context's `settledWith`
     (Q2), decide to move it to `quarantined`.
   - Nothing else. In M2-A no decision clears an instance.
4. **Apply** every decision together, attach the evidence (attempt id, generation, the
   status and settlement exactly as sent, our read's start and finish on the monotonic
   clock), and record the lifecycle observation.
5. **Raise the generation once**, and only if step 4 added or changed an instance. Because
   this comes after step 4, the attempt's own changes cannot invalidate it; the raise
   invalidates every *other* decision made against the earlier generation.
6. **Publish** at most a `MarketStale` withdrawal, if the market was ready. The step never
   publishes `MarketReady`; readiness is re-evaluated afterwards by the normal rule, and
   `MarketReady` comes only from the book-resync path, which checks every condition again
   synchronously before publishing.

Conditions for a confirmation in step 3 (all must hold):

1. the attempt is live (step 1);
2. the response is current (step 2);
3. the instance is still `open`;
4. every read in the attempt was **started** after the instance was raised and after the
   attempt began (ordering of our own requests on the monotonic clock; no claim about
   SIG's clock), and none is reused from an earlier attempt;
5. every read succeeded and passed the client's scope checks;
6. the blocker-specific predicate (section 2) holds using only documented comparisons.

**A superseded response may still quarantine.** A completed, live, superseded response that
shows `settled` or `closed` quarantines the market. This is local policy (4.1): a stale read
cannot show the market is open, but showing it was settled or closed at some point is
enough for this engine to stop trading it. It is not proof that SIG cannot reopen or correct
it (Q9).

**In M2-A the step can only restrict and confirm.** No predicate that clears a blocker is
implemented until section 10's questions are answered or Arnav decides otherwise (section
11).

### 4.7 How concurrent events supersede recovery [Proposed]

- **Concurrent pushes.** Books keep applying by version while an attempt runs, as today.
  A push that raises a blocker raises the generation; the attempt's response is then
  superseded.
- **New blockers.** As above. The new instance is included in the next attempt.
- **Reconnects.** Every re-join opens an unobserved window with a fence (4.3) and raises
  the generation. Whether the window blocks until confirmed is D-1.
- **Revision resets.** [Unknown] whether they occur (Q7). None is detectable from
  documented fields: the next batches simply look like duplicates. Three fail-closed
  detectors are proposed for duplicate batches, whose arrays are otherwise ignored:
  a `marketSettled` item or a null-sequence trade in a duplicate raises the corresponding
  blocker (redundant but harmless after a genuine retried send, which repeats content
  already seen); a sequenced trade `id` in a duplicate that the topic has never seen
  raises a new reason, `REVISION_ANOMALY`. A genuine retried send repeats trades
  already accepted, so an unseen id appears only when the original batch was discarded
  (the market is then already blocked) or the id has left the 4,096-id memory (a false
  positive that blocks, never one that clears).
- **Closing.** Two meanings:
  - *Feed closing* (`aclose()`): readiness ends at once; attempts are cancelled and their
    responses dropped (4.5).
  - *Market closing*: [Documented] no realtime signal exists for `open` → `closed` (1.6,
    item 6). A lifecycle read showing `closed` or `settled` quarantines the market through
    the observe/commit step (4.6).

### 4.8 No snapshot across endpoints

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
  deadline (*configurable*) on the injected monotonic clock. Past it, the attempt is
  cancelled, and a response that reaches the observe/commit step late is dropped (4.5).
- **Cancellation.** Cancelling an attempt (deadline, `aclose()`) propagates
  `CancelledError`; nothing is applied and no event is published. Supersession does not
  cancel an attempt (4.5).
- **Shared rate budget.** All reads share the account limiter (80 of 100 reads a minute).
  [Code] The feed already gates orderbook resyncs at 40 starts a minute
  (`realtime.py:291`). Recovery reads get their own gate (*configurable*, like the resync
  gate) so that they cannot starve account reconciliation or resyncs, and the shared
  limiter still has no priorities. Note the scale: after a reconnect the existing resync
  alone needs one read per market (237 for every market). Where a lifecycle check covers
  many markets, it uses the market list [TMKTS] (three pages for 237 markets) rather than
  one market-detail read per market.
- **Retry scheduling.** One attempt per market at a time; triggers during an attempt
  coalesce into one follow-up, which starts after the current attempt ends (as for
  resyncs). Failed attempts back off exponentially with jitter and a floor from any
  `Retry-After` (*configurable* base and cap). After a *configurable* number of failed
  attempts the market is reported as `escalated` in its readiness result: still not ready,
  still retried at the capped interval, and visible to the operator. Escalation never
  clears anything. A quarantined market is not retried.
- **Partial or stale evidence never clears a blocker.** This follows from E2, E3 and 4.6.
- **Expiry and clock skew are unchanged.** No recovery read is a book read. A recovery
  attempt that completes after a held book's `nextExpiryAt` may still pass through the
  observe/commit step (expiry does not raise the generation), but readiness is not
  published until a book read covers the expiry under the existing rules, which with the
  skew bound unknown means a versioned read made at or after it (example F-6).

## 6. Engine readiness

### 6.1 Proposed per-market result [Proposed]

An engine-internal, immutable `MarketReadiness` value, built on request from the feed's
state and the ledger. It lives in `engine/`; no `shared/` schema is proposed (the PRD
reserves readiness schemas for Arnav's approval).

| Field | Content |
| --- | --- |
| `market_id` | The market |
| `status` | `READY`, `NOT_READY` or `QUARANTINED` |
| `generation` | The market's generation when evaluated |
| `evaluated_mono`, `evaluated_utc` | Local monotonic time (for ordering) and local UTC time (for display only) |
| `reasons` | Human-readable reasons, one per failing condition, for example "blocker: revision gap (revisions 812–815) open; missing Q1, Q3, Q4" or "history unknown: no retained blocker history" |
| `closing` | True from the moment `aclose()` begins |
| `history` | Ledger origin (`unknown`, `bootstrap`, `loaded`) and whether `HISTORY_UNKNOWN` is open |
| `transport` | Subscribed; token valid |
| `books` | Per exchange: version or null, source, complete, expiry known, outstanding expiry |
| `continuity` | Last accepted revision; each unobserved window with its cause, fence and state (open, or confirmed through `R`), and the fixed note that confirmation covers the revisioned stream only (4.3) |
| `lifecycle` | Last lifecycle read: status, `settledWith`, `settledOn` as sent, when our read started and finished (monotonic), its generation, whether it was current or superseded, and the fixed note "unversioned" |
| `blockers` | Every open and quarantined instance: reason, raised at, generation, revision interval, state, evidence collected, missing guarantees (question ids) |
| `escalated` | True after the configured number of failed attempts |
| `does_not_cover` | The fixed statement that this result says nothing about account state, orders, risk, configuration or reference data |

- `QUARANTINED` when any instance is quarantined. Recovery stops for that market. This is
  local policy (4.1).
- `READY` only when all of these hold: the feed is not closing; today's `_ready_now` holds;
  no instance is open or quarantined (so `HISTORY_UNKNOWN` is absent); no unobserved window
  is open; and the market's initial-state evidence meets the bootstrap declaration (D-3).
  While D-1 is undecided, an unconfirmed window counts as open: M2-A relaxes nothing that a
  decision would govern (9.2).
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
- A `READY` result means "no loss detected in the revisioned stream since history began,
  plus the initial-state evidence the bootstrap declaration requires". It does not mean
  nothing was lost: delivery is best-effort ([RT]), and a missed standalone
  `market_settled` is undetectable (1.6, item 3).

### 6.3 Protective cancellation stays available

Protective cancellation is not gated on `MarketReadiness`. A market that is `NOT_READY`,
`QUARANTINED`, escalated or of unknown history may still have resting orders, and
cancelling them must remain possible, scoped to the configured tournament, within the rate
budget and audited (`CLAUDE.md` hard rule 3). Nothing in this design blocks a cancel, and a
cancel path never places or enlarges an order. No cancel path exists yet.

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
`marketSettled`; `MARKET_SETTLED` opens, g=5. t=0.6 A's response arrives, live, showing
`open`. It is superseded, and `open` restricts nothing, so the step changes nothing and
the generation stays 5. t=2 attempt B starts (g=5). t=2.4 market detail shows `settled`
with the same `settledWith`. In one step, B (current) moves `MARKET_SETTLED` to
`quarantined` and adds a quarantined `LIFECYCLE_NOT_OPEN`; then g=6. B's own changes do not
invalidate it, because the raise comes after them. Result: `QUARANTINED`; `blockers()`
returns `REVISION_GAP`, `MARKET_SETTLED` and `LIFECYCLE_NOT_OPEN`. The `REVISION_GAP`
instance stays open and visible; it is never recorded as cleared.

**F-3. Truncated history.** A tape walk of a busy exchange reads three pages, each with
`hasMore: true`, and reaches its page cap (as the recorded sample did under a three-page
cap, 3.4). The client raises a pagination error and returns no partial tape. The attempt
fails, the blocker stays open, and the next attempt waits for the backoff.

**F-4. Restart.** t=0 `REVISION_GAP` opens on market 258.
- *Today:* t=60 the process restarts; the new feed has no blockers, and at t=63 its first
  orderbook read publishes 258 ready. Nothing was reconciled (1.5).
- *M2-A, new process:* the new ledger's origin is `unknown`, so every market, 258 included,
  carries `HISTORY_UNKNOWN`. At t=63 the lifecycle read shows `open` and the orderbook
  resync succeeds; 258 is still `NOT_READY` ("history unknown"), and no `MarketReady` is
  published (AT-02).
- *M2-A, same process:* the engine replaces a feed object that stopped. The new feed is given
  the same ledger, so `REVISION_GAP` is still open and visible, and the replacement opens an
  unobserved window with fence = the ledger's last accepted revision (AT-03).
- *M2-C (D-4):* the ledger is persisted and reloaded (origin `loaded`), so the same holds
  across processes: the open instance survives, and the downtime is an unobserved window
  with the persisted fence (AT-29).

**F-5. Superseded response after a reconnect.** g=7, attempt A (g=7) requests market detail.
t=1 the socket closes: a window opens with fence `F` = the last accepted revision, and g=8.
A is not cancelled. t=3 the re-join. t=4 A's response arrives before A's deadline, with the
feed still live, showing `settled`. It is superseded, so it cannot confirm anything, but it
restricts: a quarantined `LIFECYCLE_NOT_OPEN` is added, then g=9. The market is
`QUARANTINED` and is not retried. Had A's deadline passed before its response reached the
step, the response would have been dropped even though it showed `settled`; the follow-up
attempt would read again. Had it shown `open`, nothing would have changed.

**F-6. Expiry crossed during a recovery read.** A pushed book for exchange 947 carries
`nextExpiryAt` E. At t=0 (before E) an attempt requests market detail; at t=2 the local
clock passes E and `BOOK_EXPIRY` triggers a book resync; at t=3 the market-detail response
arrives. It may pass through the observe/commit step (expiry does not raise the
generation), but readiness is not published: the expired book is still held. The resync's
book read is unversioned and, with `expiry_skew_margin_s` unset, cannot cover E, so the
market stays not ready and is read again. Only a versioned read whose `asOf.at` is at or
after E clears it (`realtime_topic.py:509`).

**F-7. Shutdown during an attempt.** t=0 an attempt is waiting for its read when `aclose()`
begins, and the transport's `close()` takes two seconds. From t=0, `is_ready` is false for
every market and `readiness()` reports "feed closing", although `close()` has not returned.
The attempt is cancelled. t=0.2 its response arrives showing `settled`: it is dropped,
because a cancelled attempt may not even restrict. No instance, ledger entry, generation or
event changes. The existing teardown then withdraws readiness that was already withdrawn
(4.5).

**F-8. Revision reset (hypothetical, Q7).** Last accepted revision 5,000. SIG redeploys and
the topic restarts at revision 1. *Today:* every batch is a duplicate; its books apply, its
trades and settlements are silently ignored, and the market stays ready. *Proposed:* a
duplicate carrying `marketSettled`, a null-sequence trade, or a trade id never seen opens a
blocker (4.7). A reset during a period with no trades and no settlement remains
undetectable, and an open window can never be confirmed, because no batch advances past its
fence (fail-closed).

**F-9. Reconnect on a quiet topic.** Last accepted revision 40. t=0 the socket closes; the
window's fence is 40; a settlement batch (revision 41) is published and lost. t=5 the
re-join; the orderbook read succeeds. *Today:* the market is ready at t=6 and stays ready
while no batch arrives. *Proposed:* the window stays open until a clean batch advances past
40 (4.3). The next batch, (41, 42], shows a gap and opens `REVISION_GAP`. A retried
(39, 40] confirms nothing. A lifecycle read may quarantine the market if it shows `settled`,
or show `open` if market detail lags (Q1). Whether an open window blocks trading is D-1;
M2-A treats it as blocking.

**F-10. `resyncRequired` hides a settlement.** A batch for revisions 90–95 arrives flagged
`resyncRequired`, with books and some trades only. `RESYNC_REQUIRED` opens. A lifecycle
read shows `settled`: the market is quarantined (`LIFECYCLE_NOT_OPEN`, `QUARANTINED`). A
lifecycle read shows `open`: inconclusive (Q1, Q12); the market stays blocked.

**F-11. A missed standalone settlement (Q10).** An admin settles the market with a
standalone `market_settled` event, which the socket drops. No revision moves, so nothing
is detected, and a window confirmed in the meantime still says nothing about it (4.3). The
next periodic lifecycle read (proposed for M2-B) is the only detector, and whether it
reflects the settlement is Q1.

**F-12. Rate limiting during recovery.** An attempt's market-detail read receives 429 with
`Retry-After: 60`. The client imposes the cooldown on the shared limiter and retries
within its deadline; the deadline expires first, so the read fails. The attempt fails; the
next one waits at least 60 s. No blocker changes, and the market stays not ready.

## 8. Offline acceptance-test matrix

Tests use the existing fake transport and harness (`tests/sig_client/realtime_kit.py`) and
`httpx.MockTransport` REST responses, with the injected monotonic and wall clocks. No live
calls. Unless a row says otherwise, the feed uses a `bootstrap` ledger created by the test
(4.2). "A" marks tests for M2's first slice; "later" marks tests that need a later slice or
an answer from SIG.

| ID | Scenario | Expected | Slice |
| --- | --- | --- | --- |
| AT-01 | Each blocking reason, then any number of successful orderbook resyncs | Market never ready; blocker open and listed by `blockers()`; result `NOT_READY` with the reason | A |
| AT-02 | Each blocking reason; then a new feed with a new default (`unknown`) ledger, as after a process restart; its lifecycle read shows `open` and its orderbook resync succeeds | `blockers()` is `{HISTORY_UNKNOWN}`; `is_ready` false; result `NOT_READY` with "history unknown"; no `MarketReady` published, ever | A |
| AT-03 | Each blocking reason; then the feed stops and a new feed object is created with the same ledger | The instance is still open and listed by `blockers()`; not ready; an unobserved window with fence = the ledger's last accepted revision is open | A |
| AT-04 | `MARKET_SETTLED`, current attempt, market detail shows `settled` with equal `settledWith` | `MARKET_SETTLED` quarantined; `LIFECYCLE_NOT_OPEN` added; both listed by `blockers()`; result `QUARANTINED`; never ready, even after more resyncs | A |
| AT-05 | `MARKET_SETTLED`, market detail shows `open` | Instance stays `open`; result `NOT_READY` with "inconclusive (Q1)" | A |
| AT-06 | `MARKET_SETTLED`, market detail shows `settled` with a different label | `MARKET_SETTLED` stays `open` with the label mismatch named (Q2); `LIFECYCLE_NOT_OPEN` quarantines the market | A |
| AT-07 | Second `marketSettled` while the first attempt's read is in flight | The first response is superseded and confirms nothing; a follow-up attempt targets both instances | A |
| AT-08 | Any new blocker during an attempt; the live response shows `open`, or `settled` | `open`: nothing changes; `settled`: quarantine only, no confirmation | A |
| AT-09 | Reconnect (SUBSCRIBED) during an attempt | Same as AT-08 | A |
| AT-10 | Token refresh and token expiry during an attempt | Same as AT-08 | A |
| AT-11 | Superseded response, completed before its deadline with the feed live, showing `settled` or `closed` | Quarantined `LIFECYCLE_NOT_OPEN`; generation raised once; no confirmation; nothing cleared; no ready published | A |
| AT-12 | `aclose()` begins during an attempt while `transport.close()` is pending (slow-close transport); the response then arrives showing `settled` | `is_ready` false and status `NOT_READY` ("feed closing") while `close()` is pending; the attempt is cancelled; no instance, ledger entry, generation or event changes | A |
| AT-13 | Market detail 503 or 429 until the attempt deadline | Attempt fails; blocker open; next attempt waits for backoff and any `Retry-After` | A |
| AT-14 | Market detail with a context for another tournament | Scope error; attempt fails; nothing applied | A |
| AT-15 | Bootstrap, first subscription: lifecycle read shows `closed` or `settled` | Quarantined `LIFECYCLE_NOT_OPEN`; never ready | A |
| AT-16 | Bootstrap, first subscription: lifecycle read fails until its deadline | Not ready until a lifecycle read succeeds | A |
| AT-17 | Book expiry crossed while a recovery read is in flight, skew bound unset | The response may pass the step if otherwise valid; no ready until a versioned read at or after the expiry | A |
| AT-18 | Same, with an unversioned book read and a configured bound | Existing `_covers` rule decides; recovery code does not change the outcome | A |
| AT-19 | Readiness result content for every state | Every failing condition has a human-readable reason and its question ids; `closing`, `history`, `continuity` and `does_not_cover` present | A |
| AT-20 | Duplicate batch carrying `marketSettled` | `MARKET_SETTLED` opens | later (M2-B) |
| AT-21 | Duplicate batch carrying a null-sequence trade | `NULL_TRADE_SEQUENCE` opens | later (M2-B) |
| AT-22 | Duplicate batch carrying an unseen trade id | `REVISION_ANOMALY` opens; an id evicted from memory also opens it (false positive accepted) | later (M2-B) |
| AT-23 | Tape walk hits the page cap, repeats a cursor or an item, or reports `complete: false` | Attempt fails; blocker open | later (Q3, Q4) |
| AT-24 | Tape walk complete over a past window, then a later read returns an extra row (late projection) | First walk never cleared anything | later (Q3, Q4) |
| AT-25 | `NULL_TRADE_SEQUENCE` with any combination of tape and price reads | Never cleared while Q3 to Q6 and Q11 are open | A |
| AT-26 | Periodic lifecycle sweep finds a market `closed` | Quarantined `LIFECYCLE_NOT_OPEN`; result `QUARANTINED` | later (M2-B) |
| AT-27 | Recovery gate saturated | Attempts wait for the gate; resync gate and shared limiter behaviour unchanged | A |
| AT-28 | Escalation after the configured number of failures | `escalated` true; still not ready; retries continue at the capped interval | A |
| AT-29 | Blocker open; process restart with a persisted ledger | Origin `loaded`; instance still open; downtime window with the persisted fence | later (M2-C, D-4) |
| AT-30 | Last accepted 40; socket close captures fence 40; after the re-join a batch with `previousRevision` 39 and `revision` 40 arrives | Treated as a duplicate; the window stays open; the market stays not ready | A |
| AT-31 | As AT-30, then a clean batch (40, 41] | Window confirmed through 41; the result states that the confirmation covers the revisioned stream only | A |
| AT-32 | Fence 40, then a batch (42, 43] | `REVISION_GAP` opens; the window stays open | A |
| AT-33 | Fence 40, then (40, 41] flagged `resyncRequired`, or malformed with a parsed `delivery`, or missing a named book | Revision advances but the window stays open; the batch's own blocker opens | A |
| AT-34 | Reconnect before any batch was ever accepted for the topic | No fence; the window cannot be confirmed by any batch | A |
| AT-35 | A window is confirmed; a standalone `market_settled` sent during it was dropped by the transport | Nothing claims the standalone event was delivered: the confirmation's coverage note is present, and no instance or readiness field states otherwise | A |
| AT-36 | The attempt's deadline passes after its response arrived but before the observe/commit step runs | Response dropped; nothing changes | A |
| AT-37 | A superseded response that completed while live reaches the step after `aclose()` has begun | Dropped; closing takes precedence over restriction | A |
| AT-38 | A current attempt's read shows `settled` with an equal label | Confirmation and quarantine applied together; generation raised exactly once, after them; the commit is not rejected by its own change | A |
| AT-39 | Search of `engine/` for the bootstrap declaration | No module in `engine/` creates a `BootstrapDeclaration` | A |

G-8 also requires the concrete socket adapter to pass the transport contract tests (M3);
that is outside M1 and M2.

## 9. M2 plan

### 9.1 Smallest slice: M2-A

Within `engine/sig_client/` only; no `shared/` change, no new REST endpoint, no order
code, no persistence, no live call.

1. The in-memory `RecoveryLedger` (4.1, 4.2): blocker instances replacing the internal
   `set[StaleReason]`, ledger origin and `HISTORY_UNKNOWN`, last accepted revision per
   topic, unobserved windows and fences, generations. A feed constructed without a ledger
   gets a new `unknown` one. `BootstrapDeclaration` exists for tests only (AT-39).
2. The `blockers()` compatibility change of 4.1, reviewed: open and quarantined reasons,
   new blocking reasons `HISTORY_UNKNOWN` and `LIFECYCLE_NOT_OPEN`.
3. The continuity predicate of 4.3, recorded in the readiness result.
4. A recovery attempt runner: one per market, its own read gate, an overall deadline,
   backoff, escalation and cancellation (section 5), with the lifetime guards of 4.5.
5. The observe/commit step of 4.6, with lifecycle reads through the existing `get_market`
   (`rest.py:566`). They only restrict (quarantine) or confirm a settlement.
6. `MarketReadiness` (6.1) and a `readiness(market_id)` accessor; `is_ready` becomes
   `readiness(market_id).status is READY`.
7. Tests AT-01 to AT-19, AT-25, AT-27, AT-28 and AT-30 to AT-39.

M2-A clears no blocker and, in production, never reports `READY`: every process starts
with unknown history. It makes "blocked" explicit and explained, stops a restart or a new
feed object from laundering a blocker, detects settled and closed markets that the feed
misses today (1.6, items 2 and 6), and builds the fencing that any future clearing rule
needs. It reads no trade tape: no predicate can use a tape walk yet (3.2), so the reads
would spend budget for nothing.

### 9.2 Prerequisites for M2-A

- This note reviewed and approved by Arnav, with the independent review, including the
  `blockers()` compatibility change (4.1).
- Values for the configurable bounds (recovery read gate, attempt deadline, backoff,
  escalation count), set by Arnav.
- Confirmation that `MarketReadiness` and the ledger stay engine-internal for now.
- **No decision D-1 to D-6 is needed.** M2-A implements none of the relaxations they
  govern: unobserved windows stay open until confirmed, no bootstrap is declared in
  production, nothing is persisted, nothing is cleared manually, and lost-window and
  trade blockers keep blocking. That is the absence of a relaxation, not the adoption of
  any option; each decision stays open, and any later relaxation needs its own review.
- Nothing from SIG is needed for M2-A, and M3 is not needed (fake transport).

### 9.3 Later slices (not proposed for implementation yet)

- **M2-B:** duplicate-batch detectors (4.7) and a periodic lifecycle sweep with the market
  list [TMKTS] (*configurable* interval).
- **M2-C:** a durable ledger (origin `loaded`) with blocker instances, windows, fences and
  the last accepted revision per topic (D-4). Needs a storage decision; SQLite storage is
  not started.
- **M2-D:** clearing predicates and a production bootstrap, only for guarantees SIG
  documents (section 10) or policies Arnav adopts (section 11), each with its own tests
  (AT-23, AT-24 and new ones).

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
  the topic, including across SIG deploys and restarts and across a client's reconnects?
  If it can reset, how is a client told? (The epoch assumption of 4.3.)
- **Q8 (lost windows).** How should a client find the time or sequence span of a missed
  revision range, so that it knows which tape window to read?
- **Q9 (settlement after-effects).** Can a tournament settlement be corrected or reversed,
  or a closed market reopened? Is a client told?
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

Each is a policy choice, not a platform fact. This note adopts none of them and recommends
none. Until each is decided, M2-A keeps the behaviour that relaxes nothing (9.2).

- **D-1 (unobserved windows).** After a reconnect, a socket or channel error, a token
  refresh or expiry, or a replacement feed sharing the ledger, must a market stay not ready
  until its window is confirmed by the predicate of 4.3 (a clean batch advancing past the
  captured fence, under the epoch assumption)? *Strict* closes item 1 of 1.6 for active
  markets but leaves quiet markets, and any topic with no fence, not ready after every
  reconnect and every token refresh (about every three hours). *As today* accepts that a
  loss in the window is detected only when a later batch arrives. Either way, confirmation
  never covers standalone events (4.3). D-1 does not apply to initial bootstrap (D-3) or to
  a restart without retained history (`HISTORY_UNKNOWN`, 4.2).
- **D-2 (trade-stream continuity).** Does any trading consumer depend on SIG's trade
  stream being complete (for example last-trade marks or lag measurement used in
  decisions)? If none does, `NULL_TRADE_SEQUENCE` and the trade part of lost windows could
  stop blocking new trading, which needs a PRD change. Until decided, they block.
- **D-3 (bootstrap policy).** When may history be declared to begin (a
  `BootstrapDeclaration`), by whom, and what initial-state evidence does it accept? The
  candidate is a lifecycle read showing `open`, made after the subscription, whose
  freshness is unproven (Q1). D-3 governs only the start of history. It never applies to
  restart or reconnect recovery, where history already exists (D-1, D-4), and it cannot
  turn a restart without retained history into a bootstrap. Undecided; until it is decided,
  no production path declares bootstrap, so no market is ever ready in production.
- **D-4 (durable ledger).** Persist the ledger across processes (origin `loaded`)? Then a
  blocked or quarantined market stays so for the rest of the competition unless a clearing
  rule exists. Without persistence, M2-A stays safe because every new process has unknown
  history (4.2), but no market can ever be ready after a restart.
- **D-5 (manual clearing).** Should an operator ever be able to clear a blocker, lift a
  quarantine or declare bootstrap? If so, it would be a controlled, audited command (track
  C1), never automatic. M2 proposes none.
- **D-6 (settlement standard for lost windows).** If D-3 accepts a lifecycle read for
  bootstrap, should the same standard (a lifecycle read showing `open`, made after the
  window) also settle the *settlement* part of a lost window? The trade part would still
  need D-2 or Q3 to Q8. This would make lost-window blockers clearable with residual risk.

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
