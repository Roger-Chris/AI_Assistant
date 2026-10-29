"""The thinking loop over HTTP: the phone app (through the hub) and anything else on this machine."""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from .. import agent
from ..auth import require_token
from ..config import settings

router = APIRouter(prefix="/agent", tags=["agent"], dependencies=[Depends(require_token)])


class Turn(BaseModel):
    role: str  # user | assistant
    content: str


class Ask(BaseModel):
    text: str
    source: str = "app"  # app | voice | call | web (voice: short spoken answers)
    history: list[Turn] = []  # earlier turns of this conversation, oldest first (the app sends the last few)


@router.post("/ask")
async def ask(body: Ask) -> dict:
    if not settings.agent_enabled:
        raise HTTPException(404, "the thinking loop is off (AGENT_ENABLED)")
    if not body.text.strip():
        raise HTTPException(400, "empty request")
    return await agent.ask(body.text.strip(), source=body.source, history=[t.model_dump() for t in body.history])
