# AGENTS.md — bellwether

Rules and context for Antigravity agents (and any other AI coding agent).
Read this first on every task.

## What this project is

A real-time trading system for the SIG Predictions Cup 2026 (US midterm election
markets on SIG's "Super Market" platform). Owner: Arnav (approves and merges everything).

- Full spec: `docs/PRD.md` (diagrams in `docs/PRD.pdf`)
- Stage plans: `docs/stages/stage-N.md`
- Dashboard design spec: `docs/stages/dashboard-spec.md`

## How to work

1. Do exactly one task ID at a time (e.g. `S2-T1`). If no task ID was given, ask for one.
2. Work on a branch named `<folder>/<task-id>`, e.g. `dashboard/s2-t1`.
3. Stop when the task's "Done when" condition passes; run the tests.
4. Open a pull request with what changed, how it was tested, and browser screenshots
   for any UI change.
5. When unsure, stop and ask in the pull request instead of guessing.

## Folder ownership

| Folder | Owner |
| --- | --- |
| `dashboard/`, `data/` | Antigravity |
| `engine/`, `models/`, `mock_exchange/`, `deploy/` | Claude Code — do not edit |
| `shared/` | Ask Arnav before changing |
| `config/risk.yaml` | Arnav only — never edit |
| `docs/` | Edit only if the task says so |

Code reviews of Claude Code's pull requests are welcome: comment, don't commit.

## Hard rules (never break these)

1. **Never call the live SIG API** (`thesuper.market`) and never log in to the SIG website
   in the browser. The dashboard is developed against fake data or the local engine.
2. **Browser use is for localhost only** (checking the dashboard) and the public data
   sources a task names (e.g. Polymarket's public price history).
3. **Never read, print, log or commit `.env` or any key.**
4. **The dashboard never places trades.** Its only commands are `/kill`, `/resume`,
   `/mode` and `/override`, sent to the local engine API.
5. **No merges, no deploys.** Arnav reviews and merges everything.

## Dashboard stack and style

- React + Vite + TypeScript, Zustand for state, uPlot or TradingView Lightweight Charts
- Data shapes come from the JSON Schema generated from `shared/schemas.py`
- Terminal-style design of our own: near-black background, monospace text, amber labels,
  green up / red down, dense panels, keyboard-first (F1–F7, `/` command line). No
  Bloomberg name, logo or copied layout. Full rules in the dashboard spec.

## Data work (`data/`)

- Pipelines only; raw data files are git-ignored
- Every dataset gets a validation script that checks totals or counts against a
  published source
