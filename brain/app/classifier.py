"""Stage-1 triage, task-aware.

For every message that survives the pre-filter, the cheap model sees: recent messages in the
chat, the OPEN tasks that came from this chat, and the new message. It answers with an ACTION:

  new       — a fresh task / event / question (creates a pending_confirmation Task)
  update    — this message adds a due date / detail / priority to an existing task
  complete  — this message is evidence an existing task is done ("sent", a document from me)
  none      — nothing to do (chit-chat, fyi, noise)

That is what stops "can you send X" + "I'll send in 10 min" becoming two cards, and what lets
the file you actually send close the task.
"""
from __future__ import annotations

import json
import logging
from datetime import datetime, timedelta, timezone
from typing import Literal, Optional
from zoneinfo import ZoneInfo

from dateutil import parser as dateparser
from pydantic import BaseModel, Field, ValidationError, field_validator
from sqlmodel import Session, select

from . import graph
from .config import settings
from .db import Message, Task, TaskKind, TaskStatus, utcnow
from .llm import LLM, classifier_profile

log = logging.getLogger("brain.classifier")


class Classification(BaseModel):
    action: Literal["new", "update", "complete", "none"] = "none"
    task_id: Optional[int] = None  # for update / complete
    kind: Literal["task", "event", "question", "fyi", "noise"] = "noise"
    title: str = ""
    details: str = ""
    due_at: Optional[str] = None  # ISO-8601 with offset, or null
    people: list[str] = Field(default_factory=list)
    priority: Literal["low", "normal", "high", "urgent"] = "normal"
    confidence: float = 0.0
    reason: str = ""

    @field_validator("title", "details", "reason", mode="before")
    @classmethod
    def _none_to_blank(cls, v):
        return "" if v is None else v

    @field_validator("people", mode="before")
    @classmethod
    def _none_to_list(cls, v):
        if v is None:
            return []
        return [v] if isinstance(v, str) else v

    @field_validator("priority", mode="before")
    @classmethod
    def _default_priority(cls, v):
        return v if v in ("low", "normal", "high", "urgent") else "normal"


SYSTEM_PROMPT = """You are the triage layer of a personal assistant for {owner}.
You read ONE new message — a WhatsApp message or an EMAIL — (with recent context and the OPEN TASKS already tracked for this chat/sender) and decide what it means for {owner}.

First pick `action`:
- update: the message adds information to one of the OPEN TASKS listed (a due time, a detail, a change of plan, higher urgency). Set task_id. Fill only the fields that changed (due_at/details/priority/title), leave others empty.
- complete: the message is evidence that one of the OPEN TASKS is done. Set task_id. Examples: {owner} says "sent"/"done"/"submitted"; {owner} sends the file the task was about; a CONFIRMATION arrives for something he was asked to do — "thank you for registering", "registration complete", "your application has been received", "payment successful", "your status is confirmed", "we have received your submission" — even when it comes from a different sender or address than the mail that asked for it. Match it to the task by what it is about, not by who sent it.
- new: a NEW commitment / event / question for {owner} not already in OPEN TASKS.
- none: nothing actionable (chit-chat, fyi, someone else's task, acknowledgements).

If action is new (or update), also fill:
- kind: task (something {owner} must DO/DELIVER — a request to them or a commitment they made), event (be somewhere at a time), question (someone is waiting for a REPLY from {owner} — any direct question to {owner} in a direct chat counts, even casual ones like "bus boarded?" or "where are you?"; these are tracked quietly and only surfaced if {owner} doesn't reply), fyi, noise.
- title: short imperative, <= 80 chars, in {owner}'s perspective ("Send project report to Prof. Kumar").
- due_at: resolve relative times ("in 10 mins", "tomorrow", "Friday 5pm") against NOW ({now}, timezone {tz}) to ISO-8601 WITH offset. Date without time -> 18:00 local. Null if none.
- people: names involved (use the sender names given).
- priority: urgent only for same-day + explicitly urgent wording; high for within ~48h or exams/professors/deadlines; else normal; low for vague "sometime".

Rules:
- WHAT YOU KNOW (when present) is the assistant's memory of {owner} and this sender: trust it over your guesses about who people are and which organisations he actually belongs to.
- Only task/event/question that concern {owner} specifically. A group message asking "someone" is fyi unless {owner} is named, is clearly responsible, or replied agreeing.
- If from_me is true, {owner} SENT it: it's `new` only if {owner} committed to something; it's `update`/`complete` if it relates to an open task; otherwise none.
- EXCEPTION — the SELF CHAT ("Chat: notes to self"): everything there is {owner} talking to you, the assistant. An instruction ("send me the SH files today", "remind me to pay fees") is a `task` for {owner}. A forwarded confirmation, screenshot, ticket or invite is an `event` (or task) with its date/time. Never dismiss self-chat messages as "sent to himself".
- EMAIL rules (when the chat line says "email"). The bar is a RELATIONSHIP {owner} ALREADY HAS, not an offer. Ask: did {owner} sign up, apply, buy, owe, or get personally named? If not, it is `none` no matter how urgent it sounds.
  Surface as `task`: bills, fees, EMIs, payment reminders, renewals for accounts {owner} holds (title "Pay <what> <amount>", due = the due date, priority high); a form / verification / submission {owner} started or his college requires; something a real person wrote to {owner} personally and is waiting on.
  Surface as `event`: only an event {owner} is already committed to — he registered or applied, he is a named participant, or it is from his college / a programme he belongs to (due = the event's start).
  `none` — even with a date, a deadline, "Action Required" or "Last chance": marketing webinars, masterclasses, info sessions, demos, open courses, career fairs and counselling invitations from consultancies, ed-tech, universities he has not applied to, or vendors; product announcements, deprecations, migration notices and feature news from services (including ones he uses) unless the mail names an account of his that will break on a date; job alerts, recommended-opportunity digests, social notifications, newsletters, contest promotions; receipts for things already paid, OTPs, login alerts, shipping updates.
  A mail marked MASS MAIL was sent to a list, not to {owner} — for that, only an existing account, booking, registration, application or bill of his can become a task or event. "Join our event", "register now", "you are invited" in mass mail is `none`.
  Include the amount and any reference number in `details`.
- Do NOT create a `new` task that duplicates an OPEN TASK — use update instead.
- confidence: 0-1 that your action+kind is right. Be honest.
- reason: one short sentence.

Answer with ONLY a JSON object with keys: action, task_id, kind, title, details, due_at, people, priority, confidence, reason."""


def _fmt_context(ctx: list[Message]) -> str:
    lines = []
    for m in ctx:
        who = "ME" if m.from_me else (m.sender_name or m.sender_id.split("@")[0])
        t = m.sent_at.astimezone(ZoneInfo(settings.tz)).strftime("%a %d %b %H:%M")
        body = m.text or f"[{m.media_type}]"
        lines.append(f"[{t}] {who}: {body[:400]}")
    return "\n".join(lines) if lines else "(no earlier messages)"


def _fmt_tasks(tasks: list[Task]) -> str:
    if not tasks:
        return "(none)"
    out = []
    for t in tasks:
        due = t.due_at.astimezone(ZoneInfo(settings.tz)).strftime("%a %d %b %H:%M") if t.due_at and t.due_at.tzinfo \
            else (t.due_at.strftime("%a %d %b %H:%M UTC") if t.due_at else "no due")
        where = f" (from {t.chat_id.split(':')[-1]})" if t.chat_id and ":" in t.chat_id else ""
        out.append(f"#{t.id} [{t.kind.value}, {t.status.value}] {t.title} — due {due}{where}")
    return "\n".join(out)


_LIVE = [TaskStatus.open, TaskStatus.pending_confirmation, TaskStatus.watching]


def open_tasks_for_chat(session: Session, chat_id: str, limit: int = 8) -> list[Task]:
    return list(session.exec(
        select(Task).where(Task.chat_id == chat_id, Task.status.in_(_LIVE))  # type: ignore[attr-defined]
        .order_by(Task.created_at.desc()).limit(limit)
    ).all())


def candidate_tasks(session: Session, msg: Message) -> list[Task]:
    """What the model may update or complete: this chat first, then other recent open items from
    the same source. A "thanks for registering" mail usually comes from a different address than
    the one that asked you to register, so chat scope alone would never close it."""
    tasks = open_tasks_for_chat(session, msg.chat_id)
    seen = {t.id for t in tasks}
    room = max(0, settings.cross_chat_task_candidates - len(tasks))
    if room:
        cutoff = utcnow() - timedelta(days=settings.cross_chat_task_days)
        others = session.exec(
            select(Task).join(Message, Task.source_message_id == Message.id)  # type: ignore[arg-type]
            .where(Task.status.in_(_LIVE), Task.created_at >= cutoff, Message.source == msg.source)  # type: ignore[attr-defined]
            .order_by(Task.created_at.desc()).limit(room + len(seen))
        ).all()
        tasks += [t for t in others if t.id not in seen][:room]
    return tasks


def build_messages(msg: Message, ctx: list[Message], tasks: list[Task], owner: str,
                   who: str = "") -> list[dict]:
    tz = ZoneInfo(settings.tz)
    now = datetime.now(tz).strftime("%A %Y-%m-%d %H:%M %z")
    who = "ME" if msg.from_me else (msg.sender_name or msg.sender_id.split("@")[0])
    if msg.source == "email":
        acct = msg.chat_id.split(":")[1] if msg.chat_id.count(":") >= 2 else "primary"
        bulk = " [MASS MAIL — sent to a list, not written to him]" if msg.is_bulk else ""
        chat = f"email to {owner}'s '{acct}' Gmail account, from {msg.sender_name} <{msg.sender_id}>{bulk}"
    elif msg.is_self:
        chat = "notes to self (the owner's own 'message yourself' chat — instructions to the assistant)"
    else:
        chat = f"group '{msg.chat_name}'" if msg.is_group else f"direct chat with {msg.chat_name or who}"
    user = (
        (f"WHAT YOU KNOW:\n{who}\n\n" if who else "")
        + f"Chat: {chat}\n"
        f"OPEN TASKS you may update or complete:\n{_fmt_tasks(tasks)}\n\n"
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
    return to_utc(dt)


def to_utc(dt: datetime) -> datetime:
    """A due time as aware UTC, how every timestamp is stored. A time without an offset means local
    time in TZ. Older SQLAlchemy dropped offsets and kept the local wall time, which notify/scheduler
    then read as UTC (5.5 h off); newer SQLModel refuses times without an offset outright."""
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=ZoneInfo(settings.tz))
    return dt.astimezone(timezone.utc)


def touch(task: Task) -> None:
    task.updated_at = utcnow()


class Outcome(BaseModel):
    """What happened, for the notifier."""
    action: str
    task: Optional[Task] = None
    changed: dict = {}

    model_config = {"arbitrary_types_allowed": True}


async def classify_message(session: Session, msg: Message, owner: str = "the user") -> Optional[Outcome]:
    ctx = session.exec(
        select(Message)
        .where(Message.chat_id == msg.chat_id, Message.id != msg.id, Message.sent_at <= msg.sent_at)
        .order_by(Message.sent_at.desc())
        .limit(settings.context_window)
    ).all()
    ctx = list(reversed(ctx))
    tasks = candidate_tasks(session, msg)
    by_id = {t.id: t for t in tasks}

    llm = LLM(classifier_profile())
    try:
        who = ""
        if settings.graph_enabled:
            try:
                who = graph.context_for(session, msg)
            except Exception:  # noqa: BLE001 — memory is never load-bearing
                log.exception("graph context failed")
        raw = await llm.complete_json(build_messages(msg, ctx, tasks, owner, who))
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

    outcome: Optional[Outcome] = None
    target = by_id.get(result.task_id) if result.task_id else None
    conf_ok = result.confidence >= settings.classifier_min_confidence

    # any reply from the owner in this chat answers the questions being watched there — silently
    if msg.from_me:
        for t in tasks:
            if t.status == TaskStatus.watching:
                t.status = TaskStatus.done
                touch(t)
                session.add(t)

    if result.action == "complete" and target and conf_ok:
        target.status = TaskStatus.done
        touch(target)
        session.add(target)
        outcome = Outcome(action="complete", task=target)

    elif result.action == "update" and target and conf_ok:
        changed = {}
        due = _parse_due(result.due_at)
        if due and due != target.due_at:
            target.due_at, changed["due_at"] = due, due
        if result.details and result.details != target.details:
            target.details, changed["details"] = result.details, result.details
        if result.priority and result.priority != target.priority and result.priority != "normal":
            target.priority, changed["priority"] = result.priority, result.priority
        if changed:
            touch(target)
            session.add(target)
            outcome = Outcome(action="update", task=target, changed=changed)

    elif result.action in ("new", "update") and result.kind in ("task", "event", "question") and conf_ok:
        due = _parse_due(result.due_at)
        if result.kind == "event" and due and due < datetime.now(ZoneInfo("UTC")) - timedelta(hours=1):
            log.info("dropping past event %r (due %s)", result.title[:60], due)
            session.commit()
            return None
        # "update" with no valid target falls through to creating a task
        task = Task(
            kind=TaskKind(result.kind),
            status=TaskStatus.watching if result.kind == "question" and not msg.is_self
            else TaskStatus.pending_confirmation,
            title=result.title[:200] or (msg.text[:80] if msg.text else "Untitled"),
            details=result.details or None,
            due_at=due,
            priority=result.priority,
            people=", ".join(result.people) or None,
            confidence=result.confidence,
            source_message_id=msg.id,
            chat_id=msg.chat_id,
        )
        session.add(task)
        outcome = Outcome(action="new", task=task)

    session.commit()
    if outcome and outcome.task:
        session.refresh(outcome.task)
        log.info("%s task #%s (%s, %.2f): %s", outcome.action, outcome.task.id, outcome.task.kind.value,
                 result.confidence, outcome.task.title)
    return outcome


async def classify_pending(session: Session, owner: str = "the user", limit: int = 50) -> list[Outcome]:
    """Catch-up pass: classify anything ingested but not yet classified (e.g. after an outage)."""
    pending = session.exec(
        select(Message).where(Message.classified == False).order_by(Message.sent_at).limit(limit)  # noqa: E712
    ).all()
    out = []
    for m in pending:
        o = await classify_message(session, m, owner)
        if o:
            out.append(o)
    return out
