"""The watcher: every few minutes, look for what you are about to miss. Quiet unless it matters.

Rules, each with its own alert key so one thing is raised once:

  deadline   an open task due within 6 h: notification (risk6:<id>); within 1 h: alarm (risk1:<id>).
             Events use their start time, which is what due_at holds for them.
  promise    something you committed to ("I'll send it in 10 mins": the task came from, or was
             updated by, your own message) is past due and still open: notification (promise:<id>).
  bill       a task that names money owed: notification 2 days before (bill2:<id>), alarm on the due
             day (billday:<id>).
  renewal    a task or a message about a renewal / expiry: notification 7 days before and on the day
             (renew:<id>, renewday:<id>; messages renewmsg:<id>, renewmsgday:<id>).
  health     WhatsApp silent for hours while it normally flows, or Gmail polls failing: one
             notification a day (health:whatsapp:<date>, health:gmail:<label>:<date>).
  call       an alarm still unanswered after ESCALATE_MINUTES becomes a voice call (call:<alarm id>),
             at most CALLS_PER_DAY a day. Inside quiet hours only deadline and bill alarms call.
             Alarms are held through quiet hours (alerts.py), so an accepted task due within the
             hour at night is raised as a call straight away (risk1 at level 4, same cap).

Who is waiting: `close_relation()` tells notify.unanswered_question to ring (alarm) instead of
notify when the sender is family, a professor/advisor/manager, or a chat listed in WATCH_VIP_CHATS.

Every watcher alert goes through `interrupt()`, which first applies what you taught it (three
"Not important" answers for the same kind of alert from the same chat in 30 days lower that kind
from that chat by one level), then asks the cheap model whether this is worth interrupting you
now (WATCH_JUDGE; on error or timeout the rule stands). Something lowered to silent is still
recorded (level 0, status done) so the same key is not reconsidered every five minutes.

A key that was ever raised (answered or not) is not raised again by the watcher: an answered
alert is an answer, and "done"/"not important" must not come back five minutes later.
"""
from __future__ import annotations

import asyncio
import json
import logging
import re
from datetime import date, datetime, timedelta
from typing import Optional
from zoneinfo import ZoneInfo

from dateutil import parser as dateparser
from sqlalchemy import func
from sqlmodel import Session, or_, select

from . import alerts, graph
from .classifier import to_utc
from .config import settings
from .db import Alert, Edge, Entity, Message, Note, Task, TaskKind, TaskStatus, engine, utcnow
from .llm import LLM, classifier_profile
from .notify import fmt_due

log = logging.getLogger("brain.watcher")

SILENT, BRIEF, NOTIFY, ALARM, CALL = alerts.SILENT, alerts.BRIEF, alerts.NOTIFY, alerts.ALARM, alerts.CALL
LIVE = (TaskStatus.open, TaskStatus.pending_confirmation)
CALLABLE_IN_QUIET = {"deadline", "bill"}  # someone waiting never rings you at night
JUDGE_TIMEOUT = 8.0
LEARN_DISMISSALS = 3
LEARN_DAYS = 30
PROMISE_LOOKBACK = timedelta(hours=24)  # older broken promises are the 18:00 overdue list's job
WHATSAPP_NORMAL_PER_WEEK = 20  # fewer than this in the week before: silence is normal, not a fault
GMAIL_ERROR_COUNT = 3  # consecutive failed polls before it is worth telling you
GMAIL_ERROR_MINUTES = 30
ESCALATE_WINDOW = timedelta(hours=2)  # an alarm unanswered for longer was missed; a call now is noise

# money owed. Mirrors gmail._MINE's bill wording; kept apart because this runs on task titles
_BILL = re.compile(
    r"(₹|\b(?:rs|inr)\.?\s?\d|\bamount (?:due|payable)\b|\bbills?\b|\bfees?\b|\bpayments?\b|\bemi\b"
    r"|\bdues\b|\bpay\b|\bpremium\b|\binvoice\b)", re.I)
_RENEW = re.compile(r"\b(renew\w*|expir\w*|valid (?:till|until|upto|up to)|subscription)\b", re.I)
# messages: "offer valid till" is marketing, so a message needs a renewal or expiry word
_RENEW_MSG = re.compile(r"\b(renew\w*|expir\w*|subscription)\b", re.I)
_MON = r"(?:jan|feb|mar|apr|may|jun|jul|aug|sep|sept|oct|nov|dec)[a-z]*\.?"
_DATE = re.compile(
    rf"(\b\d{{4}}-\d{{2}}-\d{{2}}\b|\b\d{{1,2}}[/.-]\d{{1,2}}[/.-]\d{{2,4}}\b"
    rf"|\b\d{{1,2}}(?:st|nd|rd|th)?\s+{_MON},?\s+\d{{4}}\b|\b{_MON}\s+\d{{1,2}}(?:st|nd|rd|th)?,?\s+\d{{4}}\b)", re.I)

# who counts as close: words in the graph's one-line summary of the sender, or a relation to you
_CLOSE = re.compile(
    r"\b(mother|father|mom|mum|dad|amma|appa|sister|brother|wife|husband|son|daughter|uncle|aunt|aunty"
    r"|grandmother|grandfather|grandma|grandpa|cousin|family|parents?|girlfriend|boyfriend|fianc[eé]e?"
    r"|professor|prof|advis[eo]r|guide|supervisor|mentor|manager|boss|hod|dean|principal|faculty|teacher)\b",
    re.I)
CLOSE_RELS = {"advisor_of", "family_of", "parent_of", "child_of", "sibling_of", "spouse_of",
              "manager_of", "supervises", "teaches"}


# ---------------------------------------------------------------- helpers

def _utc(dt: datetime) -> datetime:
    return to_utc(dt)


def _local(dt: datetime) -> datetime:
    return _utc(dt).astimezone(ZoneInfo(settings.tz))


def _seen(session: Session, key: str) -> bool:
    """Was this key ever raised, answered or not."""
    return session.exec(select(Alert.id).where(Alert.key == key).limit(1)).first() is not None


def _source(session: Session, task_id: Optional[int]) -> Optional[str]:
    t = session.get(Task, task_id) if task_id else None
    return t.chat_id if t else None


def _task_dismissed(session: Session, task_id: int) -> bool:
    """You said "not important" about this task once: the watcher leaves it alone."""
    return session.exec(select(Alert.id).where(Alert.task_id == task_id, Alert.ack == "dismiss")
                        .limit(1)).first() is not None


def _task_alerted(session: Session, task_id: int, level: int, since: Optional[datetime] = None) -> bool:
    """Already interrupted you about this task at least this loudly (since `since`)."""
    q = select(Alert.id).where(Alert.task_id == task_id, Alert.level >= level)
    if since:
        q = q.where(Alert.created_at >= since)
    return session.exec(q.limit(1)).first() is not None


def _plain(title: str) -> str:
    """An alert title without its leading emoji."""
    return re.sub(r"^\W+", "", title)


def is_bill(t: Task) -> bool:
    return bool(_BILL.search(f"{t.title}\n{t.details or ''}"))


def is_renewal(t: Task) -> bool:
    return bool(_RENEW.search(f"{t.title}\n{t.details or ''}"))


def learned_level(session: Session, kind: str, source: Optional[str], level: int,
                  now: Optional[datetime] = None) -> int:
    """Three or more "Not important" answers to this kind of alert from this chat in the last
    30 days: one level quieter (never below silent). Reads the Alert table; alerts.py is unchanged."""
    if level <= SILENT:
        return level
    since = _utc(now or utcnow()) - timedelta(days=LEARN_DAYS)
    q = select(func.count(Alert.id)).select_from(Alert).where(Alert.kind == kind, Alert.ack == "dismiss",
                                           Alert.acked_at >= since)
    if source is None:
        q = q.where(Alert.task_id.is_(None))  # type: ignore[union-attr]
    else:
        q = q.join(Task, Alert.task_id == Task.id).where(Task.chat_id == source)  # type: ignore[arg-type]
    n = session.exec(q).one()
    if n >= LEARN_DISMISSALS:
        log.info("%s from %s dismissed %d times in %d days: one level quieter", kind, source, n, LEARN_DAYS)
        return level - 1
    return level


JUDGE_PROMPT = """You decide whether {owner}'s assistant should interrupt him RIGHT NOW.
It is {now} ({tz}). He wants at most three interruptions a day: only things he is about to miss,
someone important waiting on him, or money/deadlines with a real cost. Routine, already-handled or
vague items are not worth it.

Proposed {how}: {title}
{body}

Answer with ONLY a JSON object: {{"interrupt": true or false, "reason": "one short sentence"}}"""

_HOW = {NOTIFY: "phone notification", ALARM: "ringing alarm", CALL: "voice call"}


async def judged_level(level: int, title: str, body: str, now: Optional[datetime] = None) -> int:
    """Ask the cheap model once. "No" means one level quieter (a notification becomes silent);
    any error, timeout or unclear answer keeps the rule's decision."""
    if not settings.watch_judge or level < NOTIFY:
        return level
    now = _utc(now or utcnow())
    prompt = JUDGE_PROMPT.format(owner=settings.owner_name, tz=settings.tz, how=_HOW.get(level, "alert"),
                                 now=_local(now).strftime("%A %d %B %Y %H:%M"), title=title, body=body[:600])
    try:
        raw = await asyncio.wait_for(
            LLM(classifier_profile(), timeout=JUDGE_TIMEOUT).complete_json(
                [{"role": "user", "content": prompt}], max_tokens=400),
            JUDGE_TIMEOUT)
    except Exception as e:  # noqa: BLE001 — the rule's decision stands
        log.info("judge unavailable (%s); keeping level %d for %r", type(e).__name__, level, title[:60])
        return level
    v = raw.get("interrupt") if isinstance(raw, dict) else None
    if isinstance(v, str):
        v = {"true": True, "false": False}.get(v.strip().lower())
    if v is False:
        log.info("judge: not worth interrupting for %r (%s)", title[:60], (raw or {}).get("reason", ""))
        return level - 1 if level > NOTIFY else SILENT
    return level


def _record_silent(session: Session, kind: str, title: str, body: str, task_id: Optional[int],
                   key: Optional[str], now: datetime) -> Alert:
    a = Alert(level=SILENT, kind=kind, title=title[:200], body=body[:2000], task_id=task_id, key=key,
              status="done", channel="none", deliver_after=now, created_at=now)
    session.add(a)
    session.commit()
    session.refresh(a)
    return a


async def interrupt(session: Session, level: int, kind: str, title: str, body: str = "", *,
                    task_id: Optional[int] = None, key: Optional[str] = None, judge: bool = True,
                    min_level: int = SILENT, supersede: bool = False,
                    now: Optional[datetime] = None) -> Optional[Alert]:
    """Learning, then the model's yes/no, then alerts.raise_alert. Below `min_level` (a call the
    model talked down) or silent: recorded only, so the key is not reconsidered."""
    if level <= SILENT:
        return await alerts.raise_alert(session, level, kind, title, body)  # logged, nothing kept
    now = _utc(now or utcnow())
    level = learned_level(session, kind, _source(session, task_id), level, now)
    if judge:
        level = await judged_level(level, title, body, now)
    if level <= SILENT or level < min_level:
        return _record_silent(session, kind, title, body, task_id, key, now)
    return await alerts.raise_alert(session, level, kind, title, body, task_id=task_id, key=key,
                                    supersede=supersede, now=now)


# ---------------------------------------------------------------- who is waiting

def _sender_entity(session: Session, src: Message) -> Optional[Entity]:
    """The graph's node for whoever sent this, by exact key or exact alias. Never fuzzy."""
    keys = [k for k in {graph.slug(src.sender_name or ""), graph.slug(src.sender_id or "")} if k]
    if not src.is_group and src.chat_name:
        keys.append(graph.slug(src.chat_name))
    if not keys:
        return None
    hit = session.exec(select(Entity).where(Entity.key.in_(keys), Entity.kind == "person")).first()  # type: ignore[attr-defined]
    if hit:
        return hit
    for k in keys:
        for cand in session.exec(select(Entity).where(Entity.aliases.contains(k), Entity.kind == "person")).all():  # type: ignore[union-attr]
            if k in cand.aliases.split(","):
                return cand
    return None


def close_relation(session: Session, src: Optional[Message]) -> Optional[str]:
    """Why this sender is someone you do not keep waiting, or None. Explicit list
    (WATCH_VIP_CHATS), then the graph: a close relation to you, or a close word in their summary."""
    if not src:
        return None
    vip = {c.strip() for c in settings.watch_vip_chats.split(",") if c.strip()}
    if src.chat_id in vip or src.sender_id in vip:
        return "on your VIP list"
    e = _sender_entity(session, src)
    if not e or e.is_owner:
        return None
    o = graph.owner(session)
    if o:
        edge = session.exec(select(Edge).where(
            or_((Edge.src_id == e.id) & (Edge.dst_id == o.id), (Edge.src_id == o.id) & (Edge.dst_id == e.id)),
            Edge.rel.in_(CLOSE_RELS))).first()  # type: ignore[attr-defined]
        if edge:
            return edge.rel.replace("_", " ")
    m = _CLOSE.search(e.summary or "")
    return m.group(1).lower() if m else None


# ---------------------------------------------------------------- rules

def _live_tasks(session: Session, lo: datetime, hi: datetime) -> list[Task]:
    return list(session.exec(select(Task).where(Task.status.in_(LIVE), Task.due_at.is_not(None),  # type: ignore[attr-defined,union-attr]
                                                Task.due_at > lo, Task.due_at <= hi)
                             .order_by(Task.due_at)).all())


def _icon(t: Task) -> str:
    return "📅" if t.kind == TaskKind.event else "⏳"


async def deadlines(session: Session, now: datetime) -> int:
    """Due within 6 h: a notification; within 1 h: an alarm. Skipped when you already heard about
    it that loudly recently, or said it was not important."""
    raised = 0
    for t in _live_tasks(session, now, now + timedelta(hours=6)):
        if _task_dismissed(session, t.id):
            continue
        due = _utc(t.due_at)  # type: ignore[arg-type]
        mins = int((due - now).total_seconds() // 60)
        when = "starts" if t.kind == TaskKind.event else "is due"
        body = f"{t.title} {when} {fmt_due(t)} (in {mins} min)." if mins < 90 else \
            f"{t.title} {when} {fmt_due(t)} (in {mins // 60} h {mins % 60} min)."
        if due - now <= timedelta(hours=1):
            key = f"risk1:{t.id}"
            if _seen(session, key) or _task_alerted(session, t.id, ALARM):
                continue
            # the six-hour notice is superseded by the alarm
            for old in session.exec(select(Alert).where(Alert.key == f"risk6:{t.id}",
                                                        Alert.status.in_(alerts.OPEN))).all():  # type: ignore[attr-defined]
                old.status = "done"
                session.add(old)
            session.commit()
            # at night an alarm would wait for the morning, after the deadline: an accepted task calls
            level = ALARM
            if alerts.quiet_until(now) and t.status == TaskStatus.open                     and calls_today(session, now) < settings.calls_per_day:
                level, body = CALL, call_script(None, t, title=f"{t.title} {when} {fmt_due(t)}")
            a = await interrupt(session, level, "deadline", f"{_icon(t)} Within the hour: {t.title}", body,
                                task_id=t.id, key=key, now=now)
        else:
            key = f"risk6:{t.id}"
            if _seen(session, key) or _task_alerted(session, t.id, NOTIFY, since=now - timedelta(hours=6)):
                continue
            a = await interrupt(session, NOTIFY, "deadline", f"{_icon(t)} Today: {t.title}", body,
                                task_id=t.id, key=key, now=now)
        raised += bool(a and a.level > SILENT)
    return raised


def _promised(session: Session, t: Task) -> bool:
    """You committed to it: the task came from your own message, or one of your messages updated it
    (the classifier's stored answer names the task). Notes to yourself are not promises."""
    if t.source_message_id:
        src = session.get(Message, t.source_message_id)
        if src and src.from_me and not src.is_self:
            return True
    hit = session.exec(select(Message.id).where(
        Message.from_me == True, Message.is_self == False,  # noqa: E712
        Message.classification.contains(f'"task_id":{t.id},'),  # type: ignore[union-attr]
        Message.classification.contains('"action":"update"')).limit(1)).first()  # type: ignore[union-attr]
    return hit is not None


async def promises(session: Session, now: datetime) -> int:
    raised = 0
    for t in _live_tasks(session, now - PROMISE_LOOKBACK, now):
        key = f"promise:{t.id}"
        if t.kind == TaskKind.event or _seen(session, key) or _task_dismissed(session, t.id):
            continue
        if not _promised(session, t):
            continue
        who = f" to {t.people}" if t.people else ""
        a = await interrupt(session, NOTIFY, "promise", f"🤝 You promised{who}: {t.title}",
                            f"It was due {fmt_due(t)} and is still open.", task_id=t.id, key=key, now=now)
        raised += bool(a and a.level > SILENT)
    return raised


async def bills(session: Session, now: datetime) -> int:
    """Money owed: a notification two days before, an alarm on the due day. A renewal that is also a
    bill had its seven-day notice instead of the two-day one."""
    raised = 0
    today = _local(now).date()
    for t in _live_tasks(session, now - timedelta(days=1), now + timedelta(days=2)):
        if not is_bill(t) or _task_dismissed(session, t.id):
            continue
        due_day = _local(t.due_at).date()  # type: ignore[arg-type]
        if due_day == today:
            key, level, title = f"billday:{t.id}", ALARM, f"💸 Due today: {t.title}"
        elif _utc(t.due_at) > now and not is_renewal(t):  # type: ignore[arg-type]
            key, level, title = f"bill2:{t.id}", NOTIFY, f"💸 Due {fmt_due(t)}: {t.title}"
        else:
            continue
        if _seen(session, key):
            continue
        body = f"{t.title}, due {fmt_due(t)}." + (f" {t.details}" if t.details else "")
        a = await interrupt(session, level, "bill", title, body, task_id=t.id, key=key, now=now)
        raised += bool(a and a.level > SILENT)
    return raised


def renewal_date(text: str) -> Optional[date]:
    """The date a renewal/expiry message names, when it states one with a year near the keyword."""
    m = _RENEW_MSG.search(text or "")
    if not m:
        return None
    window = text[max(0, m.start() - 60): m.end() + 120]
    d = _DATE.search(window)
    if not d:
        return None
    raw = d.group(1)
    try:
        if re.fullmatch(r"\d{4}-\d{2}-\d{2}", raw):
            return date.fromisoformat(raw)
        return dateparser.parse(raw, dayfirst=True).date()
    except (ValueError, OverflowError):
        return None


def upcoming_renewals(session: Session, now: datetime, days: int = 7) -> list[tuple[date, str, str]]:
    """(day, what, key suffix) for renewals in the next `days` days: tasks and messages. Used by the
    renewal rule and the weekly review."""
    today = _local(now).date()
    out: list[tuple[date, str, str]] = []
    for t in _live_tasks(session, now - timedelta(days=1), now + timedelta(days=days + 1)):
        if is_renewal(t):
            d = _local(t.due_at).date()  # type: ignore[arg-type]
            if today <= d <= today + timedelta(days=days):
                out.append((d, t.title, f"t{t.id}"))
    since = now - timedelta(days=60)
    carded = {mid for mid in session.exec(select(Task.source_message_id).where(Task.source_message_id.is_not(None))).all()}  # type: ignore[union-attr]
    from .gmail import _MINE

    for m in session.exec(select(Message).where(Message.sent_at >= since, Message.from_me == False)).all():  # noqa: E712
        if m.id in carded or not _RENEW_MSG.search(m.text or ""):
            continue
        if m.is_bulk and not _MINE.search(m.text or ""):
            continue  # a list mail about some subscription, not yours
        d = renewal_date(m.text)
        if d and today <= d <= today + timedelta(days=days):
            subject = (m.text or "").removeprefix("Subject: ").split("\n", 1)[0][:80]
            out.append((d, f"{subject} (from {m.sender_name or m.chat_name or m.sender_id})", f"m{m.id}"))
    return sorted(out)


async def renewals(session: Session, now: datetime) -> int:
    raised = 0
    today = _local(now).date()
    for d, what, ref in upcoming_renewals(session, now, days=7):
        kind_ref, ident = ref[0], int(ref[1:])
        task_id = ident if kind_ref == "t" else None
        if task_id and _task_dismissed(session, task_id):
            continue
        base = "renew" if kind_ref == "t" else "renewmsg"
        if d == today:
            if task_id and is_bill(session.get(Task, task_id)):  # type: ignore[arg-type]
                continue  # the bill rule rings on the day
            key, title = f"{base}day:{ident}", f"🔁 Renews/expires today: {what}"
        else:
            key, title = f"{base}:{ident}", f"🔁 Renews/expires {d:%a %d %b}: {what}"
        if _seen(session, key):
            continue
        a = await interrupt(session, NOTIFY, "renewal", title, f"{what} — {d:%A %d %B}.", task_id=task_id,
                            key=key, now=now)
        raised += bool(a and a.level > SILENT)
    return raised


async def health(session: Session, now: datetime) -> int:
    raised = 0
    day = _local(now).date().isoformat()
    last = session.exec(select(func.max(Message.received_at)).where(Message.source == "whatsapp")).one()
    if last is not None:
        quiet_for = now - _utc(last)
        if quiet_for > timedelta(hours=settings.watch_whatsapp_silent_hours):
            week_before = session.exec(select(func.count(Message.id)).where(
                Message.source == "whatsapp", Message.received_at >= _utc(last) - timedelta(days=7))).one()
            key = f"health:whatsapp:{day}"
            if week_before >= WHATSAPP_NORMAL_PER_WEEK and not _seen(session, key):
                a = await interrupt(session, NOTIFY, "health", "📵 WhatsApp may be logged out",
                                    f"No WhatsApp message has arrived for {int(quiet_for.total_seconds() // 3600)} h. "
                                    "Check the bridge (journalctl --user -u assistant-bridge) and re-scan the QR "
                                    "if it was unlinked.", key=key, now=now)
                raised += bool(a and a.level > SILENT)
    for n in session.exec(select(Note).where(Note.key.startswith("gmail_error_"))).all():  # type: ignore[attr-defined]
        try:
            info = json.loads(n.value)
            since = _utc(datetime.fromisoformat(info["since"]))
        except (ValueError, KeyError, TypeError):
            continue
        label = n.key.removeprefix("gmail_error_")
        key = f"health:gmail:{label}:{day}"
        if info.get("count", 0) < GMAIL_ERROR_COUNT or now - since < timedelta(minutes=GMAIL_ERROR_MINUTES) \
                or _seen(session, key):
            continue
        a = await interrupt(session, NOTIFY, "health", f"📭 Gmail ({label}) is not being read",
                            f"Polling has failed {info.get('count')} times since {_local(since):%a %H:%M}: "
                            f"{str(info.get('error', ''))[:200]}. If the sign-in expired, re-run "
                            f"python -m app.google_auth {label}.", key=key, now=now)
        raised += bool(a and a.level > SILENT)
    return raised


def call_script(a: Optional[Alert], t: Optional[Task], title: str = "") -> str:
    """One or two sentences the voice call reads out."""
    who = settings.owner_name.split()[0] if settings.owner_name else "Hi"
    if a is None:
        return f"{who}, this is your assistant: {title}. Open the app and mark it done or snooze it."
    what = t.title if t else _plain(a.title)
    if a.kind == "question":
        return f"{who}, {_plain(a.title)}, and you have not answered the alarm. Please reply to them."
    when = f", due {fmt_due(t)}" if t and t.due_at else ""
    return f"{who}, this is your assistant: {what}{when}. The alarm went unanswered; open the app and mark it done or snooze it."


def calls_today(session: Session, now: datetime) -> int:
    midnight = datetime.combine(_local(now).date(), datetime.min.time(), tzinfo=ZoneInfo(settings.tz))
    return session.exec(select(func.count(Alert.id)).where(Alert.level == CALL,
                                                           Alert.created_at >= _utc(midnight))).one()


async def escalate(session: Session, now: datetime) -> int:
    """An alarm still unanswered after ESCALATE_MINUTES (and sent within ESCALATE_WINDOW): a call,
    CALLS_PER_DAY at most. Inside quiet hours only deadline and bill alarms call; a deadline that
    has already passed never does."""
    cutoff = now - timedelta(minutes=settings.escalate_minutes)
    alarms = session.exec(select(Alert).where(Alert.level == ALARM, Alert.status == "sent",
                                              Alert.ack.is_(None), Alert.sent_at <= cutoff,  # type: ignore[union-attr,operator]
                                              Alert.sent_at >= now - ESCALATE_WINDOW)  # type: ignore[operator]
                          .order_by(Alert.id)).all()
    if not alarms:
        return 0
    calls = calls_today(session, now)
    quiet = alerts.quiet_until(now) is not None
    made = 0
    for a in alarms:
        key = f"call:{a.id}"
        if _seen(session, key):
            continue
        if calls >= settings.calls_per_day:
            log.info("call cap (%d) reached today; alarm #%s stays an alarm", settings.calls_per_day, a.id)
            break
        if quiet and a.kind not in CALLABLE_IN_QUIET:
            continue
        t = session.get(Task, a.task_id) if a.task_id else None
        if t and t.status in (TaskStatus.done, TaskStatus.rejected):
            continue
        if t and t.due_at and a.kind == "deadline" and _utc(t.due_at) < now:
            continue  # too late for a call to help
        c = await interrupt(session, CALL, "call", f"📞 {_plain(a.title)}", call_script(a, t),
                            task_id=a.task_id, key=key, min_level=CALL, now=now)
        if c and c.level == CALL:
            calls += 1
            made += 1
    return made


async def run(session: Session, now: Optional[datetime] = None) -> dict:
    """One pass of every rule. Each rule is independent: one failing does not stop the others."""
    now = _utc(now or utcnow())
    out: dict[str, int] = {}
    # escalation first: it acts on alarms from earlier passes, never on one raised a moment ago
    for name, rule in (("escalate", escalate), ("bills", bills), ("renewals", renewals),
                       ("deadlines", deadlines), ("promises", promises), ("health", health)):
        try:
            out[name] = await rule(session, now)
        except Exception:  # noqa: BLE001
            log.exception("watcher rule %s failed", name)
            session.rollback()
            out[name] = 0
    if any(out.values()):
        log.info("watcher: %s", out)
    return out


async def watch() -> None:
    """The scheduled job."""
    with Session(engine) as session:
        await run(session)
