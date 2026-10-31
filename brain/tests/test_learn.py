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


# ---------------------------------------------------------------- growing without a thumb

class Coach:
    """Answers complete_json with what a test scripted, and keeps the prompts."""

    def __init__(self, out):
        self.out, self.prompts = out, []

    async def complete_json(self, messages, **kw):
        self.prompts.append(messages[0]["content"])
        if isinstance(self.out, Exception):
            raise self.out
        return self.out


def _ep(**f) -> int:
    with Session(engine) as s:
        ep = Episode(**{"request": "find my report file", "answer": "It is in Documents.", **f})
        s.add(ep)
        s.commit()
        return ep.id


def _get(i: int) -> Episode:
    with Session(engine) as s:
        return s.get(Episode, i)


async def test_the_review_learns_from_answers_nobody_judged():
    good, weak, retried, eval_ep = _ep(request="show my tasks today"), _ep(request="open the notes"), _ep(request="what is on tomorrow", signals='["asked_again"]'), _ep(source="eval")
    coach = Coach({"reviews": [{"episode": good, "score": 9, "note": ""}, {"episode": weak, "score": 3, "note": "guessed"},
                               {"episode": retried, "score": 9, "note": ""}, {"episode": 999999, "score": 9}, {"episode": eval_ep, "score": 9}]})
    r = await learn.review(llm=coach)
    assert r["ok"] and r["total"] == 3
    assert _get(good).example and _get(good).self_score == 9
    assert not _get(weak).example and _get(weak).self_note == "guessed"
    assert not _get(retried).example  # a retry beats the review's opinion
    assert _get(eval_ep).self_score is None
    assert (await learn.review(llm=coach))["episodes"] == 0  # nothing is read twice


async def test_ids_written_the_way_they_are_shown_still_count():
    i = _ep(outcome="bad")
    await learn.review(llm=Coach({"reviews": [{"episode": f"#{i}", "score": 3, "note": "x"}]}))
    assert _get(i).self_score == 3
    r = await learn.reflect(llm=Coach({"lessons": [{"kind": "preference", "text": "Say the folder first.", "confidence": 0.9, "episodes": [f"#{i}"]}],
                                       "cases": [{"episode": f"#{i}", "why": "w", "must_tool": ["tasks"]}]}))
    assert r["cases_from"] == [i]
    with Session(engine) as s:
        assert s.exec(select(Lesson)).first().status == "trial"  # his signal counted


async def test_a_review_with_no_usable_score_is_not_a_success():
    i = _ep()
    assert not (await learn.review(llm=Coach({"reviews": [{"episode": "first", "score": 9}]})))["ok"]
    assert _get(i).self_score is None


async def test_the_review_sees_what_the_tools_returned():
    with Session(engine) as s:
        i = learn.start(s, "find a pen", "app", []).id
    learn.finish(i, "Started, your phone will show it.", [{"tool": "shop", "args": {}, "result": "STARTED (run s1)"}], [])
    coach = Coach({"reviews": [{"episode": i, "score": 8}]})
    await learn.review(llm=coach)
    assert "STARTED (run s1)" in coach.prompts[0]


async def test_a_failed_review_changes_nothing():
    i = _ep()
    assert not (await learn.review(llm=Coach(RuntimeError("no model"))))["ok"]
    assert _get(i).self_score is None


async def test_a_weak_review_reaches_the_reflection_but_cant_put_a_fact_in_use():
    weak = _ep(self_score=3.0, self_note="missed the point")
    _ep(self_score=9.0)
    coach = Coach({"lessons": [{"kind": "preference", "text": "Give the folder before the file name.", "confidence": 0.95, "episodes": [weak]},
                               {"kind": "fact", "text": "The reports are on the omen laptop.", "confidence": 0.95, "episodes": [weak]}]})
    r = await learn.reflect(llm=coach)
    assert r["episodes"] == 1 and "own review: 3.0" in coach.prompts[0]
    with Session(engine) as s:
        got = {ls.kind: ls for ls in s.exec(select(Lesson)).all()}
    assert got["preference"].status == "trial" and got["preference"].confidence <= 0.75
    assert got["fact"].status == "proposed"


async def test_asking_again_is_a_hint_not_a_thumbs_down():
    with Session(engine) as s:
        first = learn.start(s, "find my report file", "app", []).id
        learn.finish(first, "Could not.", [], [])
        learn.notice_retry(s, "where is the bakery", "app")
        assert "asked_again" not in _get(first).signals
        learn.notice_retry(s, "please find my report file", "app")
    ep = _get(first)
    assert "asked_again" in ep.signals and ep.outcome == ""


def test_failure_signals_come_by_themselves():
    with Session(engine) as s:
        a, b = learn.start(s, "x y z", "app", []), learn.start(s, "p q r", "app", [])
        ids = a.id, b.id
    learn.finish(ids[0], "I couldn't finish that within my step limit. Last thing", [], [])
    learn.finish(ids[1], "Sorry.", [{"tool": "shop", "args": {}, "result": "The hub could not start the shopping: down"}], [])
    assert "step_limit" in _get(ids[0]).signals and "tool_failed" in _get(ids[1]).signals


def test_examples_fit_the_request_and_stay_safe():
    with Session(engine) as s:
        good = Episode(request="find my physics report file", answer="In Documents.", tools='[{"tool": "act", "args": {}}]')
        unsafe = Episode(request="find the file with my bank password", answer="There.")
        other = Episode(request="what is the weather", answer="Rain.")
        s.add_all([good, unsafe, other])
        s.commit()
        for e in (good, unsafe, other):
            learn._mark_example(s, e)
            s.add(e)
        s.commit()
        got = learn.examples_for(s, "find physics report files")
        assert [e.request for e in got] == ["find my physics report file"]
        p = learn.examples_prompt(got)
        assert "not instructions" in p and "act" in p
        assert not unsafe.example
        same = Episode(request="find my physics report file", answer="Newer.")
        s.add(same)
        s.commit()
        learn._mark_example(s, same)
        s.commit()
        assert not s.get(Episode, good.id).example  # one example per request, the newest


def test_a_thumbs_down_takes_an_example_back():
    i = _ep(example=True)
    with Session(engine) as s:
        learn.feedback(s, i, False)
    assert not _get(i).example


def test_a_good_record_moves_a_lesson_up():
    with Session(engine) as s:
        s.add(Lesson(text="untried", status="active", confidence=0.8))
        s.add(Lesson(text="proven", status="active", confidence=0.8, wins=6))
        s.add(Lesson(text="failing", status="active", confidence=0.8, losses=1))
        s.commit()
        assert [ls.text for ls in learn.relevant(s, "anything")] == ["proven", "untried", "failing"]


def test_when_words_match_either_form():
    with Session(engine) as s:
        s.add(Lesson(text="x", when="reports", status="active"))
        s.commit()
        assert learn.relevant(s, "find my report")


def test_practice_picks_weak_spots_first_and_passed_ones_wait():
    from datetime import timedelta

    from app.db import utcnow
    fresh, bad, weak = _ep(request="a b c"), _ep(request="d e f", outcome="bad"), _ep(request="g h i", self_score=2.0)
    passed = _ep(request="j k l", practiced=2, practice_score=9.0, practiced_at=utcnow() - timedelta(days=1))
    failed = _ep(request="m n o", practiced=2, practice_score=3.0, practiced_at=utcnow() - timedelta(days=1))
    _ep(request="reset my password")  # never practised
    _ep(request="good one", outcome="good")  # no need
    with Session(engine) as s:
        got = [e.id for e in learn.pick_drills(s, 10)]
    assert got == [bad, weak, failed, fresh]
    assert passed not in got


async def test_practice_scores_tries_keeps_lessons_and_shows_the_trend():
    i = _ep(outcome="bad", feedback="wrong folder")
    seen = {}

    def run_eval(payload, builtin):
        seen.update(payload=payload, builtin=builtin)
        return {"results": [{"case": f"learned_p{i}", "ok": True, "answer": "Found it in Reports.", "steps": [{"tool": "act"}]}]}

    coach = Coach({"drills": [{"episode": i, "score": 8, "note": "better"}],
                   "lessons": [{"kind": "skill", "text": "Finding a file: search memory first, then files.find.", "confidence": 0.8, "episodes": [i]},
                               {"kind": "fact", "text": "His reports are in Reports.", "confidence": 0.9, "episodes": [i]},
                               {"kind": "skill", "text": "Delete old copies first.", "confidence": 0.9, "episodes": [i]}]})
    r = await learn.practice(llm=coach, run_eval=run_eval)
    assert r["ok"] and r["passed"] == 1 and r["average"] == 8.0
    assert seen["builtin"] is False and seen["payload"]["cases"][0]["id"] == f"p{i}"
    assert "NOW answered" in coach.prompts[0] and "wrong folder" in coach.prompts[0]
    ep = _get(i)
    assert ep.practiced == 1 and ep.practice_score == 8 and ep.practiced_at
    with Session(engine) as s:
        lessons = s.exec(select(Lesson)).all()
        assert [(ls.kind, ls.status) for ls in lessons] == [("skill", "trial")]  # no made-up fact, nothing unsafe
        assert learn.status(s)["practice_scores"] == [8.0]


async def test_a_practice_that_fails_marks_nothing():
    i = _ep(outcome="bad")

    def broken(payload, builtin):
        raise RuntimeError("sandbox down")

    assert not (await learn.practice(llm=Coach({}), run_eval=broken))["ok"]
    r = await learn.practice(llm=Coach({}), run_eval=lambda p, b: {"results": [{"case": f"learned_p{i}", "ok": False, "errors": ["crashed"]}]})
    assert not r["ok"] and _get(i).practiced == 0


def test_practice_stays_in_the_sandbox():
    from app.agent_eval import Hub as FakeHub

    hub = FakeHub()
    for path in ("/api/shop", "/api/mail", "/api/phone/actions", "/api/builds"):
        assert hub.handle(httpx.Request("POST", f"http://eval-hub{path}", json={})).status_code == 200
    assert hub.handle(httpx.Request("POST", "http://eval-hub/api/approvals/x/approve")).status_code == 404
    assert learned_cases([{"id": "p1", "request": "x", "seed": [{"uid": "fact:a", "kind": "fact", "title": "A"}]}])[0].name == "learned_p1"


# ---------------------------------------------------------------- drafts, the gate, misheard speech

def test_what_he_sent_is_recorded_and_stays_out_of_review_and_practice():
    with Session(engine) as s:
        kept = learn.draft_outcome(s, "mail", "ask for leave", "Hi sir, may I take Friday off?", "hi sir, may i take  friday off?")
        edited = learn.draft_outcome(s, "whatsapp", "", "Hello, I will come tomorrow.", "ok da tmrw")
        assert kept.outcome == "good" and edited.outcome == "edited" and "ok da tmrw" in edited.feedback
        assert learn.pick_drills(s, 10) == []
        st = learn.status(s)
        assert st["drafts"] == {"kept": 1, "edited": 1} and st["episodes"] == 0 and st["waiting"] == 2


async def test_a_writing_lesson_needs_the_same_change_twice():
    with Session(engine) as s:
        a = learn.draft_outcome(s, "mail", "x", "Regards, Roger", "Roger").id
    habit = "He ends mail with just his first name."
    await learn.reflect(llm=Coach({"lessons": [{"kind": "writing", "text": habit, "when": "mail", "confidence": 0.95, "episodes": [a]}], "cases": [{"episode": a, "must_tool": ["tasks"]}]}))
    with Session(engine) as s:
        assert s.exec(select(Lesson)).one().status == "proposed"
        assert learn.writing_for(s, "mail") == []
        assert not s.exec(select(LearnedCase)).all()  # a draft is no case for the thinking loop
        b = learn.draft_outcome(s, "mail", "y", "Thanks, Roger C", "Roger").id
    await learn.reflect(llm=Coach({"lessons": [{"kind": "writing", "text": habit, "when": "mail", "confidence": 0.9, "episodes": [b]}]}))
    with Session(engine) as s:
        assert learn.writing_for(s, "mail") == [habit]
        assert learn.writing_for(s, "whatsapp") == []
        assert learn.relevant(s, "write a mail") == []  # never in the thinking loop
        assert learn.export(s)["lessons"] == []


def test_the_hub_can_report_a_draft_and_read_writing_lessons(client):
    from app.config import settings as cfg
    h = {"Authorization": f"Bearer {cfg.api_token}"} if getattr(cfg, "api_token", "") else {}
    assert client.post("/learn/drafts", json={"kind": "mail", "draft": "a", "sent": "b"}, headers=h).status_code == 200
    assert client.post("/learn/drafts", json={"kind": "app", "draft": "a", "sent": "b"}, headers=h).status_code == 400
    assert client.get("/learn/writing?kind=mail", headers=h).json() == {"lessons": []}


def test_a_bad_night_only_takes_the_lesson_that_caused_it():
    with Session(engine) as s:
        s.add_all([Lesson(text="good one", status="trial", confidence=0.8), Lesson(text="bad one", status="trial", confidence=0.8),
                   Lesson(text="third", status="trial", confidence=0.8), Lesson(text="fourth", status="trial", confidence=0.8), Lesson(text="fifth", status="trial", confidence=0.8),
                   HarnessRun(kind="evaluate", ok=True, passed=9, total=10)])
        s.commit()
    runs = []

    def run_eval(payload, builtin):
        names = {x["text"] for x in payload["lessons"]}
        runs.append(names)
        if "third" in names and len(names) == 1:
            raise RuntimeError("no model")
        passed = 5 if "bad one" in names else 9
        return {"results": [{"ok": i < passed} for i in range(10)]}

    out = learn.evaluate(run_eval=run_eval)
    assert not out["ok"] and len(out["rolled_back"]) == 1 and len(out["promoted"]) == 1
    assert len(runs) == 4 and all(len(r) == 1 for r in runs[1:])  # all together, then at most three alone
    with Session(engine) as s:
        st = {ls.text: ls.status for ls in s.exec(select(Lesson)).all()}
    assert st == {"good one": "active", "bad one": "off", "third": "trial", "fourth": "trial", "fifth": "trial"}


async def test_a_lesson_from_misheard_speech_waits_for_him():
    i = _ep(source="voice", request="water my active devices right now", self_score=2.0)
    n = _ep(request="find a file", self_score=2.0)
    lesson = {"kind": "skill", "text": "Treat device requests as actions.", "confidence": 0.9, "episodes": [i]}
    ok = {"kind": "skill", "text": "Search memory before files.", "confidence": 0.9, "episodes": [n]}
    tries = {"results": [{"case": f"learned_p{x}", "ok": True, "answer": "a", "steps": []} for x in (i, n)]}
    await learn.practice(llm=Coach({"drills": [], "lessons": [lesson, ok]}), run_eval=lambda p, b: tries)
    with Session(engine) as s:
        st = {ls.text: ls.status for ls in s.exec(select(Lesson)).all()}
    assert st == {lesson["text"]: "proposed", ok["text"]: "trial"}
