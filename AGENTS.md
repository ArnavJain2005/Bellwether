# AGENTS.md — bellwether

Rules and context for Antigravity agents (and any other AI coding agent).
Read this first on every task.

## What this project is

A real-time trading system for the SIG Predictions Cup 2026 (US midterm election
markets on SIG's "Super Market" platform). Owner: Arnav (approves and merges everything).

- **Authoritative spec: `docs/PRD.md`.** It holds the confirmed requirements (REQ-1 to
  REQ-14), roadmap tracks, acceptance gates (G-1 to G-11) and open questions. Where
  anything else disagrees with it, the PRD wins.
- Diagrams: `docs/PRD.pdf` (Oct 2 version, not regenerated; the PRD text wins on conflict).
- **Historical drafts, not policy:** `docs/PLAN-v2.md` and `docs/stages/*.md`, including
  the old dashboard spec (`docs/stages/dashboard-spec.md`), may exist as untracked local
  files. They are not part of the repository, are not current policy and must not be
  cited as requirements. Do not edit, commit or delete them.

## Current status (Arnav updates this section)

Trading is **not activated** and no hosting is provisioned. The engine has read-only SIG
clients but no order path, risk checks, pauses, reconciliation or control API yet, and no
dashboard work has started. See the PRD roadmap for the full state of `main`.

**Antigravity's data track is independent.** The S1-T5 venue-market work (including any
coverage audit of its output) and S1-T6 race mapping (roadmap R1) depend only on the
merged S1-T5 output. They do not wait for, and must not take on, the engine's
account-state (A1 to A3), market-data recovery (M1, M2), socket adapter (M3) or order-path
(O1, O2) work.

## How to work

1. Do exactly one task ID at a time: a roadmap track ID from the PRD or a legacy ID still
   in use (e.g. `S1-T6`). If no task ID was given, ask for one.
2. Work on a branch named `<folder>/<task-id>`, e.g. `data/s1-t6`.
3. Stop when the done-when condition in the task prompt passes; run the tests.
4. Open a pull request with what changed, how it was tested, and browser screenshots
   for any UI change.
5. When unsure, stop and ask in the pull request instead of guessing.

## Folder ownership

| Folder | Owner |
| --- | --- |
| `dashboard/`, `data/` | Antigravity |
| `engine/`, `models/`, `mock_exchange/`, `tests/`, `deploy/` | Claude Code — do not edit |
| `shared/` | Ask Arnav before changing |
| `config/risk.yaml` | Arnav only — never edit |
| `docs/` | Edit only if the task says so |

Code reviews of Claude Code's pull requests are welcome: comment, don't commit. When
reviewing order, risk or control code, check it against the hard rules in `CLAUDE.md`
and gate G-6 in the PRD.

## Hard rules (never break these)

1. **Never call the live SIG API** (`thesuper.market`) and never log in to the SIG website
   in the browser. The dashboard is developed against fake data or the local engine.
2. **Browser use is for localhost only** (checking the dashboard) and the public data
   sources a task names (e.g. Polymarket's public price history).
3. **Never read, print, log or commit `.env` or any key.**
4. **The dashboard never places SIG trades and never calls SIG.** Every dashboard
   command goes to the engine's control API, and the engine authorizes, validates and
   carries it out. No dashboard command bypasses the checks that apply to it: readiness
   for resume, validation for configuration changes. Cancellation never places an order.
   The old `/kill`, `/resume`, `/mode` and `/override` command set is superseded.
5. **Risk limits and strategy settings change only through Arnav.** The PRD's pilot
   ceilings are provisional and not activated configuration. Never edit `config/risk.yaml`
   or use any control to change limits or settings yourself.
6. **No merges, deploys, provisioning or activation.** Arnav reviews and merges
   everything, deploys, creates or changes cloud resources, and decides when trading
   leaves dry run.

## Dashboard scope (later, roadmap D1)

The dashboard comes after the engine and its terminal controls (REQ-7; D1 depends on C1).
These are **requirements, not implemented features**: none of the engine endpoints they
need exist yet. Eventual scope (REQ-8, REQ-9):

- Monitoring of engine state, prices, orders, fills, positions, pauses and health.
- Pause.
- Resume, which the engine allows only after its readiness check passes.
- Cancellation (all orders, or one market), carried out by the engine. It must stay
  available while the engine is paused or not ready for new trading (`CLAUDE.md` hard
  rule 3).
- Editing implemented strategy settings and risk limits, as proposed changes the engine
  validates, versions, writes to an audit log and assesses against existing positions and
  pending orders before they take effect.

The PRD's command list also includes loss reset. Fair-value override and mode switching
are not confirmed requirements. Engine protections never depend on the dashboard
(REQ-10). The control API is to be bound to localhost and reached through an SSH tunnel.

## Dashboard stack and style

- React + Vite + TypeScript, Zustand for state, uPlot or TradingView Lightweight Charts
- Data shapes come from the JSON Schema generated from `shared/schemas.py`. The
  `RiskLimits`, `RiskState`, `Mode`, `ModeCommand` and `OverrideCommand` shapes reflect
  superseded policy; do not build controls on them. Schemas for pause state, readiness
  and configuration versions do not exist yet and need Arnav's approval.
- Prices in the shared schemas are Bellwether's internal units (ticks or milli), not SIG
  wire values; see the units notes in `shared/schemas.py` and do not infer API contracts
  from them.
- Design goals (confirmed): simple, intuitive, visually polished, fast and smooth.
  Progressive disclosure (the essentials first, detail on demand) and clear controls,
  whose effect and current state are obvious, guide the design.
- F1–F7 keys and a command line may be offered as optional desktop shortcuts, never as
  the only way to do something.
- Our own design: no Bloomberg name, logo or copied layout. The Oct 2 dashboard spec
  (dense, keyboard-first terminal style) is a historical draft, not a requirement.
- Do not start UI implementation until a dashboard task (D1) is assigned; its task
  prompt will state the detailed design rules.

## Data work (`data/`)

- Pipelines only; raw data files are git-ignored
- Every dataset gets a validation script that checks totals or counts against a
  published source
