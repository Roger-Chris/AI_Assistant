"""The thinking loop: scripted model moves, a fake hub, real memory and tasks."""
from __future__ import annotations

import json

import httpx
import pytest
from sqlalchemy import text
from sqlmodel import Session, select

from app import agent, memory
from app import telegram as tgmod
from app.config import settings
from app.db import MemoryRecord, Task, engine


@pytest.fixture(autouse=True)
def _clean():
    """Tests share one database: remove the tasks and memory these tests write."""
    yield
    with Session(engine) as s:
        for t in s.exec(select(Task).where(Task.title.startswith("zzagent"))).all():  # type: ignore[union-attr]
            s.delete(t)
        for r in s.exec(select(MemoryRecord).where((MemoryRecord.source == "agent")
                                                    | MemoryRecord.uid.startswith("project:zzagent")  # type: ignore[union-attr]
                                                    | MemoryRecord.uid.startswith("person:zzagent"))).all():  # type: ignore[union-attr]
            s.execute(text("DELETE FROM memory_fts WHERE uid = :u"), {"u": r.uid})
            s.delete(r)
        s.commit()


class Script:
    """Answers each model turn with the next scripted move; records what it was shown."""

    def __init__(self, moves):
        self.moves = list(moves)
        self.seen: list[list[dict]] = []

    async def complete(self, messages, **kw):
        self.seen.append([dict(m) for m in messages])
        m = self.moves.pop(0)
        return m if isinstance(m, str) else json.dumps(m)


@pytest.fixture
def hub(monkeypatch):
    calls: list[dict] = []

    def handler(req: httpx.Request) -> httpx.Response:
        if req.url.path == "/api/nodes":
            return httpx.Response(200, json={"self": {"name": "linux-hub"}, "peers": [{"name": "omen", "online": True}]})
        if req.url.path == "/api/actions":
            return httpx.Response(200, json=[{"name": "files.find", "risk": "safe", "describe": "find files"},
                                             {"name": "files.write", "risk": "ask", "describe": "write a file"}])
        if req.url.path == "/api/act":
            body = json.loads(req.content)
            calls.append(body)
            if body["action"] == "files.find":
                return httpx.Response(200, json={"status": "done", "node": body["node"], "action": "files.find",
                                                 "result": {"ok": True, "output": "D:/evtol/zzthrust_test.csv"}})
            return httpx.Response(200, json={"status": "pending", "node": body["node"], "action": body["action"],
                                             "approval": {"id": "a1", "summary": "Write 5 characters to plan.md"}})
        return httpx.Response(404)

    monkeypatch.setattr(settings, "hub_url", "http://hub.test")
    monkeypatch.setattr(agent.Hub, "transport", httpx.MockTransport(handler))
    return calls


async def test_recall_tools_and_answer_are_remembered(hub):
    with Session(engine) as s:
        memory.put(s, "project:zzagent-evtol", kind="project", title="Zzagent heavy-lift eVTOL",
                   body="X8 coaxial airframe, tethered tests")
        s.commit()
    script = Script([
        {"tool": "memory_about", "args": {"name": "zzagent heavy-lift evtol"}},
        {"tool": "act", "args": {"device": "omen", "action": "files.find", "args": {"name": "zzthrust"}, "reason": "find the sheet"}},
        {"answer": "Your thrust sheet is D:/evtol/zzthrust_test.csv on the laptop."},
    ])
    res = await agent.ask("where is my zzagent eVTOL thrust sheet?", llm=script)
    assert res["answer"].startswith("Your thrust sheet")
    assert [s["tool"] for s in res["steps"]] == ["memory_about", "act"]
    assert res["pending"] == []
    system = script.seen[0][0]["content"]
    assert "Zzagent heavy-lift eVTOL" in system  # recalled before the first step
    assert "omen: online" in system and "files.write [ask]" in system
    assert "X8 coaxial" in script.seen[1][-1]["content"]  # memory_about result shown to the model
    assert "zzthrust_test.csv" in script.seen[2][-1]["content"]
    assert hub == [{"node": "omen", "action": "files.find", "args": {"name": "zzthrust"}, "reason": "find the sheet"}]
    with Session(engine) as s:
        work = s.exec(select(MemoryRecord).where(MemoryRecord.title.contains("zzagent eVTOL thrust"))).first()  # type: ignore[union-attr]
        assert work and work.kind == "work" and "files.find" in work.body


async def test_a_risky_action_is_reported_as_waiting_not_done(hub):
    script = Script([
        {"tool": "act", "args": {"device": "omen", "action": "files.write", "args": {"path": "plan.md", "content": "hello"}}},
        {"answer": "I've asked to write plan.md; approve it on your phone."},
    ])
    res = await agent.ask("save hello to plan.md on the laptop", llm=script)
    assert res["pending"] == [{"device": "omen", "action": "files.write", "status": "pending"}]
    assert "has not run yet" in script.seen[1][-1]["content"].lower()


async def test_bad_json_gets_a_second_chance_and_the_step_limit_holds(hub, monkeypatch):
    monkeypatch.setattr(settings, "agent_max_steps", 2)
    script = Script([
        "I think I should search",  # not JSON
        {"tool": "tasks", "args": {}},
        {"tool": "tasks", "args": {}},
        {"tool": "tasks", "args": {}},  # over the limit: refused, told to answer
        {"answer": "Done looking."},
    ])
    res = await agent.ask("what's on my plate?", llm=script)
    assert res["answer"] == "Done looking."
    assert len(res["steps"]) == 2
    assert "exactly one JSON object" in script.seen[1][-1]["content"]
    assert "No more tool calls" in script.seen[4][-1]["content"]


async def test_remember_and_add_task(hub):
    with Session(engine) as s:
        memory.put(s, "person:zzagent-jayesh", kind="person", title="Zzagent Jayesh")
        s.commit()
    script = Script([
        {"tool": "remember", "args": {"text": "Jayesh prefers calls after 6 pm", "kind": "fact", "subject": "Zzagent Jayesh"}},
        {"tool": "add_task", "args": {"title": "zzagent call Jayesh", "due_at": "2026-11-02T18:30"}},
        {"answer": "Noted, and added the call."},
    ])
    await agent.ask("remember Jayesh likes calls after 6 and remind me to call him Monday 6:30 pm", llm=script)
    with Session(engine) as s:
        about = memory.about(s, "person:zzagent-jayesh")
        assert about["linked"][0]["body"] == "Jayesh prefers calls after 6 pm"
        t = s.exec(select(Task).where(Task.title == "zzagent call Jayesh")).first()
        assert t and t.due_at.hour == 13  # 18:30 IST stored as 13:00 UTC


async def test_no_hub_is_said_plainly(monkeypatch):
    monkeypatch.setattr(settings, "hub_url", "")
    script = Script([{"tool": "act", "args": {"device": "omen", "action": "files.find", "args": {"name": "x"}}},
                     {"answer": "The hub is down."}])
    await agent.ask("find x", llm=script)
    assert "could not run it" in script.seen[1][-1]["content"]
    assert "hub not reachable" in script.seen[0][0]["content"]


def test_endpoint_needs_the_token(client):
    assert client.post("/agent/ask", json={"text": "hi"}, headers={"Authorization": "Bearer nope"}).status_code == 401


async def test_telegram_free_text_only_from_the_owner(monkeypatch):
    sent: list[dict] = []

    async def fake_tg(method, session=None, **params):
        sent.append({"method": method, **params})
        return {"ok": True}

    asked: list[str] = []

    async def fake_ask(text, **kw):
        asked.append(text)
        return {"answer": "on it", "steps": [], "pending": [{"action": "files.write"}]}

    monkeypatch.setattr(tgmod, "tg", fake_tg)
    monkeypatch.setattr(agent, "ask", fake_ask)
    monkeypatch.setattr(settings, "telegram_chat_id", "")
    await tgmod._handle_message({"chat": {"id": 555}, "text": "hello"})
    assert asked == [] and "TELEGRAM_CHAT_ID=555" in sent[-1]["text"]  # no owner set: nobody is trusted
    sent.clear()
    monkeypatch.setattr(settings, "telegram_chat_id", "111")
    await tgmod._handle_message({"chat": {"id": 999}, "text": "delete everything"})
    assert asked == [] and sent == []  # a stranger gets nothing
    await tgmod._handle_message({"chat": {"id": 111}, "text": "save the plan"})
    assert asked == ["save the plan"]
    assert "Waiting for your Approve" in sent[-1]["text"]
