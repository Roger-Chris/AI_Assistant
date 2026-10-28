"""The watcher: deadlines at risk, promises, bills, renewals, who is waiting, health, calls,
the model's yes/no and learning from "Not important"."""
import asyncio
import json
import uuid
from datetime import date, datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import pytest
from sqlmodel import Session, select

from app import alerts, gmail, graph, notify, watcher
from app.config import settings
from app.db import Alert, Edge, Entity, Message, Note, Task, TaskKind, TaskStatus, engine

IST = ZoneInfo("Asia/Kolkata")
# a month ahead of the real clock, so rows other tests wrote "now" are far in the past
BASE = (datetime.now(IST) + timedelta(days=30)).replace(hour=10, minute=0, second=0, microsecond=0)
CHAT = "watch-test@s.whatsapp.net"


def at(hours=0.0, minutes=0.0, base=BASE):
    return base + timedelta(hours=hours, minutes=minutes)


@pytest.fixture(autouse=True)
def clean(monkeypatch):
    monkeypatch.setattr(settings, "quiet_hours", "23:00-07:00")
    monkeypatch.setattr(settings, "hub_url", "")
    monkeypatch.setattr(settings, "watch_vip_chats", "")
    monkeypatch.setattr(settings, "watch_judge", True)
    _wipe()
    yield
    _wipe()


def _wipe():
    with Session(engine) as s:
        for a in s.exec(select(Alert)).all():
            s.delete(a)
        s.commit()
        for t in s.exec(select(Task).where(Task.chat_id.startswith("watch-"))).all():  # type: ignore[union-attr]
            s.delete(t)
        s.commit()
        for m in s.exec(select(Message).where(Message.chat_id.startswith("watch-"))).all():  # type: ignore[union-attr]
            s.delete(m)
        for n in s.exec(select(Note).where(Note.key.startswith("gmail_"))).all():  # type: ignore[attr-defined]
            s.delete(n)
        ents = s.exec(select(Entity).where(Entity.key.startswith("wt-"))).all()  # type: ignore[union-attr]
        for e in ents:
            for edge in s.exec(select(Edge).where((Edge.src_id == e.id) | (Edge.dst_id == e.id))).all():
                s.delete(edge)
            s.delete(e)
        s.commit()


def task(s, title, due=None, kind=TaskKind.task, status=TaskStatus.open, chat=CHAT, src=None, details=None):
    t = Task(title=title, kind=kind, status=status, due_at=due, chat_id=chat,
             source_message_id=src.id if src else None, details=details)
    s.add(t)
    s.commit()
    s.refresh(t)
    return t


def message(s, text, from_me=False, chat=CHAT, sender="919800000001@s.whatsapp.net", name="Asha",
            source="whatsapp", sent=None, received=None, is_bulk=False, external_id=None, classification=None):
    m = Message(source=source, external_id=external_id or f"wt-{uuid.uuid4()}", chat_id=chat, chat_name=name,
                sender_id=sender, sender_name=name, from_me=from_me, text=text, is_bulk=is_bulk,
                sent_at=sent or BASE, received_at=received or BASE, classified=True,
                classification=classification)
    s.add(m)
    s.commit()
    s.refresh(m)
    return m


def task_alerts(s, t):
    return s.exec(select(Alert).where(Alert.task_id == t.id).order_by(Alert.id)).all()


# ---------------------------------------------------------------- deadlines

async def test_deadline_six_hours_then_one_hour():
    with Session(engine) as s:
        t = task(s, "Submit lab record", due=at(5))
        await watcher.run(s, now=at())
        [a] = task_alerts(s, t)
        assert (a.level, a.key, a.status) == (alerts.NOTIFY, f"risk6:{t.id}", "sent")
        await watcher.run(s, now=at(minutes=5))
        assert len(task_alerts(s, t)) == 1  # once

        await watcher.run(s, now=at(4, 10))
        six, one = task_alerts(s, t)
        assert (one.level, one.key) == (alerts.ALARM, f"risk1:{t.id}")
        s.refresh(six)
        assert six.status == "done"  # superseded by the alarm


async def test_deadline_skips_finished_far_and_dismissed():
    with Session(engine) as s:
        done = task(s, "Already done", due=at(2), status=TaskStatus.done)
        dropped = task(s, "Dropped", due=at(2), status=TaskStatus.rejected)
        far = task(s, "Next week", due=at(24 * 6))
        meh = task(s, "Not important to me", due=at(3))
        s.add(Alert(level=2, kind="new_task", title="x", task_id=meh.id, status="dismissed", ack="dismiss"))
        s.commit()
        await watcher.run(s, now=at())
        assert not task_alerts(s, done) and not task_alerts(s, dropped) and not task_alerts(s, far)
        assert len(task_alerts(s, meh)) == 1  # only the dismissed one


async def test_event_uses_its_start_and_recent_new_task_alert_counts():
    with Session(engine) as s:
        ev = task(s, "Viva in lab 3", due=at(0, 45), kind=TaskKind.event)
        fresh = task(s, "Reply to HOD", due=at(4))
        s.add(Alert(level=2, kind="new_task", title="Reply to HOD", task_id=fresh.id, status="sent",
                    created_at=at(-1)))
        s.commit()
        await watcher.run(s, now=at())
        [a] = task_alerts(s, ev)
        assert a.level == alerts.ALARM and "Viva" in a.title
        assert len(task_alerts(s, fresh)) == 1  # you heard about it an hour ago


async def test_pending_confirmation_tasks_count_but_watching_questions_do_not():
    with Session(engine) as s:
        p = task(s, "Pending card", due=at(2), status=TaskStatus.pending_confirmation)
        q = task(s, "a question", due=at(2), kind=TaskKind.question, status=TaskStatus.watching)
        await watcher.run(s, now=at())
        assert len(task_alerts(s, p)) == 1 and not task_alerts(s, q)


# ---------------------------------------------------------------- promises

async def test_promise_from_your_own_message():
    with Session(engine) as s:
        mine = message(s, "I'll send it in 10 mins", from_me=True)
        t = task(s, "Send the slides to Asha", due=at(-0.5), src=mine)
        theirs = message(s, "send me the slides")
        u = task(s, "Send slides (their ask)", due=at(-0.5), src=theirs)
        await watcher.run(s, now=at())
        [a] = task_alerts(s, t)
        assert (a.kind, a.key, a.level) == ("promise", f"promise:{t.id}", alerts.NOTIFY)
        assert not task_alerts(s, u)


async def test_promise_from_your_update():
    with Session(engine) as s:
        theirs = message(s, "can you send the form?")
        t = task(s, "Send the form", due=at(-0.2), src=theirs)
        message(s, "will send by 9:45", from_me=True,
                classification=json.dumps({"action": "update", "task_id": t.id, "kind": "task"},
                                          separators=(",", ":")))
        await watcher.run(s, now=at())
        assert [a.kind for a in task_alerts(s, t)] == ["promise"]


async def test_notes_to_self_and_old_promises_are_not_nagged():
    with Session(engine) as s:
        note = message(s, "remind me to pay fees", from_me=True)
        note.is_self = True
        s.add(note)
        s.commit()
        t = task(s, "Pay fees", due=at(-0.5), src=note)
        mine = message(s, "I'll do it", from_me=True)
        old = task(s, "Old promise", due=at(-48), src=mine)
        await watcher.promises(s, at().astimezone(timezone.utc))
        assert not task_alerts(s, t) and not task_alerts(s, old)


# ---------------------------------------------------------------- bills and renewals

async def test_bill_two_days_before_then_alarm_on_the_day():
    with Session(engine) as s:
        due = at(24 * 2 - 1)  # day after tomorrow, 09:00
        t = task(s, "Pay electricity bill ₹1,240", due=due)
        await watcher.bills(s, at(-24).astimezone(timezone.utc))  # three days before: nothing
        assert not task_alerts(s, t)
        await watcher.bills(s, at().astimezone(timezone.utc))
        [a] = task_alerts(s, t)
        assert (a.kind, a.level, a.key) == ("bill", alerts.NOTIFY, f"bill2:{t.id}")
        await watcher.bills(s, due.replace(hour=7, minute=30).astimezone(timezone.utc))
        two, day = task_alerts(s, t)
        assert (day.level, day.key) == (alerts.ALARM, f"billday:{t.id}")


def test_bill_and_renewal_words():
    assert watcher.is_bill(Task(title="Pay hostel fees"))
    assert watcher.is_bill(Task(title="Credit card", details="Amount due Rs. 4,500"))
    assert watcher.is_bill(Task(title="EMI for the laptop"))
    assert not watcher.is_bill(Task(title="Send feedback form"))
    assert watcher.is_renewal(Task(title="Renew passport"))
    assert watcher.is_renewal(Task(title="Bus pass valid till Friday"))
    assert not watcher.is_renewal(Task(title="Send report"))


def test_renewal_dates_in_messages():
    assert watcher.renewal_date("Your subscription expires on 12/11/2026. Renew now") == date(2026, 11, 12)
    assert watcher.renewal_date("Policy renewal due 3rd Dec 2026") == date(2026, 12, 3)
    assert watcher.renewal_date("membership expiry: 2026-12-01") == date(2026, 12, 1)
    assert watcher.renewal_date("Offer valid till 30/11/2026") is None  # marketing, no renewal word
    assert watcher.renewal_date("Your plan will renew soon") is None  # no date, nothing to do


async def test_renewal_task_seven_days_before_and_on_the_day():
    with Session(engine) as s:
        due = at(24 * 6 + 8)
        t = task(s, "Renew bike insurance", due=due)
        await watcher.renewals(s, at().astimezone(timezone.utc))
        [a] = task_alerts(s, t)
        assert (a.kind, a.key) == ("renewal", f"renew:{t.id}")
        await watcher.renewals(s, at(24 * 6).astimezone(timezone.utc))
        assert task_alerts(s, t)[-1].key == f"renewday:{t.id}"


async def test_renewal_from_a_message_and_marketing_is_ignored():
    with Session(engine) as s:
        d = (BASE + timedelta(days=5)).strftime("%d/%m/%Y")
        mine = message(s, f"Subject: Domain renewal\n\nYour domain expires on {d}.", source="email",
                       chat="watch-mail", name="Registrar", sent=at(-24))
        message(s, f"Subject: Sale\n\nPremium subscription offer, expires {d}!", source="email",
                chat="watch-promo", name="Shop", sent=at(-24), is_bulk=True)
        await watcher.renewals(s, at().astimezone(timezone.utc))
        got = s.exec(select(Alert).where(Alert.kind == "renewal")).all()
        assert [a.key for a in got] == [f"renewmsg:{mine.id}"]
        ups = watcher.upcoming_renewals(s, at().astimezone(timezone.utc))
        assert any("Domain renewal" in what for _, what, _ in ups)


# ---------------------------------------------------------------- who is waiting

def _person(s, name, summary=None, sender=None):
    e = graph.upsert_entity(s, name=name, kind="person", summary=summary, source="manual",
                            aliases=[sender] if sender else [])
    s.commit()
    return e


async def test_professor_waiting_rings_and_stranger_notifies():
    with Session(engine) as s:
        _person(s, "WT Kumar", summary="his project guide, professor at CEG")
        prof = message(s, "Did you finish the report?", name="WT Kumar", sender="wt-1@s.whatsapp.net")
        stranger = message(s, "are you coming?", name="WT Somebody", sender="wt-2@s.whatsapp.net")
        assert watcher.close_relation(s, prof) == "guide"
        assert watcher.close_relation(s, stranger) is None
        tp = task(s, "Answer Kumar", kind=TaskKind.question, src=prof)
        ts = task(s, "Answer somebody", kind=TaskKind.question, src=stranger)
        await notify.unanswered_question(s, tp, prof, 30)
        await notify.unanswered_question(s, ts, stranger, 30)
        assert task_alerts(s, tp)[0].level == alerts.ALARM
        assert task_alerts(s, ts)[0].level == alerts.NOTIFY


def test_close_relation_by_edge_vip_and_never_fuzzy(monkeypatch):
    with Session(engine) as s:
        o = graph.ensure_owner(s)
        mum = _person(s, "WT Mary", sender="wt-mum@s.whatsapp.net")
        graph.link(s, mum, "parent_of", o)
        s.commit()
        by_alias = message(s, "where are you?", name="Mummy", sender="wt-mum@s.whatsapp.net")
        assert watcher.close_relation(s, by_alias) == "parent of"
        # a similar name is not the same person
        _person(s, "WT Kumaran", summary="his professor")
        assert watcher.close_relation(s, message(s, "hi?", name="WT Kumar", sender="wt-9@s.whatsapp.net")) is None
        monkeypatch.setattr(settings, "watch_vip_chats", "wt-boss@s.whatsapp.net")
        vip = message(s, "call me", name="Boss", sender="wt-boss@s.whatsapp.net")
        assert watcher.close_relation(s, vip) == "on your VIP list"


# ---------------------------------------------------------------- health

async def test_whatsapp_silent_while_it_normally_flows():
    with Session(engine) as s:
        last = at(-13).astimezone(timezone.utc)
        for i in range(25):
            message(s, f"msg {i}", received=last - timedelta(hours=i * 5))
        await watcher.health(s, at().astimezone(timezone.utc))
        await watcher.health(s, at(1).astimezone(timezone.utc))  # once a day
        got = s.exec(select(Alert).where(Alert.kind == "health")).all()
        assert len(got) == 1 and "WhatsApp" in got[0].title


async def test_whatsapp_quiet_but_rarely_used_is_fine():
    with Session(engine) as s:
        message(s, "lonely", received=at(-20).astimezone(timezone.utc))
        await watcher.health(s, at().astimezone(timezone.utc))
        assert not s.exec(select(Alert).where(Alert.kind == "health")).all()


async def test_gmail_errors_recorded_and_alerted_once_persistent():
    with Session(engine) as s:
        t0 = at(-1).astimezone(timezone.utc)
        gmail.record_poll(s, "college", "RefreshError: invalid_grant", now=t0)
        await watcher.health(s, at(-0.9).astimezone(timezone.utc))
        gm = select(Alert).where(Alert.kind == "health", Alert.key.startswith("health:gmail"))  # type: ignore[union-attr]
        assert not s.exec(gm).all()  # one blip: nothing
        gmail.record_poll(s, "college", "RefreshError: invalid_grant", now=t0 + timedelta(minutes=5))
        gmail.record_poll(s, "college", "RefreshError: invalid_grant", now=t0 + timedelta(minutes=10))
        info = json.loads(s.get(Note, "gmail_error_college").value)
        assert info["count"] == 3 and info["since"] == t0.isoformat()
        await watcher.health(s, at().astimezone(timezone.utc))
        [a] = s.exec(gm).all()
        assert "college" in a.title and "invalid_grant" in a.body
        gmail.record_poll(s, "college", None)
        assert s.get(Note, "gmail_error_college") is None


async def test_poll_all_records_failures(monkeypatch):
    monkeypatch.setattr(gmail, "accounts", lambda: ["wtacct"])

    class Broken:
        def users(self):
            raise RuntimeError("token revoked")

    monkeypatch.setitem(gmail._service_override, "wtacct", Broken())
    await gmail.poll_all()
    with Session(engine) as s:
        assert "token revoked" in json.loads(s.get(Note, "gmail_error_wtacct").value)["error"]


# ---------------------------------------------------------------- escalation to a call

def _alarm(s, kind="deadline", sent=None, task_id=None, title="⏳ Within the hour: Submit form"):
    a = Alert(level=alerts.ALARM, kind=kind, title=title, status="sent", channel="none",
              sent_at=(sent or at(-0.2)).astimezone(timezone.utc), created_at=(sent or at(-0.2)), task_id=task_id)
    s.add(a)
    s.commit()
    s.refresh(a)
    return a


async def test_unanswered_alarm_becomes_a_call():
    with Session(engine) as s:
        t = task(s, "Submit form", due=at(0.5))
        a = _alarm(s, task_id=t.id)
        fresh = _alarm(s, sent=at(-0.02))  # two minutes old: not yet
        assert await watcher.escalate(s, at().astimezone(timezone.utc)) == 1
        [c] = s.exec(select(Alert).where(Alert.level == alerts.CALL)).all()
        assert c.key == f"call:{a.id}" and c.task_id == t.id and c.kind == "call"
        assert "Submit form" in c.body and len(c.body) < 300
        assert await watcher.escalate(s, at(minutes=5).astimezone(timezone.utc)) == 1  # the fresh one now
        assert s.exec(select(Alert).where(Alert.key == f"call:{fresh.id}")).first()


async def test_answered_alarms_do_not_call_and_calls_are_capped(monkeypatch):
    monkeypatch.setattr(settings, "calls_per_day", 2)
    with Session(engine) as s:
        acked = _alarm(s)
        alerts.ack(s, acked, "done")
        snoozed = _alarm(s)
        alerts.ack(s, snoozed, "snooze")
        for _ in range(3):
            _alarm(s)
        assert await watcher.escalate(s, at().astimezone(timezone.utc)) == 2
        assert await watcher.escalate(s, at(minutes=10).astimezone(timezone.utc)) == 0  # cap reached
        assert len(s.exec(select(Alert).where(Alert.level == alerts.CALL)).all()) == 2


async def test_quiet_hours_call_only_for_deadlines():
    night = BASE.replace(hour=23, minute=40)
    with Session(engine) as s:
        q = _alarm(s, kind="question", sent=night - timedelta(minutes=10), title="❓ Mum is waiting")
        d = _alarm(s, kind="bill", sent=night - timedelta(minutes=10))
        assert await watcher.escalate(s, night.astimezone(timezone.utc)) == 1
        assert s.exec(select(Alert).where(Alert.key == f"call:{d.id}")).one().status == "sent"  # rings at night
        assert not s.exec(select(Alert).where(Alert.key == f"call:{q.id}")).first()


# ---------------------------------------------------------------- the model's yes/no

async def test_judge_says_no_records_silently(fake_llm):
    fake_llm.script["Collect parcel"] = {"interrupt": False, "reason": "can wait"}
    with Session(engine) as s:
        t = task(s, "Collect parcel", due=at(3))
        await watcher.run(s, now=at())
        [a] = task_alerts(s, t)
        assert a.level == alerts.SILENT and a.status == "done" and a.sent_at is None
        await watcher.run(s, now=at(minutes=5))
        assert len(task_alerts(s, t)) == 1  # not asked again
    assert any("interrupting" in c[-1]["content"] or "interrupt" in c[-1]["content"] for c in fake_llm.calls)


async def test_judge_no_lowers_an_alarm_to_a_notification(fake_llm):
    fake_llm.script["Board the train"] = {"interrupt": "false"}
    with Session(engine) as s:
        t = task(s, "Board the train", due=at(0.5))
        await watcher.run(s, now=at())
        assert task_alerts(s, t)[0].level == alerts.NOTIFY


async def test_judge_timeout_or_off_keeps_the_rule(fake_llm, monkeypatch):
    class Slow:
        async def complete(self, *a, **k):
            await asyncio.sleep(1)
            return '{"interrupt": false}'

    from app import llm as llm_mod
    monkeypatch.setattr(llm_mod.LLM, "override", Slow())
    monkeypatch.setattr(watcher, "JUDGE_TIMEOUT", 0.05)
    assert await watcher.judged_level(alerts.NOTIFY, "anything", "") == alerts.NOTIFY

    monkeypatch.setattr(llm_mod.LLM, "override", fake_llm)
    fake_llm.script["anything"] = {"interrupt": False}
    monkeypatch.setattr(settings, "watch_judge", False)
    n = len(fake_llm.calls)
    assert await watcher.judged_level(alerts.NOTIFY, "anything", "") == alerts.NOTIFY
    assert len(fake_llm.calls) == n


# ---------------------------------------------------------------- learning from "Not important"

async def test_three_dismissals_from_a_chat_lower_that_kind():
    with Session(engine) as s:
        for i in range(3):
            old = task(s, f"old {i}", chat="watch-noisy")
            s.add(Alert(level=2, kind="deadline", title="x", task_id=old.id, status="dismissed", ack="dismiss",
                        acked_at=at(-24 * 3).astimezone(timezone.utc)))
        s.commit()
        noisy = task(s, "Noisy group deadline", due=at(4), chat="watch-noisy")
        other = task(s, "Other chat deadline", due=at(4), chat="watch-calm")
        await watcher.deadlines(s, at().astimezone(timezone.utc))
        assert task_alerts(s, noisy)[0].level == alerts.BRIEF
        assert task_alerts(s, other)[0].level == alerts.NOTIFY
        # older than 30 days does not count
        assert watcher.learned_level(s, "deadline", "watch-noisy", alerts.NOTIFY,
                                     now=at(24 * 40).astimezone(timezone.utc)) == alerts.NOTIFY
        # never below silent
        assert watcher.learned_level(s, "deadline", "watch-noisy", alerts.SILENT,
                                     now=at().astimezone(timezone.utc)) == alerts.SILENT


async def test_learning_applies_to_questions_too():
    with Session(engine) as s:
        for i in range(3):
            old = task(s, f"q {i}", chat="watch-chatty", kind=TaskKind.question)
            s.add(Alert(level=2, kind="question", title="x", task_id=old.id, status="dismissed", ack="dismiss",
                        acked_at=datetime.now(timezone.utc) - timedelta(days=1)))
        s.commit()
        src = message(s, "u there?", chat="watch-chatty", name="WT Chatty", sender="wt-chatty@s.whatsapp.net")
        t = task(s, "Reply", kind=TaskKind.question, chat="watch-chatty", src=src)
        await notify.unanswered_question(s, t, src, 25)
        assert task_alerts(s, t)[0].level == alerts.BRIEF


async def test_night_deadline_calls_straight_away_for_accepted_tasks_only():
    night = BASE.replace(hour=23, minute=30)
    with Session(engine) as s:
        acc = task(s, "Upload the thesis", due=night + timedelta(minutes=40))
        card = task(s, "Maybe a party", due=night + timedelta(minutes=40), status=TaskStatus.pending_confirmation)
        await watcher.deadlines(s, night.astimezone(timezone.utc))
        [c] = task_alerts(s, acc)
        assert (c.level, c.status, c.key) == (alerts.CALL, "sent", f"risk1:{acc.id}")
        assert "Upload the thesis" in c.body
        [p] = task_alerts(s, card)
        assert (p.level, p.status) == (alerts.ALARM, "pending")  # held until the morning


async def test_stale_alarms_and_passed_deadlines_do_not_call():
    with Session(engine) as s:
        _alarm(s, sent=at(-3))  # missed hours ago
        gone = task(s, "Was due", due=at(-0.1))
        _alarm(s, task_id=gone.id)
        assert await watcher.escalate(s, at().astimezone(timezone.utc)) == 0
