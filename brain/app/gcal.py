"""Google Calendar mirror.

Confirmed events (and, optionally, tasks with a due time) are written to a dedicated calendar named
`CALENDAR_NAME` in the owner's Google account, so they show on the phone with normal reminders.
Nothing is written for pending_confirmation / watching / rejected tasks; a task that becomes
rejected loses its event; a done task keeps its event (it happened).

All Google calls go through `service()`, which tests replace with a fake.
"""
from __future__ import annotations

import logging
from datetime import datetime, timedelta
from typing import Any, Optional
from zoneinfo import ZoneInfo

from sqlmodel import Session, select

from .config import settings
from .db import Note, Task, TaskKind, TaskStatus, utcnow

log = logging.getLogger("brain.gcal")

_service_override: Any = None  # tests


def service():
    """Authenticated Calendar API client, or None if Google isn't authorised yet."""
    if _service_override is not None:
        return _service_override
    from googleapiclient.discovery import build

    from .google_auth import load_credentials

    creds = load_credentials()
    if not creds:
        return None
    return build("calendar", "v3", credentials=creds, cache_discovery=False)


def calendar_id(session: Session, svc) -> str:
    """Find or create the Assistant calendar; cache its id in the Note table."""
    n = session.get(Note, "calendar_id")
    if n:
        return n.value
    for cal in svc.calendarList().list().execute().get("items", []):
        if cal.get("summary") == settings.calendar_name:
            session.merge(Note(key="calendar_id", value=cal["id"]))
            session.commit()
            return cal["id"]
    created = svc.calendars().insert(body={"summary": settings.calendar_name, "timeZone": settings.tz}).execute()
    session.merge(Note(key="calendar_id", value=created["id"]))
    session.commit()
    log.info("created calendar %r (%s)", settings.calendar_name, created["id"])
    return created["id"]


def _aware(dt: datetime) -> datetime:
    return dt if dt.tzinfo else dt.replace(tzinfo=ZoneInfo("UTC"))


def event_body(task: Task) -> Optional[dict]:
    """The Google event for a task, or None if it shouldn't be on the calendar."""
    if not task.due_at:
        return None
    if task.kind == TaskKind.question:
        return None
    if task.kind == TaskKind.task and not settings.calendar_sync_tasks_with_due:
        return None
    start = _aware(task.due_at).astimezone(ZoneInfo(settings.tz))
    length = timedelta(hours=1) if task.kind == TaskKind.event else timedelta(minutes=30)
    title = task.title if task.kind == TaskKind.event else f"Due: {task.title}"
    desc = "\n".join(x for x in [task.details, f"With: {task.people}" if task.people else "",
                                 f"assistant task #{task.id}"] if x)
    return {
        "summary": title,
        "description": desc,
        "start": {"dateTime": start.isoformat(), "timeZone": settings.tz},
        "end": {"dateTime": (start + length).isoformat(), "timeZone": settings.tz},
        "reminders": {"useDefault": False,
                      "overrides": [{"method": "popup", "minutes": 30 if task.kind == TaskKind.event else 60}]},
        "extendedProperties": {"private": {"assistant_task_id": str(task.id)}},
    }


def sync_task(session: Session, svc, cal: str, task: Task) -> str:
    """Bring one task's calendar state in line. Returns what happened."""
    body = event_body(task) if task.status in (TaskStatus.open, TaskStatus.done) else None
    if body and task.status == TaskStatus.done and not task.calendar_event_id:
        body = None  # don't create events for things already finished
    if body:
        if task.calendar_event_id:
            svc.events().update(calendarId=cal, eventId=task.calendar_event_id, body=body).execute()
            what = "updated"
        else:
            ev = svc.events().insert(calendarId=cal, body=body).execute()
            task.calendar_event_id = ev["id"]
            what = "created"
    elif task.calendar_event_id:
        try:
            svc.events().delete(calendarId=cal, eventId=task.calendar_event_id).execute()
        except Exception as e:  # noqa: BLE001 — already gone is fine
            log.info("delete event %s: %s", task.calendar_event_id, e)
        task.calendar_event_id = None
        what = "deleted"
    else:
        what = "skipped"
    task.calendar_synced_at = utcnow()
    session.add(task)
    return what


def sync_dirty(session: Session) -> dict:
    """Every task whose updated_at is newer than its last sync."""
    svc = service()
    if not svc:
        return {"skipped": "google not authorised"}
    cal = calendar_id(session, svc)
    counts: dict[str, int] = {}
    for t in session.exec(select(Task)).all():
        if t.calendar_synced_at and _aware(t.calendar_synced_at) >= _aware(t.updated_at):
            continue
        try:
            what = sync_task(session, svc, cal, t)
        except Exception:  # noqa: BLE001
            log.exception("calendar sync failed for task %s", t.id)
            continue
        counts[what] = counts.get(what, 0) + 1
    session.commit()
    if any(k != "skipped" for k in counts):
        log.info("calendar sync: %s", counts)
    return counts


def upcoming(session: Session, hours: int = 36) -> list[dict]:
    """Events from ALL the owner's calendars in the next `hours` — for the brief."""
    svc = service()
    if not svc:
        return []
    now = datetime.now(ZoneInfo("UTC"))
    out = []
    for cal in svc.calendarList().list().execute().get("items", []):
        res = svc.events().list(calendarId=cal["id"], timeMin=now.isoformat(),
                                timeMax=(now + timedelta(hours=hours)).isoformat(),
                                singleEvents=True, orderBy="startTime", maxResults=25).execute()
        for ev in res.get("items", []):
            start = ev.get("start", {})
            out.append({"calendar": cal.get("summary"), "title": ev.get("summary", "(no title)"),
                        "start": start.get("dateTime") or start.get("date"),
                        "assistant": "assistant_task_id" in ev.get("extendedProperties", {}).get("private", {})})
    out.sort(key=lambda e: e["start"] or "")
    return out
