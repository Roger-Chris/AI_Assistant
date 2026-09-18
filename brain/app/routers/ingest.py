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
from ..classifier import classify_message, open_tasks_for_chat
from ..config import settings
from ..db import Chat, Message, engine, get_session
from ..prefilter import prefilter
from .. import notify, perceive

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
    is_self: bool = False
    text: str = ""
    media_type: Optional[str] = None
    media_b64: Optional[str] = None  # voice notes, if FORWARD_AUDIO is on
    media_mime: Optional[str] = None
    forwarded: bool = False
    timestamp: int  # unix seconds


def touch_chat(session: Session, chat_id: str, name: Optional[str], is_group: bool,
               sent_at: datetime, n: int = 1) -> Chat:
    chat = session.exec(select(Chat).where(Chat.chat_id == chat_id)).first()
    if not chat:
        chat = Chat(chat_id=chat_id, is_group=is_group)
    if name and not chat.name:
        chat.name = name
    chat.message_count += n
    if not chat.last_message_at or sent_at > chat.last_message_at.replace(tzinfo=sent_at.tzinfo):
        chat.last_message_at = sent_at
    session.add(chat)
    return chat


def _mark_skipped(session: Session, msg: Message, reason: str) -> None:
    msg.classified = True
    msg.classification = f'{{"kind":"noise","reason":"prefilter:{reason}"}}'
    session.add(msg)
    session.commit()


def _gate(session: Session, msg: Message, chat: Chat, forwarded: bool) -> Optional[str]:
    """Muted chat, then the rule-based pre-filter (which knows whether this chat has open tasks)."""
    if chat.muted:
        return "muted-chat"
    has_open = bool(open_tasks_for_chat(session, msg.chat_id, limit=1))
    return prefilter(msg.text, is_group=msg.is_group, from_me=msg.from_me, forwarded=forwarded,
                     media_type=msg.media_type, has_open_task=has_open, is_self=msg.is_self)


async def _process(message_id: int, *, perceive_kind: Optional[str] = None, mime: Optional[str] = None,
                   forwarded: bool = False, caption: str = "") -> None:
    """Runs after the HTTP response: (perceive media ->) gate -> classify -> notify."""
    with Session(engine) as session:
        msg = session.get(Message, message_id)
        if not msg or msg.classified:
            return
        chat = session.exec(select(Chat).where(Chat.chat_id == msg.chat_id)).first()

        if perceive_kind and msg.media_path:
            try:
                with open(msg.media_path, "rb") as f:
                    data = f.read()
                text = await perceive.perceive(session, perceive_kind, mime or "application/octet-stream", data)
            except Exception:  # noqa: BLE001
                log.exception("perceive failed for message %s", message_id)
                text = None
            if perceive_kind == "audio":
                msg.text = f"[voice note] {text}" if text else "[voice note — could not transcribe]"
            else:
                desc = text or "[image]"
                if "[no actionable content]" in desc:
                    msg.text = (f"[image] {caption}" if caption else "").strip()
                    session.add(msg)
                    if not caption:
                        _mark_skipped(session, msg, "image-no-content")
                        return
                else:
                    msg.text = f"[image: {desc}]" + (f" {caption}" if caption else "")
            session.add(msg)
            session.commit()

        if chat:
            skip = _gate(session, msg, chat, forwarded)
            if skip:
                _mark_skipped(session, msg, skip)
                return

        try:
            outcome = await classify_message(session, msg, settings.owner_name)
        except Exception:  # noqa: BLE001 — never let one bad message kill the worker
            log.exception("classification failed for message %s", message_id)
            return
        if not outcome or not outcome.task:
            return
        try:
            if outcome.action == "new":
                await notify.new_task(session, outcome.task, msg)
            elif outcome.action == "update":
                await notify.task_updated(session, outcome.task, outcome.changed, msg)
            elif outcome.action == "complete":
                await notify.task_completed(session, outcome.task, msg)
        except Exception:  # noqa: BLE001
            log.exception("notify failed for task %s", outcome.task.id)


_EXT = {"audio/ogg; codecs=opus": "ogg", "audio/ogg": "ogg", "audio/mp4": "m4a", "audio/mpeg": "mp3",
        "image/jpeg": "jpg", "image/png": "png", "image/webp": "webp"}


@router.post("/whatsapp")
async def ingest_whatsapp(payload: WhatsAppMessage, bg: BackgroundTasks,
                          session: Session = Depends(get_session)) -> dict:
    existing = session.exec(select(Message).where(Message.external_id == payload.id)).first()
    if existing:
        return {"ok": True, "duplicate": True, "message_id": existing.id}

    media_path = None
    if payload.media_b64:
        os.makedirs(MEDIA_DIR, exist_ok=True)
        mime = (payload.media_mime or "").split(";")[0].strip()
        ext = _EXT.get(payload.media_mime or "", _EXT.get(mime, "bin"))
        media_path = os.path.join(MEDIA_DIR, f"{payload.id}.{ext}")
        with open(media_path, "wb") as f:
            f.write(base64.b64decode(payload.media_b64))

    text = payload.text
    if payload.media_type == "audio" and not text:
        text = "[voice note]"
    elif payload.media_type in ("document", "image", "video") and payload.from_me and not text:
        text = f"[sent a {payload.media_type}]"  # lets the classifier close "send X" tasks

    msg = Message(
        source="whatsapp",
        external_id=payload.id,
        chat_id=payload.chat_id,
        chat_name=payload.chat_name,
        sender_id=payload.sender_id,
        sender_name=payload.sender_name,
        from_me=payload.from_me,
        is_group=payload.is_group,
        is_self=payload.is_self,
        text=text,
        media_type=payload.media_type,
        media_path=media_path,
        sent_at=datetime.fromtimestamp(payload.timestamp, tz=timezone.utc),
    )
    session.add(msg)
    chat = touch_chat(session, payload.chat_id, payload.chat_name, payload.is_group, msg.sent_at)
    session.commit()
    session.refresh(msg)

    if chat.muted:
        _mark_skipped(session, msg, "muted-chat")
        return {"ok": True, "message_id": msg.id, "skipped": "muted-chat"}

    # media we can perceive goes to the background first (transcribe / describe), then gate+classify
    if media_path and payload.media_type in ("audio", "image"):
        bg.add_task(_process, msg.id, perceive_kind=payload.media_type,
                    mime=(payload.media_mime or "").split(";")[0].strip() or None,
                    forwarded=payload.forwarded, caption=payload.text)
        return {"ok": True, "message_id": msg.id, "perceiving": payload.media_type}

    skip = _gate(session, msg, chat, payload.forwarded)
    if skip:
        _mark_skipped(session, msg, skip)
        return {"ok": True, "message_id": msg.id, "skipped": skip}
    bg.add_task(_process, msg.id, forwarded=payload.forwarded)
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
        touch_chat(session, m.chat_id, m.chat_name, m.is_group, datetime.fromtimestamp(m.timestamp, tz=timezone.utc))
    # names arrive late for some groups (rate limits / later chunks): backfill rows that lack one
    names = {m.chat_id: m.chat_name for m in batch.messages if m.is_group and m.chat_name}
    for chat_id, name in names.items():
        for row in session.exec(select(Message).where(Message.chat_id == chat_id, Message.chat_name.is_(None))).all():  # type: ignore[union-attr]
            row.chat_name = name
            session.add(row)
    session.commit()
    log.info("history batch: %d received, %d stored", len(batch.messages), added)
    return {"ok": True, "received": len(batch.messages), "stored": added}


class Directory(BaseModel):
    names: dict[str, str] = {}    # jid -> display name (groups and contacts)
    aliases: dict[str, str] = {}  # lid jid -> phone-number jid


@router.post("/whatsapp/directory")
def ingest_directory(d: Directory, session: Session = Depends(get_session)) -> dict:
    """Bridge sends what it knows about chats/contacts; we repair stored rows in place:
    merge LID-addressed rows into their phone-number chat, and fill missing names."""
    merged = renamed = 0
    for lid, pn in d.aliases.items():
        if lid == pn:
            continue
        for row in session.exec(select(Message).where(Message.chat_id == lid)).all():
            row.chat_id = pn
            merged += 1
            session.add(row)
        for row in session.exec(select(Message).where(Message.sender_id == lid)).all():
            row.sender_id = pn
            session.add(row)
    session.flush()
    for jid, name in d.names.items():
        jid = d.aliases.get(jid, jid)
        for row in session.exec(select(Message).where(Message.chat_id == jid, Message.chat_name.is_(None))).all():  # type: ignore[union-attr]
            row.chat_name = name
            renamed += 1
            session.add(row)
    for lid, pn in d.aliases.items():
        c_lid = session.exec(select(Chat).where(Chat.chat_id == lid)).first()
        if c_lid:
            c_pn = session.exec(select(Chat).where(Chat.chat_id == pn)).first()
            if c_pn:
                c_pn.message_count += c_lid.message_count
                c_pn.muted = c_pn.muted or c_lid.muted
                session.delete(c_lid)
                session.add(c_pn)
            else:
                c_lid.chat_id = pn
                session.add(c_lid)
    session.flush()
    for jid, name in d.names.items():
        c = session.exec(select(Chat).where(Chat.chat_id == d.aliases.get(jid, jid))).first()
        if c and not c.name:
            c.name = name
            session.add(c)
    session.commit()
    log.info("directory: %d names, %d aliases -> %d rows merged, %d rows named",
             len(d.names), len(d.aliases), merged, renamed)
    return {"ok": True, "merged": merged, "renamed": renamed}
