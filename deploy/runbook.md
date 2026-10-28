# Dry-run deployment and reboot runbook (for later review)

Written Oct 28, 2026 against `main` at `efdb5a57cf22ec99c4fe69f228e341fe6dde52bc`.

This runbook lets Arnav review, ahead of time, how a dry-run deployment and a reboot
exercise would go. **Nothing in it can be executed end to end today.** Every step is
labelled:

- **NOW**: can be done today, without creating any resource (account checks, reading
  portals);
- **FUTURE**: needs a server, so only after G-9 passes and Arnav decides to create one;
- **BLOCKED (X)**: needs an interface that does not exist yet, named by its roadmap track.

**This runbook never switches the engine to live trading.** Leaving dry run is X1, gated
by G-11. It is not part of H1 or H2 and has no step here. The H2 soak itself (G-10) is
described in phases G and H as a plan. Running it, and recording its evidence, are H2.

Only Arnav performs these steps. No agent receives a key or server access.

Design references: [`design.md`](design.md). Account evidence:
[`hosting-evidence.md`](hosting-evidence.md).

## Phase 0: Gates before anything is created

| # | Prerequisite | Evidence | Status (Oct 28) |
| --- | --- | --- | --- |
| 0.1 | G-9 rows all `pass` for the chosen candidate | [`hosting-evidence.md` section 4](hosting-evidence.md#4-decision-table) | Not met |
| 0.2 | Arnav's written decision to create the server, naming candidate, SKU, region, disk and IP choice | Private index, plus a note in the H2 PR | Not made |
| 0.3 | An engine entry point exists that starts in startup pause and has **dry run as its code default** | O2, S1, C1 merged | Not built |
| 0.4 | M3 socket adapter merged (otherwise the feed cannot run) | M3 PR | Not built |
| 0.5 | Decision on which key the soak uses (proposed: read-only key), and whether a read-only key can mint a realtime token | Arnav's check | Open |

Phases A to C below can be prepared after 0.1 and 0.2. Phases D onward also need 0.3 to
0.5.

## Phase A: Account checks (NOW)

1. Work through [`hosting-evidence.md`](hosting-evidence.md) sections 1 to 2 and fill in
   the decision table.
2. Keep evidence outside the repository. Put only `pass`/`fail`/`unknown`, the date and
   the ref into the table.
3. If any row is `fail`, stop. H1 hosting stays blocked.

## Phase B: Create resources (FUTURE: Arnav only, after Phase 0.1–0.2)

This is a checklist of choices to confirm in the portal, not a script.

1. Create only the items in [`design.md` section 7](design.md#7-proposed-cloud-resources-per-candidate).
   On the final review page of the create form, confirm:
   - the VM size;
   - the OS disk type and size;
   - one public IP;
   - no extra disks;
   - no Marketplace image;
   - boot diagnostics as decided;
   - no managed identity;
   - no monitoring agent or backup.
2. Network security group or security list: allow inbound 22/tcp only, from Arnav's
   source address where practical. Deny everything else.
3. Within 1 hour of creation, and again after 24 hours: check cost analysis, or the usage
   page on the Sponsorships portal, for every meter that appears. Any meter not expected
   from the design means **stop and delete** (Phase J), then record why.
4. Confirm the spending limit is still **On** and no payment method was added. Save a dated
   screenshot (E-AZ-12, E-AZ-13).

## Phase C: Host baseline (FUTURE)

Run on the server as Arnav's admin user. These commands print no secrets.

```bash
timedatectl
```

Expect `Time zone: Etc/UTC` and `System clock synchronized: yes`.

```bash
chronyc tracking
```

Expect a small offset and a reachable source.

```bash
sudo sshd -T | grep -Ei '^(passwordauthentication|permitrootlogin|pubkeyauthentication)'
```

Expect `passwordauthentication no`, `permitrootlogin no` and `pubkeyauthentication yes`.

```bash
grep -E '^\s*Storage=' /etc/systemd/journald.conf /etc/systemd/journald.conf.d/*.conf
```

Expect `Storage=persistent`, with a `SystemMaxUse` cap.

```bash
findmnt -T /var/lib/bellwether
```

Expect the OS disk or an attached persistent disk. It must not be `/mnt` or the resource
disk, and not a network filesystem.

Create the service user and directories exactly as in
[`design.md` section 3](design.md#3-persistent-state-sqlite-wal-and-the-idempotency-journal).
Then check the result:

```bash
stat -c '%a %U:%G %n' /var/lib/bellwether /etc/bellwether
```

Expect `700 bellwether:bellwether` and `750 root:bellwether`.

## Phase D: Configuration and secret handoff (FUTURE; key placement waits for 0.5)

1. Arnav creates the secret file directly on the server, typing or pasting the key only
   into the editor:

   ```bash
   sudoedit /etc/bellwether/engine.env
   ```

   The only line is `SUSQ_API_KEY=<key>`. Never paste the key into a chat, an agent, a PR,
   a shell command line or a screenshot.
2. Check it without revealing it. These commands print a mode, an owner and a count:

   ```bash
   sudo stat -c '%a %U:%G' /etc/bellwether/engine.env
   ```

   Expect `600 root:root`.

   ```bash
   sudo grep -c '^SUSQ_API_KEY=.' /etc/bellwether/engine.env
   ```

   Expect `1`.
3. Non-secret configuration (tournament slug and UUID) goes in `/etc/bellwether/` as
   decided by C1. `config/risk.yaml` is **not** placed on the server for H2: risk limits
   exist only after Arnav commits them, and only matter at X1.
4. Confirm that no repository `.env` exists on the server:

   ```bash
   find /opt/bellwether -name '.env*' -not -name '.env.example'
   ```

   Expect no output.

## Phase E: Persistent state and backup checks (BLOCKED: O2, R2 storage)

Once storage exists, run as the service user or with sudo:

```bash
sudo ls -l /var/lib/bellwether
```

Expect the database, its `-wal` and its `-shm` files, all `-rw-------` and owned by
`bellwether`.

```bash
sudo -u bellwether sqlite3 /var/lib/bellwether/backup/<latest>.db 'PRAGMA integrity_check;'
```

Expect `ok`. Run this on the **backup copy**, never by opening the live database with
another writer.

- Confirm that the backup schedule ran and that retention is capped (design section 3).
- Pull one backup to the laptop over `scp` and run `integrity_check` there too. Keep it
  as evidence.
- **Restore drill** (only in H2, only on a stopped engine): restore an older backup, start
  the engine, and confirm that it stays paused until readiness reconciles every SIG open
  order against the journal (design section 3).

## Phase F: Localhost binding and SSH tunnel (BLOCKED: C1)

```bash
sudo ss -ltnp
```

Expect only `sshd` on `0.0.0.0:22` / `[::]:22`, and the engine control interface on
`127.0.0.1:<port>`. Nothing else may listen on a public address.

From the laptop:

```bash
ssh -N -L 8700:127.0.0.1:8700 <admin>@<server>
```

The port is a placeholder until C1 names one. With the tunnel open, the terminal
controls on the laptop reach `127.0.0.1:8700`.

From the laptop, *without* the tunnel, check that the port is closed from outside:

```bash
nc -vz -w 5 <server-public-ip> 8700
```

Expect the connection to fail.

## Phase G: Dry-run verification (BLOCKED: O2, S1, C1)

1. The engine's status command (C1) reports **dry run** as the engine's own mode, and
   names where it comes from: the code default, not a unit or environment setting.
2. The logs for the whole run show **zero requests to order placement endpoints**
   (`POST /orders`, `/orders/batch`, `/orders/multi-leg`). O2 must log every would-be order
   as a dry-run record with its `reason`, so they can be counted.
3. If the soak uses a read-only key (0.5), any placement attempt would also be refused by
   SIG. The soak must not rely on that refusal; any attempt is a failure to investigate.
4. Arnav checks the tournament's open orders on the SIG website at the start and end of
   the soak. The count must not change because of the engine.
5. The engine starts in **startup pause**, and its readiness check reports each item
   (cash scope, positions, open orders, unknown keys, market readiness, reference
   freshness, configuration, rate budget, clock).

## Phase H: Reboot exercise (BLOCKED: H2 — M3, S1, C1)

**Before the reboot.** Record each item in the evidence table.

| Check | Command or source |
| --- | --- |
| Service active, PID, start time | `systemctl status bellwether-engine` (unit name decided in H2) |
| Engine pause state, readiness result, unknown-key count | Status command (C1) over the tunnel |
| Database files present, sizes, modification times | `sudo ls -l --time-style=full-iso /var/lib/bellwether` |
| Latest backup integrity | `PRAGMA integrity_check` on the latest backup |
| Last log lines | `journalctl -u bellwether-engine -n 50 --no-pager` |
| Clock | `timedatectl` |
| SIG open orders (tournament) | SIG website, by Arnav |

**Reboot.** Use `sudo systemctl reboot` for a graceful reboot. Separately, also test a
portal **Restart**, and a portal **Stop (deallocate) then Start**. Deallocation is the
nearest test to credit exhaustion, but it is still a graceful stop.

**After the reboot.**

| Check | Expected |
| --- | --- |
| `systemctl status bellwether-engine` | Active, new PID, started once (no crash loop) |
| `journalctl -b -1 -u bellwether-engine --no-pager \| tail` | Previous boot's logs present (persistent journal) |
| Engine status | Startup pause first. Readiness check ran. No order sent before it passed. Still dry run |
| Database files | Same files present. The WAL replayed (no lost committed rows: compare journal row counts before and after) |
| `timedatectl` | Synchronized before readiness passed |
| Public IP | Note whether it changed after the deallocate test. Update the SSH rule if needed |
| SIG open orders | Unchanged by the engine |

## Phase I: Fault and allowance-exhaustion response (FUTURE)

**Server unreachable, stopped, deallocated or out of credit:**

1. Assume resting orders are live and may fill. The engine can do nothing.
2. If any order could be resting, which means only after X1, Arnav cancels it himself on
   the SIG website. During H2 there should be none, because the soak is dry run.
3. **Do not upgrade** the subscription or add a payment method to bring the server back.
   That breaks REQ-4. Record the outage instead.
4. When the server is back, do not intervene in the engine's startup. It starts paused
   and must pass readiness (design section 10). Check the restart against Phase H's
   "after" table.
5. Record start time, cause, detection time, end time and what rested. This is input to
   the G-4 bounds and the post-mortem.

**Repeated crash or start-limit hit:** the service stays stopped by design. Read
`journalctl -u bellwether-engine`. Do not raise the start limit to "keep it running".

**Disk nearly full:** check the journald cap and the backup retention. The engine should
fault-pause when journal writes fail. That behaviour belongs to S1 and is BLOCKED until
then.

## Phase J: Stop and roll back (FUTURE)

1. Stop the engine: `sudo systemctl stop bellwether-engine`. Any cancel-on-shutdown is
   best effort (design section 9).
2. Check the tournament's open orders on the SIG website. Cancel anything left, by hand.
3. Prevent restart: `sudo systemctl disable bellwether-engine`.
4. Take a final consistent backup and copy it off the server (Phase E).
5. Remove the key: `sudo shred -u /etc/bellwether/engine.env`. If a trade key was ever
   placed on the server, rotate it in SIG.
6. If the server is no longer needed: delete the VM **and** its disk, public IP, network
   security group and VNet. Deleting the resource group deletes them all. Confirm in cost
   analysis over the following days that no meter remains. Keep no snapshot unless G-9
   shows it free.

## Evidence capture

Keep the evidence outside the repository (hosting-evidence section 1). Put only the
status and the ref into the H2 PR.

| Ref | Phase | What | Evidence form | Date | Result |
| --- | --- | --- | --- | --- | --- |
| R-01 | 0 | G-9 decision recorded | Decision table row | | |
| R-02 | B | Resources created match design; no unexpected meter at 1 h and 24 h | Cost analysis screenshots | | |
| R-03 | B | Spending limit On, no payment method, after creation | Screenshots | | |
| R-04 | C | Clock synchronized, UTC, SSH hardened, persistent journal, state on persistent disk | Command output (no secrets) | | |
| R-05 | D | Secret file mode and count check; no repository `.env` | Command output | | |
| R-06 | E | Backup integrity on server and laptop; restore drill keeps engine paused | Command output, status output | | |
| R-07 | F | Only sshd public; control interface on 127.0.0.1; external port closed | `ss`, `nc` output | | |
| R-08 | G | Dry run from code default; zero placement requests; SIG open orders unchanged | Status, log count, SIG website | | |
| R-09 | H | Reboot, restart and deallocate/start: startup pause, readiness, persistent logs and DB | Before/after tables | | |
| R-10 | I | Any outage: cause, detection, duration, resting exposure | Incident note | | |
| R-11 | J | Teardown: no remaining meters | Cost analysis | | |

R-08 and R-09 are G-10 evidence, and they belong to **H2**, not H1. Offline tests in this
repository are never cloud, SIG or G-10 evidence.
