# worker — the HP Omen (Phase 4)

Everything that needs the RTX 5050 lives here and **never** in `brain/`:

- `faster-whisper` for voice notes / dictation
- a local model (Ollama) for anything you don't want leaving the house
- file / browser automation

It registers with the brain by heartbeat (`POST /workers/heartbeat` every 30 s) so the brain
knows when the laptop is on. When it's off, the brain falls back to cloud providers.

Until Phase 4 this folder only holds `heartbeat.py`, which you can run to prove the
registration path works end to end.
