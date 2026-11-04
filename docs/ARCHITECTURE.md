# Bellwether — System Architecture & Execution Lifecycle

> **Scope:** Deep-dive reference for engineers and technical reviewers covering the end-to-end topology, the 10-second asynchronous trading loop, the write-ahead idempotency journal, the market-data recovery ledger, and the strict process isolation boundary between the trading bot and the monitoring dashboard.

**Related specifications:**
- [Main README](../README.md) · [Quantitative Strategies & Risk Gate](STRATEGIES_AND_RISK.md)
- [Product Requirements Document (`docs/PRD.md`)](PRD.md) · [Binding Bot/Dashboard Contract (`bot/CONTRACT.md`)](../bot/CONTRACT.md)
- [Order Wire Contracts (`docs/design/order-wire-contracts.md`)](design/order-wire-contracts.md) · [Account State Contracts (`docs/design/account-state-contracts.md`)](design/account-state-contracts.md) · [Market Data Recovery (`docs/design/market-data-recovery.md`)](design/market-data-recovery.md)

---

## 1. High-Level System Topology

Bellwether is structured into four strictly separated subsystems:
1. **Offline Discovery & Contract Equivalence Pipeline (`data/`, `markets/`, `bot/map.yaml`)** — Fetches open markets from Kalshi and Polymarket, validates settlement rules against SIG's 237 US midterm election contracts, and produces an owner-approved mapping of **202 contracts across 101 races**.
2. **SIG Exchange Client & Engine Primitives (`engine/sig_client/`, `engine/account/`)** — Provides exact `Decimal` wire parsing (`wire.py`), a shared sliding-window rate limiter (`rate_limiter.py`: 100 reads/min, 30 writes/min), async REST reads (`rest.py`, `account_reads.py`), a WebSocket feed with revision-gap detection (`realtime_socket.py`, `realtime_feed.py`), and a fail-closed recovery ledger (`recovery.py`).
3. **Election-Week Trading & Capital Rotation Loop (`bot/`)** — Runs the 10-second trading pass (`main.py`), collects public reference quotes (`refs.py`), evaluates exit and entry strategies (`strategy.py`, `exits.py`), enforces the 12-stage risk gate (`risk.py`) and staged limits ladder (`ladder.py`), and executes orders through a write-ahead SQLite idempotency journal (`orders.py`, `sig_writer.py`).
4. **Read-Only Operator Monitor (`dashboard/`)** — A Streamlit UI (`app.py`, `db.py`) that opens `bot/state/bellwether.db` via a `mode=ro` URI, never imports `bot/` or `engine/`, makes zero network calls, and interacts with the bot exclusively by atomically creating `bot/state/STOP`.

```mermaid
flowchart TB
  subgraph Venues["External Venues (Network Boundary)"]
    SIG["SIG Super Market API<br/>REST /orders · /prices · /portfolio<br/>(100 reads/min · 30 writes/min)"]
    Kalshi["Kalshi Public API<br/>Order Books & Rules"]
    Poly["Polymarket CLOB API<br/>Order Books & Tokens"]
  end

  subgraph DataTrack["1. Contract Discovery & Verification (data/ · bot/map.yaml)"]
    Fetch["fetch_venue_markets.py<br/>4,455 Kalshi · 6,325 Polymarket"]
    R1["r1_mapping/<br/>Candidate & Rule Matcher"]
    MapYAML["bot/map.yaml<br/>202 Approved Contracts (101 Races)<br/>11 Excluded Runoff/Ambiguous Races"]
    Fetch --> R1 --> MapYAML
  end

  subgraph ConfigTrack["Owner-Controlled Configuration (config/)"]
    RiskYAML["config/risk.yaml<br/>Caps · Scale · Rotation · Edge"]
    LadderYAML["config/ladder.yaml<br/>Staged Caps (L0 -> L1 -> L2 -> L3)"]
    ExitsYAML["config/exits.yaml<br/>Complement-Buy Exit Settings"]
  end

  subgraph BotProcess["2. Trading Process (bot/main.py — systemd: bellwether-bot)"]
    Loop["10s Async Iteration Loop<br/>(bot/main.py)"]
    RefCol["Reference Collector<br/>(bot/refs.py)<br/>Staleness · Spread · Disagreement"]
    SigClient["SIG REST Client & RateLimiter<br/>(engine/sig_client/rest.py · wire.py)"]
    Ladder["Limits Ladder<br/>(bot/ladder.py)"]
    Strat["Strategy Engine (bot/strategy.py)<br/>1. Exits (Val Reversal · Take-Profit · Edge-Gone)<br/>2. Entries (Complement · Reference)<br/>3. Reinvest Ranking (Highest Edge First)"]
    RiskGate["Single Risk Gate<br/>(bot/risk.py)<br/>risk.check() · risk.check_exit()"]
    OrderPath["Idempotent OrderPath<br/>(bot/orders.py)<br/>Journal BEFORE Network Send"]
    Writer["SIG Order Writer<br/>(bot/sig_writer.py)<br/>Same-Key Retries · Redacted Logs"]

    MapYAML --> Loop
    RiskYAML --> Loop
    LadderYAML --> Ladder --> Loop
    ExitsYAML --> Loop

    Loop --> RefCol
    Loop --> SigClient
    RefCol --> Strat
    SigClient --> Strat
    Strat --> RiskGate
    RiskGate -->|Approved| OrderPath
    OrderPath --> Writer
  end

  subgraph StateStore["3. Local State Boundary (bot/state/ — CONTRACT.md)"]
    DB[("SQLite WAL Database<br/>bot/state/bellwether.db<br/>heartbeat · orders · decisions<br/>quotes_latest · exposure · snapshots · events")]
    Flags["Control Files<br/>STOP · PAUSED · LADDER_HOLD"]
  end

  subgraph DashProcess["4. Read-Only Monitor (dashboard/ — systemd: bellwether-dashboard)"]
    DashDB["Read-Only Connector<br/>(dashboard/db.py)<br/>file:...?mode=ro"]
    DashUI["Streamlit Monitor v2<br/>(dashboard/app.py)<br/>127.0.0.1:8501 via SSH Tunnel"]
    DashDB --> DashUI
  end

  Kalshi -->|Public HTTPS| RefCol
  Poly -->|Public HTTPS| RefCol
  SIG -->|Exact Decimal Wire| SigClient
  Writer -->|POST and DELETE Orders| SIG

  Loop -->|Atomic Upserts and Snapshots| DB
  RiskGate -->|Blocked Verdicts| DB
  OrderPath -->|Commit journal and update status| DB
  Flags -->|Checked Every Pass| Loop
  OrderPath -->|On unknown order create PAUSED| Flags

  DB -->|SQLite URI mode=ro| DashDB
  DashUI -->|Confirmed STOP Creation| Flags
```

---

## 2. The 10-Second Trading Loop (`bot/main.py`)

Every iteration of `Bot.iteration()` executes a deterministic sequence designed to prioritize safety controls, close profitable or stale holdings before opening new exposure, and keep within SIG's shared rate budget.

```mermaid
sequenceDiagram
  autonumber
  participant FS as Control Files (STOP / PAUSED / LADDER_HOLD)
  participant Bot as Bot Loop (bot/main.py)
  participant SIG as SIG REST API (engine/sig_client)
  participant Refs as Kalshi and Polymarket (bot/refs.py)
  participant Strat as Strategy and Rotation (bot/strategy.py)
  participant Risk as Risk Gate (bot/risk.py)
  participant Journal as OrderPath and SQLite (bot/orders.py)

  Bot->>FS: Check bot/state/STOP
  alt STOP file exists or signal received
    Bot->>SIG: POST /orders/cancel-all (if live)
    Bot->>Journal: Record stop event and final heartbeat
    Bot-->>Bot: Exit 0 (systemd does not restart)
  end

  Bot->>Journal: Evaluate Limits Ladder (L0 to L3)
  alt Every 60s (Account & Snapshot Cycle)
    Bot->>SIG: GET /tournaments/{slug} (balance)
    Bot->>SIG: GET /tournaments/{slug}/portfolio/positions
    Bot->>SIG: Walk GET /orders?status=open
    Bot->>Journal: Refresh resting order statuses & reconcile positions
    Bot->>Journal: Cancel stale orders (> order_expiry_seconds)
    Bot->>Journal: Insert 60s snapshot & per-race exposure
  end

  Bot->>Refs: Fetch Kalshi and Polymarket order books (concurrent)
  Bot->>SIG: GET /exchanges/prices (chunks of 50)
  Bot->>Journal: Upsert quotes_latest for all 202 contracts

  rect rgb(22, 36, 32)
    Note over Bot,Journal: Phase 1: Reduce-Only Exits (Before Buys)
    alt config/exits.yaml enabled == true and not PAUSED/LADDER_HOLD
      Bot->>Strat: evaluate_exit() per held contract (Val Reversal / Take-Profit / Edge-Gone)
      loop Each Exit Candidate
        Bot->>SIG: Fresh positions & order book read
        Bot->>Risk: risk.check_exit() (held qty, live exits, grid, rate budget)
        alt Approved
          Bot->>Journal: Commit journaled row (action=buy, complement exit)
          Bot->>SIG: POST /orders (complement buy that nets against holding)
          Bot->>SIG: Immediately cancel any unfilled remainder
          Bot->>Journal: Update row (filled, partial, cancelled, unknown) and log exit event
        end
      end
    end
  end

  rect rgb(24, 30, 44)
    Note over Bot,Journal: Phase 2: Ranked Entries & Reinvestment
    Bot->>Strat: evaluate_complement() & evaluate_reference() across all races
    Strat->>Strat: rank_buys() — Sort candidates by highest edge first
    loop Each Ranked Buy Candidate (while free cash > 2% of balance)
      Bot->>SIG: Read exchange order book depth (if read budget allows)
      Bot->>Risk: risk.check() (12-stage gate: caps, 15% race fraction, reserve, cooldown)
      alt Verdict == ALLOWED
        Bot->>Journal: Commit journaled row with deterministic idempotency key
        Bot->>SIG: POST /orders (same-key retry on transient errors)
        Bot->>Journal: Record final status and reinvest event
      else Verdict == DENIED
        Bot->>Journal: Append 'blocked' decision with exact reason
      end
    end
  end

  Bot->>Journal: Rewrite single-row heartbeat (id=1) with loop_ms, rate counts and caps
```

---

## 3. State Machines

### 3.1 Order Idempotency Journal (`bot/orders.py`)

To prevent duplicate orders across network timeouts or process crashes, **every order is committed to SQLite with status `journaled` before a single byte is sent over the network**. If a request times out or returns a 5xx/network error, the bot never guesses whether the order landed: it marks the order `unknown` and creates `bot/state/PAUSED`, which blocks all new orders until reconciled.

```mermaid
stateDiagram-v2
  [*] --> journaled: Row committed to SQLite<br/>BEFORE network send

  journaled --> rejected: Process crashed before send<br/>(resolved on startup)
  journaled --> sent: Bytes leave socket<br/>(same idempotency key on retry)

  sent --> filled: SIG confirms full fill<br/>(quantity_traded == quantity)
  sent --> partial: SIG confirms partial fill<br/>(remainder cancelled on exit)
  sent --> resting: SIG accepts onto book<br/>(status = open)
  sent --> cancelled: SIG cancels immediately<br/>(0 shares filled)
  sent --> expired: SIG expires immediately
  sent --> rejected: HTTP 4xx validation / rejection
  sent --> unknown: Network timeout, 5xx, or<br/>unparseable wire response

  resting --> filled: Subsequent account_cycle()<br/>confirms full fill
  resting --> partial: Subsequent account_cycle()<br/>confirms partial fill + cancel
  resting --> cancelled: Stale order timeout (>120s)<br/>or startup/STOP cancel-all
  resting --> expired: Exchange expiration

  unknown --> PAUSED_STATE: Writes bot/state/PAUSED<br/>Counts at 100% size in risk caps
  PAUSED_STATE --> filled: Operator/startup reconciles<br/>against SIG fills
  PAUSED_STATE --> cancelled: Operator/startup confirms<br/>not on SIG
```

### 3.2 Staged Limits Ladder (`bot/ladder.py` & `config/ladder.yaml`)

When `config/ladder.yaml` is active, the bot starts conservatively at `L0` and only steps up to `L1`, `L2`, and `L3` when all four gates pass simultaneously: wall-clock schedule (`not_before_utc`), live mode active (`require_live`), minimum fill count (`min_fills`), and an uninterrupted error-free window (`clean_minutes` with zero error events, reconciliation mismatches, or `unknown` orders).

```mermaid
stateDiagram-v2
  [*] --> L0: Startup (or ladder.yaml sha256 changed)

  state "L0 (Initial)" as L0
  state "L1 (Scale 0.5 · Cap 10k)" as L1
  state "L2 (Scale 1.0 · Cap 35k)" as L2
  state "L3 (Scale 1.0 · Cap 65k)" as L3
  state "LADDER_HOLD (Frozen)" as Hold

  L0 --> L1: UTC >= not_before_utc<br/>+ live + >=1 fill + 45m clean
  L1 --> L2: UTC >= not_before_utc<br/>+ live + >=3 fills + 60m clean
  L2 --> L3: UTC >= not_before_utc<br/>+ live + >=3 fills + 60m clean

  L0 --> Hold: bot/state/LADDER_HOLD present
  L1 --> Hold: bot/state/LADDER_HOLD present
  L2 --> Hold: bot/state/LADDER_HOLD present
  Hold --> L0: LADDER_HOLD removed (resumes current step)

  L3 --> L2: Operator lowers max_step to L2
  L2 --> L1: Operator lowers max_step to L1
  L1 --> L0: Operator lowers max_step to L0
```

### 3.3 Realtime Market-Data Recovery Ledger (`engine/sig_client/recovery.py`)

For high-frequency WebSocket feeds (`engine/sig_client/realtime_feed.py`), every market maintains an independent state machine in the recovery ledger. Any revision gap, malformed frame, or socket disconnect immediately transitions affected markets to a blocked state (`GAP_DETECTED` / `RESYNCING`) until a fresh REST snapshot at revision $\ge$ the gap is verified.

```mermaid
stateDiagram-v2
  [*] --> UNINITIALIZED: Market registered

  UNINITIALIZED --> BOOTSTRAP_PENDING: Subscription requested
  BOOTSTRAP_PENDING --> HEALTHY: Initial REST snapshot establishes baseline revision R

  HEALTHY --> HEALTHY: Delta arrives with revision == R + 1 (R set to R + 1)
  HEALTHY --> GAP_DETECTED: Delta revision > R + 1 or socket disconnect

  GAP_DETECTED --> RESYNCING: REST resync initiated (Trading Blocked)
  RESYNCING --> HEALTHY: Fresh REST snapshot verified at revision >= gap target
  RESYNCING --> GAP_DETECTED: Resync read fails or new gap occurs during resync
```

---

## 4. Process Isolation & Security Architecture

| Boundary Rule | Implementation | Verification |
| :--- | :--- | :--- |
| **Single Order Entry Point** | Only [`bot/orders.py`](../bot/orders.py) (`OrderPath.place` and `OrderPath.place_exit`) may invoke `SigWriter.place_order`, and only after `risk.check` or `risk.check_exit` returns `allowed=True`. | Enforced in CI by AST inspection in [`bot/tests/test_bypass.py`](../bot/tests/test_bypass.py). |
| **Secret Redaction** | `SUSQ_API_KEY` is read exclusively from environment variables (`/etc/bellwether.env`), wrapped in a secret string type, and scrubbed from exceptions, logs, reprs, and SQLite error columns. | Enforced by [`bot/tests/test_secrets.py`](../bot/tests/test_secrets.py) and pre-commit `gitleaks`. |
| **Dashboard Read-Only Isolation** | [`dashboard/db.py`](../dashboard/db.py) opens SQLite via `file:.../bellwether.db?mode=ro`. It never imports `bot/` or `engine/`, never reads `.env`, and makes zero network requests. | Enforced by AST & SQLite write-rejection tests in [`dashboard/tests/test_dashboard.py`](../dashboard/tests/test_dashboard.py). |
| **Single Permitted Control Write** | The only filesystem write in `dashboard/` is `write_stop_file()`, which creates `bot/state/STOP` using exclusive file creation (`mode="x"`) after a two-click operator confirmation. | Verified across 43 unit and `AppTest` integration tests in `dashboard/tests/`. |

