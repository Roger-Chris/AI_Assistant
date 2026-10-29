"""What happened, turned into alerts (alerts.py decides how loudly), plus the Telegram transport.

Quiet by default: a new task interrupts you only when it is high priority or due within a day;
updates and completions are silent (the app and the morning brief show them). Telegram is the
fallback channel when the Orchestrator hub's phone app is not set up.
"""
from __future__ import annotations

import logging
from datetime import timedelta
from typing import Optional
from zoneinfo import ZoneInfo

import httpx
from sqlmodel import Session

from . import alerts
from .config import settings
from .db import Message, Note, Task, utcnow

log = logging.getLogger("brain.notify")
TG = "https://api.telegram.org/bot{token}/{method}"


def _chat_id(session: Optional[Session]) -> str:
    if settings.telegram_chat_id:
        return settings.telegram_chat_id
    if session:
        n = session.get(Note, "telegram_chat_id")
        if n:
            return n.value
    return ""


async def tg(method: str, session: Optional[Session] = None, **params) -> Optional[dict]:
    if not settings.telegram_bot_token:
        return None
    # long-poll calls pass `timeout` (seconds Telegram holds the request); give the client headroom
    http_timeout = float(params.get("timeout", 0)) + 15
    async with httpx.AsyncClient(timeout=http_timeout) as c:
        r = await c.post(TG.format(token=settings.telegram_bot_token, method=method), json=params)
        if r.status_code != 200:
            log.warning("telegram %s failed: %s %s", method, r.status_code, r.text[:200])
            return None
        return r.json()


async def send(text: str, session: Optional[Session] = None, buttons: Optional[list[list[dict]]] = None,
               priority: str = "normal") -> bool:
    """A Telegram message. True when Telegram took it."""
    chat_id = _chat_id(session)
    if not chat_id:
        log.info("no telegram chat id yet; would send: %s", text[:120])
        return False
    params: dict = {"chat_id": chat_id, "text": text, "parse_mode": "HTML",
                    "disable_notification": priority == "low"}
    if buttons:
        params["reply_markup"] = {"inline_keyboard": buttons}
    return await tg("sendMessage", session, **params) is not None


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
