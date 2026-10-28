# Phase 5 night log: it looks after itself

Branch `night/phase5-selfcare` (from `origin/main` 57275cb). Not merged.

## Built

1. **ntfy fallback** (`brain/app/alerts.py`): delivery order hub → ntfy → Telegram. `channels()`
   lists the configured ones; the first that takes the alert wins, and if all configured ones fail
   the alert stays pending for `deliver_due`. ntfy is JSON-published to the server root with topic,
   title, message, priority (brief 3, notify 4, alarm/call 5), tags `alert-<id>` + kind, and when
   `NTFY_CLICK_URL` is set a `click` URL and an "Open app" view action. Bearer `NTFY_TOKEN` if set.
2. **Health** (`brain/app/health.py`, job `health` every 5 min, `GET /health/detail` with the brain
   token). Checks: hub (`GET /api/health`, any status < 500 = alive; alert after 30 min down),
   reach (nothing can reach you: logged at ERROR and the dead-man ping goes to `<url>/fail`),
   disk (< 2 GB free where brain.db lives), db (brain.db + -wal > 1024 MB), backup (only with
   `BACKUP_REPO`: status file says failed, or last ok > 36 h). One NOTIFY per check per local day
   (key `health:<name>:<date>`, checked across all statuses, so a dismissed one is not re-raised
   that day); a passing check closes its open alert.
3. **Backups** (`deploy/linux/backup.sh`, `assistant-backup.{service,timer}.in`, `restore.md`):
   restic to `BACKUP_REPO`; every `*.db`/`*.sqlite` under both `data/` folders is copied with
   `sqlite3 .backup` (Python's backup API if the CLI is missing) into
   `~/.cache/assistant-backup/sqlite/{ai_assistant,orchestrator}/`, live DB files are excluded;
   `restic init` on first run; `forget --prune` 7/4/6; `restic check` on Sundays; writes
   `data/brain/backup-status` (`ok|failed <utc>`) for the health check. Timer 03:45,
   Persistent=true, Nice/idle IO. install.sh installs + enables it only when `BACKUP_REPO` is set
   and removes it when emptied.
4. Docs: deploy/linux/README.md (section 5), .env.example, CLAUDE.md.

## Decisions

- ntfy action buttons do not ack: the brain is loopback-only, so acks stay in the app/Telegram.
- Hub outage clock is in memory: a brain restart restarts the 30 min count (acceptable).
- "Reach" is not raised as an alert (it could not be delivered); it is logged and fails the
  healthchecks.io ping, which emails the owner.
- Added a backup-freshness check (not in the brief) so a silently broken backup is noticed.
- scheduler.py: only an import line and one `add_job` line (another agent edits it tonight).
- Backups use restic's absolute paths; restore.md says how to map them back.

## Tested

- 76/76 pytest (61 old + 15 new), ruff clean, `bash -n` on all scripts.
- backup.sh smoke-tested in Git Bash with a fake `restic` shim: both databases (WAL mode) snapshot
  with their rows, correct restic arguments/excludes/paths, status file ok/failed, missing password
  fails, empty BACKUP_REPO exits 0.

## Untested (needs the Linux machine)

- Real restic + rclone to Google Drive, `restic init`/`forget`/`check`, an actual restore.
- install.sh's timer install/remove path and the units under systemd (needs systemctl).
- A real ntfy server and a real healthchecks.io ping.
- `sqlite3` CLI path in backup.sh (smoke test used the Python fallback).

## The owner must set (in .env, then deploy/linux/install.sh)

- `NTFY_TOPIC` (+ ntfy app subscribed), optional `NTFY_CLICK_URL`, `NTFY_TOKEN`, `NTFY_URL`.
- `HEALTHCHECK_URL` from a healthchecks.io check (5 min period).
- `BACKUP_REPO` after `rclone config` (remote `gdrive`), restic password at
  `~/.config/assistant/restic-password` (copy kept off the machine); `sudo apt install restic rclone sqlite3`.
