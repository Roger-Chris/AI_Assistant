"""Task API — what the phone app talks to (through the hub's /api/brain/tasks)."""
from __future__ import annotations

from datetime import datetime
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, field_validator
from sqlmodel import Session, select

from ..alerts import close_for_task
from ..auth import require_token
from ..classifier import to_utc, touch
from ..db import Message, Task, TaskKind, TaskStatus, get_session

router = APIRouter(prefix="/tasks", tags=["tasks"], dependencies=[Depends(require_token)])


class TaskOut(BaseModel):
    id: int
    kind: TaskKind
    status: TaskStatus
    title: str
    details: Optional[str]
    due_at: Optional[datetime]
    priority: str
    people: Optional[str]
    confidence: float
    source_chat: Optional[str] = None
    source_text: Optional[str] = None
    # Where the task came from: "email" | "whatsapp", or "manual" when made by hand.
    source: str = "manual"
    created_at: datetime


def to_out(session: Session, t: Task) -> TaskOut:
    src = session.get(Message, t.source_message_id) if t.source_message_id else None
    return TaskOut(
        id=t.id, kind=t.kind, status=t.status, title=t.title, details=t.details, due_at=t.due_at,
        priority=t.priority, people=t.people, confidence=t.confidence,
        source_chat=(src.chat_name or src.chat_id) if src else None,
        source_text=src.text[:300] if src else None, source=src.source if src else "manual",
        created_at=t.created_at,
    )


@router.get("", response_model=list[TaskOut])
def list_tasks(status: Optional[TaskStatus] = None, limit: int = 100,
               session: Session = Depends(get_session)):
    q = select(Task)
    if status:
        q = q.where(Task.status == status)
    else:
        q = q.where(Task.status.in_([TaskStatus.pending_confirmation, TaskStatus.open]))  # type: ignore[attr-defined]
    q = q.order_by(Task.due_at.is_(None), Task.due_at, Task.created_at.desc()).limit(limit)  # type: ignore[attr-defined]
    return [to_out(session, t) for t in session.exec(q).all()]


def _due_utc(v: Optional[datetime]) -> Optional[datetime]:
    return to_utc(v) if v else v


class TaskCreate(BaseModel):
    title: str
    kind: TaskKind = TaskKind.task
    details: Optional[str] = None
    due_at: Optional[datetime] = None
    priority: str = "normal"
    # Found by something else (a recorded conversation): waits for his Keep or Not a task.
    unconfirmed: bool = False

    _utc = field_validator("due_at")(_due_utc)


@router.post("", response_model=TaskOut, status_code=201)
def create_task(body: TaskCreate, session: Session = Depends(get_session)):
    fields = body.model_dump(exclude={"unconfirmed"})
    status = TaskStatus.pending_confirmation if body.unconfirmed else TaskStatus.open
    t = Task(**fields, status=status, confidence=0.6 if body.unconfirmed else 1.0, notified=True)
    session.add(t)
    session.commit()
    session.refresh(t)
    return to_out(session, t)


def _transition(session: Session, task_id: int, status: TaskStatus) -> TaskOut:
    t = session.get(Task, task_id)
    if not t:
        raise HTTPException(404, "no such task")
    t.status = status
    touch(t)
    session.add(t)
    session.commit()
    if status in (TaskStatus.done, TaskStatus.rejected):
        close_for_task(session, task_id)
    session.refresh(t)
    return to_out(session, t)


@router.post("/{task_id}/confirm", response_model=TaskOut)
def confirm(task_id: int, session: Session = Depends(get_session)):
    return _transition(session, task_id, TaskStatus.open)


@router.post("/{task_id}/reject", response_model=TaskOut)
def reject(task_id: int, session: Session = Depends(get_session)):
    return _transition(session, task_id, TaskStatus.rejected)


@router.post("/{task_id}/done", response_model=TaskOut)
def done(task_id: int, session: Session = Depends(get_session)):
    return _transition(session, task_id, TaskStatus.done)


class TaskPatch(BaseModel):
    title: Optional[str] = None
    details: Optional[str] = None
    due_at: Optional[datetime] = None
    priority: Optional[str] = None

    _utc = field_validator("due_at")(_due_utc)


@router.patch("/{task_id}", response_model=TaskOut)
def patch(task_id: int, body: TaskPatch, session: Session = Depends(get_session)):
    t = session.get(Task, task_id)
    if not t:
        raise HTTPException(404, "no such task")
    for k, v in body.model_dump(exclude_unset=True).items():
        setattr(t, k, v)
    touch(t)
    session.add(t)
    session.commit()
    session.refresh(t)
    return to_out(session, t)
