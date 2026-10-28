# deploy/ — H1 hosting preparation

Roadmap track **H1** (PRD): ₹0 hosting verification and deployment preparation, gate
**G-9**. Written Oct 28, 2026 against `main` at
`efdb5a57cf22ec99c4fe69f228e341fe6dde52bc`.

> **Nothing in this folder deploys, provisions, starts or configures anything.** No server
> exists, G-9 is not met, and the engine has no entry point that could run unattended.
> This folder holds documents only. It has no script, service unit, container image or
> launcher, because no real engine interface exists for one to start
> ([`design.md` section 1](design.md#1-what-the-repository-can-run-today)).

| File | What it is | Who acts on it |
| --- | --- | --- |
| [`hosting-evidence.md`](hosting-evidence.md) | The account evidence checklist for OQ-H1 to OQ-H4. It also holds the official documentation checked, the candidate decision table and the G-9 status | Arnav collects the evidence |
| [`design.md`](design.md) | The single-server design: process layout, SQLite/WAL and journal placement, backups, time, restart and logs, secrets, identity, network, proposed resources, health signals, outage protection (REQ-14) and what a restart can and cannot recover | Review |
| [`runbook.md`](runbook.md) | A later dry-run deployment and reboot exercise, with each step labelled NOW, FUTURE or BLOCKED (by a named track), and an evidence table | Arnav, later (H2) |

## Prerequisites before any of it becomes runnable

1. **G-9 passes** for one candidate, on Arnav's account evidence. Azure for Students is
   first; Oracle Always Free is evaluated only if Azure fails.
2. **G-4 bounds are recorded**, so that G-9's second clause (exposure stays within those
   bounds when the allowance ends) can be assessed.
3. The engine has an entry point with **dry run as its code default** and a startup pause
   until readiness passes. That needs O2, S1 and C1, plus M3 for the feed and M2 for
   blocker recovery.

## Boundaries

- Only Arnav verifies accounts, creates cloud resources, deploys, merges and activates.
  No agent receives a key or server access.
- Account evidence (screenshots, billing records, support replies) is kept outside this
  repository. Only statuses and references are committed.
- H2 (the unattended dry-run soak, the reboot, fault and loss-trigger demonstrations, and
  G-10) is a separate track. These documents plan for it but do not perform or pass it.
- No step here switches the engine out of dry run.
