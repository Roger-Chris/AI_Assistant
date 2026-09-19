"""The scenario: someone asks for a file, I agree, both say ok, then I send it."""
import base64
import time

import pytest

from app import perceive as perceive_mod
from tests.test_pipeline import wa

CHAT = {"chat_id": "life@s.whatsapp.net", "chat_name": "Jayesh L", "sender_id": "life@s.whatsapp.net"}


def me(text, **kw):
    return wa(text, from_me=True, sender_name="me", **{**CHAT, **kw})


def them(text, **kw):
    return wa(text, from_me=False, sender_name="Jayesh L", **{**CHAT, **kw})


def my_tasks(client):
    return [t for t in client.get("/tasks").json() if t.get("source_chat") == "Jayesh L"]


def test_request_commit_ok_send(client, fake_llm):
    # 1. the request -> new task
    fake_llm.script["send me the sih report"] = {
        "action": "new", "kind": "task", "title": "Send SIH report to Jayesh", "confidence": 0.9}
    client.post("/ingest/whatsapp", json=them("bro can you send me the SIH report"))
    tasks = my_tasks(client)
    assert len(tasks) == 1 and tasks[0]["status"] == "pending_confirmation"
    tid = tasks[0]["id"]

    # 2. my commitment -> UPDATE (adds due), not a second card. The model was shown the open task.
    fake_llm.script["10 mins"] = {"action": "update", "task_id": tid, "kind": "task",
                                  "due_at": "2026-09-18T21:10:00+05:30", "confidence": 0.85}
    client.post("/ingest/whatsapp", json=me("I'll send in 10 mins"))
    assert f"#{tid} [task, pending_confirmation] Send SIH report" in fake_llm.calls[-1][-1]["content"]
    tasks = my_tasks(client)
    assert len(tasks) == 1
    assert tasks[0]["due_at"].startswith("2026-09-18T21:10")

    # 3. their "ok" -> prefilter, no model call
    n = len(fake_llm.calls)
    r = client.post("/ingest/whatsapp", json=them("ok"))
    assert r.json()["skipped"] == "chitchat"
    assert len(fake_llm.calls) == n

    # 4. my "ok" -> passes (open task in chat, from me) but the model says none
    fake_llm.default = {"action": "none", "kind": "noise", "confidence": 0.9}
    r = client.post("/ingest/whatsapp", json=me("ok"))
    assert "skipped" not in r.json()
    assert len(my_tasks(client)) == 1

    # 5. I send the file -> COMPLETE
    fake_llm.script["sent a document"] = {"action": "complete", "task_id": tid, "confidence": 0.9}
    r = client.post("/ingest/whatsapp", json=me("", media_type="document"))
    assert "skipped" not in r.json()
    assert my_tasks(client) == []
    done = [t for t in client.get("/tasks?status=done").json() if t["id"] == tid]
    assert len(done) == 1


def test_my_document_without_open_task_is_not_sent_to_model(client, fake_llm):
    n = len(fake_llm.calls)
    r = client.post("/ingest/whatsapp", json=me("", media_type="document", chat_id="other@s.whatsapp.net",
                                                 sender_id="other@s.whatsapp.net", chat_name="Other"))
    assert r.json()["skipped"] == "media-from-me-no-open-task"
    assert len(fake_llm.calls) == n


@pytest.mark.asyncio
async def test_voice_note_is_transcribed_then_classified(client, fake_llm, monkeypatch):
    async def fake_perceive(session, kind, mime, data):
        assert kind == "audio" and data == b"OggS-fake"
        return "machan submit the form by tomorrow evening"
    monkeypatch.setattr(perceive_mod, "perceive", fake_perceive)
    fake_llm.script["submit the form"] = {"action": "new", "kind": "task", "title": "Submit the form",
                                          "due_at": "2026-09-19T18:00:00+05:30", "confidence": 0.9}
    r = client.post("/ingest/whatsapp", json=them("", media_type="audio", media_mime="audio/ogg; codecs=opus",
                                                   media_b64=base64.b64encode(b"OggS-fake").decode(),
                                                   chat_id="vn@s.whatsapp.net", sender_id="vn@s.whatsapp.net",
                                                   chat_name="Priya"))
    assert r.json()["perceiving"] == "audio"
    tasks = [t for t in client.get("/tasks").json() if t.get("source_chat") == "Priya"]
    assert len(tasks) == 1 and tasks[0]["title"] == "Submit the form"
    assert tasks[0]["source_text"].startswith("[voice note] machan submit")


@pytest.mark.asyncio
async def test_meme_image_is_dropped_without_model_call(client, fake_llm, monkeypatch):
    async def fake_perceive(session, kind, mime, data):
        return "[no actionable content]"
    monkeypatch.setattr(perceive_mod, "perceive", fake_perceive)
    n = len(fake_llm.calls)
    client.post("/ingest/whatsapp", json=them("", media_type="image", media_mime="image/jpeg",
                                               media_b64=base64.b64encode(b"\xff\xd8fake").decode(),
                                               chat_id="img@s.whatsapp.net", sender_id="img@s.whatsapp.net",
                                               chat_name="Memes"))
    assert len(fake_llm.calls) == n
    _ = time


def test_self_chat_note_becomes_task(client, fake_llm):
    fake_llm.script["send me all the files"] = {"action": "new", "kind": "task", "title": "Send SH files",
                                                "due_at": "2026-09-18T18:00:00+05:30", "confidence": 0.9}
    r = client.post("/ingest/whatsapp", json=wa("[voice note] send me all the files related to SH today",
                                                 from_me=True, is_self=True, chat_id="me@s.whatsapp.net",
                                                 sender_id="me@s.whatsapp.net", chat_name="Me (notes to self)",
                                                 media_type="audio"))
    assert "skipped" not in r.json()
    assert "notes to self" in fake_llm.calls[-1][-1]["content"]
    assert [t for t in client.get("/tasks").json() if t["title"] == "Send SH files"]


def test_question_is_watched_then_surfaced_or_closed(client, fake_llm, monkeypatch):
    import asyncio
    from datetime import timedelta

    from sqlmodel import Session, select

    from app import scheduler as sched
    from app.db import Task, TaskStatus, engine, utcnow
    sent = []
    async def fake_send(text, session=None, buttons=None, priority="normal"):
        sent.append(text)
    monkeypatch.setattr(sched, "unanswered_question",
                        lambda session, t, src, m: fake_send(f"waiting:{t.title}"))
    q = {"chat_id": "q@s.whatsapp.net", "sender_id": "q@s.whatsapp.net", "chat_name": "Julie", "sender_name": "Julie"}
    fake_llm.script["bus boarded"] = {"action": "new", "kind": "question", "title": "Reply to Julie: bus boarded?",
                                      "confidence": 0.9}
    r = client.post("/ingest/whatsapp", json=wa("Bus boarded ?", **q))
    assert "skipped" not in r.json()
    # watched, not surfaced, not in /tasks
    with Session(engine) as s:
        t = s.exec(select(Task).where(Task.chat_id == "q@s.whatsapp.net")).one()
        assert t.status == TaskStatus.watching
        tid = t.id
    assert not [x for x in client.get("/tasks").json() if x["id"] == tid]
    # not yet past grace -> sweep does nothing
    asyncio.run(sched.unanswered_sweep())
    assert sent == []
    # age it past grace with no reply -> surfaced
    with Session(engine) as s:
        t = s.get(Task, tid)
        t.created_at = utcnow() - timedelta(minutes=30)
        s.add(t)
        s.commit()
    asyncio.run(sched.unanswered_sweep())
    assert sent == ["waiting:Reply to Julie: bus boarded?"]
    with Session(engine) as s:
        assert s.get(Task, tid).status == TaskStatus.open

    # a second question, answered by my reply -> closed silently
    fake_llm.script["where are you"] = {"action": "new", "kind": "question", "title": "Reply: where are you", "confidence": 0.9}
    client.post("/ingest/whatsapp", json=wa("where are you?", **q))
    fake_llm.default = {"action": "none", "kind": "noise", "confidence": 0.9}
    client.post("/ingest/whatsapp", json=wa("on the bus, 10 min", from_me=True, **{**q, "sender_id": "me@s.whatsapp.net", "sender_name": "me"}))
    with Session(engine) as s:
        t2 = s.exec(select(Task).where(Task.title == "Reply: where are you")).one()
        assert t2.status == TaskStatus.done


def test_classifier_tolerates_null_fields(client, fake_llm):
    """Gemini sometimes returns title/details/people as null — must not crash the message."""
    fake_llm.script["null fields probe"] = {"action": "none", "kind": "noise", "title": None,
                                            "details": None, "people": None, "priority": None,
                                            "confidence": 0.9, "reason": None}
    r = client.post("/ingest/whatsapp", json=wa("null fields probe please", chat_id="nul@s.whatsapp.net",
                                                 sender_id="nul@s.whatsapp.net", chat_name="Nul"))
    assert "skipped" not in r.json()
    from sqlmodel import Session, select

    from app.db import Message, engine
    with Session(engine) as s:
        m = s.exec(select(Message).where(Message.chat_id == "nul@s.whatsapp.net")).one()
        assert m.classified and "error" not in (m.classification or "")


def test_event_already_past_is_not_created(client, fake_llm):
    fake_llm.script["webinar yesterday"] = {"action": "new", "kind": "event", "title": "Attend the webinar",
                                            "due_at": "2020-01-01T10:00:00+05:30", "confidence": 0.95}
    client.post("/ingest/whatsapp", json=wa("webinar yesterday, join us", chat_id="past@s.whatsapp.net",
                                             sender_id="past@s.whatsapp.net", chat_name="Past"))
    assert not [t for t in client.get("/tasks").json() if t["title"] == "Attend the webinar"]
