# Restoring from a backup

`deploy/linux/backup.sh` (nightly, `assistant-backup.timer`) puts these into one restic
repository (`BACKUP_REPO`), encrypted with the password in `BACKUP_PASSWORD_FILE`:

| in the snapshot (absolute paths of the machine that made it) | what |
|---|---|
| `~/AI_Assistant/data/` | everything except live `*.db`/`*.sqlite` files (WhatsApp session, Google tokens, media, CV) |
| `~/AI_Assistant/.env` | settings and keys |
| `~/Orchestrator/data/` | hub data except live databases (style book, voice notes, push subscriptions) |
| `~/Orchestrator/.env`, `fcm-service-account.json`, `gmail-client.json` | hub secrets |
| `~/.cache/assistant-backup/sqlite/ai_assistant/...` | a safe copy of every AI_Assistant database, e.g. `brain/brain.db` |
| `~/.cache/assistant-backup/sqlite/orchestrator/...` | a safe copy of every Orchestrator database, e.g. `orchestrator.db` |

The databases are stored separately on purpose: the live files were skipped and these copies were
made with SQLite's online backup, so they are consistent. **Put them back by hand (step 4).**

Paths below assume the checkouts are `~/AI_Assistant` and `~/Orchestrator`; adjust if yours differ
(the Orchestrator may be `~/ai-orchestrator`).

## What you need

- The restic password. Keep a copy **outside** this machine (password manager): without it the
  backup cannot be read by anyone, you included.
- Access to the repository: for Google Drive, a working `rclone` remote with the same name
  (`rclone config` on the new machine, see README, Backups); for S3/R2, the same keys.
- `sudo apt install -y restic rclone sqlite3`

## 1. Point restic at the repository

```bash
export RESTIC_REPOSITORY='rclone:gdrive:assistant-backup'      # your BACKUP_REPO
export RESTIC_PASSWORD_FILE=~/.config/assistant/restic-password # recreate it first if needed
# S3/R2 only: export AWS_ACCESS_KEY_ID=... AWS_SECRET_ACCESS_KEY=...
restic snapshots                    # lists them; pick "latest" or an ID
```

## 2. Stop everything that writes

```bash
systemctl --user stop assistant-backup.timer assistant-bridge assistant-brain
systemctl --user stop orchestrator-hub 2>/dev/null || true
```

Also stop any other WhatsApp bridge on the same session (the old laptop): two bridges get it logged
out.

## 3. Restore into a scratch folder

```bash
restic restore latest --target ~/restore      # or a snapshot ID instead of latest
ls ~/restore/home/*/                          # the old machine's home folder is inside
OLD=$(echo ~/restore/home/*)                  # if there is only one
```

To restore a single file: `restic restore latest --target ~/restore --include '/home/<user>/AI_Assistant/.env'`.
To look around first: `restic ls latest | less`.

## 4. Put the files back

```bash
# code first, if this is a new machine
git clone https://github.com/Roger-Chris/AI_Assistant.git ~/AI_Assistant   # and the Orchestrator

# AI_Assistant
mkdir -p ~/AI_Assistant/data
cp -a "$OLD/AI_Assistant/data/." ~/AI_Assistant/data/
cp -a "$OLD/AI_Assistant/.env" ~/AI_Assistant/.env
cp -a "$OLD/.cache/assistant-backup/sqlite/ai_assistant/." ~/AI_Assistant/data/
rm -f ~/AI_Assistant/data/brain/brain.db-wal ~/AI_Assistant/data/brain/brain.db-shm   # stale, from before

# Orchestrator
mkdir -p ~/Orchestrator/data
cp -a "$OLD/Orchestrator/data/." ~/Orchestrator/data/
cp -a "$OLD/Orchestrator/"{.env,fcm-service-account.json,gmail-client.json} ~/Orchestrator/ 2>/dev/null
cp -a "$OLD/.cache/assistant-backup/sqlite/orchestrator/." ~/Orchestrator/data/
rm -f ~/Orchestrator/data/orchestrator.db-wal ~/Orchestrator/data/orchestrator.db-shm

chmod 700 ~/AI_Assistant/data; chmod 600 ~/AI_Assistant/.env ~/Orchestrator/.env
```

Check each database before starting:

```bash
sqlite3 ~/AI_Assistant/data/brain/brain.db 'pragma integrity_check; select count(*) from task;'
sqlite3 ~/Orchestrator/data/orchestrator.db 'pragma integrity_check;'
```

Both should print `ok`.

## 5. Start again

```bash
~/AI_Assistant/deploy/linux/install.sh       # units, timer (if BACKUP_REPO is set), restart
# the Orchestrator: its own deploy/linux/install.sh
curl -s localhost:8000/health
```

Then remove the scratch copy: `rm -rf ~/restore` (it holds your secrets unencrypted).

## Testing a restore (do this once)

A backup you have never restored is a hope, not a backup. Once, after the first nightly run:

```bash
restic restore latest --target /tmp/restore-test --include '*/assistant-backup/sqlite/*'
sqlite3 /tmp/restore-test/home/*/.cache/assistant-backup/sqlite/ai_assistant/brain/brain.db 'pragma integrity_check;'
rm -rf /tmp/restore-test
```

`restic check` runs every Sunday on its own; `restic check --read-data-subset=5%` reads back a
sample of the actual data when you want more confidence.
