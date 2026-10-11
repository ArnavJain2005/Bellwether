# CLAUDE.md — bellwether

Rules and context for Claude Code. Read this first in every session.

## What this project is

A real-time trading system for the SIG Predictions Cup 2026 (US midterm election
markets on SIG's "Super Market" platform, Oct 1 – Nov 4, 2026). Goal: a strong final
rank and Smart Score through disciplined, risk-adjusted trading, concentrated on
election night. Owner: Arnav (approves and merges everything).

- Full spec: `docs/PRD.md` (diagrams in `docs/PRD.pdf`)
- Stage plans: `docs/stages/stage-N.md`
- Current stage: **Stage 0 — Setup** (Arnav updates this line)

## How to work

1. Do exactly one task ID at a time (e.g. `S1-T2`). If no task ID was given, ask for one.
2. Work on a branch named `<folder>/<task-id>`, e.g. `engine/s1-t2`.
3. Stop when the task's "Done when" condition passes. Run `ruff check .` and `pytest`.
4. Open a pull request: what changed, how it was tested, anything uncertain.
5. When unsure, stop and ask in the pull request. Never guess on anything that touches orders.

## Folder ownership

| Folder | Owner |
| --- | --- |
| `engine/`, `models/`, `mock_exchange/`, `tests/`, `deploy/` | Claude Code |
| `dashboard/`, `data/` | Antigravity — do not edit |
| `shared/` | Ask Arnav before changing; both sides depend on it |
| `config/risk.yaml` | Arnav only — never edit risk limits |
| `docs/` | Arnav / Claude chat — edit only if the task says so |

## Hard rules (never break these)

1. **Never call the live SIG API during development.** Use `mock_exchange/` and the
   recorded fixtures in `tests/fixtures/`. The only exceptions are scripts Arnav runs
   himself (the fixture recorder, `sig_dump.py`). All API keys share one rate budget
   with the live bot.
2. **Never read, print, log or commit `.env` or any key.** Load secrets only through
   `engine/config.py` from environment variables. `.env` is git-ignored.
3. **Every order goes through `risk.check(intent)`.** No code path may reach the order
   endpoints without it; a test enforces this.
4. **Never send market orders.** Limit orders only, on the 0.005 tick, 0.005–0.995.
5. **A sell never exceeds the quantity held.** Selling more silently becomes buying the
   opposite side. Short views are explicit NO buys.
6. **Every order has an idempotency key, saved before sending.** On 502
   `ORDER_STATUS_UNKNOWN` or 503, retry with the *same* key only.
7. **No merge without tests.** Order and risk code also needs a second-model review.
8. **No deploys.** Only Arnav deploys to the server.

## Platform facts

- Base URL: `https://www.thesuper.market/api/v1`; auth header `Authorization: Bearer <key>`
- Tournament: slug `midterm-elections`, id `bda92870-621e-47b0-bc3c-3602c5c26f55`.
  Always pass `tournamentId` explicitly; calls without it silently use a default context.
- Rate limits per account: 100 reads + 30 writes per minute. Our budget: 80 reads, 25 writes.
  A batch (≤ 50 orders) or multi-leg (≤ 10) request counts as one write.
- Prices are YES-denominated in [0, 1]; NO costs `1 − price`.
- Realtime: Supabase private channels via `POST /realtime/token` (3h tokens).
  Tournament market topic: `tournament:{tournamentId}:market:{marketId}`.
  Track `delivery.revision` / `previousRevision`; resync over REST on any gap,
  `resyncRequired`, reconnect or token refresh.
- Markets: 237 YES/NO markets across 117 races. Each race has a Democratic and a
  Republican market (three races also have an Independent one). No composite,
  multi-outcome or linked markets.
- Full API spec: `docs/api/openapi.json`.

## Code conventions

- Python 3.11, type hints everywhere, Pydantic v2 models from `shared/schemas.py`
- `httpx` (async) for REST, `asyncio` throughout, SQLite in WAL mode for storage
- Prices stored as integer ticks (`price × 200`); convert only at the API boundary
- All timestamps stored in UTC; the dashboard displays IST
- Every trade decision logs a human-readable `reason`
- Tests with `pytest`; lint with `ruff`
