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
