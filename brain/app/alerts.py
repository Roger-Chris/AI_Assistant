"""Alerts: when, and how loudly, the assistant interrupts you. Quiet by default.

Every notification is an Alert with a level:
  0 silent   handled and logged, nothing is sent (it shows in the app and the morning brief)
  1 brief    the morning brief
  2 notify   a phone notification: something needs you today
  3 alarm    a ringing full-screen alert in the app (Phase 3; a notification until then)
  4 call     the assistant calls you (Phase 3)
Inside quiet hours only calls go out; the rest waits for the window to end.

Delivery is the Orchestrator hub's POST /api/notify (the phone app); when the hub is not set up or
does not answer, ntfy (NTFY_TOPIC). You answer an alert with done, snooze or dismiss ("not important"),
in the app (POST /alerts/{id}/ack through the hub).
"""
from __future__ import annotations

import logging
from datetime import datetime, time, timedelta
from typing import Optional
from zoneinfo import ZoneInfo

import httpx
from sqlalchemy import or_
from sqlmodel import Session, select

from .classifier import touch
from .config import settings
from .db import Alert, Task, TaskStatus, utcnow

log = logging.getLogger("brain.alerts")

SILENT, BRIEF, NOTIFY, ALARM, CALL = 0, 1, 2, 3, 4
OPEN = ("pending", "sent")
ACTIONS = ("done", "snooze", "dismiss")

# tests swap in an httpx.MockTransport for the hub and for ntfy
hub_transport: Optional[httpx.AsyncBaseTransport] = None
ntfy_transport: Optional[httpx.AsyncBaseTransport] = None

# alert level -> ntfy priority (1 min .. 3 default .. 4 high .. 5 max/urgent)
NTFY_PRIORITY = {BRIEF: 3, NOTIFY: 4, ALARM: 5, CALL: 5}


def _aware(dt: datetime) -> datetime:
    return dt if dt.tzinfo else dt.replace(tzinfo=ZoneInfo("UTC"))


def quiet_until(now: datetime) -> Optional[datetime]:
    """When the quiet window `now` falls in ends (aware UTC), or None outside quiet hours."""
    start_s, _, end_s = settings.quiet_hours.partition("-")
    if not start_s.strip() or not end_s.strip():
        return None
    start, end = time.fromisoformat(start_s.strip()), time.fromisoformat(end_s.strip())
    if start == end:
        return None
    tz = ZoneInfo(settings.tz)
    local = _aware(now).astimezone(tz)
    t = local.time()
    inside = start <= t < end if start < end else (t >= start or t < end)
    if not inside:
        return None
    end_day = local.date() if t < end else local.date() + timedelta(days=1)
    return datetime.combine(end_day, end, tzinfo=tz).astimezone(ZoneInfo("UTC"))


def as_dict(a: Alert) -> dict:
    """What the hub and the app see."""
    return {"id": a.id, "level": a.level, "kind": a.kind, "title": a.title, "body": a.body,
            "task_id": a.task_id, "status": a.status, "created_at": _aware(a.created_at).isoformat()}


async def raise_alert(session: Session, level: int, kind: str, title: str, body: str = "",
                      task_id: Optional[int] = None, key: Optional[str] = None,
                      supersede: bool = False, now: Optional[datetime] = None) -> Optional[Alert]:
    """Records an alert and sends it now, or when quiet hours end. A key that already has an open
    alert is not raised twice; `supersede` closes earlier open alerts of the same kind (yesterday's
    brief)."""
    if level <= SILENT:
        log.info("silent %s: %s", kind, title)
        return None
    if key:
        old = session.exec(select(Alert).where(Alert.key == key, Alert.status.in_(OPEN))).first()  # type: ignore[attr-defined]
        if old:
            return old
    now = now or utcnow()
    if supersede:
        for old in session.exec(select(Alert).where(Alert.kind == kind, Alert.status.in_(OPEN))).all():  # type: ignore[attr-defined]
            old.status = "done"
            session.add(old)
    hold = quiet_until(now) if level < CALL else None
    a = Alert(level=level, kind=kind, title=title[:200], body=body[:2000], task_id=task_id, key=key,
              deliver_after=hold or now, created_at=now)
    session.add(a)
    session.commit()
    session.refresh(a)
    if hold:
        log.info("alert #%s held for quiet hours until %s", a.id, hold)
    else:
        await deliver(session, a)
    return a


def channels() -> list[str]:
    """The channels that are set up, in the order they are tried."""
    return [name for name, on in (("hub", settings.hub_url), ("ntfy", settings.ntfy_topic)) if on]


async def deliver(session: Session, a: Alert) -> bool:
    """Phone app through the hub, else ntfy. False, and still pending, when a channel
    is set up but none took it; the next deliver_due pass tries again."""
    senders = {"hub": lambda: _to_hub(a), "ntfy": lambda: _to_ntfy(a)}
    setup = channels()
    channel = "none" if not setup else None  # nowhere to send: it still shows in the app list and the brief
    for name in setup:
        if await senders[name]():
            channel = name
            break
    if channel is None:
        log.warning("alert #%s not delivered; will retry", a.id)
        return False
    a.status, a.channel, a.sent_at, a.ack = "sent", channel, utcnow(), None
    session.add(a)
    session.commit()
    return True


async def _to_hub(a: Alert) -> bool:
    headers = {"Authorization": f"Bearer {settings.orc_token}"} if settings.orc_token else {}
    try:
        async with httpx.AsyncClient(timeout=10, transport=hub_transport) as c:
            r = await c.post(f"{settings.hub_url.rstrip('/')}/api/notify", headers=headers, json=as_dict(a))
    except httpx.HTTPError as e:
        log.warning("hub unreachable for alert #%s: %s", a.id, e)
        return False
    if r.status_code >= 300:
        log.warning("hub refused alert #%s: %s %s", a.id, r.status_code, r.text[:200])
        return False
    return True


async def _to_ntfy(a: Alert) -> bool:
    """A push through ntfy. Answers (done/snooze/dismiss) stay in the app: the brain is
    loopback-only, so ntfy's action buttons could not reach it. Tapping opens NTFY_CLICK_URL."""
    msg: dict = {"topic": settings.ntfy_topic, "title": a.title, "message": a.body or a.title,
                 "priority": NTFY_PRIORITY.get(a.level, 3), "tags": [f"alert-{a.id}", a.kind]}
    if settings.ntfy_click_url:
        msg["click"] = settings.ntfy_click_url
        msg["actions"] = [{"action": "view", "label": "Open app", "url": settings.ntfy_click_url}]
    headers = {"Authorization": f"Bearer {settings.ntfy_token}"} if settings.ntfy_token else {}
    try:
        async with httpx.AsyncClient(timeout=10, transport=ntfy_transport) as c:
            # JSON publishing goes to the server root, the topic is in the body
            r = await c.post(settings.ntfy_url.rstrip("/") + "/", headers=headers, json=msg)
    except httpx.HTTPError as e:
        log.warning("ntfy unreachable for alert #%s: %s", a.id, e)
        return False
    if r.status_code >= 300:
        log.warning("ntfy refused alert #%s: %s %s", a.id, r.status_code, r.text[:200])
        return False
    return True


async def deliver_due(session: Session, now: Optional[datetime] = None) -> int:
    """Sends alerts whose time has come: held through quiet hours, snoozed, or failed earlier."""
    now = now or utcnow()
    quiet = quiet_until(now)
    sent = 0
    due = session.exec(select(Alert).where(Alert.status == "pending", Alert.deliver_after <= now)
                       .order_by(Alert.id)).all()  # type: ignore[arg-type]
    for a in due:
        if quiet and a.level < CALL:
            continue
        sent += await deliver(session, a)
    return sent


def ack(session: Session, a: Alert, action: str, now: Optional[datetime] = None) -> Alert:
    """Your answer. done also finishes the task behind the alert; snooze sends it again later;
    dismiss ("not important") closes it and is kept so later phases can learn from it."""
    if action not in ACTIONS:
        raise ValueError(f"action must be one of {', '.join(ACTIONS)}")
    now = now or utcnow()
    a.ack, a.acked_at = action, now
    if action == "snooze":
        a.status, a.deliver_after = "pending", now + timedelta(minutes=settings.snooze_minutes)
    else:
        a.status = "done" if action == "done" else "dismissed"
    if action == "done" and a.task_id:
        t = session.get(Task, a.task_id)
        if t and t.status not in (TaskStatus.done, TaskStatus.rejected):
            t.status = TaskStatus.done
            touch(t)
            session.add(t)
    session.add(a)
    session.commit()
    session.refresh(a)
    return a


def close_for_task(session: Session, task_id: int) -> None:
    """The task was finished or dropped some other way: its alerts no longer need you."""
    for a in session.exec(select(Alert).where(Alert.task_id == task_id, Alert.status.in_(OPEN))).all():  # type: ignore[attr-defined]
        a.status = "done"
        session.add(a)
    session.commit()


def open_alerts(session: Session) -> list[Alert]:
    """What needs you now: sent, or held only by quiet hours. Snoozed ones stay hidden until due."""
    q = select(Alert).where(or_(Alert.status == "sent", (Alert.status == "pending") & (Alert.ack.is_(None))))  # type: ignore[union-attr]
    return list(session.exec(q.order_by(Alert.level.desc(), Alert.id.desc())).all())  # type: ignore[attr-defined]
