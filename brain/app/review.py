"""Background work: the weekly review, the monthly noise report, and Gmail draft replies.

  weekly    Sunday 18:00 local, a brief (level 1): done this week, what slipped, next week's
            deadlines and renewals. Written by the planner model; a plain list if it is down.
  monthly   1st of the month 09:00, a brief: last month's interruptions per level, Done vs
            "Not important", the dismiss rate and calls, against the targets (at most 3
            interruptions a day, 2 calls a week, under 20% "Not important").
  drafts    DRAFTS_ENABLED (off by default): a person (not a list) mailed you a question that became
            a question task; the planner writes a reply and it is saved as a Gmail draft in that
            thread. Never sent: the scope allows drafts, and nothing here calls send.
"""
from __future__ import annotations

import base64
import calendar
import logging
from datetime import date, datetime, timedelta
from email.message import EmailMessage
from typing import Optional
from zoneinfo import ZoneInfo

from sqlmodel import Session, select

from . import alerts, gmail, watcher
from .classifier import to_utc
from .config import settings
from .db import Alert, Message, Note, Task, TaskKind, TaskStatus, engine, utcnow
from .llm import LLM, planner_profile
from .notify import fmt_due

log = logging.getLogger("brain.review")

LIVE = (TaskStatus.open, TaskStatus.pending_confirmation)
TARGET_PER_DAY, TARGET_CALLS_PER_WEEK, TARGET_DISMISS = 3, 2, 0.20


def _tasks(session: Session, *where) -> list[Task]:
    return list(session.exec(select(Task).where(*where).order_by(Task.due_at)).all())  # type: ignore[arg-type]


def _lines(tasks: list[Task]) -> str:
    return "\n".join(f"- {t.title} · {fmt_due(t)}" for t in tasks) or "- (none)"


WEEKLY_PROMPT = """You are {owner}'s personal assistant writing his weekly review. Today is {today} ({tz}).
Plain text, no markdown headers, under 180 words, three short parts:
1. Done this week (one line, grouped).
2. Slipped: what passed its date and is still open, and what to do about each (drop, redo, reply).
3. Next week, in date order, with renewals and anything to prepare for.
Be specific; no filler.

DONE THIS WEEK:
{done}

SLIPPED (past due, still open):
{slipped}

NEXT 7 DAYS:
{coming}

RENEWALS / EXPIRIES:
{renewals}"""


async def weekly(session: Session, now: Optional[datetime] = None) -> Optional[Alert]:
    now = to_utc(now or utcnow())
    week = timedelta(days=7)
    done = _tasks(session, Task.status == TaskStatus.done, Task.updated_at >= now - week)
    slipped = _tasks(session, Task.status.in_(LIVE), Task.due_at >= now - week, Task.due_at < now)  # type: ignore[attr-defined,operator]
    coming = _tasks(session, Task.status.in_(LIVE), Task.due_at >= now, Task.due_at <= now + week)  # type: ignore[attr-defined,operator]
    ren = watcher.upcoming_renewals(session, now, days=7)
    ren_text = "\n".join(f"- {d:%a %d %b}: {what}" for d, what, _ in ren) or "- (none)"
    tz = ZoneInfo(settings.tz)
    prompt = WEEKLY_PROMPT.format(owner=settings.owner_name, today=now.astimezone(tz).strftime("%A %d %B %Y"),
                                  tz=settings.tz, done=_lines(done), slipped=_lines(slipped),
                                  coming=_lines(coming), renewals=ren_text)
    try:
        text = await LLM(planner_profile()).complete([{"role": "user", "content": prompt}], max_tokens=900)
        if not text or not text.strip():
            raise ValueError("empty answer")
    except Exception as e:  # noqa: BLE001 — a plain list beats no review
        log.warning("planner unavailable for the weekly review (%s); sending the plain list", e)
        text = (f"Weekly review (planner offline)\nDone ({len(done)}):\n{_lines(done)}\n"
                f"Slipped:\n{_lines(slipped)}\nNext 7 days:\n{_lines(coming)}\nRenewals:\n{ren_text}")
    return await alerts.raise_alert(session, alerts.BRIEF, "weekly", "🗓️ Weekly review", text,
                                    key=f"weekly:{now.astimezone(tz).date()}", supersede=True, now=now)


def _last_month(now: datetime) -> tuple[datetime, datetime, date]:
    """[start, end) of the previous calendar month, local midnight to midnight, as UTC."""
    tz = ZoneInfo(settings.tz)
    first = now.astimezone(tz).date().replace(day=1)
    prev = (first - timedelta(days=1)).replace(day=1)
    start = datetime.combine(prev, datetime.min.time(), tzinfo=tz)
    end = datetime.combine(first, datetime.min.time(), tzinfo=tz)
    return to_utc(start), to_utc(end), prev


def month_stats(session: Session, start: datetime, end: datetime) -> dict:
    """What reached the phone between start and end: silent records and never-sent alerts are not
    interruptions."""
    sent = session.exec(select(Alert).where(Alert.created_at >= start, Alert.created_at < end,
                                            Alert.level >= alerts.BRIEF, Alert.sent_at.is_not(None))).all()  # type: ignore[union-attr]
    per_level = {lvl: sum(a.level == lvl for a in sent) for lvl in (1, 2, 3, 4)}
    loud = [a for a in sent if a.level >= alerts.NOTIFY]
    done = sum(a.ack == "done" for a in loud)
    dismissed = sum(a.ack == "dismiss" for a in loud)
    days = max(1, round((end - start).total_seconds() / 86400))
    by_kind: dict[str, int] = {}
    for a in loud:
        by_kind[a.kind] = by_kind.get(a.kind, 0) + 1
    return {"per_level": per_level, "interruptions": len(loud), "done": done, "dismissed": dismissed,
            "unanswered": sum(a.ack is None for a in loud), "dismiss_rate": dismissed / len(loud) if loud else 0.0,
            "calls": per_level[4], "days": days, "per_day": len(loud) / days,
            "calls_per_week": per_level[4] / (days / 7), "by_kind": by_kind}


def month_text(st: dict, month: date) -> str:
    lv = st["per_level"]
    verdict = []
    if st["per_day"] > TARGET_PER_DAY:
        verdict.append(f"too many interruptions ({st['per_day']:.1f}/day, target ≤{TARGET_PER_DAY})")
    if st["calls_per_week"] > TARGET_CALLS_PER_WEEK:
        verdict.append(f"too many calls ({st['calls_per_week']:.1f}/week, target ≤{TARGET_CALLS_PER_WEEK})")
    if st["dismiss_rate"] > TARGET_DISMISS:
        verdict.append(f"too many \"Not important\" ({st['dismiss_rate']:.0%}, target <{TARGET_DISMISS:.0%})")
    kinds = ", ".join(f"{k} {n}" for k, n in sorted(st["by_kind"].items(), key=lambda x: -x[1])) or "none"
    return (f"{month:%B %Y}: {st['interruptions']} interruptions ({st['per_day']:.1f}/day): "
            f"{lv[2]} notifications, {lv[3]} alarms, {lv[4]} calls; plus {lv[1]} briefs.\n"
            f"Answered Done {st['done']}, Not important {st['dismissed']}, no answer {st['unanswered']}. "
            f"Dismiss rate {st['dismiss_rate']:.0%}.\n"
            f"By kind: {kinds}.\n"
            + ("Needs tuning: " + "; ".join(verdict) + "." if verdict else "Within targets."))


async def monthly(session: Session, now: Optional[datetime] = None) -> Optional[Alert]:
    now = to_utc(now or utcnow())
    start, end, month = _last_month(now)
    text = month_text(month_stats(session, start, end), month)
    return await alerts.raise_alert(session, alerts.BRIEF, "monthly", f"📊 {calendar.month_name[month.month]} in alerts",
                                    text, key=f"monthly:{month:%Y-%m}", supersede=True, now=now)


# ---------------------------------------------------------------- Gmail drafts

DRAFT_PROMPT = """Write a reply email from {owner} to {sender}. {owner} has not answered yet.
Short and natural, in {owner}'s voice: answer what the mail asks where the mail itself gives the
answer; where only {owner} knows (a date, a yes/no, a number), leave a placeholder in square
brackets like [confirm date]. No subject line. End with "{first}".

{context}THE MAIL:
{text}"""


def _gmail_ref(m: Message) -> Optional[tuple[str, str]]:
    """(account label, Gmail message id) from external_id gmail:<label>:<id>."""
    parts = (m.external_id or "").split(":", 2)
    return (parts[1], parts[2]) if len(parts) == 3 and parts[0] == "gmail" else None


def draft_candidates(session: Session, now: datetime) -> list[tuple[Task, Message]]:
    rows = session.exec(
        select(Task, Message).join(Message, Task.source_message_id == Message.id)  # type: ignore[arg-type]
        .where(Task.kind == TaskKind.question,
               Task.status.in_([TaskStatus.watching, TaskStatus.open, TaskStatus.pending_confirmation]),  # type: ignore[attr-defined]
               Task.created_at >= now - timedelta(days=2), Message.source == "email",
               Message.is_bulk == False, Message.from_me == False)  # noqa: E712
    ).all()
    return [(t, m) for t, m in rows if not session.get(Note, f"gmail_draft_{t.id}")]


async def email_drafts(session: Session, now: Optional[datetime] = None) -> int:
    """Draft (never send) replies to questions people mailed you. Off unless DRAFTS_ENABLED."""
    if not settings.drafts_enabled:
        return 0
    now = to_utc(now or utcnow())
    made = 0
    for t, m in draft_candidates(session, now):
        ref = _gmail_ref(m)
        if not ref:
            continue
        label, gid = ref
        try:
            svc = gmail.service(label)
            if not svc:
                continue
            orig = svc.users().messages().get(userId="me", id=gid, format="metadata",
                                              metadataHeaders=["Message-ID", "Subject", "From", "References"]).execute()
        except Exception:  # noqa: BLE001
            log.exception("could not read mail %s for a draft", gid)
            continue
        h = {x["name"].lower(): x["value"] for x in orig.get("payload", {}).get("headers", [])}
        context = ""
        if settings.graph_enabled:
            try:
                from . import graph
                context = graph.context_for(session, m)
                context = f"WHAT YOU KNOW:\n{context}\n\n" if context else ""
            except Exception:  # noqa: BLE001 — memory is never load-bearing
                context = ""
        first = settings.owner_name.split()[0] if settings.owner_name else ""
        prompt = DRAFT_PROMPT.format(owner=settings.owner_name, sender=m.sender_name or m.sender_id,
                                     first=first, context=context, text=(m.text or "")[:4000])
        try:
            reply = await LLM(planner_profile()).complete([{"role": "user", "content": prompt}], max_tokens=900)
        except Exception as e:  # noqa: BLE001 — try again on the next pass
            log.warning("planner unavailable for a draft (%s)", e)
            continue
        if not reply or not reply.strip():
            continue
        mail = EmailMessage()
        mail["To"] = h.get("from") or m.sender_id
        subject = h.get("subject", "")
        mail["Subject"] = subject if subject.lower().startswith("re:") else f"Re: {subject}"
        if h.get("message-id"):
            mail["In-Reply-To"] = h["message-id"]
            mail["References"] = (h.get("references", "") + " " + h["message-id"]).strip()
        mail.set_content(reply.strip())
        raw = base64.urlsafe_b64encode(mail.as_bytes()).decode()
        body: dict = {"message": {"raw": raw}}
        if orig.get("threadId"):
            body["message"]["threadId"] = orig["threadId"]
        try:
            d = svc.users().drafts().create(userId="me", body=body).execute()
        except Exception:  # noqa: BLE001
            log.exception("could not save a draft for task #%s", t.id)
            continue
        session.merge(Note(key=f"gmail_draft_{t.id}", value=str(d.get("id", ""))))
        session.commit()
        made += 1
        log.info("draft reply saved for task #%s (%s)", t.id, m.sender_id)
    return made


# ---------------------------------------------------------------- scheduled jobs

async def weekly_review() -> None:
    with Session(engine) as session:
        await weekly(session)


async def monthly_report() -> None:
    with Session(engine) as session:
        await monthly(session)


async def drafts() -> None:
    with Session(engine) as session:
        await email_drafts(session)
