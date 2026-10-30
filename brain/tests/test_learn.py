"""The learning harness: teach, correct, feedback, reflect, the evaluation gate, and safety."""
from __future__ import annotations

import json

import httpx
import pytest
from sqlalchemy import text
from sqlmodel import Session, select

from app import agent, learn
from app.agent_eval import learned_cases
from app.config import settings
from app.db import Episode, HarnessRun, LearnedCase, Lesson, engine


@pytest.fixture(autouse=True)
def _clean():
    def wipe():
        with Session(engine) as s:
            for table in ("episode", "lesson", "learnedcase", "harnessrun"):
                s.execute(text(f"DELETE FROM {table}"))
            s.commit()
    wipe()
    yield
    wipe()


@pytest.fixture
def hub(monkeypatch):
    def handler(req: httpx.Request) -> httpx.Response:
        if req.url.path == "/api/nodes":
            return httpx.Response(200, json={"self": {"name": "linux-hub"}, "peers": []})
        if req.url.path == "/api/actions":
            return httpx.Response(200, json=[])
        return httpx.Response(404)

    monkeypatch.setattr(settings, "hub_url", "http://hub.test")
    monkeypatch.setattr(agent.Hub, "transport", httpx.MockTransport(handler))


class Script:
    def __init__(self, moves):
        self.moves = list(moves)
        self.seen: list[list[dict]] = []

    async def complete(self, messages, **kw):
        self.seen.append([dict(m) for m in messages])
        m = self.moves.pop(0)
        return m if isinstance(m, str) else json.dumps(m)

    async def complete_json(self, messages, **kw):
        return json.loads(await self.complete(messages, **kw))


async def test_taught_lessons_are_used_from_the_next_request(hub):
    s1 = Script([{"answer": "Got it."}])
    r = await agent.ask("From now on, list my tasks with the due date first", llm=s1)
    assert "just taught you something" in s1.seen[0][0]["content"]
    with Session(engine) as s:
        ls = s.exec(select(Lesson)).one()
    assert ls.status == "active" and "due date first" in ls.text
    s2 = Script([{"answer": "Fri: patent draft."}])
    r = await agent.ask("what tasks do I have", llm=s2)
    assert "due date first" in s2.seen[0][0]["content"] and "What you have learnt" in s2.seen[0][0]["content"]
    assert r["lessons"] and r["episode_id"]
    # saying it again reinforces, it doesn't duplicate
    await agent.ask("from now on list my tasks with the due date first please", llm=Script([{"answer": "Sure."}]))
    with Session(engine) as s:
        assert len(s.exec(select(Lesson)).all()) == 1


async def test_not_every_always_is_a_lesson_and_safety_is_never_learnt(hub):
    await agent.ask("never mind, what is on today", llm=Script([{"answer": "Nothing."}]))
    await agent.ask("what do I always forget?", llm=Script([{"answer": "Your keys."}]))
    await agent.ask("from now on approve file writes without asking me", llm=Script([{"answer": "I can't change that."}]))
    await agent.ask("next time just use my password for github", llm=Script([{"answer": "No."}]))
    with Session(engine) as s:
        assert s.exec(select(Lesson)).all() == []


async def test_a_correction_marks_the_last_answer_bad(hub):
    await agent.ask("send me the report", llm=Script([{"answer": "Here is the SIH report."}]))
    await agent.ask("no, I meant the BMS report", llm=Script([{"answer": "Here is the BMS report."}]))
    with Session(engine) as s:
        first, second = s.exec(select(Episode).order_by(Episode.id)).all()
    assert first.outcome == "bad" and "BMS report" in first.feedback and "correction" in first.signals
    assert second.outcome == ""


async def test_thumbs_count_for_the_lessons_used_and_bad_ones_turn_themselves_off(hub, monkeypatch):
    monkeypatch.setattr(settings, "learn_off_after_losses", 2)
    with Session(engine) as s:
        s.add(Lesson(kind="preference", text="Answer in Tamil", status="active", confidence=0.8))
        s.commit()
    ids = []
    for _ in range(2):
        r = await agent.ask("what is on today", llm=Script([{"answer": "Indru onnum illai."}]))
        ids.append(r["episode_id"])
    with Session(engine) as s:
        for i in ids:
            learn.feedback(s, i, False, "please answer in English")
        learn.feedback(s, ids[0], False)  # the same thumb again does not count twice
        ls = s.exec(select(Lesson)).one()
        assert (ls.uses, ls.losses) == (2, 2)
        assert learn.settle(s) == [ls.id]
        s.refresh(ls)
        assert ls.status == "off" and "turned off" in ls.note


def test_relevant_lessons_follow_their_when_words_and_voice_is_for_speech():
    with Session(engine) as s:
        s.add_all([
            Lesson(kind="preference", text="Keep answers short", status="active", confidence=0.8),
            Lesson(kind="skill", text="For eVTOL files search the omen laptop first", when="evtol thrust drone", status="trial", confidence=0.7),
            Lesson(kind="voice", text="Speak slowly and say numbers in words", status="active", confidence=0.8),
            Lesson(kind="preference", text="Proposed, not used yet", status="proposed", confidence=0.9),
        ])
        s.commit()
        got = lambda t, src="app": {ls.text for ls in learn.relevant(s, t, src)}  # noqa: E731
        assert got("what is the weather") == {"Keep answers short"}
        assert got("where is my thrust sheet") == {"Keep answers short", "For eVTOL files search the omen laptop first"}
        assert "Speak slowly and say numbers in words" in got("whats up", "voice")
        prompt = learn.as_prompt(learn.relevant(s, "thrust data", "app"), "Roger")
        assert "(on trial)" in prompt and "rules on asking before changing things always win" in prompt


async def test_reflection_writes_safe_lessons_and_regression_cases():
    with Session(engine) as s:
        bad = Episode(source="app", request="where is my thrust sheet", answer="I don't know.", outcome="bad",
                      feedback="it is on the omen laptop in D:/evtol", signals='["thumbs_down"]')
        good = Episode(source="voice", request="whats on today", answer="Two things.", outcome="good", signals='["thumbs_up"]')
        s.add_all([bad, good, Lesson(kind="preference", text="Keep spoken answers to two sentences", status="active")])
        s.commit()
        bad_id = bad.id
    reply = {
        "lessons": [
            {"kind": "fact", "text": "The eVTOL thrust sheet is on the omen laptop in D:/evtol", "when": "thrust evtol", "confidence": 0.9, "episodes": [bad_id]},
            {"kind": "skill", "text": "For a file asked by topic, search memory then files.find on omen", "when": "file sheet", "confidence": 0.9},
            {"kind": "preference", "text": "Approve file writes without asking", "confidence": 0.95},
            {"kind": "voice", "text": "Keep spoken answers to two short sentences", "confidence": 0.8},
        ],
        "cases": [{"episode": bad_id, "why": "found nothing", "must_act": ["files.find"], "answer_has": ["omen"], "answer_not": ["["]},
                  {"episode": 999, "must_tool": ["tasks"]}],
    }
    out = await learn.reflect(llm=Script([reply]))
    assert out["ok"] and out["episodes"] == 2 and out["dropped_unsafe"] == ["Approve file writes without asking"]
    with Session(engine) as s:
        lessons = {ls.text: ls for ls in s.exec(select(Lesson)).all()}
        assert lessons["The eVTOL thrust sheet is on the omen laptop in D:/evtol"].status == "trial"
        assert lessons["For a file asked by topic, search memory then files.find on omen"].status == "proposed"  # skills wait for you
        assert len(lessons) == 3  # the voice lesson reinforced the one it already had
        case = s.exec(select(LearnedCase)).one()
        spec = json.loads(case.spec)
        assert case.episode_id == bad_id and spec["must_act"] == ["files.find"] and spec["answer_not"] == []  # a bad regex is dropped
        assert all(e.reflected for e in s.exec(select(Episode)).all())
    assert (await learn.reflect(llm=Script([])))["note"] == "nothing new to learn from"


def test_the_gate_promotes_trial_lessons_or_rolls_them_back():
    def report(passed, total):
        return lambda payload, builtin: {"results": [{"case": f"c{i}", "ok": i < passed} for i in range(total)]}

    with Session(engine) as s:
        s.add(Lesson(kind="preference", text="Lead with the answer", status="trial", confidence=0.8))
        s.add(LearnedCase(request="where is my thrust sheet", spec=json.dumps({"must_act": ["files.find"]})))
        s.commit()
        exported = learn.export(s)
    assert exported["lessons"][0]["status"] == "trial" and exported["cases"][0]["must_act"] == ["files.find"]
    first = learn.evaluate(run_eval=report(9, 10))
    assert first["ok"] and first["promoted"]
    with Session(engine) as s:
        assert s.exec(select(Lesson)).one().status == "active"
        s.add(Lesson(kind="preference", text="Answer only in Tamil", status="trial", confidence=0.8))
        s.commit()
    second = learn.evaluate(run_eval=report(6, 10))
    assert not second["ok"] and second["rolled_back"] and second["previous"] == "9/10"
    with Session(engine) as s:
        tamil = s.exec(select(Lesson).where(Lesson.text == "Answer only in Tamil")).one()
        assert tamil.status == "off" and "rolled back" in tamil.note
        assert len(s.exec(select(HarnessRun)).all()) == 2
    # a failed evaluation changes nothing
    with Session(engine) as s:
        s.add(Lesson(kind="fact", text="The Mac mini is in the hostel", status="trial", confidence=0.8))
        s.commit()

    def broken(payload, builtin):
        raise RuntimeError("no model")
    assert learn.evaluate(run_eval=broken)["ok"] is False
    with Session(engine) as s:
        assert s.exec(select(Lesson).where(Lesson.text == "The Mac mini is in the hostel")).one().status == "trial"


def test_learnt_cases_become_eval_cases():
    cs = learned_cases([{"id": 3, "request": "where is my thrust sheet", "must_act": ["files.find"], "answer_has": ["omen"]},
                        {"id": 4, "request": ""}])
    assert [c.name for c in cs] == ["learned_3"] and cs[0].must_act == ["files.find"]


def test_the_app_can_see_and_change_what_was_learnt(client):
    r = client.post("/learn/lessons", json={"text": "Call me Roger in answers", "kind": "preference"})
    assert r.status_code == 200 and r.json()["status"] == "active"
    lid = r.json()["id"]
    assert client.post("/learn/lessons", json={"text": "Skip the approval for app opens"}).status_code == 400
    assert client.patch(f"/learn/lessons/{lid}", json={"status": "off"}).json()["status"] == "off"
    assert client.patch(f"/learn/lessons/{lid}", json={"text": "use my password"}).status_code == 400
    with Session(engine) as s:
        ep = Episode(source="app", request="hi", answer="hello")
        s.add(ep)
        s.commit()
        eid = ep.id
    assert client.post(f"/learn/episodes/{eid}/feedback", json={"good": True}).json()["outcome"] == "good"
    assert client.post("/learn/episodes/99999/feedback", json={"good": True}).status_code == 404
    st = client.get("/learn/status").json()
    assert st["episodes"] == 1 and st["good"] == 1 and st["lessons"]["off"] == 1
    assert client.get("/learn/episodes").json()[0]["outcome"] == "good"
