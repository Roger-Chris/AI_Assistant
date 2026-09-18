"""Storage. SQLite via SQLModel — one file, trivially copied to the Mac mini later."""
from __future__ import annotations

import os
from datetime import datetime, timezone
from enum import Enum
from typing import Optional

from sqlmodel import Field, Session, SQLModel, create_engine

from .config import settings


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


class Message(SQLModel, table=True):
    """Every inbound message we've seen (WhatsApp for now; email later)."""

    id: Optional[int] = Field(default=None, primary_key=True)
    source: str = Field(index=True)  # "whatsapp" | "email" | "telegram"
    external_id: str = Field(index=True, unique=True)  # WA message id, keeps ingest idempotent
    chat_id: str = Field(index=True)  # WA jid (xxx@s.whatsapp.net or xxx@g.us)
    chat_name: Optional[str] = None  # group subject or contact push name
    sender_id: str
    sender_name: Optional[str] = None
    from_me: bool = False
    is_group: bool = False
    text: str = ""
    media_type: Optional[str] = None  # "audio" | "image" | "document" ...
    media_path: Optional[str] = None  # where we stored the bytes, if any
    sent_at: datetime = Field(index=True)
    received_at: datetime = Field(default_factory=utcnow)
    classified: bool = Field(default=False, index=True)
    classification: Optional[str] = None  # raw JSON from the classifier, for debugging


class TaskKind(str, Enum):
    task = "task"
    event = "event"
    question = "question"


class TaskStatus(str, Enum):
    pending_confirmation = "pending_confirmation"  # extracted, waiting for the user to accept
    open = "open"
    done = "done"
    rejected = "rejected"


class Task(SQLModel, table=True):
    """A commitment / event / open question extracted from a message (or created by hand)."""

    id: Optional[int] = Field(default=None, primary_key=True)
    kind: TaskKind = Field(default=TaskKind.task, index=True)
    status: TaskStatus = Field(default=TaskStatus.pending_confirmation, index=True)
    title: str
    details: Optional[str] = None
    due_at: Optional[datetime] = Field(default=None, index=True)
    priority: str = "normal"  # low | normal | high | urgent
    people: Optional[str] = None  # comma-separated names
    confidence: float = 0.0
    source_message_id: Optional[int] = Field(default=None, foreign_key="message.id")
    created_at: datetime = Field(default_factory=utcnow)
    updated_at: datetime = Field(default_factory=utcnow)
    notified: bool = False  # pushed to the phone yet?


class Worker(SQLModel, table=True):
    """Part-time heavy-compute nodes (the Omen). They heartbeat; the brain routes jobs by liveness."""

    id: Optional[int] = Field(default=None, primary_key=True)
    name: str = Field(unique=True, index=True)
    url: str  # http://<tailscale-ip>:<port>
    capabilities: str = ""  # comma-separated: "stt,local-llm,browser"
    last_seen: datetime = Field(default_factory=utcnow)


class Note(SQLModel, table=True):
    """Free-form key/value scratch (e.g. telegram chat id discovered at runtime)."""

    key: str = Field(primary_key=True)
    value: str


# --- engine -------------------------------------------------------------------

def _make_engine():
    url = settings.database_url
    if url.startswith("sqlite:///"):
        path = url.replace("sqlite:///", "", 1)
        d = os.path.dirname(path)
        if d:
            os.makedirs(d, exist_ok=True)
    return create_engine(url, connect_args={"check_same_thread": False} if "sqlite" in url else {})


engine = _make_engine()


def init_db() -> None:
    SQLModel.metadata.create_all(engine)


def get_session():
    with Session(engine) as session:
        yield session
