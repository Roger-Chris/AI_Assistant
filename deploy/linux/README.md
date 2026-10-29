# Running on the Linux machine (no Docker)

The brain and the bridge run as **systemd user services** straight from this checkout. There is
no Docker here. On a hard disk with 8 GB of RAM, images only add slow builds and a networking
layer, and the Orchestrator hub on the same machine has to run natively anyway.

| service | what | listens on |
|---|---|---|
| `assistant-brain` | `brain/.venv/bin/uvicorn`, Python 3.12 via `uv` | `127.0.0.1:8000` only |
| `assistant-bridge` | `node bridge/index.js` | nothing; it only talks to the brain |

The brain is never on the network. The bridge and the Orchestrator hub reach it on localhost.

## 1. Prerequisites (once)

```bash
sudo apt update && sudo apt install -y git curl
curl -LsSf https://astral.sh/uv/install.sh | sh        # uv (installs Python 3.12 for the brain)
```

**Node 22 LTS.** The bridge needs 20+, but the Orchestrator hub needs 22.13+, so install 22 once
for both. If `apt-cache policy nodejs` offers 22 or newer, `sudo apt install -y nodejs npm` is
enough. Otherwise use NodeSource:

```bash
curl -fsSL https://deb.nodesource.com/setup_22.x | sudo -E bash -
sudo apt install -y nodejs
```

**Tailscale**, logged into your tailnet: `curl -fsSL https://tailscale.com/install.sh | sh`, then
`sudo tailscale up`.

**Compressed swap (recommended on 8 GB).** The hub's AI CLIs come in bursts, and swapping to a
hard disk is very slow. zram keeps swap compressed in RAM: `sudo apt install -y zram-tools`, set
`PERCENT=50` in `/etc/default/zramswap`, then `sudo systemctl restart zramswap`.

Open a new shell afterwards so `uv` is on your `PATH`.

## 2. Get the code and your data

```bash
git clone https://github.com/Roger-Chris/AI_Assistant.git ~/AI_Assistant
cd ~/AI_Assistant
```

Moving from the Windows laptop? **Stop it there first.** Two bridges on one WhatsApp session get it
logged out, and copying a SQLite file while it is being written can corrupt it:

```powershell
# on Windows, in the AI_Assistant folder
docker compose stop
scp -r data .env <you>@<linux-machine>:~/AI_Assistant/
```

The `data/` layout is unchanged (`data/brain/brain.db`, `data/bridge-auth/`, `data/brain/google/`,
`data/brain/media/`), so your messages, tasks, graph, WhatsApp session and Google tokens all carry
over. An old `.env` is fine as it is: `native.env` overrides its Docker-only values
(`BRAIN_URL=http://brain:8000`, `DATABASE_URL`).

Starting fresh instead? Skip the copy. The installer creates `.env` from `.env.example` and stops
so you can fill it in.

## 3. Install and start

```bash
deploy/linux/install.sh
```

It checks the tools, creates `brain/.venv`, installs the bridge's packages, writes
`~/.config/systemd/user/assistant-{brain,bridge}.service`, enables lingering (so the services start
at boot and keep running after you log out), starts the brain and waits for `/health`. It can be
run again at any time.

**Pairing WhatsApp** (only if you didn't copy `data/bridge-auth/`):

```bash
deploy/linux/run bridge          # shows the QR: WhatsApp -> Linked devices -> Link a device
# wait for "connected", Ctrl-C, then:
systemctl --user start assistant-bridge
```

## 4. Google sign-in on a machine with no browser

Skip this if you copied `data/brain/google/` with its `token*.json` files. Otherwise put the OAuth
client JSON at `data/brain/google/credentials.json`, then from **the laptop** open a tunnel and run
the sign-in through it:

```bash
ssh -L 8765:localhost:8765 <you>@<linux-machine>
cd ~/AI_Assistant && deploy/linux/run brain python -m app.google_auth           # primary
cd ~/AI_Assistant && deploy/linux/run brain python -m app.google_auth college   # a second account
```

Open the printed URL in the laptop's browser. The redirect to `localhost:8765` travels back
through the tunnel. Then run `systemctl --user restart assistant-brain`.

Seed the memory graph from your CV (put it at `data/brain/profile/resume.docx`):

```bash
deploy/linux/run brain python -m app.seed_profile ../data/brain/profile/resume.docx
```

## 5. Looking after itself (optional, recommended)

Three things make the machine tell you when it is in trouble and survive a dead disk. Each one is
off until you set it in `.env`; run `deploy/linux/install.sh` (or `systemctl --user restart
assistant-brain`) afterwards. Nothing here signs in or creates accounts for you.

### A second way to your phone: ntfy

Alerts go to the hub (the phone app) first. When the hub does not answer they go to ntfy.

1. Install the **ntfy** app on the phone (Play Store or F-Droid).
2. Make a topic name nobody can guess: `openssl rand -hex 16` and prefix it, e.g. `roger-a1b2...`.
   On ntfy.sh anyone who knows the topic can read it, and alert texts pass through their server.
   (A self-hosted ntfy server works the same: set `NTFY_URL` to it.)
3. In the app: **Subscribe to topic**, same name, server `ntfy.sh` (or yours).
4. In `.env`: `NTFY_TOPIC=<that name>`, and optionally `NTFY_CLICK_URL=http://<hub tailscale ip>:4317`
   (tapping opens the hub) and `NTFY_TOKEN` if your server needs one.

Levels map to ntfy priorities: brief 3 (default), notification 4 (high), alarm and call 5 (max).
Done / Snooze / Not important stay in the app: ntfy cannot reach the loopback-only brain.

### Health checks and a dead-man switch

Every 5 minutes the brain checks itself (`brain/app/health.py`) and raises one notification a day
per problem: hub not answering for 30 min, under 2 GB free on the data disk, `brain.db` over 1 GB,
nightly backup failed or older than 36 h. If nothing at all can reach you (hub down and no ntfy)
it logs `NOTHING CAN REACH THE OWNER` and tells healthchecks.io the check failed.
`curl -s -H "Authorization: Bearer $BRAIN_TOKEN" localhost:8000/health/detail` shows the results.

For the case where the whole machine dies:

1. Sign up at https://healthchecks.io (free), **Add Check**: period 5 minutes, grace 15 minutes.
2. Copy its ping URL (`https://hc-ping.com/<uuid>`) into `.env` as `HEALTHCHECK_URL`.
3. Under Integrations, make sure email (or their phone app) is on.

When the pings stop, healthchecks.io tells you.

### Nightly backups (restic)

`deploy/linux/backup.sh` backs up `data/` and `.env` of this repo and of the Orchestrator
(`../Orchestrator` or `../ai-orchestrator`, or `ORCHESTRATOR_DIR`), plus the hub's
`fcm-service-account.json` and `gmail-client.json`. Every SQLite database is first copied with
SQLite's online backup (never a raw copy of a live file). restic encrypts and deduplicates, and
keeps 7 daily, 4 weekly and 6 monthly snapshots. The installer adds `assistant-backup.timer`
(03:45 nightly, catches up after the machine was off) only when `BACKUP_REPO` is set, and removes
it again when it is emptied. Restoring: `deploy/linux/restore.md`.

```bash
sudo apt install -y restic rclone sqlite3
```

**Google Drive through rclone** (you do this once; it signs in to your Google account):

```bash
rclone config
#  n  (new remote) -> name: gdrive -> storage: drive -> client_id/secret: leave empty
#  scope: 1 (full access) or 3 (drive.file, only files rclone creates) -> root folder / service account: empty
#  advanced config: n -> auto config: n  (this machine has no browser)
#  it prints an "rclone authorize" command: run that on the laptop (rclone installed there),
#  sign in in the browser, paste the token it prints back here -> shared drive: n -> y (keep)
rclone mkdir gdrive:assistant-backup
rclone lsd gdrive:                      # should list assistant-backup
```

**The restic password** (lose it and the backup is unreadable, so keep a copy in your password
manager, off this machine):

```bash
mkdir -p ~/.config/assistant && chmod 700 ~/.config/assistant
openssl rand -base64 32 > ~/.config/assistant/restic-password
chmod 600 ~/.config/assistant/restic-password
```

Then in `.env`: `BACKUP_REPO=rclone:gdrive:assistant-backup` (the default password file is the one
above; set `BACKUP_PASSWORD_FILE` only to use another). For S3 or Cloudflare R2 instead:
`BACKUP_REPO=s3:https://<account>.r2.cloudflarestorage.com/<bucket>` with `AWS_ACCESS_KEY_ID` and
`AWS_SECRET_ACCESS_KEY` in `.env`.

```bash
deploy/linux/install.sh                 # installs the timer
deploy/linux/backup.sh                  # first run now (creates the restic repository)
systemctl --user list-timers assistant-backup.timer
```

## Everyday

| | |
|---|---|
| status | `systemctl --user status assistant-brain assistant-bridge` |
| logs | `journalctl --user -u assistant-brain -f` (or `-u assistant-bridge`) |
| restart | `systemctl --user restart assistant-brain` |
| update | `deploy/linux/update.sh` (`git pull --ff-only`, then the installer) |
| health | `curl -s localhost:8000/health` |
| tests | `cd brain && uv run --extra dev pytest -q` |
| self-checks | `curl -s -H "Authorization: Bearer $BRAIN_TOKEN" localhost:8000/health/detail` |
| backup now | `deploy/linux/backup.sh` (logs of the nightly one: `journalctl --user -u assistant-backup`) |
| database | `sqlite3 data/brain/brain.db` (fine natively; the "disk I/O error" was a Docker thing) |

`deploy/linux/run brain|bridge [command]` runs anything with the same environment as the
services: `.env`, then `native.env` on top.

## Files here

- `native.env`: paths and addresses for running without Docker. Loaded after `.env`.
- `*.service.in`: unit templates. The installer fills in the repo path, `node` and your `PATH`.
- `install.sh`, `update.sh`, `run`: as above. `env.sh` is their shared `.env` reader.
- `backup.sh`, `assistant-backup.{service,timer}.in`: nightly restic backup; `restore.md`: getting it back.
