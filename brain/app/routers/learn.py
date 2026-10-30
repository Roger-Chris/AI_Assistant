"""The learning harness over HTTP (learn.py): feedback from the app, lessons to see and change, runs."""
from __future__ import annotations

import json
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlmodel import Session, col, select

from .. import learn
from ..auth import require_token
from ..db import Episode, LearnedCase, Lesson, get_session, utcnow

router = APIRouter(prefix="/learn", tags=["learn"], dependencies=[Depends(require_token)])


class Feedback(BaseModel):
    good: bool
    note: str = ""
    signal: str = ""  # thumbs_up | thumbs_down | denied | …


@router.get("/status")
def status(session: Session = Depends(get_session)) -> dict:
    return learn.status(session)


@router.post("/episodes/{episode_id}/feedback")
def feedback(episode_id: int, body: Feedback, session: Session = Depends(get_session)) -> dict:
    try:
        ep = learn.feedback(session, episode_id, body.good, body.note, body.signal)
    except KeyError:
        raise HTTPException(404, "no such episode") from None
    return {"id": ep.id, "outcome": ep.outcome}


@router.get("/episodes")
def episodes(limit: int = 30, session: Session = Depends(get_session)) -> list[dict]:
    rows = session.exec(select(Episode).where(Episode.source != "eval").order_by(col(Episode.id).desc()).limit(min(limit, 200))).all()
    return [{"id": e.id, "at": e.at, "source": e.source, "request": e.request, "answer": e.answer[:600], "outcome": e.outcome,
             "feedback": e.feedback, "signals": json.loads(e.signals or "[]"), "reflected": e.reflected} for e in rows]


def _lesson(ls: Lesson) -> dict:
    return {"id": ls.id, "kind": ls.kind, "text": ls.text, "when": ls.when, "status": ls.status, "confidence": round(ls.confidence, 2),
            "uses": ls.uses, "wins": ls.wins, "losses": ls.losses, "note": ls.note, "updated_at": ls.updated_at}


@router.get("/lessons")
def lessons(status: Optional[str] = None, session: Session = Depends(get_session)) -> list[dict]:
    q = select(Lesson).order_by(col(Lesson.updated_at).desc())
    if status:
        q = q.where(Lesson.status == status)
    return [_lesson(ls) for ls in session.exec(q).all()]


class LessonIn(BaseModel):
    text: str
    kind: str = "preference"
    when: str = ""


@router.post("/lessons")
def add_lesson(body: LessonIn, session: Session = Depends(get_session)) -> dict:
    """A lesson you write yourself: active at once (never one that touches safety)."""
    text = " ".join(body.text.split())[:400]
    if not text or body.kind not in learn.KINDS:
        raise HTTPException(400, f"a lesson needs text and a kind ({', '.join(learn.KINDS)})")
    if not learn.safe(text) or not learn.safe(body.when):
        raise HTTPException(400, "lessons can't change how approvals, passwords, deleting or payments work")
    ls = Lesson(kind=body.kind, text=text, when=body.when.strip()[:200], status="active", confidence=0.9, note="written by you")
    session.add(ls)
    session.commit()
    session.refresh(ls)
    return _lesson(ls)


class LessonPatch(BaseModel):
    status: Optional[str] = None  # active | off | trial
    text: Optional[str] = None
    when: Optional[str] = None


@router.patch("/lessons/{lesson_id}")
def change_lesson(lesson_id: int, body: LessonPatch, session: Session = Depends(get_session)) -> dict:
    ls = session.get(Lesson, lesson_id)
    if not ls:
        raise HTTPException(404, "no such lesson")
    if body.status:
        if body.status not in ("active", "off", "trial"):
            raise HTTPException(400, "status: active, off or trial")
        ls.status, ls.note = body.status, {"active": "kept by you", "off": "turned off by you", "trial": "back on trial"}[body.status]
    if body.text is not None:
        t = " ".join(body.text.split())[:400]
        if not t or not learn.safe(t):
            raise HTTPException(400, "lessons can't change how approvals, passwords, deleting or payments work")
        ls.text = t
    if body.when is not None:
        if not learn.safe(body.when):
            raise HTTPException(400, "lessons can't change how approvals, passwords, deleting or payments work")
        ls.when = body.when.strip()[:200]
    ls.updated_at = utcnow()
    session.add(ls)
    session.commit()
    session.refresh(ls)
    return _lesson(ls)


@router.get("/cases")
def cases(session: Session = Depends(get_session)) -> list[dict]:
    return [{"id": c.id, "episode_id": c.episode_id, "request": c.request, "why": c.why, "active": c.active,
             "spec": json.loads(c.spec or "{}")} for c in session.exec(select(LearnedCase).order_by(col(LearnedCase.id).desc())).all()]


@router.post("/cases/{case_id}/off")
def case_off(case_id: int, session: Session = Depends(get_session)) -> dict:
    c = session.get(LearnedCase, case_id)
    if not c:
        raise HTTPException(404, "no such case")
    c.active = False
    session.add(c)
    session.commit()
    return {"id": c.id, "active": False}


@router.post("/reflect")
async def reflect_now() -> dict:
    return await learn.reflect()


@router.post("/run")
async def run_now() -> dict:
    """The whole nightly pass now: settle, reflect, and evaluate if anything is on trial."""
    return await learn.nightly()


@router.get("/export")
def export(session: Session = Depends(get_session)) -> dict:
    """The lessons in use and the learnt cases, in agent_eval's --learned format."""
    return learn.export(session)
