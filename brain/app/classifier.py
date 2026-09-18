"""Stage-1 classification: is this message a task / event / question / fyi / noise?

Runs on the cheap model for every inbound message. Only task/event/question above the
confidence threshold become Task rows (status pending_confirmation) for the user to accept.
"""
from __future__ import annotations

import json
import logging
from datetime import datetime
from typing import Literal, Optional
from zoneinfo import ZoneInfo

from dateutil import parser as dateparser
from pydantic import BaseModel, Field, ValidationError
from sqlmodel import Session, select

from .config import settings
from .db import Message, Task, TaskKind, TaskStatus, utcnow
from .llm import LLM, classifier_profile

log = logging.getLogger("brain.classifier")


class Classification(BaseModel):
    kind: Literal["task", "event", "question", "fyi", "noise"]
    title: str = ""
    details: str = ""
    due_at: Optional[str] = None  # ISO-8601 with offset, or null
    people: list[str] = Field(default_factory=list)
    priority: Literal["low", "normal", "high", "urgent"] = "normal"
    confidence: float = 0.0
    reason: str = ""


SYSTEM_PROMPT = """You are the triage layer of a personal assistant for the user named {owner}.
You read ONE new WhatsApp message (with a little chat context) and decide what it means for {owner}.

Classify `kind` as exactly one of:
- task: something {owner} must DO or DELIVER (a request to them, or a commitment they made: "I'll send it tomorrow").
- event: something to be at, at a time (meeting, call, class, deadline moved to a time, "let's meet at 5").
- question: someone is waiting on an ANSWER from {owner} that is not trivial to give.
- fyi: information worth knowing but no action (results announced, links, updates).
- noise: greetings, memes, forwards, chit-chat, group banter, acknowledgements ("ok", "👍").

Rules:
- Only create task/event/question when it concerns {owner} specifically. A group message asking "someone" to do something is fyi unless {owner} is named, is clearly responsible, or replied agreeing.
- If from_me is true, the message was SENT by {owner}: it is a task only if {owner} committed to something ("I'll do X"); otherwise noise/fyi.
- `title`: short imperative, <= 80 chars, in {owner}'s perspective ("Send project report to Prof. Kumar").
- `due_at`: resolve relative dates ("tomorrow", "Friday 5pm", "next week") against NOW ({now}, timezone {tz}) to an ISO-8601 datetime WITH offset. If a date but no time is given, use 18:00 local. Null if none.
- `people`: names involved (sender, people mentioned). Use the sender name given.
- `priority`: urgent only for same-day + explicitly urgent wording; high for within ~48h or exams/professors/deadlines; else normal; low for vague "sometime".
- `confidence`: 0-1 that your kind is right. Be honest; ambiguous chit-chat should be low.
- `reason`: one short sentence.

Answer with ONLY a JSON object with keys: kind, title, details, due_at, people, priority, confidence, reason."""


def _fmt_context(ctx: list[Message]) -> str:
    lines = []
    for m in ctx:
        who = "ME" if m.from_me else (m.sender_name or m.sender_id.split("@")[0])
        t = m.sent_at.astimezone(ZoneInfo(settings.tz)).strftime("%a %d %b %H:%M")
        body = m.text or f"[{m.media_type}]"
        lines.append(f"[{t}] {who}: {body}")
    return "\n".join(lines) if lines else "(no earlier messages)"


def build_messages(msg: Message, ctx: list[Message], owner: str) -> list[dict]:
    tz = ZoneInfo(settings.tz)
    now = datetime.now(tz).strftime("%A %Y-%m-%d %H:%M %z")
    who = "ME" if msg.from_me else (msg.sender_name or msg.sender_id.split("@")[0])
    chat = f"group '{msg.chat_name}'" if msg.is_group else f"direct chat with {msg.chat_name or who}"
    user = (
        f"Chat: {chat}\n"
        f"Earlier messages:\n{_fmt_context(ctx)}\n\n"
        f"NEW MESSAGE from {who} (from_me={str(msg.from_me).lower()}) "
        f"at {msg.sent_at.astimezone(tz).strftime('%a %d %b %H:%M')}:\n{msg.text or f'[{msg.media_type}]'}"
    )
    return [
        {"role": "system", "content": SYSTEM_PROMPT.format(owner=owner, now=now, tz=settings.tz)},
        {"role": "user", "content": user},
    ]


def _parse_due(s: Optional[str]) -> Optional[datetime]:
    if not s:
        return None
    try:
        dt = dateparser.isoparse(s)
    except (ValueError, TypeError):
        try:
            dt = dateparser.parse(s)
        except (ValueError, TypeError):
            return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=ZoneInfo(settings.tz))
    return dt


async def classify_message(session: Session, msg: Message, owner: str = "the user") -> Optional[Task]:
    """Classify one message; persist the raw result; create a Task if warranted."""
    ctx = session.exec(
        select(Message)
        .where(Message.chat_id == msg.chat_id, Message.id != msg.id, Message.sent_at <= msg.sent_at)
        .order_by(Message.sent_at.desc())
        .limit(settings.context_window)
    ).all()
    ctx = list(reversed(ctx))

    llm = LLM(classifier_profile())
    try:
        raw = await llm.complete_json(build_messages(msg, ctx, owner))
        result = Classification.model_validate(raw)
    except (ValidationError, ValueError, KeyError) as e:
        log.warning("classifier returned unusable output for msg %s: %s", msg.id, e)
        msg.classified = True
        msg.classification = json.dumps({"error": str(e)})
        session.add(msg)
        session.commit()
        return None

    msg.classified = True
    msg.classification = result.model_dump_json()
    session.add(msg)

    task: Optional[Task] = None
    if result.kind in ("task", "event", "question") and result.confidence >= settings.classifier_min_confidence:
        task = Task(
            kind=TaskKind(result.kind),
            status=TaskStatus.pending_confirmation,
            title=result.title[:200] or (msg.text[:80] if msg.text else "Untitled"),
            details=result.details or None,
            due_at=_parse_due(result.due_at),
            priority=result.priority,
            people=", ".join(result.people) or None,
            confidence=result.confidence,
            source_message_id=msg.id,
        )
        session.add(task)
    session.commit()
    if task:
        session.refresh(task)
        log.info("task #%s (%s, %.2f): %s", task.id, task.kind.value, task.confidence, task.title)
    return task


async def classify_pending(session: Session, owner: str = "the user", limit: int = 50) -> list[Task]:
    """Catch-up pass: classify anything ingested but not yet classified (e.g. after an outage)."""
    pending = session.exec(
        select(Message).where(Message.classified == False).order_by(Message.sent_at).limit(limit)  # noqa: E712
    ).all()
    out = []
    for m in pending:
        t = await classify_message(session, m, owner)
        if t:
            out.append(t)
    return out


def touch(task: Task) -> None:
    task.updated_at = utcnow()
