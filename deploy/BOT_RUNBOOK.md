# Election-week bot runbook (BOT-EW, Ubuntu 24.04)

Copy-paste commands for Arnav. Every command that touches SIG with the trade key is
marked **LIVE**. Agents never run any of this (CLAUDE.md hard rules 1 and 8).

The examples use `ArnavJain2005/Bellwether` and a server you reach as `you@host`.

## 1. Server setup (once)

Python 3.11 is not in Ubuntu 24.04's own archive, so it comes from the deadsnakes PPA.

```bash
sudo apt update
sudo apt install -y software-properties-common git sqlite3 chrony
sudo add-apt-repository -y ppa:deadsnakes/ppa
sudo apt update
sudo apt install -y python3.11 python3.11-venv
timedatectl   # "System clock synchronized: yes"
```

Create the service user and its folder:

```bash
sudo useradd --system --create-home --home-dir /home/bellwether --shell /usr/sbin/nologin bellwether
sudo mkdir -p /opt/bellwether
sudo chown bellwether:bellwether /opt/bellwether
```

## 2. Clone the private repository (read-only token)

On GitHub: Settings → Developer settings → Fine-grained tokens → generate a token for
**this repository only**, permission **Contents: Read-only**, short expiry.

The token is typed at a hidden prompt, used once, and removed from the remote URL:

```bash
read -rs GH_TOKEN && export GH_TOKEN
sudo -u bellwether git clone "https://oauth2:${GH_TOKEN}@github.com/ArnavJain2005/Bellwether.git" /opt/bellwether
sudo -u bellwether git -C /opt/bellwether remote set-url origin https://github.com/ArnavJain2005/Bellwether.git
unset GH_TOKEN
```

Later updates (paste the token again at the prompt; `git pull` asks for a username,
type `oauth2`):

```bash
sudo -u bellwether git -C /opt/bellwether pull
```

## 3. Python environment

```bash
cd /opt/bellwether
sudo -u bellwether python3.11 -m venv .venv
sudo -u bellwether .venv/bin/pip install --upgrade pip
sudo -u bellwether .venv/bin/pip install -e .
if [ -f dashboard/requirements.txt ]; then sudo -u bellwether .venv/bin/pip install -r dashboard/requirements.txt; else sudo -u bellwether .venv/bin/pip install streamlit; fi
sudo -u bellwether mkdir -p bot/state
```

## 4. Secrets file

`/etc/bellwether.env` holds the key. systemd reads it as root; nobody prints it.

```bash
sudo install -m 600 -o root -g bellwether /dev/null /etc/bellwether.env
sudoedit /etc/bellwether.env
```

Put exactly these two lines in it (your real key after the `=`), then save:

```
SUSQ_API_KEY=paste-the-key-here
BOT_ARGS=--dry-run
```

Check the permissions (never `cat` the file):

```bash
sudo ls -l /etc/bellwether.env   # -rw------- 1 root bellwether
```

## 5. Install the services

```bash
sudo cp /opt/bellwether/deploy/bot.service /etc/systemd/system/bellwether-bot.service
sudo cp /opt/bellwether/deploy/dashboard.service /etc/systemd/system/bellwether-dashboard.service
sudo systemctl daemon-reload
```

## 6. Check the reference prices (no key, no SIG)

```bash
cd /opt/bellwether && sudo -u bellwether .venv/bin/python -m bot.refs_check
```

Every approved contract should show a Kalshi bid/ask and a fair value. `bot/map.yaml`
must have approved contracts, or the bot refuses to start.

## 7. The NO-price probe (LIVE, places two 1-share orders)

Pick one approved exchange id from `bot/map.yaml` (`sig_exchange_id`). The probe runs as
the service user with the key from the env file, in your terminal:

```bash
EXCHANGE_ID=941   # replace with an approved sig_exchange_id
sudo systemd-run --uid=bellwether --gid=bellwether --property=EnvironmentFile=/etc/bellwether.env --working-directory=/opt/bellwether --pty --wait --collect /opt/bellwether/.venv/bin/python -m bot.probe --exchange "$EXCHANGE_ID" --i-am-arnav
```

It prints a verdict. Only if it says `no_price` or `yes_price`, set that line in
`config/risk.yaml` (`no_price_convention: ...`), and `no_orders_enabled: true` only if
you want NO orders. Otherwise leave both as they are.

## 8. Start the dry run

After the one-time setup above, run this **one command on the server** from any
directory:

```bash
bash /opt/bellwether/deploy/botctl.sh start
```

It checks the approved map, requires `mode: dry_run`, refuses to clear a STOP or
PAUSED file, and enables/starts the systemd bot service. It verifies an active
service, boot enablement and a fresh, unpaused dry-run heartbeat before reporting
success. It never turns on live mode. Keep `BOT_ARGS=--dry-run` in
`/etc/bellwether.env` (check privately in an editor; never print the key).
The bot itself requires both switches for live mode.

The service runs independently of your terminal: closing SSH does not stop it.
`Restart=on-failure` retries a crash, up to five starts per ten minutes; a
deliberate STOP exits cleanly and does not restart. A persistent PAUSED file
prevents new orders after a reboot. Startup and reconciliation can still fail;
the heartbeat check makes that visible rather than silently claiming success.
Systemd cannot keep the bot running through a host outage or lost network.

Check it later with:

```bash
bash /opt/bellwether/deploy/botctl.sh status
bash /opt/bellwether/deploy/botctl.sh logs
```

`status` exits nonzero for a missing/stale heartbeat, pause, STOP, inactive or
disabled service, or a mode mismatch. If it fails, inspect `logs` and the
PAUSED/STOP cause; do not remove either file just to make it green. To start the
dashboard too (optional), run `sudo systemctl enable --now bellwether-dashboard`
once and use the SSH tunnel in section 10.

## 9. Reading what it does

```bash
journalctl -u bellwether-bot -f
journalctl -u bellwether-bot --since "15 min ago" --no-pager
```

```bash
cd /opt/bellwether/bot/state
sqlite3 -readonly -header -column bellwether.db "SELECT * FROM heartbeat;"
sqlite3 -readonly -header -column bellwether.db "SELECT id, ts_utc, race_key, strategy, verdict, side, price_side, quantity, reason FROM decisions ORDER BY id DESC LIMIT 20;"
sqlite3 -readonly -header -column bellwether.db "SELECT key, created_utc, race_key, side, price_side, quantity, status, sig_order_id, error FROM orders WHERE status IN ('journaled','sent','resting','unknown') ORDER BY created_utc DESC;"
sqlite3 -readonly -header -column bellwether.db "SELECT id, ts_utc, kind, message FROM events WHERE level = 'error' ORDER BY id DESC LIMIT 20;"
cat PAUSED 2>/dev/null || echo "not paused"
```

## 9a. Record a server dry-run and reboot rehearsal (Arnav only)

This is evidence for the **election-week bot**, not proof that the full PRD engine
passed G-10. Run only with both `BOT_ARGS=--dry-run` and `mode: dry_run`.
Keep the trade key, raw account screenshots, server address and unredacted logs
outside GitHub. Record the UTC start/end times, deployed commit, host provider and
instance type, and a private evidence reference. An observed run, including an
incident or pause, is useful evidence; do not label it a pass by omission.

Before the run, check the deployed revision, the two dry-run switches and the
service state. Read the env file only in your editor; do not print it or include
its contents in the evidence:

```bash
git -C /opt/bellwether rev-parse HEAD
grep '^mode:' /opt/bellwether/config/risk.yaml
systemctl status bellwether-bot --no-pager
date -u +%FT%TZ
```

Confirm `BOT_ARGS=--dry-run` privately in `/etc/bellwether.env`. In the SIG
website, record the tournament's open-order IDs and any fills at the start and
end. A count alone cannot identify which orders changed. Note any other process
or manual trading using the same account during the interval.

During and after the run, save dated, redacted output from these read-only
checks. The single-row heartbeat is only the latest state; snapshots and
service logs establish the time span. The database may contain older orders or
events, so filter the records to the chosen UTC interval when evaluating them:

```bash
sqlite3 -readonly -header -column /opt/bellwether/bot/state/bellwether.db "SELECT ts_utc, mode, paused, pause_reason, balance, committed FROM heartbeat;"
sqlite3 -readonly -header -column /opt/bellwether/bot/state/bellwether.db "SELECT min(ts_utc) AS first, max(ts_utc) AS last, count(*) AS samples FROM snapshots;"
sqlite3 -readonly -header -column /opt/bellwether/bot/state/bellwether.db "SELECT verdict, count(*) FROM decisions GROUP BY verdict;"
sqlite3 -readonly -header -column /opt/bellwether/bot/state/bellwether.db "SELECT status, count(*) FROM orders GROUP BY status;"
sqlite3 -readonly -header -column /opt/bellwether/bot/state/bellwether.db "SELECT ts_utc, level, kind, message FROM events ORDER BY id DESC LIMIT 50;"
journalctl -u bellwether-bot --since "YYYY-MM-DD HH:MM:SS UTC" --until "YYYY-MM-DD HH:MM:SS UTC" --no-pager
```

Replace both journal timestamps with the recorded interval. Check the output
for secrets before storing or sharing it. A `would_order` decision demonstrates
a dry-run decision, while unchanged SIG orders and no new journaled bot orders
are independent checks. These queries **do not prove that zero placement HTTP
requests were attempted**; that stronger claim requires request-level evidence.

For a reboot check, first verify both dry-run switches again. Record the
pre-reboot boot ID (`cat /proc/sys/kernel/random/boot_id`), heartbeat, latest
snapshot time, service status, and SIG open-order inventory. Reboot the server
yourself. Record the new boot ID and the same observations, plus
`journalctl -b -1 -u bellwether-bot --no-pager` and
`sqlite3 -readonly /opt/bellwether/bot/state/bellwether.db "PRAGMA integrity_check;"`.
A new PID alone is not proof of state continuity: compare the snapshot and
order-journal records across the reboot, confirm the mode stayed `dry_run`,
and report any pause, missing data or change in SIG orders.

Use this evidence index; keep raw files private and commit only the redacted
status, date and reference after review:

| Check | Private evidence reference | Observed result | Status |
| --- | --- | --- | --- |
| Deployed commit, host and two dry-run switches | | | unknown |
| Start/end UTC; heartbeat, snapshots, decisions and events | | | unknown |
| SIG open-order IDs/fills before and after; other account activity | | | unknown |
| Reboot boot IDs, service logs, SQLite integrity and journal continuity | | | unknown |
| Hosting account eligibility, billing controls and exhaustion | See `hosting-evidence.md` | | unknown |
| Tiny SIG order/cancel and expiry behavior | See `docs/design/order-wire-contracts.md` | | unknown |

The SIG order/cancel checks are separate live-platform actions by Arnav. Inventory
all tournament open orders before any cancel-all check; the bot's live startup
currently cancels the entire tournament, including unrelated orders. Do not use
a live bot restart as a substitute for an isolated check. Account-specific hosting
evidence must satisfy `hosting-evidence.md`; a running VM alone does not prove ₹0
cost or acceptable behavior when its allowance runs out.

## 10. Dashboard over an SSH tunnel

On your laptop:

```bash
ssh -N -L 8501:127.0.0.1:8501 you@host
```

Then open http://localhost:8501. The dashboard listens on 127.0.0.1 only.

## 11. Go live (LIVE)

Both switches are needed: `mode: live` in `config/risk.yaml` and `BOT_ARGS=--live`.

1. In `config/risk.yaml` set `mode: live`, `scale` (0.10 first), and `start_balance`
   (from the SIG app) so the loss stop works:

   ```bash
   sudo -u bellwether nano /opt/bellwether/config/risk.yaml
   ```

2. In the env file change `BOT_ARGS=--dry-run` to `BOT_ARGS=--live`:

   ```bash
   sudoedit /etc/bellwether.env
   ```

3. Restart and confirm the heartbeat says `live`:

   ```bash
   sudo systemctl restart bellwether-bot
   sqlite3 -readonly /opt/bellwether/bot/state/bellwether.db "SELECT mode, paused, balance, committed FROM heartbeat;"
   ```

On a live start the bot first cancels every open order in the tournament. Raise
`scale` later (0.5, then 1.0) the same way: edit, then restart.

Also commit the same `config/risk.yaml` change to the repository, so a later
`git pull` does not undo it or conflict.

## 12. STOP

```bash
sudo -u bellwether touch /opt/bellwether/bot/state/STOP
```

Within one loop (10 s) a live bot cancels every open order, writes a stop event and
exits 0; systemd does not restart it. To run again:

```bash
sudo -u bellwether rm /opt/bellwether/bot/state/STOP
sudo systemctl start bellwether-bot
```

`sudo systemctl stop bellwether-bot` also cancels everything first when live.

## 13. Resume after PAUSED

PAUSED means no new orders; cancels still work and the heartbeat keeps running.

1. Read why:

   ```bash
   cat /opt/bellwether/bot/state/PAUSED
   sqlite3 -readonly -header -column /opt/bellwether/bot/state/bellwether.db "SELECT id, ts_utc, kind, message FROM events WHERE level = 'error' ORDER BY id DESC LIMIT 10;"
   ```

2. If an order is `unknown`, the bot sends nothing while it stays unknown. Check that
   order in the SIG app (open orders, fills). Then stop the bot and record what you
   found. Use `filled` with its real fill if it traded, otherwise `cancelled`:

   ```bash
   sudo systemctl stop bellwether-bot
   sqlite3 /opt/bellwether/bot/state/bellwether.db "UPDATE orders SET status = 'cancelled', error = coalesce(error, '') || ' | resolved by Arnav: not on SIG' WHERE key = 'THE-KEY';"
   ```

3. Remove the pause file and start (or leave running if it was not stopped):

   ```bash
   sudo -u bellwether rm /opt/bellwether/bot/state/PAUSED
   sudo systemctl start bellwether-bot
   ```

A `reconcile` pause in live mode returns while its cause remains, for example an open
order you placed by hand in the SIG app. Cancel or let it finish first.

## 13a. Update the server and the limits ladder (BOT-EW-4)

Update to `origin/main` without losing the server's edits to `config/risk.yaml`,
`bot/map.yaml` exclusions and `config/ladder.yaml` (a live stop cancels every open order):

```bash
sudo systemctl stop bellwether-bot
cd /opt/bellwether && sudo -u bellwether bash deploy/update_server.sh
sudo systemctl start bellwether-bot
```

The ladder (`config/ladder.yaml`) raises scale and caps one step at a time, only when
the step's time has passed and every health gate passes. No restart is needed for:

```bash
sudo -u bellwether touch /opt/bellwether/bot/state/LADDER_HOLD   # freeze (no climbing)
sudo -u bellwether rm /opt/bellwether/bot/state/LADDER_HOLD      # unfreeze
sudo -u bellwether /opt/bellwether/.venv/bin/python -m bot.ladder status   # every gate
```

To step down, lower `max_step` in `config/ladder.yaml` (e.g. `max_step: L1`, or `L0` for
risk.yaml's limits): the bot drops to it at its next loop. After a restart the bot
resumes its step only if ladder.yaml is byte-identical to the file that step was set
under; otherwise it starts again at L0.

## 14. Laptop fallback

If the server is unavailable, run the bot from the repository on a laptop that stays
awake and online. Use the same `config/risk.yaml`. The state folder is the laptop's
own `bot/state/`.

macOS (from the repository folder, with the venv):

```bash
read -rs SUSQ_API_KEY && export SUSQ_API_KEY
caffeinate -dimsu .venv/bin/python -m bot.main --live
```

Windows PowerShell (from the repository folder). Keep the laptop plugged in and the lid
open; the `powercfg` lines stop sleep on AC power until you undo them:

```powershell
powercfg /change standby-timeout-ac 0
powercfg /change hibernate-timeout-ac 0
$s = Read-Host -AsSecureString "SUSQ_API_KEY"
$env:SUSQ_API_KEY = [Runtime.InteropServices.Marshal]::PtrToStringAuto([Runtime.InteropServices.Marshal]::SecureStringToBSTR($s))
.venv\Scripts\python -m bot.main --live
```

Afterwards restore sleep (example values):

```powershell
powercfg /change standby-timeout-ac 30
powercfg /change hibernate-timeout-ac 60
```

Dashboard on the laptop: `.venv/bin/streamlit run dashboard/app.py` (Windows:
`.venv\Scripts\streamlit run dashboard\app.py`). STOP on a laptop: create the file
`bot/state/STOP`, or press Ctrl+C, which cancels everything first when live.

Never run the server and the laptop live at the same time: both would trade one
account, each blind to the other's journal.
