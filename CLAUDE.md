# CLAUDE.md — bellwether

## Election-week exception (Nov 2–5, 2026, approved by Arnav)

Track `BOT-EW` builds `bot/`: a minimal, buy-only trading bot for the last days of the
competition. It reuses `engine/sig_client` reads and `engine/config.py`.

- Gates G-1 to G-11 and the PRD roadmap do not apply to `bot/` until Nov 5.
  `bot/CONTRACT.md` is binding for `bot/` and `dashboard/`.
- Hard rules 2, 3, 4, 6, 7 and 8 apply in full. Rule 5 is tightened: `bot/` never sells.
- Rule 1 still applies to agents (tests use fakes only). Arnav alone runs `bot/` live.
- Rule 9: limits live in `config/risk.yaml`, which only Arnav edits.
- Arnav's decisions for `bot/` only: (a) NO orders stay disabled until Arnav sets
  `no_price_convention` in `config/risk.yaml` from the probe result (OQ-W3);
  (b) after a timeout with no response, retry the identical request with the same key
  at most 3 times within 5 minutes; still unknown → pause all new orders and wait for
  Arnav (OQ-W10).
- Ownership: `bot/` → Claude Code. `dashboard/` → Antigravity, read-only access to
  `bot/state/`, may write only `bot/state/STOP`.

Rules and context for Claude Code. Read this first in every session.

## What this project is

A real-time trading system for the SIG Predictions Cup 2026 (US midterm election
markets on SIG's "Super Market" platform, Oct 1 – Nov 4, 2026). Goal: a strong final
rank and Smart Score through disciplined, risk-adjusted trading, concentrated on
election night. Owner: Arnav (approves and merges everything).

- **Authoritative spec: `docs/PRD.md`.** It holds the confirmed requirements (REQ-1 to
  REQ-14), risk policy, roadmap tracks, acceptance gates (G-1 to G-11) and open questions.
  Where anything else disagrees with it, the PRD wins.
- Diagrams: `docs/PRD.pdf` (Oct 2 version, not regenerated; the PRD text wins on conflict).
- API spec: `docs/api/openapi.json`.
- **Historical drafts, not policy:** `docs/PLAN-v2.md` and `docs/stages/*.md` (including
  the dashboard spec) may exist as untracked local files. They are not part of the
  repository, are not current policy and must not be cited as requirements. Do not edit,
  commit or delete them. What still matters from them is carried into the PRD or recorded
  as superseded in its reconciliation appendix.

## Current status (Arnav updates this section)

Trading is **not activated** and no hosting is provisioned. No acceptance gate has
evidence yet. See the PRD roadmap for the full state of `main`.

- **Merged:** `shared/schemas.py`; a SIG REST **read** client (rate limiter, retries,
  deadlines, exact `Decimal` wire parsing) with tournament, market, price, orderbook,
  open-order, market-detail and trade-tape reads; realtime market-data feed logic
  (token refresh, revision gaps, REST orderbook resync, per-market blockers) behind the
  `RealtimeTransport` interface; a bounded read-only fixture recorder and recorded
  fixtures; the Polymarket/Kalshi venue-market fetch script (S1-T5).
- **Legacy S1-T2 and S1-T3 are only partly done.** Merged slices do not make either task
  complete. Still pending:
  - the concrete socket adapter for `RealtimeTransport` (M3), so the feed cannot run
    against SIG yet;
  - market-data recovery design and reconciliation (M1, M2): nothing clears a blocker,
    so a blocked market stays not ready for the life of the feed;
  - account-state contracts and reads (A1 to A3);
  - the entire order path: wire-contract answers, write-side client, idempotency journal,
    `risk.check`, dry run (O1, O2).
- **Not started:** market map, reference collectors, storage, fair value, risk,
  execution, pauses, restart reconciliation, readiness checks, controls, configuration,
  mock exchange, deploy scripts, dashboard. `config/`, `markets/` and `mock_exchange/`
  are empty.
- **Restart is not recovery.** Restarting the realtime feed (or any engine component)
  only discards its in-memory state, including blockers. It does not reconcile missed
  settlements, trades, orders or fills, so a freshly started component's "ready" does not
  show that anything missed earlier was recovered. Recovery needs the reconciliation and
  readiness checks in the PRD (REQ-11, G-3, G-8), which do not exist yet.

## How to work

1. Do exactly one task ID at a time: a roadmap track ID from the PRD (e.g. `A1`, `M3`) or
   a legacy ID still in use (e.g. `S1-T6`). If no task ID was given, ask for one.
2. Work on a branch named `<folder>/<task-id>`, e.g. `engine/a1`.
3. Stop when the done-when condition in the task prompt passes; the PRD's acceptance
   gates say what evidence a track ultimately needs. Run `ruff check .` and `pytest`.
4. Open a pull request: what changed, how it was tested, anything uncertain.
5. When unsure, stop and ask in the pull request. Never guess on anything that touches
   orders, risk, account state or recovery.

## Folder ownership

| Folder | Owner |
| --- | --- |
| `engine/`, `models/`, `mock_exchange/`, `tests/`, `deploy/` | Claude Code |
| `dashboard/`, `data/` | Antigravity — do not edit |
| `shared/` | Ask Arnav before changing; both sides depend on it |
| `config/risk.yaml` | Arnav only — never edit risk limits |
| `docs/` | Arnav / Claude chat — edit only if the task says so |

## Hard rules (never break these)

1. **Never call the live SIG API.** Use recorded fixtures in `tests/fixtures/sig/`, fake
   transports and, once it exists, `mock_exchange/` (whether a full mock exchange is
   still required is open, PRD section I). The only exceptions are scripts Arnav runs
   himself: the fixture recorder (`engine/sig_client/record_fixtures.py`) and one-off
   read-only scripts such as `sig_dump.py` (not in this repository). All API keys share
   one rate budget with the live bot.
2. **Never read, print, log or commit `.env` or any key.** Secrets are read only from
   environment variables through `engine/config.py`. A local `.env` is optional and
   git-ignored.
3. **Every new order passes `risk.check(intent)` and the applicable readiness checks,
   with no bypass.** This covers single, batch and multi-leg placements, sells included
   (a sell can turn into a buy), and any order change that can create or increase
   exposure. The documented API has no amend endpoint; cancel-and-replace is a new order.
   No code path may reach a placement endpoint without these checks. When the order path
   is built (O2), a bypass test must enforce this (G-6); none exists yet because no order
   path exists.

   **Protective cancellation must stay available** during fault, loss and manual pauses,
   and when the readiness check for new trading fails. It is not gated on `risk.check`
   or readiness, but it still goes through engine authorization, is scoped to the
   configured tournament (`tournamentId`), stays within the rate budget and is written to
   the audit log. A cancel path must never place, re-place or enlarge an order. This is
   required design behaviour, not an implemented feature: no cancel path exists yet.
4. **Limit orders only.** Every order carries an explicit limit price on the 0.005 tick,
   0.005–0.995. Never send a market order (`price: 1` buy, `price: 0` sell, or no price).
5. **A sell never exceeds the quantity held,** as established from SIG state for the
   configured tournament. SIG turns a sell that is flat, opposite the held side or larger
   than the holding into its complement buy. If the held quantity or its units are not
   established (OQ-W5), do not sell. Short views are explicit NO buys.
6. **Every order has an idempotency key, durably saved before sending.** On 502
   `ORDER_STATUS_UNKNOWN` or 503, retry the identical request with the *same* key only;
   on 409 `REQUEST_IN_FLIGHT`, wait the documented 90 seconds, then retry the identical
   request with the same key. Never resubmit an order whose outcome is unknown under a
   new key. Retry guarantees beyond the documented cases (key retention, retries after a
   restart or long outage, timeouts with no response: OQ-W9, OQ-W10) are unresolved. Do
   not guess them: code treats such an outcome as unknown and fails closed (the engine
   stays paused), and the design question goes to Arnav in the pull request.
7. **No merge without tests.** Order, risk and control code also needs an independent
   second-model review before merge.
8. **No deploys, provisioning, merges or activation.** Only Arnav deploys, creates or
   changes cloud resources, merges pull requests, and leaves dry run. No agent receives
   the trade key.
9. **Risk limits and strategy settings change only through Arnav.** The PRD's pilot
   ceilings are provisional: they are not in `config/risk.yaml` (which does not exist
   yet), nothing enforces them, and code must not hard-code them as active limits. Agents
   may build validation and change-control mechanisms but never use them to change values.

## Platform facts

- Base URL: `https://www.thesuper.market/api/v1`; auth header `Authorization: Bearer <key>`
- Tournament: slug `midterm-elections`, id `bda92870-621e-47b0-bc3c-3602c5c26f55`.
  Always pass `tournamentId` explicitly; calls without it silently use a default context.
  `GET /account` has no `tournamentId` parameter; its cash is not tournament cash without
  evidence (OQ-W11).
- Rate limits per account: 100 reads + 30 writes per minute. Our budget: 80 reads, 25 writes.
  A batch (≤ 50 orders) or multi-leg (≤ 10) request counts as one write.
- Market prices are YES-denominated in [0, 1]; a NO share is worth `1 − YES price`.
  Whether a NO order's wire `price` carries the YES or the NO price is open (OQ-W3).
- Realtime: Supabase private channels via `POST /realtime/token` (3h tokens).
  Tournament market topic: `tournament:{tournamentId}:market:{marketId}`.
  Track `delivery.revision` / `previousRevision`; resync the orderbook over REST on any
  gap, `resyncRequired`, reconnect or token refresh. An orderbook resync refreshes books
  only; it never clears a settlement or unsequenced-trade blocker.
- Markets: 237 YES/NO markets across 117 races. Each race has a Democratic and a
  Republican market (three races also have an Independent one). No composite,
  multi-outcome or linked markets.

## Wire values and domain units

- **Wire values are exact.** SIG responses are decoded with `Decimal`
  (`engine/sig_client/wire.py`); every JSON number keeps its exact value, and raw records
  and fixtures keep wire values unchanged. Never pass them through `float`.
- **Domain units are Bellwether's own,** defined in `shared/schemas.py`, and are not SIG's
  wire format. Currently:
  - `OrderIntent.price`: internal YES-denominated limit price for YES and NO orders alike,
    integer ticks (`price × 200`), 1–199.
  - `Fill.price`: internal YES-denominated integer ticks, 0–200 or none. This type assumes
    fills land on the 0.005 grid, which is unresolved (OQ-W7).
  - `Quote.bid` / `Quote.ask`: integer milli (`price × 1000`), 0–1000; `Quote.mid` is a
    computed float in milli.
  - `FairValue.value` and `OverrideCommand.value`: float milli, 0–1000, may be off grid.
  - `Fill.quantity` and the `Position` quantity, cost, price and P&L fields are plain
    floats. Docstrings describe signed quantities, but the wire sign, unit and
    denomination are open (OQ-W4, OQ-W5).
- **Convert only at the API boundary, and only where PRD section W has settled the rule.**
  Open: quote precision (OQ-W1), executable rounding (OQ-W2), NO-order wire price
  (OQ-W3), fill units (OQ-W4), position units (OQ-W5), `priceLimit` denomination (OQ-W6),
  fills on the tick grid (OQ-W7), routing and netting (OQ-W8). Until answered, keep the
  value in its exact wire form, flag it as unresolved and fail closed; never round or
  reinterpret it.
- **Do not infer API contracts from schema types,** field names, docstrings or examples.
  A schema type is an internal choice, not evidence of what SIG sends.
- `RiskLimits`, `RiskState`, `Mode`, `ModeCommand` and `OverrideCommand` model superseded
  Oct 2 policy (percentage limits by mode, a kill flag, `/mode` and `/override`). Do not
  build on them; replacements need Arnav's approval.

## Code conventions

- Python 3.11, type hints everywhere, Pydantic v2 models from `shared/schemas.py`
- `httpx` (async) for REST, `asyncio` throughout, SQLite in WAL mode for storage
- All timestamps stored in UTC; displays use IST
- Every trade decision logs a human-readable `reason`
- Tests with `pytest`; lint with `ruff`
