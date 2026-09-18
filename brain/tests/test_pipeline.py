import time

from app.llm import extract_json


def wa(text, **kw):
    base = {"id": f"m-{time.time_ns()}", "chat_id": "9199@s.whatsapp.net", "chat_name": "Jayesh",
            "sender_id": "9199@s.whatsapp.net", "sender_name": "Jayesh", "from_me": False,
            "is_group": False, "text": text, "timestamp": int(time.time())}
    base.update(kw)
    return base


def test_extract_json_tolerates_fences_and_prose():
    assert extract_json('```json\n{"a": 1}\n```') == {"a": 1}
    assert extract_json('Sure! {"a": 1} hope that helps') == {"a": 1}


def test_auth_required(client):
    r = client.post("/ingest/whatsapp", json=wa("hi"), headers={"Authorization": "Bearer nope"})
    assert r.status_code == 401


def test_noise_creates_no_task(client, fake_llm):
    r = client.post("/ingest/whatsapp", json=wa("good morning 👍"))
    assert r.status_code == 200
    assert client.get("/tasks").json() == []


def test_task_extracted_and_confirmable(client, fake_llm):
    fake_llm.script["send the report"] = {
        "kind": "task", "title": "Send project report to Jayesh", "details": "",
        "due_at": "2026-09-19T18:00:00+05:30", "people": ["Jayesh"], "priority": "high",
        "confidence": 0.92, "reason": "direct request",
    }
    client.post("/ingest/whatsapp", json=wa("bro can you send the report by tomorrow evening"))
    tasks = client.get("/tasks").json()
    assert len(tasks) == 1
    t = tasks[0]
    assert t["status"] == "pending_confirmation"
    assert t["title"] == "Send project report to Jayesh"
    assert t["due_at"].startswith("2026-09-19T18:00:00")
    assert t["source_text"].startswith("bro can you")

    # the classifier saw the owner name and the sender
    sys_prompt = fake_llm.calls[-1][0]["content"]
    assert "Kishaunjith" in sys_prompt

    r = client.post(f"/tasks/{t['id']}/confirm")
    assert r.json()["status"] == "open"
    r = client.post(f"/tasks/{t['id']}/done")
    assert r.json()["status"] == "done"
    assert client.get("/tasks").json() == []


def test_low_confidence_is_dropped(client, fake_llm):
    fake_llm.script["maybe"] = {"kind": "task", "title": "x", "confidence": 0.3, "reason": "guess"}
    client.post("/ingest/whatsapp", json=wa("maybe we should do something sometime"))
    assert client.get("/tasks").json() == []


def test_duplicate_message_is_idempotent(client, fake_llm):
    fake_llm.script["submit the form"] = {"kind": "task", "title": "Submit form", "confidence": 0.9}
    payload = wa("please submit the form")
    client.post("/ingest/whatsapp", json=payload)
    r = client.post("/ingest/whatsapp", json=payload)
    assert r.json()["duplicate"] is True
    assert len(client.get("/tasks").json()) == 1


def test_context_is_passed_to_classifier(client, fake_llm):
    chat = {"chat_id": "grp@g.us", "chat_name": "Project Team", "is_group": True}
    client.post("/ingest/whatsapp", json=wa("meeting tomorrow?", **chat))
    fake_llm.script["yes 5pm works"] = {"kind": "event", "title": "Team meeting", "confidence": 0.8,
                                       "due_at": "2026-09-19T17:00:00+05:30"}
    client.post("/ingest/whatsapp", json=wa("yes 5pm works", **chat))
    user_prompt = fake_llm.calls[-1][-1]["content"]
    assert "meeting tomorrow?" in user_prompt
    assert "group 'Project Team'" in user_prompt


def test_worker_heartbeat(client):
    r = client.post("/workers/heartbeat", json={"name": "omen", "url": "http://100.64.0.2:8100",
                                                "capabilities": ["stt", "local-llm"]})
    assert r.status_code == 200
    ws = client.get("/workers").json()
    assert ws[0]["name"] == "omen" and ws[0]["alive"] is True


def test_manual_task_and_patch(client):
    r = client.post("/tasks", json={"title": "Buy resistors", "priority": "low"})
    assert r.status_code == 201
    tid = r.json()["id"]
    r = client.patch(f"/tasks/{tid}", json={"title": "Buy 10k resistors"})
    assert r.json()["title"] == "Buy 10k resistors"
