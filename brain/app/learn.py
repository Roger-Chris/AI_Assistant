"""The learning harness: how the assistant gets better at being *your* assistant, safely.

    observe  -> every request to the thinking loop is an Episode (what was asked, what it did, the answer)
    signal   -> 👍/👎 from the app, a correction ("no, I meant…") right after an answer, a denied action
    teach    -> "from now on…", "next time…", "always…/never…" said to the assistant becomes a lesson at once
    reflect  -> nightly, a model reads the episodes that got a signal and writes lessons (preferences, facts,
                skills = how to do a kind of job, voice = how to speak) and regression cases from failures
    gate     -> new lessons start on *trial*: in the prompt, but the evaluation (agent_eval, fake devices,
                throwaway database) runs the built-in and learnt cases with them; worse than before -> rolled
                back, otherwise promoted to *active*
    settle   -> a lesson that keeps getting 👎 when it was used is turned off by itself
    use      -> the relevant active and trial lessons go into the thinking loop's prompt (agent.py)

Nothing learnt can loosen safety: a lesson that talks about approvals, asking first, passwords, tokens,
deleting, paying or ignoring rules is never kept (UNSAFE), and the owner sees, confirms or turns off
every lesson in the app (Settings -> Learning). docs/LEARNING.md has the picture.
"""
from __future__ import annotations

import json
import logging
import os
import re
import subprocess
import sys
import tempfile
from datetime import timedelta
from typing import Any, Optional

from sqlmodel import Session, col, select

from .config import settings
from .db import Episode, HarnessRun, LearnedCase, Lesson, engine, utcnow

log = logging.getLogger("brain.learn")

KINDS = ("preference", "fact", "skill", "rule", "voice")

# What a correction sounds like, said right after an answer.
CORRECTION = re.compile(
    r"^\s*(no+\b|nope\b|wrong\b|not (that|this|what i|it)\b|that'?s (wrong|not (it|right|what i))|"
    r"i (meant|said|asked)\b|actually\b|you (got it wrong|misunderstood|didn'?t)\b|that'?s incorrect|"
    r"stop (doing|saying)\b|why did you\b|don'?t (do|say|call|use|open)\b)",
    re.IGNORECASE,
)
# The owner teaching a standing preference.
TEACH = re.compile(r"(\b(from now on|next time|in (the )?future|going forward)\b|^\s*(please\s+)?(always|never)\s+(?!mind\b)\w|"
                   r"\bremember that i\b)", re.IGNORECASE)
# Lessons must never touch safety. Anything mentioning these is dropped, whoever proposed it.
UNSAFE = re.compile(
    r"\b(approv\w*|without asking|don'?t ask|no need to ask|skip (the )?(check|confirm\w*|asking)|auto[- ]?approv\w*|"
    r"bypass|password|passcode|otp|pin\b|token|secret|api[- ]?key|credential|delete|wipe|erase|rm -rf|format\b|"
    r"send money|pay(ment)?s?\b|transfer|upi|bank|card number|disable (the )?(safety|rules|checks)|"
    r"ignore (the |your |all )?(rules|safety|instructions|system)|jailbreak|unrestricted|full access|admin rights|sudo)\b",
    re.IGNORECASE,
)
_WORD = re.compile(r"[a-z0-9']+")
STOP = set("a an the and or but if to of in on at for with from by is are was were be been it this that these those "
           "i me my you your he she they we us our him her them what which who when where how why do does did can could "
           "would should will shall please pls just about into over then than so as not no yes ok okay".split())


def words(s: str) -> set[str]:
    return {w for w in _WORD.findall((s or "").lower()) if w not in STOP and len(w) > 2}


def similar(a: str, b: str) -> float:
    wa, wb = words(a), words(b)
    return len(wa & wb) / len(wa | wb) if wa and wb else 0.0


def safe(text: str) -> bool:
    return not UNSAFE.search(text or "")


def _ids(s: str) -> list[int]:
    return [int(x) for x in (s or "").split(",") if x.strip().isdigit()]


def _signals(ep: Episode) -> list[str]:
    try:
        return list(json.loads(ep.signals or "[]"))
    except ValueError:
        return []


def _add_signal(ep: Episode, sig: str) -> None:
    sigs = _signals(ep)
    if sig not in sigs:
        sigs.append(sig)
    ep.signals = json.dumps(sigs)


# ---------------------------------------------------------------- use: lessons in the prompt

MAX_IN_PROMPT = 8


def relevant(session: Session, text: str, source: str = "app") -> list[Lesson]:
    """Active and trial lessons that apply to this request: the always-on ones, plus those whose
    `when` words appear in it. Voice lessons only for spoken requests."""
    if not settings.learn_enabled:
        return []
    req = words(text)
    out: list[tuple[float, Lesson]] = []
    for ls in session.exec(select(Lesson).where(col(Lesson.status).in_(["active", "trial"]))).all():
        if ls.kind == "voice" and source != "voice":
            continue
        if ls.when.strip():
            hit = len(words(ls.when) & req)
            if not hit:
                continue
            score = ls.confidence + 0.2 * hit
        else:
            score = ls.confidence
        out.append((score, ls))
    out.sort(key=lambda x: -x[0])
    return [ls for _, ls in out[:MAX_IN_PROMPT]]


def as_prompt(lessons: list[Lesson], owner: str) -> str:
    if not lessons:
        return ""
    lines = [f"- {ls.text}" + (" (on trial)" if ls.status == "trial" else "") for ls in lessons]
    return (f"\n\nWhat you have learnt from working with {owner} (follow these unless they conflict with the "
            "rules above; the rules on asking before changing things always win):\n" + "\n".join(lines))


# ---------------------------------------------------------------- observe and signal

def start(session: Session, text: str, source: str, lessons: list[Lesson]) -> Episode:
    ep = Episode(source=source, request=text[:4000], lessons=",".join(str(ls.id) for ls in lessons))
    for ls in lessons:
        ls.uses += 1
        session.add(ls)
    session.add(ep)
    session.commit()
    session.refresh(ep)
    return ep


def finish(episode_id: int, answer: str, steps: list[dict], pending: list[dict]) -> None:
    with Session(engine) as s:
        ep = s.get(Episode, episode_id)
        if not ep:
            return
        ep.answer = answer[:4000]
        ep.tools = json.dumps([{"tool": st["tool"], "args": st["args"]} for st in steps], ensure_ascii=False)[:8000]
        if pending:
            _add_signal(ep, "asked_approval")
        s.add(ep)
        s.commit()


def _judge_lessons(session: Session, ep: Episode, good: bool) -> None:
    for lid in _ids(ep.lessons):
        ls = session.get(Lesson, lid)
        if not ls:
            continue
        if good:
            ls.wins += 1
        else:
            ls.losses += 1
        ls.updated_at = utcnow()
        session.add(ls)


def feedback(session: Session, episode_id: int, good: bool, note: str = "", signal: str = "") -> Episode:
    """👍/👎 (or a denied action) on one answer. Counts for the lessons that were in its prompt."""
    ep = session.get(Episode, episode_id)
    if not ep:
        raise KeyError(episode_id)
    before = ep.outcome
    ep.outcome = "good" if good else "bad"
    if note.strip():
        ep.feedback = (ep.feedback + "\n" + note.strip()).strip()[:2000]
    _add_signal(ep, signal or ("thumbs_up" if good else "thumbs_down"))
    ep.reflected = False  # new information: look at it again
    if before != ep.outcome:
        _judge_lessons(session, ep, good)
    session.add(ep)
    session.commit()
    session.refresh(ep)
    return ep


def notice_correction(session: Session, text: str, source: str) -> Optional[Episode]:
    """"No, I meant…" right after an answer marks that answer bad, with the correction as feedback."""
    if not CORRECTION.search(text or ""):
        return None
    since = utcnow() - timedelta(minutes=30)
    ep = session.exec(select(Episode).where(Episode.source == source, Episode.at >= since, Episode.answer != "")
                      .order_by(col(Episode.id).desc())).first()
    if not ep or ep.outcome == "bad":
        return None
    return feedback(session, ep.id, False, note=text, signal="correction")


def teach(session: Session, text: str) -> Optional[Lesson]:
    """"From now on…", "next time…": the owner's own words become an active lesson right away
    (unless it touches safety, or says the same as one already known)."""
    if not settings.learn_enabled or not TEACH.search(text or "") or not safe(text):
        return None
    t = " ".join(text.split())[:400]
    if len(words(t)) < 2:
        return None
    for ls in session.exec(select(Lesson).where(Lesson.status != "off")).all():
        if similar(ls.text, t) >= 0.6:
            ls.confidence = min(1.0, ls.confidence + 0.1)
            if ls.status in ("proposed", "trial"):
                ls.status, ls.note = "active", "you said it again"
            ls.updated_at = utcnow()
            session.add(ls)
            session.commit()
            return ls
    voice = bool(re.search(r"\b(speak|say|talk|voice|read (it )?aloud|out loud|tone)\b", t, re.I))
    ls = Lesson(kind="voice" if voice else "preference", text=f"{settings.owner_name} said: \"{t}\"", status="active",
                confidence=0.9, note="taught by you")
    session.add(ls)
    session.commit()
    session.refresh(ls)
    return ls


# ---------------------------------------------------------------- reflect

REFLECT = """You improve a personal assistant by studying how its recent answers went for its owner, {owner}.
Each episode below is one request: what he asked, what the assistant did (tools) and answered, and the signal
(good / bad, his correction or note).

Write JSON only:
{{"lessons": [{{"kind": "preference|fact|skill|voice", "text": "...", "when": "...", "confidence": 0.0-1.0, "episodes": [ids]}}],
  "cases": [{{"episode": id, "why": "...", "request": "...", "must_tool": [], "must_act": [], "must_not_act": [],
             "answer_has": [], "answer_not": []}}]}}

Lessons:
- preference: how he likes things done or answered ("answers about tasks: due date first, one line each").
- fact: something true about his world the assistant got wrong ("the omen laptop is called roger on the hub").
- skill: a short recipe for a kind of request that went badly ("asked for a file by topic: search memory, then files.find on omen").
- voice: how to speak to him in spoken answers.
- "text": one plain sentence, an instruction to the assistant. "when": a few words that say when it applies (empty = always).
- Only lessons the episodes clearly support; never a guess. Prefer none to a weak one.
- NEVER anything about approvals, asking first, skipping checks, passwords, tokens, keys, deleting, payments or
  ignoring rules: safety is not yours to change.

Cases: for each BAD episode that a check could catch next time, a regression case: the same request, and
checks that a correct answer passes: tools the loop must call (memory_search, memory_about, remember, tasks,
add_task, act, phone, build), device actions it must or must not call (files.find, apps.open…), regexes the
answer must or must not match. Keep checks few and certain.

Lessons it already has (don't repeat them):
{known}

Episodes:
{episodes}"""


def _episode_line(ep: Episode) -> str:
    tools = []
    try:
        tools = [t["tool"] + (f"·{t['args'].get('action')}" if isinstance(t.get("args"), dict) and t["args"].get("action") else "")
                 for t in json.loads(ep.tools or "[]")]
    except ValueError:
        pass
    return (f"#{ep.id} [{ep.source}] asked: {ep.request[:500]!r}\n  did: {', '.join(tools) or 'nothing'}\n"
            f"  answered: {ep.answer[:600]!r}\n  signal: {ep.outcome or 'none'} {', '.join(_signals(ep))}"
            + (f"\n  his note: {ep.feedback[:500]!r}" if ep.feedback else ""))


def _clean_list(v: Any, n: int = 6) -> list[str]:
    return [str(x)[:200] for x in v][:n] if isinstance(v, list) else []


def _valid_regexes(v: Any) -> list[str]:
    out = []
    for x in _clean_list(v):
        try:
            re.compile(x)
            out.append(x)
        except re.error:
            continue
    return out


async def reflect(llm=None, limit: int = 30) -> dict:
    """Read the episodes with a signal that weren't studied yet, write lessons and regression cases."""
    from .llm import LLM, planner_profile

    with Session(engine) as s:
        eps = s.exec(select(Episode).where(Episode.reflected == False, Episode.outcome != "",  # noqa: E712
                                           Episode.source != "eval").order_by(col(Episode.id).desc()).limit(limit)).all()
        if not eps:
            return _record(s, "reflect", True, summary={"episodes": 0, "note": "nothing new to learn from"})
        known = "\n".join(f"- [{ls.kind}] {ls.text}" for ls in s.exec(select(Lesson).where(Lesson.status != "off")).all()) or "(none)"
        prompt = REFLECT.format(owner=settings.owner_name, known=known[:4000],
                                episodes="\n\n".join(_episode_line(e) for e in reversed(eps)))
        model = llm or LLM(planner_profile(), timeout=180)
        try:
            out = await model.complete_json([{"role": "user", "content": prompt}], max_tokens=3000)
        except Exception as e:  # noqa: BLE001
            log.warning("learn: reflection failed: %s", e)
            return _record(s, "reflect", False, summary={"episodes": len(eps), "error": str(e)[:300]})
        ids = {e.id for e in eps}
        added, reinforced, dropped, cases = [], [], [], []
        existing = s.exec(select(Lesson).where(Lesson.status != "off")).all()
        for raw in (out.get("lessons") or [] if isinstance(out, dict) else []):
            if not isinstance(raw, dict):
                continue
            text = " ".join(str(raw.get("text") or "").split())[:400]
            kind = str(raw.get("kind") or "preference").lower()
            if not text or kind not in KINDS:
                continue
            if not safe(text) or not safe(str(raw.get("when") or "")):
                dropped.append(text)
                continue
            conf = max(0.0, min(1.0, float(raw.get("confidence") or 0.5)))
            eps_from = [int(i) for i in raw.get("episodes") or [] if str(i).isdigit() and int(i) in ids]
            twin = next((ls for ls in existing if similar(ls.text, text) >= 0.6), None)
            if twin:
                twin.confidence = min(1.0, twin.confidence + 0.1)
                twin.episodes = ",".join(sorted({*twin.episodes.split(","), *map(str, eps_from)} - {""}))
                twin.updated_at = utcnow()
                s.add(twin)
                reinforced.append(twin.id)
                continue
            # Preferences, facts and voice with good support go on trial straight away; skills and
            # rules, or anything less certain, wait for the owner in the app.
            status = "trial" if kind in ("preference", "fact", "voice") and conf >= settings.learn_trial_confidence else "proposed"
            ls = Lesson(kind=kind, text=text, when=" ".join(str(raw.get("when") or "").split())[:200], status=status,
                        confidence=conf, episodes=",".join(map(str, eps_from)), note="from reflection")
            s.add(ls)
            existing.append(ls)
            added.append(text)
        bad = {e.id for e in eps if e.outcome == "bad"}
        for raw in (out.get("cases") or [] if isinstance(out, dict) else []):
            if not isinstance(raw, dict) or not str(raw.get("episode", "")).isdigit() or int(raw["episode"]) not in bad:
                continue
            ep = next(e for e in eps if e.id == int(raw["episode"]))
            if s.exec(select(LearnedCase).where(LearnedCase.episode_id == ep.id)).first():
                continue
            spec = {"must_tool": _clean_list(raw.get("must_tool")), "must_act": _clean_list(raw.get("must_act")),
                    "must_not_act": _clean_list(raw.get("must_not_act")), "answer_has": _valid_regexes(raw.get("answer_has")),
                    "answer_not": _valid_regexes(raw.get("answer_not"))}
            if not any(spec.values()):
                continue
            s.add(LearnedCase(episode_id=ep.id, request=ep.request, spec=json.dumps(spec), why=str(raw.get("why") or "")[:300]))
            cases.append(ep.id)
        for e in eps:
            e.reflected = True
            s.add(e)
        s.commit()
        return _record(s, "reflect", True, summary={"episodes": len(eps), "added": added, "reinforced": reinforced,
                                                    "dropped_unsafe": dropped, "cases_from": cases})


# ---------------------------------------------------------------- settle and gate

def settle(session: Session) -> list[int]:
    """Lessons that keep failing when they were used are turned off."""
    off = []
    for ls in session.exec(select(Lesson).where(col(Lesson.status).in_(["active", "trial"]))).all():
        if ls.losses >= settings.learn_off_after_losses and ls.losses > ls.wins:
            ls.status, ls.note, ls.updated_at = "off", f"turned off: 👎 {ls.losses} times, 👍 {ls.wins}", utcnow()
            session.add(ls)
            off.append(ls.id)
    session.commit()
    return off


def export(session: Session) -> dict:
    """What the evaluation needs: the lessons in use and the learnt cases."""
    lessons = [{"id": ls.id, "kind": ls.kind, "text": ls.text, "when": ls.when, "status": ls.status, "confidence": ls.confidence}
               for ls in session.exec(select(Lesson).where(col(Lesson.status).in_(["active", "trial"]))).all()]
    cases = [{"id": c.id, "request": c.request, "why": c.why, **json.loads(c.spec or "{}")}
             for c in session.exec(select(LearnedCase).where(LearnedCase.active == True)).all()]  # noqa: E712
    return {"lessons": lessons, "cases": cases}


def _run_eval(payload: dict, builtin: bool) -> dict:
    """agent_eval in its own process (it uses a throwaway database and a fake hub), with these lessons and cases."""
    with tempfile.TemporaryDirectory(prefix="learn-eval-") as d:
        inp, out = os.path.join(d, "learned.json"), os.path.join(d, "report.json")
        with open(inp, "w") as f:
            json.dump(payload, f)
        args = [sys.executable, "-m", "app.agent_eval", "--learned", inp, "--out", out]
        if not builtin:
            args.append("--learned-only")
        if settings.learn_eval_model:
            args += ["--model", settings.learn_eval_model]
        p = subprocess.run(args, cwd=os.path.dirname(os.path.dirname(os.path.abspath(__file__))), capture_output=True,
                           text=True, timeout=settings.learn_eval_timeout_minutes * 60)
        if p.returncode != 0 or not os.path.exists(out):
            raise RuntimeError((p.stderr or p.stdout or "agent_eval failed")[-600:])
        with open(out) as f:
            return json.load(f)


def evaluate(run_eval=_run_eval) -> dict:
    """The gate: run the cases with the current lessons. If the pass rate fell below the last good
    run's, roll back the lessons on trial; otherwise promote them to active."""
    with Session(engine) as s:
        payload = export(s)
        trial = [ls for ls in s.exec(select(Lesson).where(Lesson.status == "trial")).all()]
        try:
            report = run_eval(payload, settings.learn_eval_builtin)
        except Exception as e:  # noqa: BLE001
            log.warning("learn: evaluation failed: %s", e)
            return _record(s, "evaluate", False, summary={"error": str(e)[:600], "trial": [ls.id for ls in trial]})
        results = report.get("results") or []
        passed, total = sum(1 for r in results if r.get("ok")), len(results)
        prev = s.exec(select(HarnessRun).where(HarnessRun.kind == "evaluate", HarnessRun.ok == True,  # noqa: E712
                                               HarnessRun.total > 0).order_by(col(HarnessRun.id).desc())).first()
        rate = passed / total if total else 1.0
        prev_rate = prev.passed / prev.total if prev else None
        worse = prev_rate is not None and rate + 1e-9 < prev_rate - settings.learn_eval_tolerance
        changed = []
        for ls in trial:
            if worse:
                ls.status, ls.note = "off", f"rolled back: evaluation fell to {passed}/{total} from {prev.passed}/{prev.total}"  # type: ignore[union-attr]
            else:
                ls.status, ls.note = "active", f"passed evaluation {passed}/{total}"
            ls.updated_at = utcnow()
            s.add(ls)
            changed.append(ls.id)
        failing = [r.get("case") for r in results if not r.get("ok")]
        s.commit()
        return _record(s, "evaluate", not worse, passed=passed, total=total,
                       summary={"rolled_back" if worse else "promoted": changed, "failing": failing[:30],
                                "previous": f"{prev.passed}/{prev.total}" if prev else None})


def _record(s: Session, kind: str, ok: bool, *, passed: int = 0, total: int = 0, summary: dict) -> dict:
    run = HarnessRun(kind=kind, ok=ok, passed=passed, total=total, summary=json.dumps(summary, ensure_ascii=False, default=str))
    s.add(run)
    s.commit()
    s.refresh(run)
    log.info("learn: %s ok=%s %s/%s %s", kind, ok, passed, total, run.summary[:300])
    return {"id": run.id, "kind": kind, "ok": ok, "passed": passed, "total": total, **summary}


async def nightly() -> dict:
    """Settle, reflect, and (when something is on trial or there are learnt cases) evaluate."""
    if not settings.learn_enabled:
        return {"skipped": "learning is off (LEARN_ENABLED)"}
    with Session(engine) as s:
        turned_off = settle(s)
    reflected = await reflect()
    out: dict[str, Any] = {"turned_off": turned_off, "reflect": reflected}
    with Session(engine) as s:
        on_trial = s.exec(select(Lesson).where(Lesson.status == "trial")).first() is not None
    if settings.learn_eval_enabled and on_trial:
        import asyncio

        out["evaluate"] = await asyncio.to_thread(evaluate)
    return out


def status(session: Session) -> dict:
    lessons = session.exec(select(Lesson)).all()
    by = {k: sum(1 for ls in lessons if ls.status == k) for k in ("proposed", "trial", "active", "off")}
    eps = session.exec(select(Episode).where(Episode.source != "eval")).all()
    runs = session.exec(select(HarnessRun).order_by(col(HarnessRun.id).desc()).limit(10)).all()
    last_eval = next((r for r in runs if r.kind == "evaluate" and r.total), None)
    return {
        "enabled": settings.learn_enabled,
        "lessons": by,
        "episodes": len(eps),
        "good": sum(1 for e in eps if e.outcome == "good"),
        "bad": sum(1 for e in eps if e.outcome == "bad"),
        "waiting": sum(1 for e in eps if e.outcome and not e.reflected),
        "cases": len(session.exec(select(LearnedCase).where(LearnedCase.active == True)).all()),  # noqa: E712
        "last_eval": {"at": last_eval.at, "passed": last_eval.passed, "total": last_eval.total} if last_eval else None,
        "runs": [{"id": r.id, "at": r.at, "kind": r.kind, "ok": r.ok, "passed": r.passed, "total": r.total,
                  "summary": json.loads(r.summary or "{}")} for r in runs],
    }
