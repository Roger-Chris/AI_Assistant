"""The thinking loop: scripted model moves, a fake hub, real memory and tasks."""
from __future__ import annotations

import json

import httpx
import pytest
from sqlalchemy import text
from sqlmodel import Session, select

from app import agent, memory
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
        if req.url.path == "/api/builds":
            body = json.loads(req.content)
            calls.append({"build": body})
            if body.get("continue") and body["name"] == "nothing":
                return httpx.Response(404, json={"error": "no earlier build of nothing to continue"})
            return httpx.Response(200, json={"id": "b1", "name": body["name"], "repo": body.get("repo", "/srv/app"),
                                             "agent": "claude", "reviewer": "codex", "status": "running"})
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
        {"tool": "memory_search", "args": {"query": "plate"}},
        {"tool": "memory_search", "args": {"query": "today"}},  # over the limit: refused, told to answer
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


async def test_the_same_call_twice_is_not_run_again(hub):
    script = Script([
        {"tool": "act", "args": {"device": "omen", "action": "files.find", "args": {"name": "zzthrust"}}},
        {"tool": "act", "args": {"device": "omen", "action": "files.find", "args": {"name": "zzthrust"}}},
        {"answer": "It's D:/evtol/zzthrust_test.csv."},
    ])
    res = await agent.ask("find my zzthrust sheet", llm=script)
    assert len(hub) == 1 and len(res["steps"]) == 1
    assert "already made exactly this call" in script.seen[2][-1]["content"]


async def test_messy_replies_are_understood(hub):
    script = Script([
        'Sure! ```json\n{"tool": "search_memory", "args": {"query": "zzmessy"}}\n``` then {"answer": "x"}',
        '{"tool": "act", "device": "omen", "action": "files.find", "arguments": {"name": "zzthrust"}}',
        '{"tool": "answer", "args": {"text": "Found it."}}',
    ])
    res = await agent.ask("zzmessy", llm=script)
    assert [s["tool"] for s in res["steps"]] == ["memory_search", "act"]
    assert hub[-1] == {"node": "omen", "action": "files.find", "args": {"name": "zzthrust"}, "reason": None}
    assert res["answer"] == "Found it."


async def test_follow_ups_see_the_conversation(hub):
    script = Script([{"answer": "Opening it needs your approval."}])
    history = [{"role": "user", "content": "where is my zzthrust sheet?"},
               {"role": "assistant", "content": "D:/evtol/zzthrust_test.csv on omen."}]
    await agent.ask("open it", llm=script, history=history)
    sent = script.seen[0]
    assert [m["role"] for m in sent] == ["system", "user", "assistant", "user"]
    assert "zzthrust_test.csv" in sent[2]["content"] and sent[3]["content"] == "open it"


def test_unknown_tools_get_the_list_back():
    from app.agent import normalise
    assert normalise({"tool": "create_task", "args": {"title": "x"}}) == {"tool": "add_task", "args": {"title": "x"}}
    assert normalise({"answer": {"a": 1}}) == {"answer": '{"a": 1}'}


async def test_spoken_requests_get_short_spoken_answers(hub):
    script = Script([{"answer": "Done."}, {"answer": "Done."}])
    await agent.ask("whats on today", llm=script, source="voice")
    assert "read aloud" in script.seen[0][0]["content"] and "speech recognition" in script.seen[0][0]["content"]
    await agent.ask("whats on today", llm=script, source="app")
    assert "read aloud" not in script.seen[1][0]["content"]


async def test_build_starts_on_the_hub_and_is_reported_as_started(hub):
    script = Script([
        {"tool": "build", "args": {"name": "Expense Tracker", "spec": "a web page to log expenses", "repo": "/srv/expenses"}},
        {"tool": "build", "args": {"name": "nothing", "spec": "more", "continue": True}},
        {"tool": "build", "args": {"name": "x"}},
        {"answer": "Started the expense tracker build; you'll get a preview to check."},
    ])
    res = await agent.ask("build me an expense tracker in /srv/expenses", llm=script)
    assert hub[0] == {"build": {"name": "Expense Tracker", "spec": "a web page to log expenses", "repo": "/srv/expenses", "continue": False}}
    assert "STARTED build b1" in script.seen[1][-1]["content"] and "NOT finished" in script.seen[1][-1]["content"]
    assert "no earlier build of nothing" in script.seen[2][-1]["content"]
    assert "needs the app's name and what to build" in script.seen[3][-1]["content"]
    assert res["pending"] == []  # a started build is not waiting on an approval
    assert [st["tool"] for st in res["steps"]][:1] == ["build"]
