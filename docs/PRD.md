# PRD — SIG Predictions Cup Trading System

Oct 2, 2026 · @Arnav · Revised Oct 12, 2026 (DOCS-T1: requirements and roadmap reconciled)

## Document status

This file is the single authoritative specification for Bellwether's requirements, risk
policy, roadmap, acceptance gates and open questions. Where an earlier plan disagrees with
it, this revision wins; the [reconciliation appendix](#appendix-reconciliation-with-earlier-plans)
lists every superseded statement and what replaced it.

- **Safety rules** in [`CLAUDE.md`](../CLAUDE.md) and [`AGENTS.md`](../AGENTS.md) still apply
  in full. Known conflicts between those files (and `shared/schemas.py`) and this revision
  are listed in the appendix for a separate, focused correction; they are not changed here.
- **Not authoritative:** `docs/PLAN-v2.md` (Oct 9) and `docs/stages/*.md` (Oct 2 exports)
  exist only as uncommitted local drafts. They are not part of the repository and are not
  current policy. Their useful content is either carried into this file or recorded as
  superseded in the appendix.
- **Diagrams:** [`PRD.pdf`](PRD.pdf) holds the Oct 2 diagrams. It was not regenerated for
  this revision; where it conflicts with this file, this file wins.
- **API facts** cite [`api/openapi.json`](api/openapi.json). "Documented" means the spec says
  so; it does not mean the behaviour has been observed on the tournament. Questions the
  spec does not settle stay open in [section W](#w-api-wire-contract) and are not answered
  by inference.

## Overview and goals

We are building a real-time trading system for the SIG Predictions Cup 2026 (US midterm
election markets, Oct 1 – Nov 4, 2026) aimed at a strong final rank and Smart Score
through disciplined, risk-adjusted trading, concentrated on election night. It doubles as
a resume project for quant, SWE and AI roles.

**Decision (Oct 2), still in force:** we do not chase top 3. Every market opened at 50c,
and the leaders turned that into gains of 350k–625k SUSQies on day one; catching them
would need concentrated tail bets that most likely end far down the table. Time budget is
1–2 hours a day, with exams Oct 17–26.

**Decision (Oct 12):** the system trades automatically within configured limits, without
per-trade approval, on a separate cloud server that runs unattended. Human monitoring is
optional, so every protection must work with nobody watching. Trading is not activated by
this document; activation follows the [acceptance gates](#acceptance-gates).

In plain terms: the system watches prices on SIG's platform, compares them with prices on
Polymarket and Kalshi, trades when SIG's price is wrong, protects itself with strict risk
limits, recovers safely from faults, and can be watched and controlled from a terminal
first and a dashboard later.

**Goals, in priority order**

1. Strong final rank with low drawdown (top 100 of \~3,900 entrants).
2. Strong Smart Score, SIG's risk-adjusted rating from P&L, ROI, win rate and volatility,
   which feeds its recruiting.
3. Never lose SUSQies to a bug: no duplicate orders, no accidental position flips, no
   runaway trading, and exposure from orders left resting during an outage kept within
   verified bounds.
4. Measured evidence for the resume: how far SIG lags Polymarket, election-night reaction
   speed, calibration.

**Success metrics**

| Metric | Target |
| --- | --- |
| Final rank | Top 100 (stretch: top 25) |
| Maximum drawdown from peak balance | Under 10% |
| Order errors causing losses | 0 |
| Unmanaged exposure during an outage | Within the bounds recorded at gate G-4: a verified maximum resting-order lifetime and a maximum commitment held in resting orders |
| Server uptime, from first unattended deployment to Nov 4 | ≥ 99% |
| Election-night reaction time to a Polymarket move | Under 5 seconds |
| Repricing-lag study | Distribution of SIG's lag behind Polymarket, published in the post-mortem |

## Confirmed requirements (Oct 12)

Confirmed by Arnav on Oct 12, 2026. Acceptance gates and open questions refer to them by
ID.

| ID | Requirement |
| --- | --- |
| REQ-1 | **Simulated funds only.** The system trades only SIG competition SUSQies. Starting balance: 100,000 SUSQies. No real money is traded anywhere, on any venue. |
| REQ-2 | **Automatic execution** within configured limits, without per-trade human approval. |
| REQ-3 | **Unattended operation** on a separate cloud server, independent of Arnav's laptop and mobile hotspot. Human monitoring is optional; safety must not depend on it. |
| REQ-4 | **₹0 hosting.** Hosting uses only eligible free resources or credits. Any resource that could create a charge is out of scope. |
| REQ-5 | **Azure for Students is the first hosting candidate.** Eligibility, permitted use and allowances are unverified (see [hosting](#hosting-and-operations)). |
| REQ-6 | **Reference data:** Polymarket and Kalshi supply reference prices for v1. Both are read-only public data sources; we hold no positions there. |
| REQ-7 | **Engine before UI.** The operational engine and terminal controls come before any UI/UX work. |
| REQ-8 | **Eventual dashboard:** monitoring; pause; readiness-checked resume; cancellation; editing implemented strategy settings and risk limits. The dashboard never places trades. |
| REQ-9 | **Controlled configuration changes:** every change is validated, versioned and written to an audit log, and includes an assessment of existing positions and pending orders against the new settings. |
| REQ-10 | **Engine protections are independent of the dashboard.** Risk checks, pauses, cancellation and reconciliation run inside the engine and keep working if the dashboard is absent, disconnected or wrong. |
| REQ-11 | **Restart recovery** reconciles account and order state with SIG before any new order is submitted. |
| REQ-12 | **Temporary faults** may resume automatically, but only after the recovery checks pass. |
| REQ-13 | **Loss-triggered pauses** require an explicit human reset; they never resume automatically. |
| REQ-14 | **Exchange-side outage protection must be verified.** A stopped or unreachable server cannot be assumed to cancel its orders. |

## Provisional pilot ceilings

**Status: provisional. Not activated and not finally selected.** They are not in
`config/risk.yaml` (that file does not exist on `main` yet), and the engine has no code
that enforces them. Only Arnav sets and commits risk limits.

| Ceiling | Provisional value (SUSQies) |
| --- | --- |
| Per order | 500 |
| Per race | 2,000 |
| Per related-market group | 3,000 |
| Total commitment, including pending orders | 10,000 |
| Minimum uncommitted cash | 90,000 |
| Loss trigger | 2,000 below starting equity, measured under a documented valuation policy |

How to read them:

- **Ceilings, not targets.** Nothing in the system aims to use the full amount. Idle cash
  is an acceptable outcome; no rule requires deploying it.
- **Not a guaranteed maximum loss.** The loss trigger pauses trading when measured equity
  falls 2,000 below the baseline. Losses can exceed that: binary contracts can settle at
  0 between checks, marks can be stale or missing, prices can gap, and orders left resting
  during an outage can fill after the engine stopped watching. The total-commitment
  ceiling (10,000) is the closer bound on what is at risk, and only if commitment is
  counted conservatively.
- **Undefined terms block activation.** "Commitment", "uncommitted cash", "race",
  "related-market group", "starting equity" and the valuation policy are not yet defined
  precisely, and the API units they depend on are open ([section W](#w-api-wire-contract)).
  Gates G-1, G-2 and G-7 ([acceptance gates](#acceptance-gates)) must close them before
  these numbers mean anything.

### Working definitions (proposed, not yet confirmed)

These are proposals for Arnav to confirm or change through the open questions; code must
not rely on them until they are confirmed in this file.

- **Commitment** (proposed, conservative): for each open position, the cash paid for it;
  plus, for each resting buy order, the cash it would consume if its unfilled remainder
  filled in full; plus, for every submission whose outcome is unknown (timeout, 502
  `ORDER_STATUS_UNKNOWN`, 503), the full amount of the order as if it had filled.
  Positions in different markets of one race are summed, not netted. How cash paid is
  read from SIG depends on the unresolved units of positions, fills, NO-side prices and
  netting (OQ-W3 to OQ-W8).
- **Uncommitted cash:** cash belonging to the configured tournament, minus every
  reservation not already deducted by SIG. **The source of tournament cash is not
  established.** `GET /account` has no `tournamentId` parameter and is documented to return
  the key's default-tournament balance (organization-bound key) or the public-global
  balance (unbound key). The engine must have evidence that the cash it uses belongs to
  the configured tournament and must never silently substitute default-context cash
  (OQ-W11). Whether any balance already excludes cash held by resting orders is not
  documented (OQ-C3).
- **Race:** the SIG race ID carried by each contract (117 races).
- **Related-market group:** not defined. Candidates are listed in OQ-C4
  ([section C](#c-commitment-and-classification)).

## Competition constraints

Every design decision below follows from these rules and API limits; breaking one costs
either SUSQies or eligibility.

| Constraint | Detail | Design consequence |
| --- | --- | --- |
| Trading window | Oct 1, 9:30pm IST → Nov 4, 10:30pm IST (12pm ET both ends) | Election night falls in IST daytime, Nov 4, 4:30am–11:30am poll closings |
| Scoring | Balance after every market resolves | Positions settle after lock. Idle cash earns nothing, but no rule here requires deploying it |
| Funds | Simulated SUSQies; starting balance 100,000 | REQ-1: nothing else is ever traded |
| Payout | Only top 3 paid | Leaders gained 4–7x on opening day; we optimise risk-adjusted return and Smart Score instead |
| Rate limit | 100 reads + 30 writes per minute, per account | WebSockets for data; batch orders (50 per write); every key shares this budget, so dev tools and agents use the mock exchange, never the live API |
| Order price grid | Documented: fresh limit prices must be on the 0.005 tick between 0.005 and 0.995; off-tick values return 400 | Every limit price we send is on that grid. Quotes, fair values and fills are not assumed to be on it; how a target becomes an executable price is open (OQ-W1, OQ-W2, OQ-W7) |
| Price denomination | Market prices are YES-denominated in \[0, 1\] | A NO share's value is 1 − the YES price. The wire price of a NO order, and the denomination of fills, positions and `priceLimit`, are open (OQ-W3 to OQ-W6) |
| Sell semantics | Documented: a `SELL` that is flat, opposite the held side, or larger than the held quantity is turned into its complement `BUY` | Sells only close held positions; a short view is taken by buying NO |
| Idempotency | Every order needs an `idempotencyKey` | Saved before sending; retry 502/503 with the same key only. Key retention and retry guarantees beyond the documented cases are open (OQ-W9, OQ-W10) |
| Market orders | Documented: `price: 1` buy, `price: 0` sell, or an omitted price is a market order | Never sent; every order carries an explicit on-tick limit price |
| One account, individual | Multi-accounts or SUSQie transfers = disqualification | No teammates trading for us |
| MNPI | No trading on non-public info | Public data only |
| Data ownership | SIG owns competition data and derived analytics | Publish code, never raw dumps; repo private until after Nov 4 |
| Resolution rules | Not returned by the API | Copy each market's Info tab into `markets/` by hand (not yet done) |

**Market facts from the Oct 2 dump**

- 237 YES/NO markets across 117 races: Senate, Governor, about 40 competitive House
  seats, and House and Senate control. Each race has a Democratic and a Republican market;
  Montana Senate, Nebraska Senate and Rhode Island Governor also have an Independent one
- No composite, multi-outcome or linked markets, so the engine's constraint alerts and
  ALL collateral never apply
- Median spread is one tick (0.5c), with about 5,600 SUSQies of depth within 2c of mid;
  SIG's market maker visibly quotes 144 markets
- SIG prices track Polymarket and Kalshi closely (Senate control: SIG 63.5% D vs 63%
  averaged); Democratic/Republican pair gaps are 0.5–1c on a few hundred shares
- 752 active traders; the top 8 are up 350k–625k SUSQies after buying at the 50c opening
  prices
- Each contract carries a race ID, stage ID and resolution type "Party Winner"; the exact
  resolution source is still to confirm from the Info tabs

## Scope

**In scope for v1**

- Read-only SIG account, order and market state; Polymarket and Kalshi reference prices
- Fair value from the reference prices, per mapped contract
- Automatic limit-order execution behind one `risk.check(intent)`, with dry run as the
  default mode
- Pauses, cancellation, restart reconciliation and readiness checks inside the engine
- Terminal controls (CLI): status, pause, readiness-checked resume, cancel, loss reset,
  validated configuration changes
- A ₹0 cloud deployment that runs unattended
- Logging of every quote, fair value, signal, order, fill, risk block, pause and
  configuration change

**Later:** the dashboard (REQ-8), election-results recorder, lag-study report, Bayesian
vote-count model (November, no trading).

**Out of scope:** real-money trading; trading on Polymarket or Kalshi; any paid hosting or
data feed; reinforcement learning; deep-learning price prediction; any LLM that places
trades; passive quoting and constraint arbitrage (cut Oct 2: SIG's market maker already
quotes one-tick spreads, and pair gaps are 0.5–1c on a few hundred shares).

## System architecture

One Python engine runs the whole loop on a cloud server. All protections live in the
engine; the terminal and, later, the dashboard only observe it and send it control
commands that the engine validates.

> Diagram: system architecture · data in, orders out, everything logged (see `docs/PRD.pdf`; Oct 2 version).

Prices flow in from three venues, become one fair value per contract, turn into signals,
and reach SIG only after the risk check. Every step writes to the SQLite log.

**Control path.** Operator commands (pause, resume, cancel, loss reset, configuration
change) enter through one engine control interface. The terminal uses it first; the
dashboard later calls the same interface through the engine API. No control path places
an order, and none bypasses `risk.check`, readiness checks or configuration validation.

## Shared contracts

The engine (Claude Code) and dashboard (Antigravity) never share code; they share only
these schemas, defined first in `shared/schemas.py` (Pydantic) and exported as JSON Schema
for the dashboard.

**Wire values and domain values are separate.** API responses are parsed exactly: every
JSON number is decoded as a `Decimal` and keeps its exact value
(`engine/sig_client/wire.py`), and raw records keep those values unchanged. Bellwether's
internal domain units are defined in `shared/schemas.py` (order limit prices as integer
ticks 1–199; quotes and fair values in milli, price × 1000). Conversion between the two
happens only at the API boundary. Where a conversion depends on an open question in
[section W](#w-api-wire-contract), the rule is not decided, and unresolved values stay in
their exact wire form rather than being rounded or reinterpreted.

**Core data model**

| Object | Key fields | Notes |
| --- | --- | --- |
| `Market` | sig\_market\_id, title, exchanges\[\], is\_composite, resolution\_text, settlement\_date, lean (D / R / none) | resolution\_text copied from the Info tab |
| `MarketMapping` | sig\_exchange\_id, polymarket\_token\_id, kalshi\_ticker, match\_confidence, mismatch\_notes | Hand-built first |
| `Quote` | source (sig/polymarket/kalshi), exchange\_id, bid, ask, mid, ts | YES-denominated, internal milli (price × 1000); quote precision per venue is open (OQ-W1) |
| `FairValue` | exchange\_id, value, inputs{}, model\_version, ts | Internal milli; may be off the order grid. inputs records each source's weight |
| `Signal` | exchange\_id, strategy, side, edge\_cents, size, reason | reason is human-readable |
| `OrderIntent` | idempotency\_key, exchange\_id, side, action, qty, price | Internal YES-denominated limit price in ticks; the wire price for NO orders is open (OQ-W3). Passes risk checks before sending |
| `Fill`, `Position`, `RiskState` | Mirror API Fill/Position; RiskState holds limits and pause state | Wire units of fills and positions are open (OQ-W4, OQ-W5, OQ-W7) |

Schemas for pause state, readiness results and configuration versions are not defined
yet; they are added to `shared/` with Arnav's approval when the tasks that need them start.

**Engine → dashboard messages** (WebSocket, JSON, one envelope)

```json
{"type": "quote_update | fair_value | signal | order | fill | position | risk | log | health",
 "ts": "2026-10-08T14:02:11Z",
 "data": { }}
```

**Dashboard → engine commands** (later; REST, password-protected): pause; resume (runs the
readiness check first and refuses if it fails); cancel (all, or one market); loss reset;
propose a configuration change (validated, versioned, audited and impact-assessed by the
engine, per REQ-9). The dashboard cannot place trades. The Oct 2 command set (`/kill`,
`/resume`, `/mode`, `/override`) is superseded; see the appendix.

**Storage:** SQLite (WAL mode), every row timestamped in UTC; one table per object above.
Parquet export nightly for analysis.

## Strategies

No strategy is implemented, selected or activated. The candidates below are kept for
design work; each one runs only through the same risk check, ceilings and gates.

| Candidate | How it works | Status |
| --- | --- | --- |
| Dislocation capture | Buy where SIG's price is at least a threshold away from fair value; exit when it converges | Proposed in the Oct 9 draft plan; thresholds not calibrated |
| Pair check within a race | If the best YES bids across a race's parties sum above 1 by more than a threshold, buy NO on every party | Proposed in the Oct 9 draft plan; the Oct 2 dump showed gaps of 0.5–1c on a few hundred shares |
| S4 election night | Reference prices are fair value; trade where SIG lags them, spread across many races | Designed after the lag data exists |

**Fair value v1:** the mid of Polymarket and Kalshi, weighted by book depth; a source is
ignored when its spread is wider than 4c or its quote is stale. For contracts under 10c or
over 90c, the estimate is pushed slightly away from 50%, because real-money markets
overprice long shots. Contracts with a resolution mismatch, or a mapping below the
confidence threshold, get no fair value and are not traded. Fair value is not rounded to
the order grid; turning it into an executable limit price is open (OQ-W2).

## Risk management

Every order passes the same pre-trade checks in one function, `risk.check(intent)`, and
any failure blocks the order and logs why. No code path reaches the order endpoints
without it; a test enforces this.

**Pre-trade checks**

- Limit price on the 0.005 tick, between 0.005 and 0.995; never a market order
- A sell never exceeds the quantity held, so it cannot silently flip into the opposite
  side; short views are explicit NO buys
- Unique idempotency key, saved before sending; retries reuse it
- Fair value younger than the configured maximum age, with at least one reference source
  live
- Per-order, per-race, per-group and total-commitment ceilings not exceeded, counting
  pending and unresolved orders; minimum uncommitted cash preserved
- The engine is not paused and its last readiness check passed
- Rate budget: under 25 of 30 writes and 80 of 100 reads per minute, leaving headroom for
  reconciliation and cancels

**Pause states**

| State | Triggered by | On entry | Leaves by |
| --- | --- | --- | --- |
| Fault pause | Stale reference data, repeated rate-limit or 5xx errors, a market not ready in the realtime feed, a local/SIG state mismatch, lost connectivity | Stop new orders; attempt to cancel resting orders | Automatically, only after the readiness check passes (REQ-12) |
| Loss pause | Equity at or below the loss trigger, under the documented valuation policy | Stop new orders; attempt to cancel resting orders | Explicit human loss reset (REQ-13), then the readiness check |
| Manual pause | Operator command (terminal now, dashboard later) | Stop new orders; cancel if the operator asks | Operator resume, then the readiness check |
| Startup | Engine start or restart | No orders until reconciled | Readiness check passes (REQ-11) |

The exact fault thresholds (Oct 2 values: no reference price for 60 seconds; five
rate-limit or 5xx errors in a minute) are carried forward as starting points and must be
confirmed in configuration.

**Readiness check** (required before the first order after startup, any resume or any
loss reset): cash for the configured tournament and positions read from SIG, with
evidence of their scope; every open order listed across all pages; every locally
journalled idempotency key with an unknown outcome resolved to placed or not placed; local
state matches SIG; every market to be traded is ready in the realtime feed with no
outstanding blocker; reference data fresh; configuration version valid; rate budget
available. Any unresolved item keeps the engine paused. Reads from different endpoints are
not assumed to form one atomic snapshot; how readiness tolerates changes between reads is
part of the recovery design (roadmap item M1).

**Market-data blockers.** The realtime feed on `main` marks a market not ready for the life
of the feed when a settlement signal or a trade without a sequence may have been missed
(including after a revision gap, a `resyncRequired` batch, a malformed message or a batch
missing a named book). An orderbook resync never clears these blockers, and restarting the
feed only discards its in-memory state: a restart is not a recovery procedure. Clearing a
blocker needs blocker-specific evidence from authoritative reads, which is not designed
yet (roadmap items M1, M2; gate G-8).

**Cancellation.** Documented: `POST /orders/cancel-all` scoped by `tournamentId` returns
200 once no targeted order remains open and 503 if it paused part-way; the same request is
retried until 200. This works only while the engine and the API are both reachable, and
its behaviour on the tournament is unverified until gate G-4 records evidence.

**Outage protection (REQ-14).** If the server stops, loses network or runs out of hosting
allowance, nothing on our side can cancel resting orders. **Resting orders may remain live
and may fill during an outage**, and the system must be designed on that assumption until
verified behaviour says otherwise. Options:

- An `expirationDate` on every resting limit order (documented as optional on limit
  orders), so orders would lapse on SIG's side without our help. Unverified on the
  tournament: whether it is accepted, and whether expired orders stop filling (OQ-G1).
  This is the only listed option that could act while our server is down.
- Counting every resting order at full fill value in commitment, so fills during an outage
  stay inside the ceilings, provided commitment is defined to cover them (OQ-C2). This
  bounds exposure; it does not stop fills.
- Placing marketable limit orders and cancelling any unfilled remainder at once. This
  needs a reachable server and API to send the cancel, so it is **not** independent
  outage protection: an outage between placement and cancel leaves the remainder resting.
  It only reduces how much normally rests.

Gate G-4 records measurable bounds: the maximum lifetime of any resting order, backed by
verified expiry behaviour, and the maximum commitment allowed to rest. None of these
options may be relied on until G-4 records evidence.

## Configuration change control

Risk limits and strategy settings change only through human action; AI agents never
change them.

- **Interim rule (until gate G-5 passes):** risk limits change only by a human commit to
  `config/risk.yaml`, and the engine reads them at startup.
- **Target (REQ-9):** a configuration change, from the terminal and later the dashboard, is
  validated (schema, ranges, cross-field consistency, e.g. minimum uncommitted cash plus
  total commitment cannot exceed starting equity), given a new version, written to an
  append-only audit log (who, when, old and new values, reason), and assessed against
  current positions and pending orders before it takes effect.
- **Open:** whether runtime changes may only tighten the committed values or may also
  loosen them; and what happens when existing positions or orders already exceed a new
  limit ([section F of the open questions](#f-configuration-changes)).

## Hosting and operations

- **Target:** one cloud VM that runs the engine unattended, independent of the laptop and
  hotspot (REQ-3), at ₹0 (REQ-4).
- **First candidate:** Azure for Students. **Unverified:** that Arnav's account is
  eligible; that the offer's terms permit running an automated trading bot for a
  competition; the credit or free allowance available; whether a card or payment method
  is attached; and what happens when credit or the free allowance runs out (VM stopped,
  deallocated or deleted; disks and logs kept or lost; any charge).
- **Backup candidate:** Oracle Cloud Always Free. Same items unverified.
- **Credit exhaustion and server stops** are treated as outages: resting orders may stay
  live and fill (REQ-14). Exposure while the server is down must stay within the bounds
  verified at gate G-4, and staying inside the ceilings must not depend on the server
  being reachable.
- **Alerts:** Telegram alerts on pauses, faults and a daily summary are useful but
  optional; no protection depends on someone reading them.
- **Deploys:** only Arnav deploys. No AI agent receives the trade key or deploys.

## Tooling, ownership and automation

Each tool owns its own folders and never edits another's; they meet only at `shared/` and
through pull requests that Arnav merges.

| Folder | Owner | Work |
| --- | --- | --- |
| `engine/` | Claude Code | API client, collectors, fair value, strategies, execution, risk, controls |
| `models/` | Claude Code | Correlated simulator, vote-count model |
| `mock_exchange/` | Claude Code | Fake SIG API replaying recorded responses, for safe testing |
| `deploy/` | Claude Code | Deploy scripts; Arnav runs them |
| `dashboard/` | Antigravity (IDE) | React UI; verified in its built-in browser |
| `data/` pipelines | Antigravity agents | Venue market lists, historical results download and cleaning |
| `shared/` | Arnav approves changes | Schemas both sides depend on |
| `config/risk.yaml` | Arnav only | Risk limits |
| `docs/` | Arnav / Claude chat; others only when a task says so | PRD, decisions, post-mortem |

**Automation**

- Pre-commit: gitleaks secret scan, ruff lint, block any `.env` file
- GitHub Actions: tests on every push
- Cross-model review of every order, risk or control pull request before merge
- Cloud server: systemd keeps the engine running; health check every 2 minutes

**Security rules**

- Two keys: a read-only key for one-off scripts like `sig_dump.py`; a read+trade key that
  exists only on the cloud server
- All keys share one 100-read, 30-write budget per minute, so once the bot is live,
  development and agents run against the mock exchange and recorded data, never the live
  API
- No AI agent ever receives the trade key or deploys to the server
- Risk limits change only as described in
  [configuration change control](#configuration-change-control)
- Engine control API and dashboard bound to localhost; reached through an SSH tunnel,
  never exposed publicly

## Repo structure and conventions

One private GitHub repo, `bellwether`, Python 3.11 for the engine and React + Vite for the
dashboard.

```
bellwether/
├── CLAUDE.md            # rules Claude Code reads every session
├── AGENTS.md            # same rules for Antigravity
├── config/              # risk.yaml, market_map.yaml (neither exists yet)
├── shared/              # schemas.py → JSON Schema
├── engine/
│   ├── sig_client/      # REST + realtime client, rate limiter, fixture recorder
│   ├── collectors/      # Polymarket and Kalshi reference prices
│   ├── fair_value/
│   ├── strategies/
│   ├── execution/
│   ├── risk/
│   └── api/             # engine control API (terminal first, dashboard later)
├── models/              # simulator/, vote_count/
├── mock_exchange/
├── dashboard/
├── data/                # pipelines only; raw data git-ignored
├── markets/             # one .md per market: Info-tab resolution text
├── tests/
└── docs/                # PRD, API spec, decisions, post-mortem
```

**Conventions**

- Branch per task; pull request to `main`; Arnav merges
- Every module ships with tests against the mock exchange or recorded fixtures; no test,
  no merge
- All times stored in UTC; displays use IST
- API values are parsed exactly (`Decimal`) and kept exact in raw records; internal domain
  units are those in `shared/schemas.py`; conversion happens only at the API boundary, and
  only where section W has settled the rule
- Secrets are read only from environment variables, through `engine/config.py`. A local
  `.env` file is optional, git-ignored and never committed; `.env.example` lists the
  variable names with placeholder values
- Every trade decision logged with its `Signal.reason`

## Roadmap

Work is organised in tracks. **An item starts as soon as its own dependencies are met; it
does not wait for unrelated tracks.** Only fixed external dates are listed: the trading
window closes Nov 4, 10:30pm IST; election-night poll closings start Nov 4, 4:30am IST;
Arnav's exams run Oct 17–26. **No replacement deadlines are set in this revision;** Arnav
sets dates. If the gates are not met in time, the system stays in dry run: not trading is
an acceptable outcome, unsafe trading is not.

**State of `main` on Oct 12** (commit `2e3e464`):

- Built: `shared/schemas.py`; SIG REST read client with rate limiter, retries, deadlines
  and exact `Decimal` wire parsing; open-order listing; market detail and trade-tape
  reads; realtime market-data feed logic with revision gaps, REST orderbook resync and
  per-market blockers; bounded read-only fixture recorder and recorded fixtures; Polymarket
  and Kalshi venue-market fetch script in `data/venue_markets/` (S1-T5).
- Gaps in the realtime feed: the socket layer is an interface (`RealtimeTransport`) with
  **no concrete adapter**, so the feed cannot run against SIG yet; and **nothing reconciles
  blocked markets**: the market-detail and trade-tape reads exist but are not used to clear
  blockers, so a blocked market stays not ready for the life of the feed.
- Not started: account-state reads beyond open orders, market map, reference collectors,
  storage, fair value, trading client, risk, execution, pauses, reconciliation, controls,
  configuration, mock exchange, deploy scripts, dashboard. `markets/` and `config/` are
  empty.

| ID | Track | Work | Depends on | Gates |
| --- | --- | --- | --- | --- |
| P0 | Docs | This reconciled PRD | none | none |
| A1 | Account state | **Contract and design review** of the account-state reads (next task, below). Design note (draft, under review): [`design/account-state-contracts.md`](design/account-state-contracts.md) | P0 | Input to G-1, G-2, G-3, G-7 |
| A2 | Account state | Raw tournament-scoped reads, keeping unresolved values exact and flagged | A1 reviewed by Arnav | G-7 |
| A3 | Account state | `AccountSnapshot` that reports what it can and cannot vouch for | A2 | Input to G-3 |
| M1 | Market data | **Recovery design:** blocker-specific recovery evidence; events that arrive during recovery reads; projection freshness of REST reads; how market readiness feeds engine readiness. Design note (draft, under review): [`design/market-data-recovery.md`](design/market-data-recovery.md) | P0 | G-8 |
| M2 | Market data | Market-data reconciliation implementing M1 with the existing market-detail and trade-tape reads | M1 reviewed | G-8 |
| M3 | Market data | Concrete socket adapter implementing `RealtimeTransport` | Existing interface | G-8; exercised in G-10 |
| R1 | Reference data | S1-T6 race mapping (Antigravity) | S1-T5 output (merged) only | Input to G-1 classification |
| R2 | Reference data | Polymarket and Kalshi collectors; SQLite storage | R1, S1-T1 | none |
| R3 | Reference data | Fair value v1 | R1, R2; quote precision (OQ-W1) | none |
| O1 | Order path | Wire-contract answers: spec citations and real-platform checks Arnav runs himself | none | G-7 |
| O2 | Order path | Write-side client, idempotency journal saved before send, `risk.check` with commitment accounting, dry-run default, bypass test; mock exchange or fake transport | O1; G-1 definitions | G-6 |
| S1 | Protections | Pause states; valuation and loss trigger; restart reconciliation; readiness check; cancellation and outage protections | A3, M2, O2 | G-2, G-3, G-4 |
| C1 | Controls | Terminal controls and configuration change control | S1 | G-5 |
| H1 | Hosting | ₹0 hosting verification by Arnav; deploy scripts | none | G-9 |
| H2 | Hosting | Unattended dry-run soak on the server | H1, M3, S1, C1 | G-10 |
| X1 | Activation | Pilot activation at the confirmed ceilings, by Arnav's explicit decision | G-1 to G-10 all passed | G-11 |
| E1 | Strategy | Election-night strategy and settings: designed and dry-run tested once its inputs exist; trades only after X1, through the configuration flow | R3, O2 | G-5, G-11 |
| D1 | Dashboard | Monitoring first, then controls, then configuration editing (REQ-8) | C1 (REQ-7) | G-5 for editing |
| W1 | Wrap-up | Post-mortem, lag study, vote-count model, repo clean-up | none | none |

**Next bounded engine task (A1; Arnav assigns the ID): account-state contract and design
review, before any code.** For each account read the engine would use (`GET /account`, and
the tournament-scoped positions, fills, P&L and orders reads), write down from
[`api/openapi.json`](api/openapi.json):

- its scope: which parameter, if any, pins it to the configured tournament.
  `GET /account` has none and is documented to return the key's default-tournament or
  public-global balance, so its cash must not be used as tournament cash without evidence;
- the units and denomination of every price and quantity it returns, citing the spec or
  marking the question open in section W;
- pagination, documented error cases (such as the tournament P&L 409 for holdings without
  a valuation price) and any coverage or projection metadata.

Then propose the A2/A3 shape: raw reads may keep unresolved values exactly as sent, but
the `AccountSnapshot` must not claim valuation consistency, atomicity across endpoints or
recovery readiness without supporting evidence, and must say which values are unresolved.
The output is a reviewed design note plus a list of the real-platform checks Arnav would
need to run. No order endpoints, no live calls, no `shared/` changes without approval.

**Legacy task IDs.** The Oct 2 stage drafts numbered tasks `S0-T*` to `S5-T*`. Merged to
`main` so far: S0-T6, S1-T1, S1-T2, S1-T3 (A and B), S1-T4, S1-T5. Legacy IDs still in
use, such as S1-T6, keep their IDs; other new work uses the track IDs above.

## Acceptance gates

Each gate needs recorded evidence (test output, a logged real-platform check, or a
written decision by Arnav) linked from this table. **No gate has evidence yet.**

| Gate | Area | Passes when | Status |
| --- | --- | --- | --- |
| G-1 | Commitment and classification | Commitment, reservation and uncommitted cash are defined in this file; the source of tournament cash is established with evidence (OQ-W11); every market is classified to a race and a related-market group, with the classification committed and reviewed; tests show each ceiling counts filled, resting and unresolved orders as defined | Not met |
| G-2 | Equity and loss trigger | A written valuation policy states the equity formula, the mark source, and what happens with stale or missing marks (including the documented 409 for holdings without a valuation price); the loss-trigger baseline is defined; tests show the trigger latches, attempts cancellation and holds until an explicit reset, and what the reset re-checks | Not met |
| G-3 | Restart and readiness | Tests show that after a restart no order is sent until tournament cash, positions, open orders (all pages) and fills are reconciled; every journalled idempotency key with an unknown outcome is resolved or keeps the engine paused; reconciliation uses only data shown to belong to the configured tournament; nothing claims atomicity across endpoints without evidence; a failed readiness check keeps the engine paused | Not met |
| G-4 | Outage protection and cancellation | Logged real-platform evidence, from checks Arnav runs himself with tiny orders priced far from the market, of: tournament-scoped cancel-all cancelling every open order; whether `expirationDate` is accepted on tournament orders and stops them filling after expiry. Recorded, measurable bounds: maximum resting-order lifetime and maximum commitment allowed to rest, each backed by that evidence. Until then, resting orders are assumed to stay live and fill during an outage | Not met |
| G-5 | Configuration changes | Tests show invalid changes are rejected; every accepted change gets a new version and an audit-log entry; the impact assessment runs against current positions and pending orders; the policy for limits already exceeded is decided and enforced | Not met |
| G-6 | Order path safety | Bypass test (no path to an order endpoint skips `risk.check`); limit-only, on-tick prices; a sell never exceeds the quantity held; idempotency key saved before send and reused on 502/503; dry-run default; second-model review done | Not met |
| G-7 | API wire contract | Every question in section W that the order path, commitment, valuation or reconciliation relies on is answered by a spec citation or logged real-platform evidence, not by inference; boundary conversion tests cover each answer; questions left open are shown to be unused or fail closed | Not met |
| G-8 | Market-data recovery | M1 design reviewed; tests show each blocker clears only on its own recovery evidence, never on an orderbook refresh or a restart; events arriving during recovery reads keep or return the market to not ready; stale REST projections are detected; the concrete socket adapter passes the transport contract tests offline | Not met |
| G-9 | ₹0 hosting | Written evidence of eligibility, permitted use, the allowance, no chargeable resource or attached payment risk, and the behaviour when the allowance is exhausted; the deployment plan keeps exposure within the G-4 bounds when the allowance ends | Not met |
| G-10 | Unattended soak | The engine runs unattended on the server in dry run, with no laptop involved, survives a reboot, recovers from injected faults through the readiness check, and stays paused after an injected loss trigger until reset | Not met |
| G-11 | Activation | G-1 to G-10 all have recorded evidence, with none waived; Arnav has confirmed the ceilings, committed `config/risk.yaml`, and recorded the decision to leave dry run | Not met |

## Logging and evaluation

We log from day one because every resume number must come from real data; nothing can be
reconstructed later.

**Logged continuously:** every quote from all three venues, every fair value with its
inputs, every signal (traded or not, with reason), every order, fill, cancel, risk block,
pause, readiness result, configuration change and loss reset, plus hourly balance, rank
and Smart Score.

| Metric | Measures | Used for |
| --- | --- | --- |
| P&L by strategy | Where profit actually came from | Post-mortem, resume |
| Brier score and calibration curve: our fair value vs SIG price | Whether our probabilities beat the platform's | Quant credibility |
| Repricing lag: time for SIG to move after Polymarket moves | Size of the speed edge | Election-night sizing, resume |
| Fill rate and slippage vs limit price | Execution quality | Tuning thresholds |
| Signals skipped by risk checks | Whether limits were too tight or loose | Post-mortem |
| Pauses, causes and time to recover | Protection behaviour | Post-mortem, SWE resume line |
| Vote-count model (November): predicted vs final margin on recorded 2026 results | Model accuracy in real time | AI/ML resume line |
| Uptime and error counts | System reliability | SWE resume line |

## Open questions

Each question names the gate it blocks. "Proposed" answers are suggestions for Arnav, not
decisions. Questions about API behaviour are answered only by a spec citation or logged
real-platform evidence.

### A. Answered

- [x] How many markets, and what kinds? 237 YES/NO markets across 117 races; no
  composite, multi-outcome or linked markets
- [x] Does ALL collateral fund new fills? Not applicable: no linked markets
- [x] Does the system trade automatically? Yes, within configured limits (REQ-2), once the
  gates pass
- [x] Which reference sources for v1? Polymarket and Kalshi (REQ-6)

### B. Platform behaviour

- [ ] Are there trading fees? Check the first fill. Edge thresholds must exceed them
- [ ] How does the leaderboard value open positions before settlement? (G-2)
- [ ] What happens to resting orders at the Nov 4 cutoff? Planned for the SIG email;
  whether it was sent is not recorded here
- [ ] Does tournament-scoped `/orders/cancel-all` cancel every open order in one write, as
  documented? (G-4)
- [ ] Which source does SIG resolve races on? Check the Info tabs; `markets/` is empty
- [ ] How fast does SIG's market maker follow Polymarket? Answered by the lag study
- [ ] Can we share code and aggregate results publicly after Nov 4? Planned for the SIG
  email

### C. Commitment and classification

- [ ] OQ-C1 (G-1): Confirm or change the proposed commitment definition, including
  whether open positions count at cost or at current value
- [ ] OQ-C2 (G-1, G-4): Do resting sells of held shares add commitment? A resting sell can
  become a buy if the held position shrinks first (documented sell canonicalisation)
- [ ] OQ-C3 (G-1): Does any SIG balance already exclude cash reserved by resting buy
  orders? Check with a tiny resting order
- [ ] OQ-C4 (G-1): What is a related-market group? Candidates: Senate control with all
  Senate races; House control with all House races; races in the same state; all races
  with the same lean (a uniform polling miss). Can one market belong to several groups?
- [ ] OQ-C5 (G-1): Within one race, do a Democratic YES and a Republican YES net against
  each other, or are they summed? Proposed: summed until a netting rule is confirmed

### D. Equity, valuation and loss reset

- [ ] OQ-D1 (G-2): Equity formula: SIG's `totalAccountValue` from tournament P&L, or our
  own tournament cash plus positions at a chosen mark (mid, bid, last trade)?
- [ ] OQ-D2 (G-2): Starting-equity baseline: the tournament's initial 100,000, or equity
  at the moment the pilot is activated?
- [ ] OQ-D3 (G-2): Stale or missing marks: pause, value at zero, or value at cost? The
  tournament P&L endpoint is documented to return 409 when holdings lack a valuation price
- [ ] OQ-D4 (G-2): What does a loss reset do: restore the same baseline, rebase to current
  equity, or require a new configuration version? Who may reset (proposed: Arnav only)?
- [ ] OQ-D5: Does election night use a different loss-trigger level? No current
  requirement allows switching the trigger off

### E. Restart, unresolved orders and readiness

- [ ] OQ-E1 (G-3): How is an unknown-outcome submission resolved: listing orders and
  matching on idempotency key, re-sending the same key, or both? What if neither settles
  it? Depends on OQ-W9 and OQ-W10
- [ ] OQ-E2 (G-3): Are fills that happened while the engine was down treated as normal
  fills, or do they pause the engine for review?
- [ ] OQ-E3 (G-3): Maximum time a fault pause may last before it needs a human
- [ ] OQ-E4 (G-3, G-8): Which authoritative read clears each realtime blocker (settlement;
  trade without a sequence), and how is a recovery read shown to be fresh enough and not
  overtaken by events that arrive while it runs?

### F. Configuration changes

- [ ] OQ-F1 (G-5): May runtime changes loosen the committed `config/risk.yaml` values, or
  only tighten them?
- [ ] OQ-F2 (G-5): When existing positions or orders exceed a new limit: reject the
  change, accept and cancel the excess pending orders, or accept in a reduce-only state?
- [ ] OQ-F3 (G-5): Where do runtime versions and the audit log live, and how do they
  relate to commits of `config/risk.yaml`?

### G. Outage protection

- [ ] OQ-G1 (G-4): Is `expirationDate` accepted on tournament orders, and do expired
  orders stop filling? What lifetime balances protection against the writes spent
  re-placing orders?
- [ ] OQ-G2 (G-4): Which order styles are allowed to rest at all?

### H. Hosting

- [ ] OQ-H1 (G-9): Is Arnav eligible for Azure for Students, and do its terms permit this
  use?
- [ ] OQ-H2 (G-9): What allowance is available, and is any payment method attached that
  could be charged?
- [ ] OQ-H3 (G-9): What happens to the VM, disks and logs when credit or the free
  allowance runs out?
- [ ] OQ-H4 (G-9): If Azure fails any check, does Oracle Cloud Always Free pass the same
  checks?

### W. API wire contract

None of these is answered by inference from field names, examples or internal schema
types.

- [ ] OQ-W1 (G-7): Quote precision: what precision do SIG REST and realtime prices carry,
  and what precision do Polymarket and Kalshi quotes carry? Are SIG quotes always on the
  0.005 grid?
- [ ] OQ-W2 (G-6, G-7): Executable rounding: how is a fair value or target price turned
  into an on-tick limit price, and in which direction for buys and sells of each side?
- [ ] OQ-W3 (G-6, G-7): NO-order wire price: when `side` is `no`, does the request `price`
  carry the YES price or the NO price?
- [ ] OQ-W4 (G-3, G-7): REST fill units: the denomination of fill prices, and the sign and
  unit of fill quantities, in the tournament fills reads
- [ ] OQ-W5 (G-1, G-2, G-7): Position units: the sign and unit of position quantities, and
  the denomination of `avgCost` and position values
- [ ] OQ-W6 (G-3, G-7): `Order.priceLimit` denomination for YES and NO orders
- [ ] OQ-W7 (G-7): Do fills always land on the 0.005 grid? `shared/schemas.py` stores
  `Fill.price` as integer ticks, which assumes they do; unresolved
- [ ] OQ-W8 (G-1, G-7): Tournament routing and netting: which of our orders are
  "engine-routed", and how does the documented netting of an opposite-signed buy against
  an existing position show up in positions, fills and commitment?
- [ ] OQ-W9 (G-3, G-6, G-7): Idempotency retention: how long does SIG remember a key, and
  does a retry after a restart or a long outage still deduplicate?
- [ ] OQ-W10 (G-3, G-6): Ambiguous-submission retries: the spec documents that retrying the
  identical request with the same key after a 502 `ORDER_STATUS_UNKNOWN` is safe, and that
  409 `REQUEST_IN_FLIGHT` means retry after 90 seconds. What is guaranteed beyond those
  cases, for example after the retention window or for a timeout with no response?
- [ ] OQ-W11 (G-1, G-3, G-7): Tournament cash: `GET /account` has no `tournamentId`
  parameter and returns the default-tournament or public-global balance. Which read gives
  cash for the configured tournament, and what evidence shows it belongs to that
  tournament?

### I. Other

- [ ] Live election-results source for the November model: free sources only (REQ-4)
- [ ] Which rules file does Antigravity read: `AGENTS.md` or its own workspace rules?
- [ ] Is a full mock exchange still required, or do fake-transport tests plus dry run
  suffice? `CLAUDE.md` still names `mock_exchange/`

## Appendix: reconciliation with earlier plans

Historical context only; **nothing in this appendix is current policy.** The original
text is in git history (this file before Oct 12) and in the uncommitted local drafts.

**Sources:** PRD = this file as of Oct 2; Stages = `docs/stages/*.md` (Oct 2, uncommitted);
Plan v2 = `docs/PLAN-v2.md` (Oct 9, uncommitted).

| Topic | Earlier statement | Source | Now |
| --- | --- | --- | --- |
| Manual-only schedule | October builds a logger and nothing that trades; manual mode runs no automated strategies until Nov 2; automated October trading cut; only the logger runs during exams | PRD, Stages 1–2 | Superseded by REQ-2 and REQ-3: automatic, unattended trading once the gates pass |
| Bot-first dates | Bot live by Oct 13, server by Oct 14–16, `v1.0` frozen Oct 16; laptop as fallback | Plan v2 | Dates withdrawn; no replacement deadlines. The laptop is never the host (REQ-3) |
| Dated milestones | Client Oct 7, logger Oct 11, execution Oct 30, rehearsal Nov 2 | PRD, Stages | Replaced by the dependency-ordered roadmap |
| Dashboard dependency | The dashboard watches the bot and can stop it; KILL is a dashboard button; a terminal dashboard is a Must for Oct 31 | PRD, Stage 4, dashboard spec | Engine protections are independent of the dashboard (REQ-10); terminal controls come first (REQ-7); the dashboard comes later (REQ-8) |
| Dashboard commands | Only `/kill`, `/resume`, `/mode`, `/override` | PRD, `AGENTS.md` | Replaced by pause, readiness-checked resume, cancellation, loss reset and validated configuration changes. Fair-value override and mode switching are not confirmed requirements |
| Price storage | Round every price; store all prices as integer ticks (price × 200) | PRD | Wire values parsed exactly as `Decimal`; internal units per `shared/schemas.py` (quotes in milli); conversion rules open where section W is open |
| Secret storage | Secrets only in `.env` | PRD | Secrets read from environment variables through `engine/config.py`; `.env` optional and git-ignored |
| Risk limits by mode | Manual mode: 2,000 per order, 5% per race, 50% minimum cash; election mode: 5,000 per order, 10% per race, 20% minimum cash | PRD | Superseded by the provisional pilot ceilings |
| Plan v2 limits | 200 → 1,000 per order; 1% → 5% per race; 10% → 40% open exposure; 80% → 30% minimum cash | Plan v2 | Superseded by the provisional pilot ceilings |
| Manual trade size | ≤ 2,000 SUSQies per manual trade | PRD | Superseded: per-order ceiling 500 (provisional) |
| Disabled loss stop | Drawdown auto-kill off in election mode | PRD | Withdrawn: a loss trigger with explicit reset applies (REQ-13); see OQ-D5 |
| Daily drawdown kill | 1% → 3% daily drawdown auto-kill; stays stopped until resumed | Plan v2 | Replaced by the loss trigger against starting equity (REQ-13) and automatic resume for temporary faults only (REQ-12) |
| Cash deployment | Enter election night with half the balance in cash; minimum cash 20%, then 0% after 9:30pm IST Nov 4; deploy remaining cash where the edge is positive | PRD, Stage 5 | Withdrawn: ceilings are not targets; minimum uncommitted cash is 90,000 (provisional); nothing requires deploying cash |
| Kill switch sufficiency | Cancel-all retried until 200 stops everything; resting orders assumed not to survive the close | PRD, Stages 3 and 5 | A stopped server cannot cancel; resting orders may stay live and fill during an outage; exchange-side behaviour must be verified (REQ-14, G-4) |
| Credit exhaustion | "If the credit runs out, the server simply stops: no surprise bill" | Stages open questions | Unverified; a stopped server leaves orders resting; G-9 requires evidence |
| Fault restart | Restart the server from the Azure portal; leave faults until after exams | Stage 2 | Temporary faults resume automatically after readiness checks (REQ-12); a restart is not by itself a recovery |
| Reference data | Polymarket only for v1; Kalshi added later | Plan v2 | Polymarket and Kalshi for v1 (REQ-6) |
| Mock exchange | Full mock exchange replaced by fake-transport tests plus dry run | Plan v2 | Open (section I); `CLAUDE.md` still requires the mock exchange |
| Risk-limit changes | Only by a human commit to `config/risk.yaml` | PRD | Interim rule until G-5; then validated, versioned, audited changes (REQ-9) |

**Instruction-file conflicts to fix in a separate task** (not changed here):

- `CLAUDE.md` and `AGENTS.md` point to `docs/stages/stage-N.md`, which is not in the
  repository; `AGENTS.md` also points to `docs/stages/dashboard-spec.md`.
- `CLAUDE.md` says the current stage is "Stage 0 — Setup".
- `CLAUDE.md` says "Prices stored as integer ticks (`price × 200`)", which does not cover
  quotes and fair values in milli or exact `Decimal` wire values.
- `AGENTS.md` hard rule 4 limits dashboard commands to `/kill`, `/resume`, `/mode` and
  `/override`, which conflicts with REQ-8.
- Neither file says that this PRD now holds the roadmap and acceptance gates, or that the
  uncommitted `docs/PLAN-v2.md` and `docs/stages/` drafts are not authoritative.

**Shared-schema items that reflect superseded or unresolved policy** (changes need Arnav's
approval; not changed here):

- `RiskLimits`, `RiskState`, `Mode`, `ModeCommand` and `OverrideCommand` model the Oct 2
  mode-based percentage limits, a `killed` flag and the superseded `/mode` and
  `/override` commands, not the pilot ceilings and pause states.
- `Fill.price` is typed as integer ticks, which assumes fills land on the 0.005 grid
  (OQ-W7).
