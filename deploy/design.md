# H1 server deployment design (design only)

Status: **design only, not runnable.** Written Oct 28, 2026 against `main` at
`efdb5a57cf22ec99c4fe69f228e341fe6dde52bc`.

This design covers one unattended server for the Bellwether engine (REQ-3, REQ-4). It
says what the server must provide and what the engine must provide before a server is
worth creating. It is not a deployment:

- no server exists;
- G-9 is not met ([`hosting-evidence.md`](hosting-evidence.md));
- the engine has no entry point that could run on a server (section 1).

Nothing in this folder starts a process, provisions a resource or calls SIG.

Contents:

1. [What the repository can run today](#1-what-the-repository-can-run-today)
2. [Single-server process layout](#2-single-server-process-layout)
3. [Persistent state: SQLite, WAL and the idempotency journal](#3-persistent-state-sqlite-wal-and-the-idempotency-journal)
4. [Time](#4-time)
5. [Process supervision, restart and logs](#5-process-supervision-restart-and-logs)
6. [Secrets, identity and network](#6-secrets-identity-and-network)
7. [Proposed cloud resources per candidate](#7-proposed-cloud-resources-per-candidate)
8. [Health signals and alerts](#8-health-signals-and-alerts)
9. [Outage protection (REQ-14) and G-4 dependencies](#9-outage-protection-req-14-and-g-4-dependencies)
10. [What a restart can and cannot recover](#10-what-a-restart-can-and-cannot-recover)
11. [Decisions blocked, and by what](#11-decisions-blocked-and-by-what)

## 1. What the repository can run today

Checked on the tree at `efdb5a5`:

| Area | Path | State |
| --- | --- | --- |
| Configuration | `engine/config.py` | Implemented. `load_sig_config(environ, tournament=...)` reads `SUSQ_API_KEY` from a mapping the caller passes. There is no default tournament. `ApiKey` never shows its value in `repr`, `str` or exceptions |
| REST reads | `engine/sig_client/rest.py`, `wire.py`, `errors.py`, `rate_limiter.py` | Implemented, offline-tested. Read-only. httpx request-log lines are filtered so that response-derived query values are redacted |
| Account reads and snapshot | `engine/sig_client/account_reads.py`, `engine/account/snapshot.py` | Implemented (A2, A3). The snapshot explicitly does not claim atomicity, freshness, established cash or readiness |
| Realtime feed | `engine/sig_client/realtime*.py` | Feed logic only. `RealtimeTransport` has **no concrete socket adapter** (M3) |
| Recovery and readiness | `engine/sig_client/recovery.py`, `readiness.py` | In-memory ledger. Nothing clears a blocker (M2), so no market reports ready |
| Fixture recorder | `engine/sig_client/record_fixtures.py` | A CLI that Arnav runs himself. Not a service |
| Venue fetcher | `data/venue_markets/fetch_venue_markets.py` | A one-off CLI. Not a service |
| Order path | `engine/execution/` | **Placeholder** (`.gitkeep` only). No write client, no idempotency journal, no dry-run switch (O2) |
| Risk | `engine/risk/` | **Placeholder.** No `risk.check` (O2) |
| Control API | `engine/api/` | **Placeholder.** No control interface (C1) |
| Collectors, fair value, strategies | `engine/collectors/`, `fair_value/`, `strategies/` | Placeholders (R2, R3, E1) |
| Storage | — | **No SQLite code anywhere** in `engine/` or `shared/` (R2, O2) |
| Configuration files | `config/` | Empty. No `risk.yaml` |
| Packaging | `pyproject.toml` | No `[project.scripts]`. No `engine/__main__.py` |

**There is no engine entry point.** No process exists that would connect the feed, keep
state, enforce pauses and stay up. So this PR adds **no service unit, launcher, container
image, start command or deploy script**. Any of those would imply that an unattended
engine exists. The service settings in [section 5](#5-process-supervision-restart-and-logs)
are a table of required properties, not a unit file.

**To make the engine runnable on a server, all of these are needed:**

| Dependency | Track | Why the server needs it |
| --- | --- | --- |
| A concrete socket adapter for `RealtimeTransport` | M3 | Without it the feed cannot connect to SIG |
| Blocker recovery and reconciliation | M2 (from M1) | Without it every market stays not ready after any gap, for the life of the process |
| Write-side client, idempotency journal saved before send, `risk.check`, **dry run as the default**, bypass test | O2 (needs O1, G-1 definitions) | The journal is the most important persistent file on the server. Dry run must be the default, not a flag the server sets |
| Pause states, restart reconciliation, readiness check, protective cancellation | S1 (needs A3, M2, O2) | The server restarts. Startup must hold the engine paused until reconciled (REQ-11) |
| Terminal controls and a localhost-only control interface | C1 (needs S1) | The only safe way to inspect or pause the engine on the server |
| An engine entry point (`python -m engine` or a console script) that runs startup-pause → readiness → loop | Not assigned yet; follows S1/C1 | The command a service would run. It must refuse to start without an explicit tournament and config |
| SQLite storage | R2, O2 | State and logs live here |

## 2. Single-server process layout

**One engine process per SIG account.** `RateLimiter` is in-process and documented
as "Share one instance per account". Every key on the account shares one budget of 100
reads and 30 writes per minute. Two processes would each believe they had the full budget.
So:

- **Engine process** (future, single instance): one asyncio process holding the one
  `RateLimiter`, the REST client, the realtime feed, the reference collectors, fair
  value, risk, execution and the control interface. Reference collectors (Polymarket,
  Kalshi) do not use the SIG budget and could be separate processes. Keeping them
  in-process is simpler and avoids extra IPC. That choice belongs to R2.
- **No other process on the server calls SIG.** The fixture recorder and any read-only
  scripts run from Arnav's machine, never alongside the engine. Those scripts still draw
  on the same account budget, so Arnav should not run them while the engine runs.
- **No dashboard on the server** in H1/H2. D1 comes after C1. When it exists, it binds to
  localhost (section 6).
- **Supervisor:** `systemd`. It restarts a failed process. It is not a safety mechanism
  (section 5).

```
VM (Linux, UTC)
├── systemd
│   └── bellwether-engine (future; one process, user "bellwether")
│        ├── SIG REST + realtime  ──► thesuper.market, Supabase realtime  (outbound 443)
│        ├── reference collectors ──► Polymarket, Kalshi public APIs      (outbound 443)
│        ├── SQLite (WAL)         ──► /var/lib/bellwether/
│        └── control interface    ──► 127.0.0.1 only (C1)
├── journald (persistent)         ──► /var/log/journal/
└── sshd (key-only)               ◄── Arnav, SSH; tunnel to the control interface
```

## 3. Persistent state: SQLite, WAL and the idempotency journal

**Placement.**

| Path | Contents | Owner, mode |
| --- | --- | --- |
| `/opt/bellwether/` | Code checkout or wheel install, plus the virtualenv | `root:root`, `0755`. Not writable by the service |
| `/etc/bellwether/` | Non-secret configuration (tournament slug and UUID; later `risk.yaml` as committed by Arnav) | `root:bellwether`, `0750`; files `0640` |
| `/etc/bellwether/engine.env` | The secret environment file (section 6) | `root:root`, `0600` |
| `/var/lib/bellwether/` | SQLite databases and their `-wal` and `-shm` files | `bellwether:bellwether`, `0700`; files `0600` (service `UMask=0077`) |
| `/var/lib/bellwether/backup/` | Local online backups | `bellwether:bellwether`, `0700` |

- **The state directory must be on the persistent OS disk** (or an attached persistent
  data disk). It must never be on an ephemeral or temporary disk: Azure's resource disk,
  usually mounted at `/mnt`, is wiped on deallocation. It must never be on a network
  filesystem (Azure Files, NFS, SMB). SQLite's WAL mode uses shared memory (`-shm`) and
  file locks that are only reliable on a local filesystem.
- **The `-wal` and `-shm` files belong to the database.** They stay in the same directory,
  and nothing copies, moves or deletes the main `.db` file on its own while the engine
  runs. A WAL file left behind after a crash is replayed when the database is next
  opened. Deleting it loses committed transactions.

**Durability of the idempotency journal (O2).** CLAUDE.md hard rule 6 requires every key
to be "durably saved before sending". In WAL mode, `PRAGMA synchronous=NORMAL` can lose
the most recent commits on power loss or an OS crash. **The journal's connection must use
`PRAGMA synchronous=FULL`**, and the send must happen only after the commit that records
the key has returned. Other high-volume tables (quotes, logs) may use `NORMAL`, if O2/R2
decide so. The journal should be a separate database file, or at least a separate
connection with `FULL`, so that log volume never delays or weakens a journal commit. This
is O2's design. The server's only job is to provide a local, persistent disk with
`fsync` behaving correctly.

**Backup.**

- Use an **online, consistent copy**: SQLite's backup API
  (`sqlite3.Connection.backup`), or `VACUUM INTO '<file>'`. Never `cp` a live database.
  After each copy, run `PRAGMA integrity_check` on the copy and keep the result.
- **Schedule and retention** (proposed): hourly during the trading window. Keep the last
  24 hourly copies and one copy per day until Nov 11. Cap the total size, so backups can
  never fill the disk; a full disk would stop journal writes and should fault-pause the
  engine.
- **Off-server copy:** Arnav may pull a backup to his own machine over SSH (`scp`). The
  laptop is never the host (REQ-3), but it can hold a copy. Backups must contain no
  secrets. Nothing in the planned storage holds keys, and the engine must never write
  one there.
- **No cloud snapshots or backup vaults.** They may be billed. G-9 must show them free
  before they are used.

**Restore is not recovery.** A restored journal is older than the one it replaces. Any key
sent after the backup was taken is missing from it, so the engine would not know those
orders exist. After any restore:

- the engine must start in startup pause;
- the readiness check must list every open order (all pages) and the fills since the
  backup time from SIG, and match them against the restored journal;
- any SIG order with no matching journal entry must keep the engine paused until Arnav
  reviews it.

How unknown orders are matched depends on OQ-E1, OQ-W9 and OQ-W10, all of which are open.

## 4. Time

- **Server clock and timezone: UTC.** All stored timestamps are UTC (CLAUDE.md
  conventions). IST is used only in displays. The engine never relies on the server's
  local timezone.
- **Synchronization:** `chrony` (the default on current Ubuntu cloud images), using the
  provider's time source (on Azure, the Hyper-V PTP clock or `time.windows.com`; on OCI,
  `169.254.169.254`). `timedatectl` must report `System clock synchronized: yes` before
  the engine starts. The engine should treat an unsynchronized clock as a readiness
  failure. That check is part of S1; it is not built.
- **Why it matters:**
  - the existing clients use monotonic clocks for deadlines and rate limiting (correct
    across clock steps);
  - stored UTC times, realtime token lifetimes (3 hours) and any future `expirationDate`
    on orders (G-4) are wall-clock values;
  - a skewed clock could make an order's expiry later than intended, so G-4's lifetime
    bound must include a clock-error allowance.

## 5. Process supervision, restart and logs

**Required service properties** (a table, not a unit file: there is nothing to run):

| Property | Intended setting | Reason |
| --- | --- | --- |
| Run as | Dedicated system user `bellwether`, no login shell, no sudo | Least privilege (section 6) |
| Restart | On failure only, with a delay of at least 60 s and a start-rate limit (for example no more than 5 starts in 30 minutes, then stay stopped) | Each start costs SIG reads (tournament verification, reconciliation) and a write (`POST /realtime/token`). A crash loop would burn the shared budget, so a persistent fault must end in a stopped, visible state |
| Stop | SIGTERM with a bounded timeout, then SIGKILL | Shutdown may attempt protective cancellation (S1). It may also fail to run at all (section 9) |
| Start order | After `network-online.target` and `time-sync.target` | The engine still checks clock sync and connectivity itself. The ordering only reduces noise |
| Filesystem | `ProtectSystem=strict`, `ReadWritePaths=/var/lib/bellwether`, `ProtectHome=yes`, `PrivateTmp=yes`, `UMask=0077` | Nothing but the state directory is writable |
| Privileges | `NoNewPrivileges=yes`, empty capability set, `RestrictAddressFamilies=AF_INET AF_INET6 AF_UNIX` | No need for any privilege |
| Secrets | `EnvironmentFile=/etc/bellwether/engine.env` (section 6) | Never `Environment=` in the unit: unit files are world-readable |
| Dry run | Comes from the engine's own default (O2), never from the unit | A unit setting must not be what keeps the engine out of live trading |

**A restart is not protection.** systemd restarts a process that crashed while the server
was up. It does nothing while the server is stopped, deallocated, out of credit or off the
network, and that is exactly when resting orders matter (section 9). A VM restart is the
same.

**Logs.**

- Primary sink: **journald with persistent storage** (`Storage=persistent`, a size cap
  such as `SystemMaxUse=1G`, and `SystemKeepFree` so the journal never fills the disk).
  Logs then survive a reboot, and `journalctl -b -1` shows the previous boot.
- Every trade decision's `reason`, every pause and every readiness result is also
  written to SQLite (PRD logging section), so analysis does not depend on journald
  retention.
- If the engine later writes its own log files, they go in `/var/log/bellwether/` with
  `logrotate` (size-based, compressed, bounded count).
- **No cloud log shipping** (Log Analytics, Monitor agent, OCI Logging beyond its free
  amount) unless G-9 shows it free.

**Secret leakage controls** (existing behaviour plus server rules):

- Existing: `ApiKey` and `RealtimeSecret` never render their values. The SIG REST client
  redacts response-derived query values in httpx log lines. Error messages from
  `engine/config.py` never echo the value.
- Server rules:
  - never set httpx or httpcore to DEBUG on the server (header-level debug output is not
    covered by the existing filter);
  - never log `os.environ`;
  - the engine must not spawn subprocesses, which would inherit the environment;
  - never put the key on a command line (it would be visible in `ps` and shell
    history);
  - never `cat` the environment file into a terminal that is being recorded or shared.

## 6. Secrets, identity and network

**Secret injection.** The key reaches the engine only as the environment variable
`SUSQ_API_KEY`, read by `engine/config.load_sig_config(os.environ, ...)`, unchanged from
today.

- Arnav creates `/etc/bellwether/engine.env` on the server with `sudoedit`. The file is
  `root:root 0600` and contains only `SUSQ_API_KEY=...`. systemd reads it as root and
  passes the value into the service's environment.
- It is not a repository `.env`: it is outside the checkout, outside `/opt/bellwether`,
  and never committed. The repository never needs a `.env` on the server.
- No agent creates, reads or receives it. The runbook's checks confirm the variable is
  present by printing counts and permissions only, never the value.
- Systemd's `LoadCredential=` would keep the key out of the environment entirely, but the
  engine would need code to read `$CREDENTIALS_DIRECTORY`. That is a change to
  `engine/config.py`, which this PR does not make. It is listed as an open question.

**Which key.** The PRD plans two keys: a read-only key, and a read+trade key that exists
only on the server. **Proposed for H2: run the dry-run soak with the read-only key.** Then
even a defect in the dry-run default could not place an order: the exchange would refuse
it. Whether a read-only key can mint a realtime token (`POST /realtime/token` is a write)
is unknown and must be checked by Arnav. The read+trade key is placed on the server only
at X1, by Arnav's decision.

**Service identity.**

- OS: the `bellwether` system user owns only `/var/lib/bellwether`. Arnav's admin user is
  separate, uses SSH keys only, and is the only sudoer.
- Cloud: **no managed identity or instance principal** is attached to the VM, and no cloud
  role is granted to it. A compromise of the VM then cannot create, resize or delete
  resources, or upgrade the subscription.

**Network.**

- Inbound: SSH (22/tcp) only, restricted by the network security group or security list
  to Arnav's current source address where practical. Password login is disabled. Arnav's
  hotspot address changes, so the rule may need updating from the portal. That is an
  access inconvenience, not a safety issue: the engine needs no inbound access.
- **Control interface (C1, not built): bound to `127.0.0.1` only.** It is reached from the
  laptop with an SSH local forward, for example `ssh -N -L 8700:127.0.0.1:8700 <server>`
  (the port is a placeholder; C1 chooses it). It is never bound to `0.0.0.0`, never
  opened in the security group, and never fronted by a public proxy. The same applies
  to the dashboard later.
- Outbound: 443 to SIG, the Supabase realtime host, Polymarket and Kalshi, and package
  mirrors. Time sync goes to the provider. Telegram is used only if alerts are adopted.

## 7. Proposed cloud resources per candidate

These are the choices to price and check in [`hosting-evidence.md`](hosting-evidence.md).
**Arnav creates nothing until G-9 records `pass`.**

| Item | Azure for Students (first) | Oracle Always Free (conditional) |
| --- | --- | --- |
| Compute | `Standard_B1s` (1 vCPU burstable, 1 GiB) if the account's Free services blade lists it. `B2ats v2` or `B2pts v2` (Arm) are alternatives only if listed | `VM.Standard.E2.1.Micro` (1/8 OCPU burstable, 1 GB) or `VM.Standard.A1.Flex` at 1 OCPU / 6 GB, within Always Free limits |
| Image | Ubuntu Server 24.04 LTS (no Marketplace or paid image) | Ubuntu 24.04 or Oracle Linux (platform image) |
| Region | One where the SKU is free, quota is above zero and subscription policy allows it (E-AZ-06, E-AZ-15). Latency to SIG is measured in H2, not assumed | The tenancy's home region (Always Free applies only there) |
| Disk | One OS disk of the type and size listed as free (P6 64 GiB per the public page; verify that the default 30 GiB P4 is covered, or choose 64 GiB). No data disk. No snapshots | Boot volume within the 200 GB Always Free total |
| Public IP | One public IPv4 (Standard SKU). Billed hourly on the public page; whether it is covered or how much credit it uses is E-AZ-09 | The one public IP included with the VNIC |
| Network | One VNet and subnet, one network security group (SSH only). **No** NAT gateway, Bastion, load balancer, VPN or Front Door | One VCN and subnet, one security list (SSH only) |
| Monitoring | Built-in platform metrics only. Boot diagnostics only if shown free. **No** Log Analytics workspace, Monitor agent, Defender plans, Backup vault | Built-in metrics within the free amounts |
| Cost guard | Spending limit **On**, verified. No payment method. A Cost Management budget with an email alert at a small amount. Budgets notify; they do not stop resources | Account stays Free Tier (not upgraded) |

The resource fit (1 GiB of memory for Python, SQLite and the feed; burst-credit behaviour
under load) is unverified. It is measured in H2. If B1s proves too small, a larger SKU is
not a free option unless G-9 shows it is.

## 8. Health signals and alerts

**Signals the engine should expose** (future: S1/C1 define them) on the localhost
control interface and in logs:

- process up, and start time;
- pause state and cause;
- last readiness result with its failing items;
- count of journalled keys with unknown outcome;
- markets ready and blocked, by blocker kind;
- age of the last successful SIG read and of the last reference quote;
- rate-budget use;
- clock-sync status;
- free disk space and SQLite file sizes;
- time of the last backup and its integrity result.

**Signals Arnav checks outside the server:**

- remaining credit and its expiry (Sponsorships portal);
- subscription status;
- VM power state in the portal;
- SIG open orders on the SIG website.

**Alerts** (Telegram or email; optional, PRD hosting section) for pauses, faults, disk
low, backup failure, clock unsynchronized, and a daily summary. An external "dead-man"
heartbeat, which alerts when the server goes silent, could tell Arnav that the server is
down. **No alert is a safety mechanism.** Alerts may not be delivered and may not be read.
When an alert fires because the server is unreachable, the engine can no longer do
anything about resting orders (section 9).

## 9. Outage protection (REQ-14) and G-4 dependencies

**Assumption until G-4 records evidence:** if the server stops, is deallocated, runs out
of credit, is reclaimed or loses network, **every resting SIG order stays live and may
fill**, for an unbounded time, possibly until the window closes on Nov 4.

- **A shutdown hook is not protection.** On SIGTERM, S1 may attempt
  `POST /orders/cancel-all` for the configured tournament. That attempt may never run:
  power loss, a platform deallocation without guest shutdown, OOM kill or SIGKILL after
  the stop timeout. If it runs, it may not reach SIG (network loss, 503 part-way, rate
  limit). Cancel-on-shutdown can reduce what rests during a *planned* stop. It must never
  be counted as a bound.
- **Credit exhaustion on Azure** stops and deallocates the VM (P-AZ-5). Whether the guest
  gets a shutdown signal first is undocumented. Treat it as a power loss.
- **systemd restart, a VM restart, an alert and the backup** all act only when the server
  is up. None of them bounds exposure while it is down.

**Missing G-4 evidence** (logged real-platform checks Arnav runs himself, with tiny orders
priced far from the market):

| Item | Status | Needed for |
| --- | --- | --- |
| Tournament-scoped `POST /orders/cancel-all` cancels every open order, including retry-until-200 behaviour after a 503 | Not checked | Planned stops, fault pauses, manual stop |
| `expirationDate` is accepted on tournament limit orders | Not checked (OQ-G1) | The only listed option that acts while the server is down |
| Expired orders stop filling at or near the expiry time | Not checked (OQ-G1) | The resting-order lifetime bound |
| **Maximum verified resting-order lifetime** | No value. Not invented here | G-4, G-9 clause 2 |
| **Maximum commitment allowed to rest** | No value. Not invented here. Depends on the commitment definition (OQ-C1, OQ-C2) | G-4, G-9 clause 2 |
| Which order styles may rest at all | Open (OQ-G2) | Strategy and risk design |

**What the deployment must eventually show for G-9's second clause.** With the outage
lasting from the moment of exhaustion to the end of the window (the worst case, because
nobody may notice), exposure stays within the G-4 bounds. That needs either:

- verified expiry with a lifetime shorter than the time to detection; or
- a resting commitment ceiling, counted at full fill value, that is acceptable to lose
  entirely.

Both are G-4 outputs. This design does not supply either, and **does not assume expiry
works**.

## 10. What a restart can and cannot recover

| After... | A process restart **can** | A process restart **cannot** |
| --- | --- | --- |
| Engine crash, server up | Reopen SQLite (the WAL replays committed transactions); reload committed configuration; start in startup pause; rebuild in-memory feed state from scratch | Know what happened at SIG while it was down; clear realtime blockers (a fresh feed has no history, so in production no market reports ready until M2 recovery evidence exists); resolve unknown-outcome keys by itself |
| VM reboot or deallocate/start | All of the above, if the state is on a persistent disk | Bring back anything stored on an ephemeral disk; keep a dynamic public IP; account for fills, settlements and cancels during the downtime |
| Credit exhaustion, then re-enable or renewal | All of the above, if the disks survived (unverified, E-AZ-17) | Undo fills that happened while down; prove the disks were not altered or lost |
| Restore from backup | Recover the state up to the backup time | Know about any order sent after the backup (section 3) |

**Before any new order after any of these** (REQ-11, G-3), the engine must:

1. stay paused;
2. read tournament-scoped cash and positions, with evidence of their scope;
3. list every open order across all pages, and the fills since the last known point;
4. resolve every journalled unknown-outcome key, or stay paused;
5. confirm that local state matches SIG;
6. confirm that every market to be traded is ready in the feed, with no outstanding
   blocker;
7. confirm that reference data is fresh, configuration is valid, rate budget is
   available, and the clock is synchronized.

Only then may the readiness check pass. None of this exists yet (S1, M2, O2).

## 11. Decisions blocked, and by what

| Decision or step | Blocked until |
| --- | --- |
| Create any cloud resource | G-9 `pass` for that candidate, recorded by Arnav |
| Choose the SKU, region and disk | E-AZ-06/08/15 (account evidence) |
| Write a service unit or launcher | An engine entry point exists (after S1/C1), with dry run as the code default (O2) |
| Copy the read-only key to the server | Server exists; H2 assigned; read-only key's realtime-token behaviour checked |
| Copy the read+trade key to the server | X1, by Arnav's decision only (G-11) |
| Localhost control interface and SSH-tunnel checks | C1 |
| Journal durability settings in code | O2 |
| Backup job | R2/O2 storage exists |
| Readiness-before-trading on the server | S1, M2, M3 |
| Any resting order on the tournament from the server | G-4 bounds recorded; G-1 to G-10 passed; X1 |
| H2 unattended dry-run soak, reboot, fault and loss-trigger demonstrations (G-10) | H1 (G-9), M3, S1, C1. **Not part of this PR** |
