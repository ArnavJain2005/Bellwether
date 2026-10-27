# Bellwether

Bellwether is an automated trading engine being built for the **SIG Predictions Cup 2026**.
The cup runs US midterm election markets on SIG's "Super Market" platform from Oct 1 to
Nov 4, 2026.

> **Status: not a trading bot yet.** No order path, risk check, pause logic,
> reconciliation, strategy or deployment exists in this repository. Nothing here can place
> an order. The engine cannot trade safely or run unattended, and trading is not
> activated.

The authoritative specification is [`docs/PRD.md`](docs/PRD.md). It holds the confirmed
requirements (REQ-1 to REQ-14), the roadmap tracks, the acceptance gates (G-1 to G-11)
and the open questions. Where this README and the PRD disagree, the PRD wins. Contributor
rules are in [`CLAUDE.md`](CLAUDE.md) and [`AGENTS.md`](AGENTS.md).

*This README describes `main` at `2d67b85` (PR #30, merged Oct 19, 2026), audited on
Oct 27, 2026.*

## Scope

- **Simulated funds only.** Bellwether trades only SIG competition SUSQies (REQ-1). No real
  money is traded anywhere.
- **SIG is the only trading venue.** Polymarket and Kalshi are read-only reference
  markets for prices (REQ-6). We hold no positions there.
- **Intended behaviour (requirements, not built):**
  - automatic execution within limits the engine enforces (REQ-2);
  - 24/7 unattended operation on a separate server, with no dependence on a laptop or
    hotspot (REQ-3).
- **Platform facts:**
  - 237 YES/NO markets across 117 races;
  - limit orders only, priced on the 0.005 tick between 0.005 and 0.995;
  - a rate limit of 100 reads and 30 writes per minute per account (our budget is 80
    reads and 25 writes).

  Details are in the [PRD](docs/PRD.md#competition-constraints) and the API spec
  ([`docs/api/openapi.json`](docs/api/openapi.json)).

## How the code is structured today

All implemented engine code is in `engine/sig_client/`. It covers SIG reads and a market
data feed only.

| Component | Path | What it does |
| --- | --- | --- |
| Configuration | `engine/config.py` | Reads the API key from the `SUSQ_API_KEY` environment variable and nowhere else. The tournament (slug + UUID) must always be given explicitly. |
| Rate limiter | `engine/sig_client/rate_limiter.py` | Token bucket plus a rolling-window guard (80 reads / 25 writes per minute). Every client of the account must share one instance. |
| REST read client | `engine/sig_client/rest.py`, `wire.py`, `errors.py` | Read-only GETs pinned to the configured tournament: tournament, markets, prices, orderbooks, market detail, trade tape, open orders. It has retries and deadlines, and parses numbers exactly as `Decimal`. |
| Account reads | `engine/sig_client/account_reads.py` | Raw tournament balance, positions and open-order reads (A2-A). Each read records where its data came from (provenance). Values stay exactly as sent and are not converted to domain units. |
| Realtime feed | `realtime.py`, `realtime_topic.py`, `realtime_wire.py`, `realtime_token.py` | Market-data feed logic: token minting and refresh, revision-gap detection, REST orderbook resync, per-market blockers. The socket layer is the `RealtimeTransport` interface, which has **no concrete adapter yet**. |
| Recovery ledger and readiness | `recovery.py`, `readiness.py` | In-memory record of what the feed may have missed (M2-A), and a per-market readiness result. Nothing clears a blocker yet. In production a ledger's history is always "unknown", so **no market ever reports ready**. |
| Fixture recorder | `engine/sig_client/record_fixtures.py` ([RECORDER.md](engine/sig_client/RECORDER.md)) | A bounded, read-only capture of SIG responses for tests. Only Arnav runs it against SIG. |
| Shared schemas | `shared/schemas.py` → `shared/schemas.json` | Pydantic contracts between engine and dashboard. Some models (`RiskLimits`, `RiskState`, `Mode`, `ModeCommand`, `OverrideCommand`) reflect superseded Oct 2 policy. Do not build on them. |
| Venue market lists | `data/venue_markets/` ([README](data/venue_markets/README.md)) | A fetcher and validator for 2026 midterm markets from Polymarket and Kalshi, enforcing decisive rules corroboration for House party-winner contracts. It does not map them to SIG contracts. |

The planned data flow is reference prices → fair value → signals → `risk.check` → SIG
orders, with everything logged to SQLite. None of it exists yet. These folders are
placeholders:

- `engine/collectors/`, `fair_value/`, `strategies/`, `execution/`, `risk/`, `api/`;
- `mock_exchange/`, `models/`, `config/`, `markets/`, `dashboard/`.

## Progress

The PRD records **no acceptance gate with evidence yet.**

| Status | Item | Evidence |
| --- | --- | --- |
| Implemented, tested offline | Shared schemas and JSON Schema export (S1-T1) | PRs #3, #4 |
| Implemented, tested offline | SIG REST read client, rate limiter, retries and deadlines (S1-T2) | PRs #6–#8, #19 |
| Implemented, tested offline | Fixture recorder; derived fixtures from three recorded captures (S1-T4, S1-T3-B) | PRs #9, #12–#14, #20, #22 |
| Implemented, tested offline | Venue market fetch script (S1-T5) | PRs #2, #11, #18, #21, #31, #34 |
| Implemented, awaiting validation | Realtime feed logic (S1-T3-A). Tested only against a fake transport, never run against SIG. | PRs #16, #17 |
| Implemented, awaiting validation | Recovery ledger and readiness foundation (M2-A). Tests only, and it cannot clear any blocker. | PR #28 |
| Implemented, awaiting validation | Raw tournament account reads (A2-A). The units, and whether the data belongs to our tournament, are still open questions (PRD section W). | PR #30 |
| Design drafted | Market-data recovery design (M1) | [`docs/design/market-data-recovery.md`](docs/design/market-data-recovery.md), PR #27 |
| Design drafted | Account-state contracts (A1) | [`docs/design/account-state-contracts.md`](docs/design/account-state-contracts.md), PR #29 |
| In progress | Market-data reconciliation (M2, later slices); account snapshot (A3) | PRD roadmap |
| Planned, not started | Concrete socket adapter (M3) | PRD roadmap |
| Planned, not started | SIG race mapping (R1); reference collectors and storage (R2); fair value (R3) | PRD roadmap |
| Planned, not started | Order path (O1, O2), pauses and restart reconciliation (S1), terminal controls (C1), hosting (H1, H2), activation (X1), dashboard (D1) | PRD roadmap |

There are no open pull requests. Unit tests cover the code above. No performance results
exist, and nothing has run against live SIG except Arnav's own read-only captures.

## Install, test and inspect

You need Python 3.11 or later; CI uses 3.11. These commands match `pyproject.toml` and
[`.github/workflows/ci.yml`](.github/workflows/ci.yml). CI runs the same three steps on
every push and pull request. The `python -m` forms work even when `ruff` and `pytest` are
not on your PATH.

```bash
pip install -e .
```

```bash
python -m ruff check .
```

```bash
python -m pytest
```

`pytest` runs `tests/` only. The venue-market tests run separately:

```bash
python -m pytest data/venue_markets/tests
```

To regenerate the dashboard's JSON Schema:

```bash
python -m shared.export_schema
```

Pre-commit runs gitleaks and ruff (`.pre-commit-config.yaml`).

Checked on Oct 27, 2026 (including PR #34 per-district House admission hardening):

- `ruff check .` passed (ruff 0.16.10);
- `pytest` ran 1,569 tests, all passed;
- the venue-market tests (`pytest data/venue_markets/tests`) ran 651 tests, all passed;
- combined tests (`pytest tests data/venue_markets/tests`) ran 2,220 tests, all passed;
- `shared/schemas.json` matched a fresh export;
- CI passed on that commit.

These are offline unit tests. They say nothing about behaviour against SIG.

**There is no command that runs the engine or trades.** Tests use recorded fixtures and
fake transports, never the live API. Do not run the fixture recorder with `--capture` or
the venue fetcher yourself:

- every API key shares one rate budget, and only Arnav runs scripts against SIG;
- the venue fetcher calls public Polymarket and Kalshi APIs.

Never commit `.env` or any key. `.env.example` lists only the variable name.

## Roadmap and acceptance gates

Work is ordered by dependency, not by date. **If the gates are not met in time, the
system stays in dry run.** Not trading is an acceptable outcome; unsafe trading is not.
Each stage below has to show recorded evidence (test output, a logged real-platform check
or a written decision by Arnav) before the next one starts. The full gate text is in the
[PRD](docs/PRD.md#acceptance-gates).

1. **Verified venue data and contract mappings.** Every SIG contract is mapped to its
   Polymarket and Kalshi counterpart, or marked "no match", with mismatch notes. Every
   market is classified to a race and a related-market group (R1, G-1). Resolution text
   is copied into `markets/`. Contracts with a low-confidence mapping get no fair value.
2. **Fresh, comparable quotes.** Collectors and storage are built (R2). Quote precision
   per venue is answered (OQ-W1). The realtime socket adapter passes the contract tests
   (M3), and blocked markets recover only on blocker-specific evidence (M1/M2, G-8).
3. **Shadow signals.** Fair value v1 (R3) and candidate strategies run in dry run only:
   they log signals and the orders they would send, but send nothing. No strategy is
   selected or calibrated yet.
4. **Execution, risk and restart reconciliation.** This stage covers:
   - a write-side client, with an idempotency key saved before every send;
   - `risk.check` with a bypass test (O2, G-6);
   - the API wire questions answered (G-7);
   - commitment that counts pending and unresolved orders (G-1);
   - a valuation policy and loss trigger (G-2);
   - restart reconciliation and readiness checks (G-3);
   - verified cancellation and outage bounds (G-4);
   - controlled configuration changes (G-5).
5. **Unattended cloud shadow run.** ₹0 hosting is verified in writing (G-9). The engine
   then runs in dry run on the server with no laptop involved. It must survive a reboot,
   recover from injected faults, and stay paused after an injected loss trigger (G-10).
6. **Limited live SIG pilot.** Only after G-1 to G-10 all have evidence. Arnav confirms
   the ceilings, commits `config/risk.yaml`, and records the decision to leave dry run
   (G-11).

### Agreed requirements and their status

Each of these is a requirement in the PRD. **None is implemented yet.**

| Requirement | Status |
| --- | --- |
| Runs 24/7 on a separate server, without the laptop or hotspot (REQ-3) | Planned (H1, H2). No hosting is provisioned. |
| Executes automatically within engine-enforced limits (REQ-2) | Planned (O2). No order path exists. |
| Exposure counts pending orders | Proposed definition only. "Commitment" is unconfirmed (OQ-C1, G-1). |
| Reconciles state with SIG after a restart, before any new order (REQ-11) | Planned (S1, G-3). Restarting a component today only discards its memory. That is not recovery. |
| Pauses on stale data or risk failures; temporary faults resume only after readiness checks (REQ-12) | Planned (S1). |
| A loss-triggered pause needs an explicit human reset (REQ-13) | Planned (S1, G-2). |
| Orders resting at SIG during an outage are addressed (REQ-14) | Open. A stopped server cannot cancel, so resting orders may stay live and fill. `expirationDate` behaviour on the tournament is unverified (OQ-G1, G-4). |
| ₹0 hosting (REQ-4); Azure for Students is the first candidate (REQ-5) | A target, not a proven deployment. Eligibility, permitted use, allowance and behaviour on exhaustion are unverified (G-9). |

### Provisional pilot figures

The tournament's starting balance is 100,000 SUSQies (PRD REQ-1). The ceilings below are
**provisional**:

- they are not in `config/risk.yaml`, which does not exist yet;
- nothing enforces them;
- they are not final policy.

They are ceilings, not targets, and the loss trigger is not a guaranteed maximum loss.

| Ceiling | Provisional value (SUSQies) |
| --- | --- |
| Per order | 500 |
| Per race | 2,000 |
| Per related-market group | 3,000 |
| Total exposure, including pending orders | 10,000 |
| Minimum uncommitted cash (reserve) | 90,000 |
| Loss trigger | 2,000 below starting equity |

Several terms they depend on are undefined: commitment, related-market group, starting
equity and the valuation policy (PRD
[provisional pilot ceilings](docs/PRD.md#provisional-pilot-ceilings)). Only Arnav sets
and commits risk limits.

## Future scope: dashboard

The engine and terminal controls come first (REQ-7). The dashboard comes later (REQ-8,
track D1), as a simple, fast UI for:

- monitoring;
- pause and readiness-checked resume;
- order cancellation;
- editing strategy settings and risk limits, through the validated, versioned and
  audited change flow (REQ-9).

The dashboard never places trades. All protections run inside the engine and must keep
working if the dashboard is absent or wrong (REQ-10). `dashboard/` is empty today.

## Open decisions, dependencies and known limitations

- **API behaviour.** Many wire-contract questions are open, and the order path,
  commitment and valuation all depend on them (PRD section W, OQ-W1 to OQ-W11):
  - whether the price on a NO order is the YES or the NO price;
  - the units of fills and positions;
  - which read gives cash for our tournament, since `GET /account` takes no
    `tournamentId`;
  - how long SIG keeps idempotency keys.

  "Documented" in the spec does not mean "observed on the tournament".
- **Mapping quality.** There is no SIG-to-venue mapping yet. The venue fetcher only
  filters candidate markets; how good its matches are is unproven. Some House races may
  have no reference market.
- **Market data.** There is no socket adapter, and blockers cannot be cleared. Feed
  readiness alone never authorizes trading.
- **Outage protection.** This is unsolved (see REQ-14 above). Cancel-all and
  `expirationDate` behaviour need real-platform checks that Arnav runs himself (G-4).
- **Hosting.** ₹0 feasibility is unverified, and so is what happens when the credit runs
  out (OQ-H1 to OQ-H4).
- **Other open decisions:**
  - what counts as commitment and as a related-market group (OQ-C1 to OQ-C5);
  - the equity formula and loss-reset semantics (OQ-D1 to OQ-D5);
  - configuration-change policy (OQ-F1 to OQ-F3);
  - whether a full mock exchange is still required (PRD section I).
- **Documentation drift.** These sources lag the merged code:
  - the PRD's "State of `main`" section and the "Current status" section of `CLAUDE.md`
    describe Oct 12 and predate the M2-A and A2-A merges;
  - `AGENTS.md` still lists the superseded `/kill`, `/resume`, `/mode` and `/override`
    dashboard commands;
  - `docs/PLAN-v2.md` and `docs/stages/` exist only as untracked local drafts. They are
    historical and not policy.

## Working on this repository

- Follow [`CLAUDE.md`](CLAUDE.md) (Claude Code) or [`AGENTS.md`](AGENTS.md) (Antigravity).
  Use one task ID per branch (`<folder>/<task-id>`) and open a pull request to `main`.
- Arnav reviews and merges every pull request, and is the only one who deploys.
- Never call the live SIG API during development.
- Never edit risk limits.
- Order, risk and control code needs tests and an independent second-model review before
  merge.
