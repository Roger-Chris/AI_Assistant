"""Shared memory API (memory.py). Devices reach it through the Linux hub's /node/memory/* proxy."""
from __future__ import annotations

from datetime import datetime
from typing import Any, Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field
from sqlalchemy.exc import IntegrityError
from sqlmodel import Session

from .. import memory
from ..auth import require_token
from ..db import get_session
from ..memory_import import import_markdown

router = APIRouter(prefix="/memory", tags=["memory"], dependencies=[Depends(require_token)])


class RecordIn(BaseModel):
    uid: Optional[str] = None
    kind: str = "note"
    title: str = ""
    body: str = ""
    data: dict[str, Any] = Field(default_factory=dict)
    tags: str | list[str] = ""
    aliases: str | list[str] = ""
    subject: Optional[str] = None
    source: str = "manual"
    device: Optional[str] = None
    deleted: bool = False
    updated_at: Optional[datetime] = None


class Batch(BaseModel):
    device: str
    records: list[RecordIn]


class Remember(BaseModel):
    text: str
    title: Optional[str] = None
    kind: str = "note"  # note | fact | decision
    subject: Optional[str] = None  # a uid or a name; a name is resolved
    tags: str | list[str] = ""
    device: str = "brain"
    source: str = "manual"


class MarkdownFile(BaseModel):
    name: str
    text: str


class ImportIn(BaseModel):
    kind: str = "project"
    files: list[MarkdownFile]
    device: str = "brain"


@router.get("/changes")
def changes(since: int = 0, limit: int = 500, session: Session = Depends(get_session)) -> dict:
    return memory.changes(session, since, limit)


@router.post("/batch")
def batch(body: Batch, session: Session = Depends(get_session)) -> dict:
    for attempt in range(3):
        try:
            results = memory.apply_batch(session, [r.model_dump(exclude_none=True) for r in body.records], body.device)
            session.commit()
            return {"results": results}
        except IntegrityError:  # another writer took the same seq; the batch is idempotent, retry
            session.rollback()
            if attempt == 2:
                raise
    raise HTTPException(500, "unreachable")


@router.get("/search")
def search(q: str, kind: Optional[str] = None, limit: int = Query(10, le=50),
           session: Session = Depends(get_session)) -> list[dict]:
    return memory.search(session, q, kind=kind, limit=limit)


@router.get("/about")
def about(name: str, session: Session = Depends(get_session)) -> dict:
    found = memory.about(session, name)
    if not found:
        raise HTTPException(404, f"nothing in memory about {name!r}")
    return found


@router.get("/record/{uid:path}")
def record(uid: str, session: Session = Depends(get_session)) -> dict:
    found = memory.get(session, uid)
    if not found:
        raise HTTPException(404, "no such record")
    return found


@router.post("/remember")
def remember(body: Remember, session: Session = Depends(get_session)) -> dict:
    if body.kind not in ("note", "fact", "decision"):
        raise HTTPException(400, "kind must be note, fact or decision")
    subject = None
    if body.subject:
        rec = memory.resolve(session, body.subject)
        subject = rec.uid if rec else None
    title = body.title or " ".join(body.text.split())[:80]
    rec, _ = memory.put(session, kind=body.kind, title=title, body=body.text, subject=subject,
                        tags=body.tags, device=body.device, source=body.source)
    session.commit()
    return memory.to_dict(rec)


@router.delete("/record/{uid:path}")
def forget(uid: str, device: str = "brain", session: Session = Depends(get_session)) -> dict:
    ok = memory.delete(session, uid, device)
    session.commit()
    return {"deleted": ok}


@router.post("/import")
def import_files(body: ImportIn, session: Session = Depends(get_session)) -> dict:
    out = [import_markdown(session, f.name, f.text, kind=body.kind, device=body.device) for f in body.files]
    session.commit()
    return {"imported": out}


@router.get("/stats")
def stats(session: Session = Depends(get_session)) -> dict:
    return memory.stats(session)


@router.post("/mirror")
def mirror(session: Session = Depends(get_session)) -> dict:
    return {"written": memory.mirror(session)}
