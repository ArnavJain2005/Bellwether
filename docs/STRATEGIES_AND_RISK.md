# Bellwether — Quantitative Strategies, Capital Rotation & Risk Gate

> **Scope:** Deep-dive mathematical and algorithmic reference for Bellwether's pricing strategies (`bot/strategy.py`), cross-venue reference fair-value formation (`bot/refs.py`), reduce-only position rotation (`bot/exits.py`, `BOT-ROTATE`), and the 12-stage fail-closed risk gate (`bot/risk.py`).

**Related specifications:**
- [Main README](../README.md) · [System Architecture & Execution Lifecycle](ARCHITECTURE.md)
- [Approved Contract Map Report (`bot/map_all_report.md`)](../bot/map_all_report.md) · [Formal R1 Mapping Criteria (`markets/README.md`)](../markets/README.md)

---

## 1. Price Grid & Buy-Only Execution Invariant

All contract prices on SIG's Super Market settle to **$1.00** (if the outcome occurs) or **$0.00** (otherwise), quoted on a strict **$0.005** tick grid in $[0.005, 0.995]$.
- **Exact `Decimal` Arithmetic:** Every wire price, book level, and cost is decoded and stored as a Python `Decimal` (`engine/sig_client/wire.py`), eliminating IEEE-754 floating-point drift (e.g., `0.535` never becomes `0.5349999999999999`).
- **Conservative Grid Snapping:** Every buy limit price $p$ is snapped **down** (floored) to the nearest $0.005$ tick:
  $$p_{\text{grid}} = \left\lfloor \frac{p}{0.005} \right\rfloor \times 0.005$$
- **Why Bellwether Never Sends `action="sell"`:** On prediction market order books, an oversized sell order (or a duplicate sell after a retry) can cross zero and silently open a short/opposite position. Bellwether enforces `action == "buy"` across the entire codebase (`bot/orders.py`). Even position exits are executed as **complement buys** on the opposite side (`YES` buy to close `NO`; `NO` buy to close `YES`), which SIG's clearing engine nets directly against the held position.

---

## 2. Entry Strategies (`bot/strategy.py`)

```mermaid
flowchart TD
  Start(["202 Approved Contracts<br/>(101 Races in bot/map.yaml)"]) --> CheckCooldown{"Live Order on Exchange<br/>or < 30s Cooldown<br/>or Post-Exit Cooldown?"}
  CheckCooldown -->|Yes| Skip1["Verdict: SKIP<br/>(busy / cooling down)"]
  CheckCooldown -->|No| SplitStrat{"Strategy Type"}

  subgraph CompStrat["Strategy A: Intra-Race Complement Arbitrage"]
    SplitStrat -->|Both D & R Approved| CheckAsks{"YES Asks:<br/>ask_D + ask_R <= 0.970?"}
    CheckAsks -->|Yes| BuyBothYes["Leg 1: BUY YES (D) @ ask_D<br/>Leg 2: BUY YES (R) @ ask_R<br/>(Equal Qty, Locks in >= 3c/share)"]
    CheckAsks -->|No| CheckBids{"NO Orders Enabled &<br/>bid_D + bid_R >= 1.030?"}
    CheckBids -->|Yes| BuyBothNo["Leg 1: BUY NO (D) @ 1 - bid_D<br/>Leg 2: BUY NO (R) @ 1 - bid_R<br/>(Equal Qty, Locks in >= 3c/share)"]
    CheckBids -->|No| SkipComp["Verdict: SKIP<br/>(no complement edge)"]
  end

  subgraph RefStrat["Strategy B: Cross-Venue Reference Pricing"]
    SplitStrat -->|Per Contract| FairVal["Form Fair Value f<br/>(bot/refs.py: Kalshi & Polymarket)"]
    FairVal --> ValidFair{"Age <= 30s &<br/>Spread <= 0.04 &<br/>|Kalshi - Poly| <= 0.03?"}
    ValidFair -->|No| SkipRef["Verdict: SKIP<br/>(ref stale / wide / venues disagree)"]
    ValidFair -->|Yes| EdgeRegime["Active Edge Threshold e:<br/>0.04 (Normal) or 0.06 (Election Night)"]
    EdgeRegime --> CheckYesEdge{"SIG ask <= f - e?"}
    CheckYesEdge -->|Yes| BuyYesRef["Leg: BUY YES @ floor(SIG ask)<br/>Edge = f - ask"]
    CheckYesEdge -->|No| CheckNoEdge{"NO Orders Enabled &<br/>SIG bid >= f + e?"}
    CheckNoEdge -->|Yes| BuyNoRef["Leg: BUY NO @ floor(1 - SIG bid)<br/>Edge = bid - f"]
    CheckNoEdge -->|No| SkipNoEdge["Verdict: SKIP<br/>(no edge)"]
  end

  BuyBothYes --> Rank["rank_buys()<br/>Sort All Candidates by Highest Edge First"]
  BuyBothNo --> Rank
  BuyYesRef --> Rank
  BuyNoRef --> Rank
```

### 2.1 Strategy A — Intra-Race Complement Arbitrage (`evaluate_complement`)
In a two-party race where both the Democratic (`D`) and Republican (`R`) contracts are approved and mutually exclusive:
- **Long YES Complement:** If the best asks on SIG satisfy:
  $$\text{ask}_D + \text{ask}_R \le \text{max\_sum\_ask} \quad (0.970)$$
  buying equal quantities $Q$ of `YES` on both `D` and `R` costs $Q \times (\text{ask}_D + \text{ask}_R) \le 0.970 Q$ while paying out $1.000 Q$ whenever either `D` or `R` wins.
- **Long NO Complement:** When `no_orders_enabled: true`, if the best bids on SIG satisfy:
  $$\text{bid}_D + \text{bid}_R \ge \text{min\_sum\_bid} \quad (1.030)$$
  the implied `NO` asks are $(1 - \text{bid}_D)$ and $(1 - \text{bid}_R)$, whose sum is $2 - (\text{bid}_D + \text{bid}_R) \le 0.970$. Buying equal quantities of `NO` on both `D` and `R` locks in at least $0.030$ per share.

### 2.2 Strategy B — Cross-Venue Reference Pricing (`evaluate_reference` & `bot/refs.py`)
Kalshi and Polymarket trade real-money contracts on the same US midterm elections. For each mapped contract, `bot/refs.py` computes a clean reference fair value $f \in (0, 1)$:
1. **Per-Venue Filtering:** A venue quote is accepted only if its timestamp is fresh ($\text{age} \le 30\text{s}$), $0 \le \text{bid} \le \text{ask} \le 1$, and its bid-ask spread is tight ($\text{ask} - \text{bid} \le 0.04$). Its midpoint is $m_v = (\text{bid}_v + \text{ask}_v)/2$.
2. **Cross-Venue Disagreement Check:** When both Kalshi and Polymarket have valid quotes, if $|m_{\text{Kalshi}} - m_{\text{Poly}}| > 0.03$, the contract is **skipped** (`venues disagree`). Otherwise $f = (m_{\text{Kalshi}} + m_{\text{Poly}})/2$ (or the single valid venue's midpoint when only one venue lists the contract).
3. **Regime-Aware Edge Threshold:** Before `election_night_from_utc` (`2026-11-03T23:00:00Z` / `04:30 IST Nov 4`), the required edge is $e = 0.04$ ($4\text{¢}$). Once polls begin closing on election night, adverse selection risk rises and the required edge automatically widens to $e = 0.06$ ($6\text{¢}$).
4. **Signal Generation:**
   - **Buy YES:** When $\text{SIG}_{\text{ask}} \le f - e$, buy `YES` at $\lfloor \text{SIG}_{\text{ask}} \rfloor_{\text{grid}}$.
   - **Buy NO:** When $\text{SIG}_{\text{bid}} \ge f + e$, buy `NO` at $\lfloor 1 - \text{SIG}_{\text{bid}} \rfloor_{\text{grid}}$ (equivalent to selling `YES` at $\text{SIG}_{\text{bid}}$).

---

## 3. Capital Rotation & Reduce-Only Exits (`BOT-ROTATE` & `BOT-EW-5`)

Once the portfolio approaches full deployment, capital rotation (`bot/strategy.py`, `bot/exits.py`) recycles capital out of positions whose edge has been captured or whose fair value has reversed, reinvesting into higher-edge opportunities.

```mermaid
flowchart TD
  Hold(["Held Position on Side S in {YES, NO}<br/>Quantity Q_held · FIFO Avg Entry e_S"]) --> Gates{"exits.yaml enabled?<br/>Not PAUSED / LADDER_HOLD?<br/>No Live Order & >30s Cooldown?"}
  Gates -->|No| SkipExit["Hold Position"]
  Gates -->|Yes| SideMap["Compute Side-Relative Prices:<br/>If S == YES: f_S = f, b_S = SIG_bid<br/>If S == NO: f_S = 1 - f, b_S = 1 - SIG_ask"]

  SideMap --> T1{"Trigger 1: Valuation Reversal<br/>b_S >= f_S + exit_edge<br/>(0.03 normal / 0.04 election night)?"}
  T1 -->|Yes| FireExit["Fire Reduce-Only Exit"]

  T1 -->|No| CheckRot{"rotation.enabled in risk.yaml<br/>& e_S known & b_S >= e_S?"}
  CheckRot -->|No| SkipExit
  CheckRot -->|Yes| T2{"Trigger 2: Take-Profit<br/>(b_S - e_S) / e_S >= 8%?"}
  T2 -->|Yes| FireExit
  T2 -->|No| T3{"Trigger 3: Edge Gone<br/>b_S >= f_S - buy_edge<br/>(and b_S >= e_S)?"}
  T3 -->|Yes| FireExit
  T3 -->|No| SkipExit

  FireExit --> CompBuy["Place Complement Buy on Opposite Side ~S<br/>Price = floor(1 - b_S)<br/>Qty = min(held - live_exits, book_depth)<br/>Cancel Any Unfilled Remainder Immediately"]
  CompBuy --> Cooldown["Block Re-Entry on Exchange for 10 Minutes<br/>Release Average Journal Cost for Ranked Reinvestment"]
```

###Three Exit Triggers (`evaluate_exit`)
Let $S \in \{\text{YES}, \text{NO}\}$ be the held side, $f_S$ be the reference fair value for side $S$ ($f$ for `YES`, $1 - f$ for `NO`), $b_S$ be SIG's best bid for side $S$ ($\text{SIG}_{\text{bid}}$ for `YES`, $1 - \text{SIG}_{\text{ask}}$ for `NO`), and $e_S$ be the FIFO average entry price of the currently held shares from the order journal (`risk.entry_price`):
1. **Valuation Reversal Exit (`BOT-EW-5`):** Fires when SIG's bid exceeds reference fair value by at least `exit_edge` ($0.03$ normal, $0.04$ election night):
   $$b_S \ge f_S + \text{exit\_edge}$$
2. **Take-Profit Exit (`BOT-ROTATE`):** Fires when the unrealized percentage gain on the position reaches `take_profit_pct` ($8\%$):
   $$\frac{b_S - e_S}{e_S} \ge 0.08$$
3. **Edge-Gone Exit (`BOT-ROTATE`):** Fires when the market has converged toward fair value so that the entry discount has disappeared ($b_S \ge f_S - \text{buy\_edge}$), **subject to the strict floor $b_S \ge e_S$** (never realizes a loss under `edge_gone`).

---

## 4. The 12-Stage Fail-Closed Risk Gate (`bot/risk.py`)

Every order (`risk.check` for entries, `risk.check_exit` for exits) must pass all applicable checks in `bot/risk.py` before reaching `OrderPath`.

```mermaid
flowchart TB
  Cand(["Candidate Order (Leg, Quantity, Price_Side)"]) --> G1{"1. Control Files:<br/>STOP or PAUSED present?"}
  G1 -->|Yes| Deny1["DENY: stop file / paused"]
  G1 -->|No| G2{"2. Unknown Orders:<br/>Any order in status 'unknown'?"}
  G2 -->|Yes| Deny2["DENY: unknown order exists"]
  G2 -->|No| G3{"3. Balance & Loss Stop:<br/>balance >= start_balance - loss_stop?"}
  G3 -->|No| Deny3["DENY: loss stop breached"]
  G3 -->|Yes| G4{"4. Write Rate Budget:<br/>writes_last_min + legs <= max_writes_per_min (20)?"}
  G4 -->|No| Deny4["DENY: write rate limit"]
  G4 -->|Yes| G5{"5. Action, Side & Price Grid:<br/>action == 'buy', price in [0.005, 0.995],<br/>exact multiple of 0.005, NO enabled?"}
  G5 -->|No| Deny5["DENY: off-grid or NO disabled"]
  G5 -->|Yes| G6{"6. Per-Order Cap:<br/>order_cost <= scale * per_order?"}
  G6 -->|No| Deny6["DENY: exceeds per_order cap"]
  G6 -->|Yes| G7{"7. Per-Race Cap:<br/>race_committed + cost <= scale * per_race?"}
  G7 -->|No| Deny7["DENY: exceeds per_race cap"]
  G7 -->|Yes| G8{"8. Portfolio Race Fraction (BOT-ROTATE):<br/>race_committed + cost <= 15% of Equity?"}
  G8 -->|No| Deny8["DENY: exceeds 15% max_race_fraction"]
  G8 -->|Yes| G9{"9. Total Committed Cap:<br/>total_committed + cost <= scale * total_committed?"}
  G9 -->|No| Deny9["DENY: exceeds total_committed"]
  G9 -->|Yes| G10{"10. Target Deployment Ratio:<br/>total_committed + cost <= target_deployment * Equity?"}
  G10 -->|No| Deny10["DENY: exceeds target_deployment"]
  G10 -->|Yes| G11{"11. Minimum Cash Reserve:<br/>balance - cost >= min_cash_reserve?"}
  G11 -->|No| Deny11["DENY: breaches min_cash_reserve"]
  G11 -->|Yes| G12["ALLOW -> Commit to SQLite Journal & Send"]
```

### Key Accounting Rules in `bot/risk.py`
- **Worst-Case Commitment Accounting (`committed`):** `total_committed` and `race_committed` include filled shares (`total_cost`), resting open orders (`price_side * remaining_qty`), **and `unknown` orders at 100% of their full notional size**, plus `preexisting_committed` from manual trading.
- **Exit Commitment Release (`exit_commitments`):** When a reduce-only exit fills, it releases the **FIFO average journal entry cost** of the closed shares (not the exit proceeds), ensuring realized gains or losses never distort the remaining cost basis.
- **Automatic Size Scaling (`joint_quantity` / `allowed_quantity`):** If a candidate order's book-depth size exceeds any cap (per-order, per-race, 15% race fraction, total committed, target deployment, or cash reserve), `bot/strategy.py` automatically scales the share quantity down to the largest integer $Q \ge 1$ that passes `risk.check`.
