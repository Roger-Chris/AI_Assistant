"""Calendar mirror against a fake Google service."""
from datetime import datetime, timedelta, timezone

from sqlmodel import Session

from app import gcal
from app.db import Task, TaskKind, TaskStatus, engine


class FakeReq:
    def __init__(self, fn):
        self.fn = fn

    def execute(self):
        return self.fn()


class FakeEvents:
    def __init__(self, store):
        self.store = store
        self.n = 0

    def insert(self, calendarId, body):
        def go():
            self.n += 1
            eid = f"ev{self.n}"
            self.store[eid] = body
            return {"id": eid}
        return FakeReq(go)

    def update(self, calendarId, eventId, body):
        def go():
            self.store[eventId] = body
            return {"id": eventId}
        return FakeReq(go)

    def delete(self, calendarId, eventId):
        return FakeReq(lambda: self.store.pop(eventId, None))

    def list(self, **kw):
        return FakeReq(lambda: {"items": [dict(id=k, summary=v["summary"], start=v["start"],
                                               extendedProperties=v.get("extendedProperties", {}))
                                          for k, v in self.store.items()]})


class FakeService:
    def __init__(self):
        self.store = {}
        self._events = FakeEvents(self.store)

    def calendarList(self):
        return type("CL", (), {"list": lambda s: FakeReq(lambda: {"items": [{"id": "cal1", "summary": "Assistant"}]})})()

    def calendars(self):
        return type("C", (), {"insert": lambda s, body: FakeReq(lambda: {"id": "calX"})})()

    def events(self):
        return self._events


def _cleanup(s: Session, *ids):
    for i in ids:
        t = s.get(Task, i)
        if t:
            s.delete(t)
    s.commit()


def test_sync_creates_updates_deletes(monkeypatch):
    fake = FakeService()
    monkeypatch.setattr(gcal, "_service_override", fake)
    due = datetime(2026, 9, 22, 8, 0, tzinfo=timezone.utc)  # 13:30 IST
    with Session(engine) as s:
        ev = Task(kind=TaskKind.event, status=TaskStatus.pending_confirmation, title="PALS orientation", due_at=due)
        q = Task(kind=TaskKind.question, status=TaskStatus.open, title="Reply to Julie", due_at=due)
        s.add(ev)
        s.add(q)
        s.commit()
        s.refresh(ev)
        s.refresh(q)
        ev_id, q_id = ev.id, q.id

        # pending -> nothing on the calendar
        assert gcal.sync_dirty(s).get("created", 0) == 0
        assert fake.store == {}

        # confirmed -> created, 1 h, IST
        ev = s.get(Task, ev_id)
        ev.status = TaskStatus.open
        ev.updated_at = datetime.now(timezone.utc) + timedelta(seconds=1)
        s.add(ev)
        s.commit()
        assert gcal.sync_dirty(s)["created"] == 1
        body = list(fake.store.values())[0]
        assert body["summary"] == "PALS orientation"
        assert body["start"]["dateTime"].startswith("2026-09-22T13:30")
        assert body["end"]["dateTime"].startswith("2026-09-22T14:30")

        # due moved -> updated in place
        ev = s.get(Task, ev_id)
        ev.due_at = due + timedelta(hours=1)
        ev.updated_at = datetime.now(timezone.utc) + timedelta(seconds=2)
        s.add(ev)
        s.commit()
        assert gcal.sync_dirty(s)["updated"] == 1
        assert list(fake.store.values())[0]["start"]["dateTime"].startswith("2026-09-22T14:30")

        # rejected -> deleted
        ev = s.get(Task, ev_id)
        ev.status = TaskStatus.rejected
        ev.updated_at = datetime.now(timezone.utc) + timedelta(seconds=3)
        s.add(ev)
        s.commit()
        assert gcal.sync_dirty(s)["deleted"] == 1
        assert fake.store == {}
        assert s.get(Task, ev_id).calendar_event_id is None
        # questions never sync
        assert s.get(Task, q_id).calendar_event_id is None
        _cleanup(s, ev_id, q_id)


def test_task_with_due_is_a_30_min_block(monkeypatch):
    fake = FakeService()
    monkeypatch.setattr(gcal, "_service_override", fake)
    with Session(engine) as s:
        t = Task(kind=TaskKind.task, status=TaskStatus.open, title="Submit form",
                 due_at=datetime(2026, 9, 19, 12, 30, tzinfo=timezone.utc))
        s.add(t)
        s.commit()
        s.refresh(t)
        gcal.sync_dirty(s)
        body = [b for b in fake.store.values() if b["summary"] == "Due: Submit form"][0]
        assert body["start"]["dateTime"].startswith("2026-09-19T18:00")
        assert body["end"]["dateTime"].startswith("2026-09-19T18:30")
        _cleanup(s, t.id)
