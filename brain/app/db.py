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
    source: str = Field(index=True)  # "whatsapp" | "email"
    external_id: str = Field(index=True, unique=True)  # WA message id, keeps ingest idempotent
    chat_id: str = Field(index=True)  # WA jid (xxx@s.whatsapp.net or xxx@g.us)
    chat_name: Optional[str] = None  # group subject or contact push name
    sender_id: str
    sender_name: Optional[str] = None
    from_me: bool = False
    is_group: bool = False
    is_self: bool = False  # the "message yourself" chat — notes / instructions to the assistant
    is_bulk: bool = False  # email sent to a list (unsubscribe header / bulk sender)
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
    watching = "watching"  # a question to the owner; surfaces only if unanswered for a grace period
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
    chat_id: Optional[str] = Field(default=None, index=True)  # which chat this came from
    calendar_event_id: Optional[str] = None
    calendar_synced_at: Optional[datetime] = None
    created_at: datetime = Field(default_factory=utcnow)
    updated_at: datetime = Field(default_factory=utcnow)
    notified: bool = False  # pushed to the phone yet?


class Alert(SQLModel, table=True):
    """Something worth your attention, and how loudly to ask for it. See alerts.py for the levels."""

    id: Optional[int] = Field(default=None, primary_key=True)
    level: int = 2  # 1 brief, 2 notification, 3 alarm, 4 call
    kind: str = Field(index=True)  # new_task | question | overdue | brief
    title: str
    body: str = ""
    task_id: Optional[int] = Field(default=None, foreign_key="task.id")
    key: Optional[str] = Field(default=None, index=True)  # at most one open alert per key
    status: str = Field(default="pending", index=True)  # pending | sent | done | dismissed
    deliver_after: datetime = Field(default_factory=utcnow, index=True)  # quiet hours, snooze
    channel: Optional[str] = None  # hub | ntfy | none
    ack: Optional[str] = None  # your answer: done | snooze | dismiss
    created_at: datetime = Field(default_factory=utcnow)
    sent_at: Optional[datetime] = None
    acked_at: Optional[datetime] = None


class Worker(SQLModel, table=True):
    """Part-time heavy-compute nodes (the Omen). They heartbeat; the brain routes jobs by liveness."""

    id: Optional[int] = Field(default=None, primary_key=True)
    name: str = Field(unique=True, index=True)
    url: str  # http://<tailscale-ip>:<port>
    capabilities: str = ""  # comma-separated: "stt,local-llm,browser"
    last_seen: datetime = Field(default_factory=utcnow)


class Chat(SQLModel, table=True):
    """One row per WhatsApp chat. `muted` = keep storing for context, never triage."""

    id: Optional[int] = Field(default=None, primary_key=True)
    chat_id: str = Field(unique=True, index=True)
    name: Optional[str] = None
    is_group: bool = False
    muted: bool = False
    message_count: int = 0
    last_message_at: Optional[datetime] = None


class Entity(SQLModel, table=True):
    """A node in the memory graph: a person, org, project, place or thing."""

    id: Optional[int] = Field(default=None, primary_key=True)
    kind: str = Field(index=True)  # person | org | project | place | thing | event_series
    key: str = Field(unique=True, index=True)  # slug used for dedup, e.g. "jayesh-daga"
    name: str
    aliases: str = ""  # comma-separated other names/handles/addresses seen
    summary: Optional[str] = None  # one line: who/what this is to the owner
    is_owner: bool = False
    source: str = "derived"  # resume | derived | chat | manual
    mention_count: int = 0
    first_seen: datetime = Field(default_factory=utcnow)
    last_seen: datetime = Field(default_factory=utcnow)


class Edge(SQLModel, table=True):
    """A directed relation between two entities, with the message that evidenced it."""

    id: Optional[int] = Field(default=None, primary_key=True)
    src_id: int = Field(foreign_key="entity.id", index=True)
    rel: str = Field(index=True)  # works_on | member_of | studies_at | knows | advisor_of | about | ...
    dst_id: int = Field(foreign_key="entity.id", index=True)
    weight: int = 1
    note: Optional[str] = None
    evidence_message_id: Optional[int] = Field(default=None, foreign_key="message.id")
    created_at: datetime = Field(default_factory=utcnow)


class Mention(SQLModel, table=True):
    """Entity seen in a message (or attached to a task) — the trail back to evidence."""

    id: Optional[int] = Field(default=None, primary_key=True)
    entity_id: int = Field(foreign_key="entity.id", index=True)
    message_id: Optional[int] = Field(default=None, foreign_key="message.id", index=True)
    task_id: Optional[int] = Field(default=None, foreign_key="task.id", index=True)
    created_at: datetime = Field(default_factory=utcnow)


class Note(SQLModel, table=True):
    """Free-form key/value scratch (e.g. the last Gmail error per account)."""

    key: str = Field(primary_key=True)
    value: str


class MemoryRecord(SQLModel, table=True):
    """One record in the shared memory pool (memory.py, docs/JARVIS.md). Replicated to every device."""

    id: Optional[int] = Field(default=None, primary_key=True)
    uid: str = Field(unique=True, index=True)  # "<kind>:<slug>" for named things, "<kind>:<random>" else
    kind: str = Field(index=True)
    title: str = ""
    body: str = ""
    data: str = ""  # JSON, kind-specific
    tags: str = ""
    aliases: str = ""
    subject: Optional[str] = Field(default=None, index=True)  # uid this record is about
    source: str = Field(default="manual", index=True)
    device: str = "brain"
    deleted: bool = Field(default=False, index=True)
    fingerprint: str = ""
    seq: int = Field(default=0, unique=True, index=True)  # position in the change log
    created_at: datetime = Field(default_factory=utcnow)
    updated_at: datetime = Field(default_factory=utcnow, index=True)


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
    _migrate_columns()
    from .memory import init_fts

    init_fts()


def _migrate_columns() -> None:
    """SQLite: create_all never adds columns to existing tables. Add any the models gained."""
    from sqlalchemy import inspect, text

    insp = inspect(engine)
    with engine.begin() as conn:
        for table in SQLModel.metadata.sorted_tables:
            if not insp.has_table(table.name):
                continue
            existing = {c["name"] for c in insp.get_columns(table.name)}
            for col in table.columns:
                if col.name not in existing:
                    ctype = col.type.compile(engine.dialect)
                    default = ""
                    if col.default is not None and getattr(col.default, "is_scalar", False):
                        v = col.default.arg
                        default = f" DEFAULT {int(v) if isinstance(v, bool) else repr(v)}"
                    conn.execute(text(f'ALTER TABLE "{table.name}" ADD COLUMN "{col.name}" {ctype}{default}'))


def get_session():
    with Session(engine) as session:
        yield session


def backfill_chats() -> None:
    """Populate the Chat table from stored messages if it's empty (first run after upgrade)."""
    from sqlalchemy import func
    from sqlmodel import select

    with Session(engine) as session:
        if session.exec(select(Chat).limit(1)).first():
            return
        rows = session.exec(
            select(Message.chat_id, func.max(Message.chat_name), func.max(Message.is_group),
                   func.count(Message.id), func.max(Message.sent_at)).group_by(Message.chat_id)
        ).all()
        for chat_id, name, is_group, n, last in rows:
            session.add(Chat(chat_id=chat_id, name=name, is_group=bool(is_group), message_count=n,
                             last_message_at=last))
        session.commit()
