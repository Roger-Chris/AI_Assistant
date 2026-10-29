"""Background work: weekly review, monthly noise report, Gmail draft replies (never sent)."""
import base64
from datetime import date, datetime, timedelta, timezone
from email import message_from_bytes
from zoneinfo import ZoneInfo

import pytest
from sqlmodel import Session, select

from app import alerts, gmail, review
from app.config import settings
from app.db import Alert, Message, Note, Task, TaskKind, TaskStatus, engine

IST = ZoneInfo("Asia/Kolkata")
BASE = (datetime.now(IST) + timedelta(days=30)).replace(hour=18, minute=0, second=0, microsecond=0)


@pytest.fixture(autouse=True)
def clean(monkeypatch):
    monkeypatch.setattr(settings, "quiet_hours", "23:00-07:00")
    monkeypatch.setattr(settings, "hub_url", "")
    _wipe()
    yield
    _wipe()
    gmail._service_override.pop("wtrev", None)


def _wipe():
    with Session(engine) as s:
        for a in s.exec(select(Alert)).all():
            s.delete(a)
        s.commit()
        for t in s.exec(select(Task).where(Task.chat_id.startswith("rev-"))).all():  # type: ignore[union-attr]
            s.delete(t)
        s.commit()
        for m in s.exec(select(Message).where(Message.chat_id.startswith("rev-"))).all():  # type: ignore[union-attr]
            s.delete(m)
        for n in s.exec(select(Note).where(Note.key.startswith("gmail_draft_"))).all():  # type: ignore[attr-defined]
            s.delete(n)
        s.commit()


def task(s, title, due=None, status=TaskStatus.open, kind=TaskKind.task, src=None, updated=None, created=None):
    t = Task(title=title, due_at=due, status=status, kind=kind, chat_id="rev-chat",
             source_message_id=src.id if src else None)
    if updated:
        t.updated_at = updated
    if created:
        t.created_at = created
    s.add(t)
    s.commit()
    s.refresh(t)
    return t


def utc(dt):
    return dt.astimezone(timezone.utc)


# ---------------------------------------------------------------- weekly

async def test_weekly_review_by_the_planner(fake_llm):
    fake_llm.raw = "Done: lab record. Slipped: fee receipt. Next week: viva Tuesday."
    with Session(engine) as s:
        task(s, "Rev lab record", status=TaskStatus.done, updated=utc(BASE - timedelta(days=2)))
        task(s, "Rev fee receipt", due=utc(BASE - timedelta(days=1)))
        task(s, "Rev viva", due=utc(BASE + timedelta(days=3)))
        task(s, "Rev renew library card", due=utc(BASE + timedelta(days=4)))
        a = await review.weekly(s, now=utc(BASE))
        assert (a.kind, a.level, a.status) == ("weekly", alerts.BRIEF, "sent")
        assert a.body.startswith("Done: lab record")
    prompt = fake_llm.calls[-1][-1]["content"]
    for bit in ("Rev lab record", "Rev fee receipt", "Rev viva", "Rev renew library card"):
        assert bit in prompt
    assert prompt.index("Rev fee receipt") < prompt.index("NEXT 7 DAYS")  # it slipped


async def test_weekly_review_falls_back_to_a_list(fake_llm, monkeypatch):
    async def down(*a, **k):
        raise RuntimeError("planner down")

    monkeypatch.setattr(fake_llm, "complete", down)
    with Session(engine) as s:
        task(s, "Rev viva", due=utc(BASE + timedelta(days=3)))
        old = await review.weekly(s, now=utc(BASE - timedelta(days=7)))
        a = await review.weekly(s, now=utc(BASE))
        assert "planner offline" in a.body and "Rev viva" in a.body
        s.refresh(old)
        assert old.status == "done"  # last week's review is superseded


# ---------------------------------------------------------------- monthly

def _sent(s, level, kind, ack, when):
    s.add(Alert(level=level, kind=kind, title="x", status="done" if ack else "sent", ack=ack,
                created_at=when, sent_at=when))


async def test_monthly_noise_report():
    first = BASE.replace(day=1, hour=9)
    last_month = (first - timedelta(days=10)).replace(hour=12)
    with Session(engine) as s:
        for _ in range(4):
            _sent(s, alerts.NOTIFY, "deadline", "done", last_month)
        _sent(s, alerts.NOTIFY, "question", "dismiss", last_month)
        _sent(s, alerts.ALARM, "bill", None, last_month)
        _sent(s, alerts.CALL, "call", "done", last_month)
        _sent(s, alerts.BRIEF, "brief", None, last_month)
        _sent(s, alerts.NOTIFY, "deadline", "dismiss", first + timedelta(hours=1))  # this month: not counted
        s.add(Alert(level=0, kind="deadline", title="silent", status="done", created_at=last_month))
        s.commit()
        start, end, month = review._last_month(utc(first))
        st = review.month_stats(s, start, end)
        assert st["per_level"] == {1: 1, 2: 5, 3: 1, 4: 1}
        assert (st["interruptions"], st["done"], st["dismissed"], st["calls"]) == (7, 5, 1, 1)
        assert round(st["dismiss_rate"], 3) == round(1 / 7, 3)
        a = await review.monthly(s, now=utc(first))
        assert a.kind == "monthly" and a.level == alerts.BRIEF
        assert "Dismiss rate 14%" in a.body and "Within targets" in a.body and f"{month:%B}" in a.title


def test_month_text_flags_noise():
    st = {"per_level": {1: 0, 2: 100, 3: 10, 4: 12}, "interruptions": 122, "done": 50, "dismissed": 40,
          "unanswered": 32, "dismiss_rate": 40 / 122, "calls": 12, "days": 30, "per_day": 122 / 30,
          "calls_per_week": 12 / (30 / 7), "by_kind": {"deadline": 80}}
    text = review.month_text(st, date(2026, 10, 1))
    assert "too many interruptions" in text and "too many calls" in text and "Not important" in text


# ---------------------------------------------------------------- Gmail drafts

class Req:
    def __init__(self, v):
        self.v = v

    def execute(self):
        return self.v


class FakeGmail:
    """messages.get + drafts.create only. There is no send here: calling it would fail the test."""

    def __init__(self):
        self.drafts_made: list[dict] = []

    def users(self):
        return self

    def messages(self):
        return self

    def drafts(self):
        return self

    def get(self, userId, id, format, metadataHeaders=None):
        return Req({"id": id, "threadId": "thread-77", "payload": {"headers": [
            {"name": "From", "value": "Dr Rao <rao@college.edu>"},
            {"name": "Subject", "value": "Project review slot"},
            {"name": "Message-ID", "value": "<abc@college.edu>"}]}})

    def create(self, userId, body):
        self.drafts_made.append(body)
        return Req({"id": f"draft-{len(self.drafts_made)}"})


def _mail(s, text, bulk=False, sender="rao@college.edu"):
    m = Message(source="email", external_id=f"gmail:wtrev:{sender}-{text[:5]}", chat_id=f"rev-email:{sender}",
                sender_id=sender, sender_name="Dr Rao", text=text, is_bulk=bulk,
                sent_at=datetime.now(timezone.utc), classified=True)
    s.add(m)
    s.commit()
    s.refresh(m)
    return m


async def test_drafts_off_by_default():
    assert settings.drafts_enabled is False
    with Session(engine) as s:
        assert await review.email_drafts(s) == 0


async def test_draft_reply_saved_in_the_thread_never_sent(fake_llm, monkeypatch):
    monkeypatch.setattr(settings, "drafts_enabled", True)
    fake = FakeGmail()
    gmail._service_override["wtrev"] = fake
    fake_llm.raw = "Dear Dr Rao,\n\nThursday 3 pm works for me. [confirm room]\n\nKishaunjith"
    with Session(engine) as s:
        q = _mail(s, "Subject: Project review slot\n\nCan you come on Thursday at 3?")
        t = task(s, "Reply to Dr Rao about the review slot", kind=TaskKind.question,
                 status=TaskStatus.watching, src=q)
        bulk = _mail(s, "Subject: Survey\n\nWill you rate us?", bulk=True, sender="noreply@shop.com")
        task(s, "Rate the shop", kind=TaskKind.question, status=TaskStatus.watching, src=bulk)
        assert await review.email_drafts(s) == 1
        assert await review.email_drafts(s) == 0  # one draft per question
        assert s.get(Note, f"gmail_draft_{t.id}").value == "draft-1"
    [d] = fake.drafts_made
    assert d["message"]["threadId"] == "thread-77"
    mail = message_from_bytes(base64.urlsafe_b64decode(d["message"]["raw"]))
    assert mail["To"] == "Dr Rao <rao@college.edu>" and mail["Subject"] == "Re: Project review slot"
    assert mail["In-Reply-To"] == "<abc@college.edu>"
    assert "Thursday 3 pm" in mail.get_payload(decode=True).decode()
    assert not hasattr(fake, "send")


async def test_no_draft_when_the_planner_fails(fake_llm, monkeypatch):
    monkeypatch.setattr(settings, "drafts_enabled", True)
    fake = FakeGmail()
    gmail._service_override["wtrev"] = fake

    async def down(*a, **k):
        raise RuntimeError("planner down")

    monkeypatch.setattr(fake_llm, "complete", down)
    with Session(engine) as s:
        q = _mail(s, "Subject: Hi\n\nAre you free?")
        task(s, "Reply", kind=TaskKind.question, status=TaskStatus.watching, src=q)
        assert await review.email_drafts(s) == 0
    assert not fake.drafts_made
