"""Inbound messages. The WhatsApp bridge POSTs here; email will too, later."""
from __future__ import annotations

import base64
import logging
import os
from datetime import datetime, timezone
from typing import Optional

from fastapi import APIRouter, BackgroundTasks, Depends
from pydantic import BaseModel
from sqlmodel import Session, select

from ..auth import require_token
from ..classifier import classify_message
from ..config import settings
from ..db import Message, engine, get_session
from .. import notify

log = logging.getLogger("brain.ingest")
router = APIRouter(prefix="/ingest", tags=["ingest"], dependencies=[Depends(require_token)])

MEDIA_DIR = settings.media_dir


class WhatsAppMessage(BaseModel):
    id: str
    chat_id: str
    chat_name: Optional[str] = None
    sender_id: str
    sender_name: Optional[str] = None
    from_me: bool = False
    is_group: bool = False
    text: str = ""
    media_type: Optional[str] = None
    media_b64: Optional[str] = None  # voice notes, if FORWARD_AUDIO is on
    media_mime: Optional[str] = None
    timestamp: int  # unix seconds


async def _process(message_id: int) -> None:
    """Runs after the HTTP response: classify, then notify if a task was created."""
    with Session(engine) as session:
        msg = session.get(Message, message_id)
        if not msg or msg.classified:
            return
        try:
            task = await classify_message(session, msg, settings.owner_name)
        except Exception:  # noqa: BLE001 — never let one bad message kill the worker
            log.exception("classification failed for message %s", message_id)
            return
        if task:
            try:
                await notify.new_task(session, task, msg)
            except Exception:  # noqa: BLE001
                log.exception("notify failed for task %s", task.id)


@router.post("/whatsapp")
async def ingest_whatsapp(payload: WhatsAppMessage, bg: BackgroundTasks,
                          session: Session = Depends(get_session)) -> dict:
    existing = session.exec(select(Message).where(Message.external_id == payload.id)).first()
    if existing:
        return {"ok": True, "duplicate": True, "message_id": existing.id}

    media_path = None
    if payload.media_b64:
        os.makedirs(MEDIA_DIR, exist_ok=True)
        ext = {"audio/ogg; codecs=opus": "ogg", "audio/ogg": "ogg", "audio/mp4": "m4a"}.get(
            payload.media_mime or "", "bin")
        media_path = os.path.join(MEDIA_DIR, f"{payload.id}.{ext}")
        with open(media_path, "wb") as f:
            f.write(base64.b64decode(payload.media_b64))

    text = payload.text
    if payload.media_type == "audio" and not text:
        # Phase 4 wires the worker's Whisper in here. Until then, mark it so it's still visible.
        text = "[voice note — transcription not configured yet]"

    msg = Message(
        source="whatsapp",
        external_id=payload.id,
        chat_id=payload.chat_id,
        chat_name=payload.chat_name,
        sender_id=payload.sender_id,
        sender_name=payload.sender_name,
        from_me=payload.from_me,
        is_group=payload.is_group,
        text=text,
        media_type=payload.media_type,
        media_path=media_path,
        sent_at=datetime.fromtimestamp(payload.timestamp, tz=timezone.utc),
    )
    session.add(msg)
    session.commit()
    session.refresh(msg)

    # Skip the LLM entirely for obvious noise: empty text or pure media with no caption.
    if text.strip() and not (payload.media_type and payload.media_type != "audio" and not payload.text):
        bg.add_task(_process, msg.id)
    else:
        msg.classified = True
        msg.classification = '{"kind":"noise","reason":"empty/media-only, skipped"}'
        session.add(msg)
        session.commit()
    return {"ok": True, "message_id": msg.id}


class HistoryBatch(BaseModel):
    messages: list[WhatsAppMessage]


@router.post("/whatsapp/history")
def ingest_history(batch: HistoryBatch, session: Session = Depends(get_session)) -> dict:
    """Backfill from WhatsApp's history sync. Stored for CONTEXT only: marked classified so the
    triage pass never touches them (no quota burn, no flood of stale task cards)."""
    known = {
        r for r in session.exec(
            select(Message.external_id).where(Message.external_id.in_([m.id for m in batch.messages]))  # type: ignore[attr-defined]
        ).all()
    }
    added = 0
    for m in batch.messages:
        if m.id in known or not m.text.strip():
            continue
        known.add(m.id)
        session.add(Message(
            source="whatsapp", external_id=m.id, chat_id=m.chat_id, chat_name=m.chat_name,
            sender_id=m.sender_id, sender_name=m.sender_name, from_me=m.from_me, is_group=m.is_group,
            text=m.text, media_type=m.media_type,
            sent_at=datetime.fromtimestamp(m.timestamp, tz=timezone.utc),
            classified=True, classification='{"kind":"history","reason":"backfilled, context only"}',
        ))
        added += 1
    # names arrive late for some groups (rate limits / later chunks): backfill rows that lack one
    names = {m.chat_id: m.chat_name for m in batch.messages if m.is_group and m.chat_name}
    for chat_id, name in names.items():
        for row in session.exec(select(Message).where(Message.chat_id == chat_id, Message.chat_name.is_(None))).all():  # type: ignore[union-attr]
            row.chat_name = name
            session.add(row)
    session.commit()
    log.info("history batch: %d received, %d stored", len(batch.messages), added)
    return {"ok": True, "received": len(batch.messages), "stored": added}
