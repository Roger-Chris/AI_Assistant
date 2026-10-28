"""Alerts: quiet hours, delivery to the hub (Telegram as fallback), and your answers."""
import json
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import httpx
import pytest
from sqlmodel import Session, select

from app import alerts, notify
from app.config import settings
from app.db import Alert, Task, TaskKind, TaskStatus, engine

IST = ZoneInfo("Asia/Kolkata")
AUTH = {"Authorization": "Bearer test-token"}


def ist(h, m=0, day=29):
    return datetime(2026, 10, day, h, m, tzinfo=IST)


@pytest.fixture(autouse=True)
def clean(monkeypatch):
    monkeypatch.setattr(settings, "quiet_hours", "23:00-07:00")
    yield
    alerts.hub_transport = None
    with Session(engine) as s:
        for a in s.exec(select(Alert)).all():
            s.delete(a)
        s.commit()


@pytest.fixture
def hub(monkeypatch):
    """A fake Orchestrator hub; set .up = False to make it refuse."""
    class Hub:
        up = True
        got: list[dict] = []
    h = Hub()
    h.got = []

    def handle(req: httpx.Request) -> httpx.Response:
        assert req.url.path == "/api/notify"
        if not h.up:
            return httpx.Response(503)
        h.got.append(json.loads(req.content))
        return httpx.Response(200, json={"ok": True})

    monkeypatch.setattr(settings, "hub_url", "http://hub:4317")
    alerts.hub_transport = httpx.MockTransport(handle)
    return h


def test_quiet_hours_window():
    assert alerts.quiet_until(ist(12)) is None
    assert alerts.quiet_until(ist(23, 30)) == ist(7, 0, day=30).astimezone(timezone.utc)
    assert alerts.quiet_until(ist(6, 59)) == ist(7).astimezone(timezone.utc)
    assert alerts.quiet_until(ist(7)) is None


def test_no_quiet_hours(monkeypatch):
    monkeypatch.setattr(settings, "quiet_hours", "")
    assert alerts.quiet_until(ist(2)) is None


async def test_alert_goes_to_the_hub(hub):
    with Session(engine) as s:
        a = await alerts.raise_alert(s, alerts.NOTIFY, "question", "Jayesh is waiting", "send it?", now=ist(10))
        assert a.status == "sent" and a.channel == "hub"
    assert hub.got[0]["title"] == "Jayesh is waiting" and hub.got[0]["level"] == 2 and hub.got[0]["id"] == a.id


async def test_hub_down_retries_later(hub):
    hub.up = False
    with Session(engine) as s:
        a = await alerts.raise_alert(s, alerts.NOTIFY, "overdue", "2 overdue", now=ist(10))
        assert a.status == "pending"
        hub.up = True
        assert await alerts.deliver_due(s, now=ist(10, 1)) == 1
        s.refresh(a)
        assert a.status == "sent"


async def test_quiet_hours_hold_until_morning(hub):
    with Session(engine) as s:
        a = await alerts.raise_alert(s, alerts.NOTIFY, "question", "late question", now=ist(23, 30))
        assert a.status == "pending" and not hub.got
        assert await alerts.deliver_due(s, now=ist(23, 45)) == 0
        assert await alerts.deliver_due(s, now=ist(7, 1, day=30)) == 1
    assert hub.got[0]["title"] == "late question"


async def test_a_call_ignores_quiet_hours(hub):
    with Session(engine) as s:
        a = await alerts.raise_alert(s, alerts.CALL, "deadline", "due in 1 hour", now=ist(23, 30))
        assert a.status == "sent"


async def test_same_key_is_not_raised_twice(hub):
    with Session(engine) as s:
        a = await alerts.raise_alert(s, alerts.NOTIFY, "question", "waiting", key="question:99", now=ist(10))
        b = await alerts.raise_alert(s, alerts.NOTIFY, "question", "still waiting", key="question:99", now=ist(10, 5))
        assert a.id == b.id
    assert len(hub.got) == 1


async def test_supersede_closes_yesterdays_brief(hub):
    with Session(engine) as s:
        old = await alerts.raise_alert(s, alerts.BRIEF, "brief", "Brief 1", key="brief:1", supersede=True, now=ist(8))
        new = await alerts.raise_alert(s, alerts.BRIEF, "brief", "Brief 2", key="brief:2", supersede=True, now=ist(8, day=30))
        s.refresh(old)
        assert old.status == "done"
        assert [a.id for a in alerts.open_alerts(s)] == [new.id]


async def test_answers_done_snooze_dismiss(hub):
    with Session(engine) as s:
        t = Task(title="Send the report", kind=TaskKind.task, status=TaskStatus.open)
        s.add(t)
        s.commit()
        s.refresh(t)
        a = await alerts.raise_alert(s, alerts.NOTIFY, "new_task", "Send the report", task_id=t.id, now=ist(10))

        alerts.ack(s, a, "snooze", now=ist(10, 5))
        assert a.status == "pending" and a not in alerts.open_alerts(s)  # hidden while snoozed
        assert await alerts.deliver_due(s, now=ist(10, 30)) == 0
        assert await alerts.deliver_due(s, now=ist(11, 6)) == 1  # back after snooze_minutes
        assert a in alerts.open_alerts(s)

        alerts.ack(s, a, "done", now=ist(11, 10))
        s.refresh(t)
        assert a.status == "done" and t.status == TaskStatus.done  # done finishes the task too

        b = await alerts.raise_alert(s, alerts.NOTIFY, "overdue", "noise", now=ist(12))
        alerts.ack(s, b, "dismiss")
        assert b.status == "dismissed" and b.ack == "dismiss"


async def test_without_any_channel_it_is_kept_for_the_app():
    with Session(engine) as s:
        a = await alerts.raise_alert(s, alerts.NOTIFY, "question", "nowhere to send", now=ist(10))
        assert a.status == "sent" and a.channel == "none"
        assert a in alerts.open_alerts(s)


def test_alerts_api(client, hub):
    with Session(engine) as s:
        s.add(Alert(level=2, kind="question", title="api alert", status="sent"))
        s.commit()
    assert client.get("/alerts", headers={"Authorization": "Bearer nope"}).status_code == 401
    listed = client.get("/alerts", headers=AUTH).json()
    a = next(x for x in listed if x["title"] == "api alert")
    r = client.post(f"/alerts/{a['id']}/ack", json={"action": "dismiss"}, headers=AUTH)
    assert r.status_code == 200 and r.json()["status"] == "dismissed"
    assert client.post(f"/alerts/{a['id']}/ack", json={"action": "explode"}, headers=AUTH).status_code == 422
    assert client.post("/alerts/999999/ack", json={"action": "done"}, headers=AUTH).status_code == 404
    assert not [x for x in client.get("/alerts", headers=AUTH).json() if x["title"] == "api alert"]


def test_finishing_the_task_closes_its_alert(client):
    with Session(engine) as s:
        t = Task(title="Pay the fee", kind=TaskKind.task, status=TaskStatus.open)
        s.add(t)
        s.commit()
        s.refresh(t)
        s.add(Alert(level=2, kind="new_task", title="Pay the fee", task_id=t.id, status="sent"))
        s.commit()
        tid = t.id
    client.post(f"/tasks/{tid}/done", headers=AUTH)
    with Session(engine) as s:
        assert s.exec(select(Alert).where(Alert.task_id == tid)).one().status == "done"


def test_only_urgent_or_soon_new_tasks_interrupt():
    now = datetime.now(timezone.utc)
    assert notify.new_task_level(Task(title="x", priority="normal", due_at=now + timedelta(days=3))) == alerts.SILENT
    assert notify.new_task_level(Task(title="x", priority="normal")) == alerts.SILENT
    assert notify.new_task_level(Task(title="x", priority="normal", due_at=now + timedelta(hours=2))) == alerts.NOTIFY
    assert notify.new_task_level(Task(title="x", priority="high")) == alerts.NOTIFY
