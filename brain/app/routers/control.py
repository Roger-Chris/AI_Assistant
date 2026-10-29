"""The phone app's controls: WhatsApp chats (mute), the brief on demand, model key usage and the
memory graph's size. Reached through the hub's /api/brain/*."""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlmodel import Session, select

from .. import graph
from ..auth import require_token
from ..db import Chat, get_session
from ..llm import usage_report

router = APIRouter(tags=["control"], dependencies=[Depends(require_token)])


@router.get("/chats")
def chats(limit: int = 60, session: Session = Depends(get_session)) -> list[dict]:
    """WhatsApp chats, busiest first. Muted chats are stored for context but never triaged."""
    rows = session.exec(select(Chat).order_by(Chat.message_count.desc()).limit(max(1, min(limit, 500)))).all()  # type: ignore[attr-defined]
    return [{"id": c.id, "chat_id": c.chat_id, "name": c.name or c.chat_id.split("@")[0], "is_group": c.is_group,
             "muted": c.muted, "messages": c.message_count, "last_message_at": c.last_message_at} for c in rows]


class Mute(BaseModel):
    muted: bool


@router.post("/chats/{chat_pk}/mute")
def mute(chat_pk: int, body: Mute, session: Session = Depends(get_session)) -> dict:
    c = session.get(Chat, chat_pk)
    if not c:
        raise HTTPException(404, "no such chat")
    c.muted = body.muted
    session.add(c)
    session.commit()
    return {"id": c.id, "name": c.name or c.chat_id, "muted": c.muted}


@router.post("/brief")
async def brief() -> dict:
    """Today's brief now (it arrives like the 8 am one: in the app)."""
    from .. import scheduler  # scheduler imports the routers' modules indirectly

    await scheduler.daily_brief()
    return {"ok": True}


@router.get("/llm/usage")
def llm_usage() -> dict:
    return {"report": usage_report()}


@router.get("/graph")
def graph_stats(session: Session = Depends(get_session)) -> dict:
    st = graph.stats(session)
    o = graph.owner(session)
    return {**st, "owner": o.name if o else None, "owner_summary": o.summary if o else None}
