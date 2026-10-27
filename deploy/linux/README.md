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

## Everyday

| | |
|---|---|
| status | `systemctl --user status assistant-brain assistant-bridge` |
| logs | `journalctl --user -u assistant-brain -f` (or `-u assistant-bridge`) |
| restart | `systemctl --user restart assistant-brain` |
| update | `deploy/linux/update.sh` (`git pull --ff-only`, then the installer) |
| health | `curl -s localhost:8000/health` |
| tests | `cd brain && uv run --extra dev pytest -q` |
| database | `sqlite3 data/brain/brain.db` (fine natively; the "disk I/O error" was a Docker thing) |

`deploy/linux/run brain|bridge [command]` runs anything with the same environment as the
services: `.env`, then `native.env` on top.

## Files here

- `native.env`: paths and addresses for running without Docker. Loaded after `.env`.
- `*.service.in`: unit templates. The installer fills in the repo path, `node` and your `PATH`.
- `install.sh`, `update.sh`, `run`: as above. `env.sh` is their shared `.env` reader.
