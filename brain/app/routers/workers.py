"""Worker registry. The Omen heartbeats here when it's on; the brain routes heavy jobs by liveness.
Phase 0 stub — the job queue itself arrives in Phase 4."""
from __future__ import annotations

from datetime import timedelta

from fastapi import APIRouter, Depends
from pydantic import BaseModel
from sqlmodel import Session, select

from ..auth import require_token
from ..db import Worker, get_session, utcnow

router = APIRouter(prefix="/workers", tags=["workers"], dependencies=[Depends(require_token)])
ALIVE_WINDOW = timedelta(seconds=90)


class Heartbeat(BaseModel):
    name: str
    url: str
    capabilities: list[str] = []


@router.post("/heartbeat")
def heartbeat(body: Heartbeat, session: Session = Depends(get_session)) -> dict:
    w = session.exec(select(Worker).where(Worker.name == body.name)).first() or Worker(name=body.name, url=body.url)
    w.url = body.url
    w.capabilities = ",".join(body.capabilities)
    w.last_seen = utcnow()
    session.add(w)
    session.commit()
    return {"ok": True}


@router.get("")
def list_workers(session: Session = Depends(get_session)) -> list[dict]:
    now = utcnow()
    out = []
    for w in session.exec(select(Worker)).all():
        last = w.last_seen if w.last_seen.tzinfo else w.last_seen.replace(tzinfo=now.tzinfo)
        out.append({"name": w.name, "url": w.url, "capabilities": w.capabilities.split(",") if w.capabilities else [],
                    "alive": (now - last) < ALIVE_WINDOW, "last_seen": w.last_seen})
    return out


def find_alive(session: Session, capability: str):
    """Used by the speech module later: first live worker that advertises `capability`."""
    now = utcnow()
    for w in session.exec(select(Worker)).all():
        last = w.last_seen if w.last_seen.tzinfo else w.last_seen.replace(tzinfo=now.tzinfo)
        if capability in w.capabilities.split(",") and (now - last) < ALIVE_WINDOW:
            return w
    return None
