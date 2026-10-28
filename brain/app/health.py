"""The brain looks after itself: self-checks every 5 minutes and a dead-man ping.

Checks (each one raises at most one NOTIFY alert a day, key `health:<name>:<local date>`, and its
open alert is closed again once the check passes):
  hub    the Orchestrator hub (HUB_URL) has not answered for HEALTH_HUB_DOWN_MINUTES
  reach  nothing can reach you: no ntfy, no Telegram and the hub is down (or not set up). This one
         cannot be sent anywhere, so it is logged loudly and the dead-man ping reports a failure
         (healthchecks.io then emails you).
  disk   the data volume has less than HEALTH_MIN_FREE_GB free
  db     brain.db (with its -wal) is bigger than HEALTH_MAX_DB_MB

WhatsApp silence and Gmail errors are watched in watcher.py, not here.

Dead-man ping: when HEALTHCHECK_URL is set (a healthchecks.io check), every run GETs it, so if the
whole machine dies healthchecks.io notices the missing pings and tells you.
"""
from __future__ import annotations

import logging
import shutil
from dataclasses import asdict, dataclass
from datetime import datetime
from pathlib import Path
from typing import Optional
from zoneinfo import ZoneInfo

import httpx
from sqlmodel import Session, select

from . import alerts
from .config import settings
from .db import Alert, engine, utcnow

log = logging.getLogger("brain.health")

# tests swap in an httpx.MockTransport (hub probe and dead-man ping)
transport: Optional[httpx.AsyncBaseTransport] = None

# when the hub first stopped answering; in memory, so a restart starts the clock again
_hub_down_since: Optional[datetime] = None
last_ping: dict = {}


@dataclass
class Check:
    name: str
    ok: bool
    detail: str
    alert: bool = False  # bad enough to tell you (the hub gets a grace period first)


def _db_path() -> Optional[Path]:
    db = engine.url.database
    return Path(db) if db and db != ":memory:" else None


async def _hub_answers() -> bool:
    headers = {"Authorization": f"Bearer {settings.orc_token}"} if settings.orc_token else {}
    try:
        async with httpx.AsyncClient(timeout=10, transport=transport) as c:
            r = await c.get(f"{settings.hub_url.rstrip('/')}/api/health", headers=headers)
    except httpx.HTTPError as e:
        log.info("hub probe failed: %s", e)
        return False
    return r.status_code < 500  # a 401 still means the hub is alive


async def check_hub(now: datetime) -> Optional[Check]:
    global _hub_down_since
    if not settings.hub_url:
        return None
    if await _hub_answers():
        _hub_down_since = None
        return Check("hub", True, "answering")
    _hub_down_since = _hub_down_since or now
    minutes = int((now - _hub_down_since).total_seconds() // 60)
    late = minutes >= settings.health_hub_down_minutes
    return Check("hub", False, f"not answering for {minutes} min at {settings.hub_url}", alert=late)


def check_reach(hub: Optional[Check]) -> Check:
    others = [c for c in alerts.channels() if c != "hub"]
    if others:
        return Check("reach", True, "fallback: " + ", ".join(others))
    if hub is not None and hub.ok:
        return Check("reach", True, "hub only (no ntfy or Telegram fallback)")
    why = "the hub is down" if hub is not None else "HUB_URL is not set"
    return Check("reach", False, f"nothing can reach you: {why} and neither NTFY_TOPIC nor Telegram is set up")


def check_disk() -> Check:
    db = _db_path()
    folder = db.parent if db else Path(".")
    while not folder.exists() and folder != folder.parent:
        folder = folder.parent
    free_gb = shutil.disk_usage(folder).free / 1024**3
    ok = free_gb >= settings.health_min_free_gb
    return Check("disk", ok, f"{free_gb:.1f} GB free on {folder.resolve()}", alert=not ok)


def check_db() -> Check:
    db = _db_path()
    if not db or not db.exists():
        return Check("db", True, "no database file")
    size = sum(p.stat().st_size for p in (db, db.with_name(db.name + "-wal")) if p.exists())
    mb = size / 1024**2
    ok = mb <= settings.health_max_db_mb
    return Check("db", ok, f"{db.name} is {mb:.0f} MB (limit {settings.health_max_db_mb} MB)", alert=not ok)


async def collect(now: Optional[datetime] = None) -> list[Check]:
    """Runs every check. No alerts, no ping: that is run()."""
    now = now or utcnow()
    hub = await check_hub(now)
    checks = [hub] if hub else []
    checks += [check_reach(hub), check_disk(), check_db()]
    return checks


async def _ping(fail: bool) -> None:
    if not settings.healthcheck_url:
        return
    url = settings.healthcheck_url.rstrip("/") + ("/fail" if fail else "")
    try:
        async with httpx.AsyncClient(timeout=10, transport=transport) as c:
            r = await c.get(url)
        last_ping.update(at=utcnow().isoformat(), status=r.status_code, fail=fail)
    except httpx.HTTPError as e:
        log.warning("dead-man ping failed: %s", e)
        last_ping.update(at=utcnow().isoformat(), status=None, fail=fail, error=str(e)[:200])


async def run(now: Optional[datetime] = None) -> list[Check]:
    """The scheduler job: check, tell you about what is wrong (once a day each), ping."""
    now = now or utcnow()
    checks = await collect(now)
    day = now.astimezone(ZoneInfo(settings.tz)).date()
    with Session(engine) as session:
        for c in checks:
            if c.ok:
                _close(session, c.name)
                continue
            log.warning("health: %s failing: %s", c.name, c.detail)
            if c.name == "reach":
                log.error("!!! NOTHING CAN REACH THE OWNER: %s. Set NTFY_TOPIC or TELEGRAM_BOT_TOKEN.", c.detail)
            if not c.alert:
                continue
            key = f"health:{c.name}:{day}"
            if session.exec(select(Alert).where(Alert.key == key)).first():
                continue  # once a day, even if you dismissed it
            await alerts.raise_alert(session, alerts.NOTIFY, "health", _TITLES[c.name], c.detail, key=key, now=now)
    await _ping(fail=any(c.name == "reach" and not c.ok for c in checks))
    return checks


_TITLES = {"hub": "Orchestrator hub is down", "disk": "Disk almost full", "db": "Database is getting big"}


def _close(session: Session, name: str) -> None:
    """A check that passes again closes its open alert."""
    q = select(Alert).where(Alert.key.startswith(f"health:{name}:"), Alert.status.in_(alerts.OPEN))  # type: ignore[union-attr,attr-defined]
    stale = session.exec(q).all()
    for a in stale:
        a.status = "done"
        session.add(a)
    if stale:
        session.commit()


def as_dicts(checks: list[Check]) -> list[dict]:
    return [asdict(c) for c in checks]


def _since() -> Optional[str]:
    return _hub_down_since.isoformat() if _hub_down_since else None


async def detail() -> dict:
    """For GET /health/detail."""
    checks = await collect()
    return {"ok": all(c.ok for c in checks), "checked_at": utcnow().isoformat(),
            "channels": alerts.channels(), "hub_down_since": _since(),
            "deadman": bool(settings.healthcheck_url), "last_ping": last_ping or None,
            "checks": as_dicts(checks)}


def reset() -> None:
    """Tests: forget the hub's outage clock and the last ping."""
    global _hub_down_since
    _hub_down_since = None
    last_ping.clear()

