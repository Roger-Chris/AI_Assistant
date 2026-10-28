"""Scheduled jobs: the daily brief (planner model) and a catch-up classification sweep."""
from __future__ import annotations

import logging
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from sqlmodel import Session, select

from . import alerts, gcal, gmail, graph
from .classifier import classify_pending, touch
from .config import settings
from .db import Message, Task, TaskStatus, engine
from .health import run as health_check
from .llm import LLM, planner_profile
from .notify import fmt_due, unanswered_question

log = logging.getLogger("brain.scheduler")
scheduler = AsyncIOScheduler(timezone=settings.tz)

BRIEF_PROMPT = """You are {owner}'s personal assistant writing the morning brief.
Today is {today} ({tz}). Below are their open tasks/events (unconfirmed ones were auto-extracted from chats and not yet accepted).

CALENDAR (next 36 hours, all calendars; "[assistant]" = mirrored from the tasks below):
{calendar}

Write a brief in plain text (no markdown headers), under 150 words:
1. What matters today, in priority order, with times.
2. Anything overdue or due in the next 2 days.
3. Unconfirmed items they should accept or drop, in one line.
Be specific and direct; no filler, no motivational lines.

TASKS:
{tasks}"""


def _dump(tasks: list[Task]) -> str:
    return "\n".join(
        f"- [{t.status.value}] ({t.kind.value}, {t.priority}) {t.title} — due {fmt_due(t)}"
        + (f" — with {t.people}" if t.people else "") for t in tasks
    ) or "(none)"


async def daily_brief() -> None:
    with Session(engine) as session:
        tasks = session.exec(
            select(Task).where(Task.status.in_([TaskStatus.open, TaskStatus.pending_confirmation]))  # type: ignore[attr-defined]
            .order_by(Task.due_at.is_(None), Task.due_at)  # type: ignore[attr-defined]
        ).all()
        tz = ZoneInfo(settings.tz)
        try:
            cal = gcal.upcoming(session)
        except Exception as e:  # noqa: BLE001
            log.warning("calendar unavailable for brief: %s", e)
            cal = []
        cal_text = "\n".join(
            f"- {e['start']} {e['title']} ({e['calendar']}){' [assistant]' if e['assistant'] else ''}" for e in cal
        ) or "(nothing, or Google not connected)"
        prompt = BRIEF_PROMPT.format(owner=settings.owner_name, today=datetime.now(tz).strftime("%A %d %B %Y"),
                                     tz=settings.tz, tasks=_dump(tasks), calendar=cal_text)
        try:
            text = await LLM(planner_profile()).complete([{"role": "user", "content": prompt}], max_tokens=400)
        except Exception as e:  # noqa: BLE001 — fall back to a plain list rather than silence
            log.warning("planner unavailable for brief (%s); sending raw list", e)
            text = "Morning brief (planner offline):\n" + _dump(tasks)
        await alerts.raise_alert(session, alerts.BRIEF, "brief", "☀️ Morning brief", text,
                                 key=f"brief:{datetime.now(tz).date()}", supersede=True)


async def catch_up() -> None:
    with Session(engine) as session:
        made = await classify_pending(session, settings.owner_name)
        if made:
            log.info("catch-up classified %d new tasks", len(made))


async def overdue_nudge() -> None:
    """Once a day at 18:00: anything open and overdue gets one nudge."""
    with Session(engine) as session:
        now = datetime.now(ZoneInfo("UTC"))
        tasks = [t for t in session.exec(select(Task).where(Task.status == TaskStatus.open)).all()
                 if t.due_at and (t.due_at if t.due_at.tzinfo else t.due_at.replace(tzinfo=ZoneInfo("UTC"))) < now - timedelta(hours=1)]
        if tasks:
            await alerts.raise_alert(session, alerts.NOTIFY, "overdue", f"⏰ {len(tasks)} overdue",
                                     "\n".join(f"#{t.id} {t.title} · {fmt_due(t)}" for t in tasks), supersede=True)


async def deliver_alerts() -> None:
    """Alerts held through quiet hours, snoozed, or not delivered the first time."""
    with Session(engine) as session:
        await alerts.deliver_due(session)


def start() -> None:
    scheduler.add_job(daily_brief, "cron", hour=settings.daily_brief_hour, minute=0, id="brief")
    scheduler.add_job(overdue_nudge, "cron", hour=18, minute=0, id="overdue")
    scheduler.add_job(catch_up, "interval", minutes=10, id="catch_up")
    scheduler.add_job(unanswered_sweep, "interval", minutes=5, id="unanswered")
    scheduler.add_job(deliver_alerts, "interval", minutes=1, id="alerts")
    scheduler.add_job(health_check, "interval", minutes=5, id="health")
    scheduler.add_job(calendar_sync, "interval", minutes=2, id="calendar")
    scheduler.add_job(gmail.poll_all, "interval", minutes=settings.gmail_poll_minutes, id="gmail")
    if settings.graph_enabled:
        scheduler.add_job(graph.nightly, "cron", hour=settings.graph_hour, minute=15, id="graph")
    scheduler.start()


async def unanswered_sweep() -> None:
    """Questions watched for longer than the grace period with no reply from the owner -> surface."""
    grace = timedelta(minutes=settings.question_grace_minutes)
    with Session(engine) as session:
        now = datetime.now(ZoneInfo("UTC"))
        for t in session.exec(select(Task).where(Task.status == TaskStatus.watching)).all():
            created = t.created_at if t.created_at.tzinfo else t.created_at.replace(tzinfo=ZoneInfo("UTC"))
            if now - created < grace:
                continue
            src = session.get(Message, t.source_message_id) if t.source_message_id else None
            replied = False
            if src:
                replied = session.exec(
                    select(Message).where(Message.chat_id == src.chat_id, Message.from_me == True,  # noqa: E712
                                          Message.sent_at > src.sent_at).limit(1)
                ).first() is not None
            if replied:
                t.status = TaskStatus.done
            else:
                t.status = TaskStatus.open
                t.priority = "high"
                await unanswered_question(session, t, src, int((now - created).total_seconds() // 60))
            touch(t)
            session.add(t)
        session.commit()


async def calendar_sync() -> None:
    with Session(engine) as session:
        try:
            gcal.sync_dirty(session)
        except Exception:  # noqa: BLE001
            log.exception("calendar sync failed")
