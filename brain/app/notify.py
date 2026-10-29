"""What happened, turned into alerts (alerts.py decides how loudly and delivers them to the app).

Quiet by default: a new task interrupts you only when it is high priority or due within a day;
updates and completions are silent (the app and the morning brief show them).
"""
from __future__ import annotations

import logging
from datetime import timedelta
from typing import Optional
from zoneinfo import ZoneInfo

from sqlmodel import Session

from . import alerts
from .config import settings
from .db import Message, Task, utcnow

log = logging.getLogger("brain.notify")


def fmt_due(task: Task) -> str:
    if not task.due_at:
        return "no date"
    dt = task.due_at if task.due_at.tzinfo else task.due_at.replace(tzinfo=ZoneInfo("UTC"))
    return dt.astimezone(ZoneInfo(settings.tz)).strftime("%a %d %b, %H:%M")


def _where(src: Message) -> str:
    chat = src.chat_name or src.chat_id.split("@")[0]
    if src.is_self:
        return "your notes"
    if src.from_me:
        return f"you → {chat}"
    if src.is_group:
        return f"{src.sender_name or src.sender_id.split('@')[0]} in {chat}"
    return chat


def new_task_level(task: Task) -> int:
    """High priority, or due within a day (or already due): a notification. Otherwise silent."""
    if task.priority in ("high", "urgent"):
        return alerts.NOTIFY
    if task.due_at:
        due = task.due_at if task.due_at.tzinfo else task.due_at.replace(tzinfo=ZoneInfo("UTC"))
        if due - utcnow() < timedelta(hours=24):
            return alerts.NOTIFY
    return alerts.SILENT


async def new_task(session: Session, task: Task, src: Message) -> None:
    icon = {"task": "📝", "event": "📅", "question": "❓"}.get(task.kind.value, "•")
    if src.source == "email":
        icon = "📧 " + icon
    quote = (src.text or "")[:200]
    body = f"Due {fmt_due(task)} · {task.kind.value} · from {_where(src)}: “{quote}”"
    from . import watcher  # watcher imports this module; it applies what "Not important" taught it

    await watcher.interrupt(session, new_task_level(task), "new_task", f"{icon} {task.title}", body,
                            task_id=task.id, key=f"task:{task.id}", judge=False)
    task.notified = True
    session.add(task)
    session.commit()


async def task_updated(session: Session, task: Task, changed: dict, src: Message) -> None:
    log.info("task #%s updated (%s) from message %s", task.id, ", ".join(changed), src.id)


async def task_completed(session: Session, task: Task, src: Message) -> None:
    log.info("task #%s done, evidence message %s", task.id, src.id)
    alerts.close_for_task(session, task.id)


async def unanswered_question(session: Session, task: Task, src: Optional[Message], minutes: int) -> None:
    """A notification; an alarm when the sender is close to you (family, professor, manager, VIP
    list: watcher.close_relation). Three "Not important" answers for this chat make it quieter."""
    from . import watcher  # watcher imports this module

    who = (src.sender_name or src.chat_name or "someone") if src else "someone"
    quote = ((src.text or "") if src else task.title)[:160]
    why = watcher.close_relation(session, src)
    level = alerts.ALARM if why else alerts.NOTIFY
    body = f"“{quote}” · {minutes} min, no reply from you." + (f" ({who}: {why})" if why else "")
    await watcher.interrupt(session, level, "question", f"❓ {who} is waiting", body, task_id=task.id,
                            key=f"question:{task.id}", judge=False)
    task.notified = True
    session.add(task)
