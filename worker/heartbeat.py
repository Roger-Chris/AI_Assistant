"""Minimal worker heartbeat. Run on the Omen: python heartbeat.py
Env: BRAIN_URL (default http://localhost:8000), BRAIN_TOKEN, WORKER_URL (this machine's tailscale ip:port)."""
import os
import time
import urllib.request
import json

BRAIN_URL = os.environ.get("BRAIN_URL", "http://localhost:8000").rstrip("/")
TOKEN = os.environ.get("BRAIN_TOKEN", "change-me")
WORKER_URL = os.environ.get("WORKER_URL", "http://localhost:8100")
CAPS = os.environ.get("WORKER_CAPS", "stt").split(",")

while True:
    body = json.dumps({"name": "omen", "url": WORKER_URL, "capabilities": CAPS}).encode()
    req = urllib.request.Request(f"{BRAIN_URL}/workers/heartbeat", data=body,
                                 headers={"Content-Type": "application/json", "Authorization": f"Bearer {TOKEN}"})
    try:
        urllib.request.urlopen(req, timeout=10)
        print("heartbeat ok")
    except Exception as e:  # noqa: BLE001
        print("heartbeat failed:", e)
    time.sleep(30)
