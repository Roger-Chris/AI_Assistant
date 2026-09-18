"""Outbound notifications to the user's phone.

Today: Telegram (interim). Later: FCM push to the Android app, and Twilio calls for the urgent tier.
Everything goes through `send()` so swapping the channel is one file.
"""
from __future__ import annotations

import logging
from typing import Optional
from zoneinfo import ZoneInfo

import httpx
from sqlmodel import Session

from .config import settings
from .db import Message, Note, Task

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
    async with httpx.AsyncClient(timeout=20) as c:
        r = await c.post(TG.format(token=settings.telegram_bot_token, method=method), json=params)
        if r.status_code != 200:
            log.warning("telegram %s failed: %s %s", method, r.status_code, r.text[:200])
            return None
        return r.json()


async def send(text: str, session: Optional[Session] = None, buttons: Optional[list[list[dict]]] = None,
               priority: str = "normal") -> None:
    """priority: normal | high | urgent. Urgent will also trigger a call once Twilio is wired."""
    chat_id = _chat_id(session)
    if not chat_id:
        log.info("no telegram chat id yet; would send: %s", text[:120])
        return
    params: dict = {"chat_id": chat_id, "text": text, "parse_mode": "HTML",
                    "disable_notification": priority == "low"}
    if buttons:
        params["reply_markup"] = {"inline_keyboard": buttons}
    await tg("sendMessage", session, **params)


def fmt_due(task: Task) -> str:
    if not task.due_at:
        return "no date"
    dt = task.due_at if task.due_at.tzinfo else task.due_at.replace(tzinfo=ZoneInfo("UTC"))
    return dt.astimezone(ZoneInfo(settings.tz)).strftime("%a %d %b, %H:%M")


async def new_task(session: Session, task: Task, src: Message) -> None:
    icon = {"task": "📝", "event": "📅", "question": "❓"}.get(task.kind.value, "•")
    flag = " 🔴" if task.priority == "urgent" else (" 🟠" if task.priority == "high" else "")
    where = src.chat_name or src.sender_name or src.chat_id.split("@")[0]
    quote = (src.text or "").replace("<", "&lt;")[:200]
    text = (
        f"{icon} <b>{task.title}</b>{flag}\n"
        f"Due: {fmt_due(task)}  ·  {task.kind.value}  ·  {int(task.confidence * 100)}%\n"
        f"From <i>{where}</i>: “{quote}”"
    )
    buttons = [[{"text": "✅ Keep", "callback_data": f"confirm:{task.id}"},
                {"text": "❌ Not a task", "callback_data": f"reject:{task.id}"}]]
    await send(text, session, buttons, priority=task.priority)
    task.notified = True
    session.add(task)
    session.commit()
