# bot/ contract (binding for bot/ and dashboard/; only Arnav changes it)

## Files

| Path | Writer | Readers | Purpose |
| --- | --- | --- | --- |
| config/risk.yaml | Arnav | bot | limits, mode, strategy settings |
| config/ladder.yaml | Arnav | bot | optional limits ladder (steps L1..Ln of scale + caps); absent = risk.yaml only |
| config/exits.yaml | Arnav | bot | optional reduce-only exits (BOT-EW-5), re-read every loop; absent or invalid = no exits |
| bot/map.yaml | bot/make_map.py drafts, Arnav approves | bot | approved SIG <-> Kalshi/Polymarket contracts |
| bot/state/bellwether.db | bot | dashboard (read-only) | SQLite, WAL mode, all bot state |
| bot/state/STOP | Arnav or dashboard | bot | exists -> cancel all orders, stop, exit |
| bot/state/PAUSED | bot | Arnav, dashboard | exists -> no new orders; Arnav deletes it to resume |
| bot/state/LADDER_HOLD | Arnav | bot | exists -> the ladder does not climb and no exits are placed (buys unaffected) |

bot/state/ is git-ignored.

## Units

- Prices: decimal strings in [0,1] stored as TEXT ("0.435"). price_side = price of the
  side bought (a NO price for a NO buy). YES-equivalent of NO price p is 1 - p.
- Money (cost, balance, caps): SUSQies as decimal strings. Cost of a buy = price_side x quantity.
- Timestamps: UTC ISO-8601 ending in Z. Displays use IST.
- race_key as in markets/r1_candidates.json (senate:PA, governor:AZ, house_control, ...).

## Tables

heartbeat (exactly one row, id = 1, rewritten every loop)
  id, ts_utc, mode ('dry_run'|'live'), version, loop_ms, reads_last_min, writes_last_min,
  paused (0/1), pause_reason, stop_seen (0/1), balance, start_balance, committed, scale,
  cap_total, cap_per_race, min_cash_reserve, edge_now, election_night (0/1),
  ladder_step ('L0'..'Ln'; NULL when there is no config/ladder.yaml)
  scale, cap_total, cap_per_race and min_cash_reserve are the active ladder step's.

ladder_state (exactly one row, id = 1; only with config/ladder.yaml; rewritten on every
step change)
  id, step ('L0'..'Ln'), since_utc, ladder_sha256 (of the ladder.yaml the step was set under)

orders (one row per idempotency key, inserted and committed BEFORE sending)
  key PK, created_utc, updated_utc, race_key, sig_market_id, sig_exchange_id,
  side ('yes'|'no'), action ('buy'), price_side, wire_price, quantity INTEGER, expires_utc,
  strategy ('complement'|'reference'|'probe'|'exit'|'exit_probe'), reason,
  status ('journaled'|'sent'|'resting'|'filled'|'partial'|'cancelled'|'expired'|'rejected'|'unknown'),
  sig_order_id, quantity_traded, total_cost, http_status INTEGER, error, response_json,
  exit_of_side ('yes'|'no'; exits only, NULL otherwise)
  An exit is a complement buy that nets against a holding: side is the side bought,
  exit_of_side the held side it closes (an exit of YES buys NO). action stays 'buy': the
  bot never sells. A database from before BOT-EW-5 is rebuilt in place on first open.

decisions (append-only; 'skip' rows at most once per contract per minute)
  id PK AUTOINCREMENT, ts_utc, race_key, sig_exchange_id, party ('D'|'R'|'I'), strategy,
  sig_bid, sig_ask, kalshi_mid, poly_mid, fair, edge,  (strategy 'exit' for exits)
  verdict ('skip'|'would_order'|'order'|'blocked'), side, price_side, quantity, reason, order_key

quotes_latest (upsert, one row per sig_exchange_id)
  sig_exchange_id PK, race_key, party, ts_utc, sig_bid, sig_ask, sig_last,
  kalshi_ticker, kalshi_bid, kalshi_ask, kalshi_ts_utc,
  poly_token, poly_bid, poly_ask, poly_ts_utc, fair, edge

exposure (upsert, one row per race_key)
  race_key PK, ts_utc, committed, cap

snapshots (append every 60 s)
  id PK AUTOINCREMENT, ts_utc, balance, committed, open_orders INTEGER, positions_json, pnl_json

events (append)
  id PK AUTOINCREMENT, ts_utc, level ('info'|'warn'|'error'), kind, message
  kinds: start, stop, pause, resume, order_rejected, order_unknown, rate_limited,
         ref_stale, mismatch, reconcile, market_closed, probe, ladder, exit, reinvest
  (BOT-ROTATE: every exit placed, and every buy placed while risk.yaml `rotation` is
  enabled, is an 'exit' or 'reinvest' event with its reason; in dry run at most once
  per exchange per minute)
