"""Shared memory: one pool of records that every device reads and writes (docs/JARVIS.md).

The brain holds the master copy. Devices keep a replica: they pull `changes(since=seq)` and push
what they wrote with `apply_batch()`. Every accepted write gets the next `seq`, so "what changed
since I last looked" is one indexed query, and an offline laptop catches up by asking for
everything after the last `seq` it saw.

Conflicts: the same `uid` written in two places keeps the newer `updated_at` (a tie keeps the
brain's copy). Deletes are tombstones (`deleted=True`) so they reach the replicas too.

Records the brain derives from its own tables (graph entities and edges, tasks) are mirrored here
by `mirror()`, source `graph` / `tasks`, and are owned by the brain: a device that wants to add
to what is known about a person writes a `fact` with `subject` set to that person's uid.
"""
from __future__ import annotations

import hashlib
import json
import logging
import re
import uuid
from datetime import datetime, timezone
from typing import Any, Iterable, Optional

from sqlalchemy import text
from sqlalchemy.exc import IntegrityError
from sqlmodel import Session, select

from .db import Edge, Entity, MemoryRecord, Task, engine, utcnow

log = logging.getLogger("brain.memory")

KINDS = {
    # things with a name
    "person", "org", "project", "place", "thing", "event_series", "device",
    # links and knowledge
    "relation", "fact", "note", "decision", "work",
    # mirrored or indexed
    "task", "event", "file",
}
_SLUG = re.compile(r"[^a-z0-9]+")
MAX_BODY = 20_000


def slug(s: str) -> str:
    return _SLUG.sub("-", (s or "").lower()).strip("-")[:80]


def named_uid(kind: str, name: str) -> str:
    """The uid for a named thing, the same on every device: `person:jayesh-daga`."""
    return f"{kind}:{slug(name)}"


def new_uid(kind: str) -> str:
    return f"{kind}:{uuid.uuid4().hex[:16]}"


def as_utc(d: datetime | str | None) -> datetime:
    if d is None:
        return utcnow()
    if isinstance(d, str):
        d = datetime.fromisoformat(d.replace("Z", "+00:00"))
    return d if d.tzinfo else d.replace(tzinfo=timezone.utc)


def _fingerprint(r: dict) -> str:
    keep = {k: r.get(k) for k in ("kind", "title", "body", "data", "tags", "aliases", "subject", "deleted")}
    return hashlib.sha1(json.dumps(keep, sort_keys=True, default=str).encode()).hexdigest()


# ---------------------------------------------------------------- schema extras

def init_fts() -> None:
    """The full-text index sits beside memory_record; create_all() does not know about FTS5."""
    with engine.begin() as conn:
        conn.execute(text(
            "CREATE VIRTUAL TABLE IF NOT EXISTS memory_fts USING fts5("
            "uid UNINDEXED, title, body, tags, aliases, tokenize='unicode61 remove_diacritics 2')"))


def _index(session: Session, rec: MemoryRecord) -> None:
    session.execute(text("DELETE FROM memory_fts WHERE uid = :uid"), {"uid": rec.uid})
    if not rec.deleted:
        session.execute(
            text("INSERT INTO memory_fts(uid, title, body, tags, aliases) VALUES (:uid, :t, :b, :g, :a)"),
            {"uid": rec.uid, "t": rec.title, "b": rec.body[:MAX_BODY], "g": rec.tags.replace(",", " "),
             "a": rec.aliases.replace(",", " ").replace("-", " ")})


def _next_seq(session: Session) -> int:
    return (session.execute(text("SELECT COALESCE(MAX(seq), 0) FROM memoryrecord")).scalar() or 0) + 1


# ---------------------------------------------------------------- writes

def to_dict(r: MemoryRecord) -> dict:
    return {
        "uid": r.uid, "kind": r.kind, "title": r.title, "body": r.body,
        "data": json.loads(r.data) if r.data else {}, "tags": r.tags, "aliases": r.aliases,
        "subject": r.subject, "source": r.source, "device": r.device,
        "created_at": as_utc(r.created_at).isoformat(), "updated_at": as_utc(r.updated_at).isoformat(),
        "deleted": r.deleted, "seq": r.seq,
    }


def _clean(fields: dict) -> dict:
    kind = (fields.get("kind") or "note").strip().lower()
    if kind not in KINDS:
        raise ValueError(f"unknown kind {kind!r}")
    data = fields.get("data") or {}
    if isinstance(data, str):
        data = json.loads(data) if data.strip() else {}
    tags = fields.get("tags") or ""
    if isinstance(tags, (list, tuple)):
        tags = ",".join(t.strip() for t in tags if t and t.strip())
    aliases = fields.get("aliases") or ""
    if isinstance(aliases, (list, tuple)):
        aliases = ",".join(a.strip() for a in aliases if a and a.strip())
    return {
        "kind": kind,
        "title": (fields.get("title") or "").strip()[:300],
        "body": (fields.get("body") or "")[:MAX_BODY],
        "data": data,
        "tags": tags[:500],
        "aliases": aliases[:1000],
        "subject": fields.get("subject") or None,
        "source": (fields.get("source") or "manual")[:40],
        "device": (fields.get("device") or "brain")[:60],
        "deleted": bool(fields.get("deleted", False)),
    }


def put(session: Session, uid: Optional[str] = None, *, updated_at: datetime | str | None = None,
        force: bool = False, **fields: Any) -> tuple[MemoryRecord, bool]:
    """Create or update one record. Returns (record, changed). Unchanged content is not written,
    so mirrors can run often without filling the change log.

    `force=False`: last writer wins, an older `updated_at` than the stored one is ignored."""
    f = _clean(fields)
    uid = uid or new_uid(f["kind"])
    when = as_utc(updated_at)
    rec = session.exec(select(MemoryRecord).where(MemoryRecord.uid == uid)).first()
    fp = _fingerprint(f)
    if rec:
        if rec.fingerprint == fp:
            return rec, False
        if not force and as_utc(rec.updated_at) >= when and updated_at is not None:
            return rec, False
    else:
        rec = MemoryRecord(uid=uid, created_at=when)
    rec.kind, rec.title, rec.body = f["kind"], f["title"], f["body"]
    rec.data = json.dumps(f["data"], default=str) if f["data"] else ""
    rec.tags, rec.aliases, rec.subject = f["tags"], f["aliases"], f["subject"]
    rec.source, rec.device, rec.deleted = f["source"], f["device"], f["deleted"]
    rec.updated_at = when
    rec.fingerprint = fp
    rec.seq = _next_seq(session)
    session.add(rec)
    session.flush()
    _index(session, rec)
    return rec, True


def put_committed(uid: Optional[str] = None, **kw: Any) -> dict:
    """`put` in its own transaction, retried once if another writer took the same seq."""
    for attempt in range(3):
        try:
            with Session(engine) as session:
                rec, _ = put(session, uid, **kw)
                session.commit()
                return to_dict(rec)
        except IntegrityError:
            if attempt == 2:
                raise
    raise RuntimeError("unreachable")


def delete(session: Session, uid: str, device: str = "brain") -> bool:
    rec = session.exec(select(MemoryRecord).where(MemoryRecord.uid == uid)).first()
    if not rec or rec.deleted:
        return False
    fields = to_dict(rec)
    fields.update(deleted=True, device=device)
    fields.pop("uid")
    fields.pop("updated_at")
    put(session, uid, force=True, **fields)
    return True


def apply_batch(session: Session, records: Iterable[dict], device: str) -> list[dict]:
    """Records pushed by a device. Each needs `uid` and `updated_at`; newer wins."""
    out = []
    for r in records:
        uid = r.get("uid")
        if not uid:
            out.append({"uid": None, "accepted": False, "error": "uid required"})
            continue
        fields = {k: v for k, v in r.items() if k not in ("uid", "updated_at", "seq", "created_at")}
        fields.setdefault("device", device)
        try:
            rec, changed = put(session, uid, updated_at=r.get("updated_at") or utcnow(), **fields)
        except (ValueError, json.JSONDecodeError) as e:
            out.append({"uid": uid, "accepted": False, "error": str(e)})
            continue
        res = {"uid": uid, "accepted": changed, "seq": rec.seq}
        if not changed:
            # Refused (older than ours) or identical: send our copy back, so a device that wrote
            # the losing edit converges even when our copy is older than its last pull.
            res["record"] = to_dict(rec)
        out.append(res)
    return out


def changes(session: Session, since: int = 0, limit: int = 500) -> dict:
    limit = max(1, min(limit, 2000))
    rows = session.exec(select(MemoryRecord).where(MemoryRecord.seq > since)
                        .order_by(MemoryRecord.seq).limit(limit + 1)).all()  # type: ignore[arg-type]
    more = len(rows) > limit
    rows = rows[:limit]
    last = rows[-1].seq if rows else since
    return {"records": [to_dict(r) for r in rows], "last_seq": last, "more": more,
            "head": _next_seq(session) - 1}


# ---------------------------------------------------------------- reads

_WORD = re.compile(r"[\w]+", re.UNICODE)


def _fts_query(q: str, joiner: str) -> str:
    words = [w for w in _WORD.findall(q.lower()) if len(w) > 1][:12]
    return f" {joiner} ".join(f'"{w}"*' for w in words)


def search(session: Session, q: str, *, kind: Optional[str] = None, limit: int = 10,
           any_word: bool = True) -> list[dict]:
    """Ranked full-text search. All words first; if that finds nothing (and `any_word`), any word."""
    limit = max(1, min(limit, 50))
    for joiner in (("AND", "OR") if any_word else ("AND",)):
        expr = _fts_query(q, joiner)
        if not expr:
            return []
        sql = ("SELECT f.uid, bm25(memory_fts, 0, 5.0, 1.0, 2.0, 4.0) AS rank FROM memory_fts f "
               "JOIN memoryrecord r ON r.uid = f.uid WHERE memory_fts MATCH :q AND r.deleted = 0")
        params: dict[str, Any] = {"q": expr, "n": limit}
        if kind:
            sql += " AND r.kind = :kind"
            params["kind"] = kind
        sql += " ORDER BY rank LIMIT :n"
        hits = session.execute(text(sql), params).all()
        if hits:
            out = []
            for uid, rank in hits:
                rec = session.exec(select(MemoryRecord).where(MemoryRecord.uid == uid)).first()
                if rec:
                    d = to_dict(rec)
                    d["score"] = round(-float(rank), 3)
                    out.append(d)
            return out
    return []


def get(session: Session, uid: str) -> Optional[dict]:
    rec = session.exec(select(MemoryRecord).where(MemoryRecord.uid == uid)).first()
    return to_dict(rec) if rec else None


def resolve(session: Session, name_or_uid: str) -> Optional[MemoryRecord]:
    """A uid, or a name: exact uid for any named kind, then an alias, then the best search hit."""
    if ":" in name_or_uid:
        rec = session.exec(select(MemoryRecord).where(MemoryRecord.uid == name_or_uid)).first()
        if rec and not rec.deleted:
            return rec
    s = slug(name_or_uid)
    for kind in ("person", "project", "org", "place", "device", "thing", "event_series"):
        rec = session.exec(select(MemoryRecord).where(MemoryRecord.uid == f"{kind}:{s}")).first()
        if rec and not rec.deleted:
            return rec
    for rec in session.exec(select(MemoryRecord).where(MemoryRecord.aliases.contains(s),  # type: ignore[union-attr]
                                                       MemoryRecord.deleted == False)).all():  # noqa: E712
        if s in rec.aliases.split(","):
            return rec
    hits = search(session, name_or_uid, limit=1, any_word=False)  # a name must match every word
    if hits:
        return session.exec(select(MemoryRecord).where(MemoryRecord.uid == hits[0]["uid"])).first()
    return None


def about(session: Session, name_or_uid: str, limit: int = 20) -> Optional[dict]:
    """Everything memory holds about one thing: the record, its relations, and records about it."""
    rec = resolve(session, name_or_uid)
    if not rec:
        return None
    rels = []
    for r in session.exec(select(MemoryRecord).where(MemoryRecord.kind == "relation",
                                                     MemoryRecord.deleted == False)  # noqa: E712
                          .where(MemoryRecord.data.contains(rec.uid))).all():  # type: ignore[union-attr]
        d = json.loads(r.data or "{}")
        if rec.uid in (d.get("src"), d.get("dst")):
            rels.append({"src": d.get("src"), "rel": d.get("rel"), "dst": d.get("dst"), "title": r.title})
    linked = session.exec(select(MemoryRecord).where(MemoryRecord.subject == rec.uid,
                                                     MemoryRecord.deleted == False)  # noqa: E712
                          .order_by(MemoryRecord.updated_at.desc()).limit(limit)).all()  # type: ignore[union-attr]
    return {"record": to_dict(rec), "relations": rels[:limit], "linked": [to_dict(r) for r in linked]}


def stats(session: Session) -> dict:
    rows = session.execute(text("SELECT kind, COUNT(*) FROM memoryrecord WHERE deleted = 0 GROUP BY kind")).all()
    return {"by_kind": {k: n for k, n in rows}, "head": _next_seq(session) - 1}


def as_prompt(records: list[dict], max_chars: int = 3000) -> str:
    """Records as compact lines for a model prompt."""
    lines, used = [], 0
    for r in records:
        body = " ".join(r["body"].split())[:300]
        line = f"- [{r['kind']}] {r['title']}" + (f": {body}" if body else "") + f"  ({r['uid']})"
        if used + len(line) > max_chars:
            break
        lines.append(line)
        used += len(line)
    return "\n".join(lines)


# ---------------------------------------------------------------- mirror the brain's own tables

def _entity_uid(e: Entity) -> str:
    return f"{e.kind}:{e.key}"


def mirror(session: Session) -> int:
    """Copy graph entities/edges and tasks into memory. Only changed content is written, and
    mirrored records whose source row is gone (merged entities, deleted edges) become tombstones."""
    written = 0
    seen: set[str] = set()
    ents = {e.id: e for e in session.exec(select(Entity)).all()}
    for e in ents.values():
        uid = _entity_uid(e)
        seen.add(uid)
        aliases = [a for a in e.aliases.split(",") if a and "@" not in a]  # chat ids stay out
        _, changed = put(session, uid, kind=e.kind, title=e.name, body=e.summary or "",
                         aliases=aliases, source="graph",
                         tags="owner" if e.is_owner else "",
                         data={"mentions": e.mention_count} if e.is_owner else {})
        written += changed
    for edge in session.exec(select(Edge)).all():
        src, dst = ents.get(edge.src_id), ents.get(edge.dst_id)
        if not src or not dst:
            continue
        uid = f"relation:{src.key}~{edge.rel}~{dst.key}"
        seen.add(uid)
        _, changed = put(session, uid, kind="relation", title=f"{src.name} {edge.rel.replace('_', ' ')} {dst.name}",
                         body=edge.note or "", source="graph",
                         data={"src": _entity_uid(src), "rel": edge.rel, "dst": _entity_uid(dst)})
        written += changed
    for t in session.exec(select(Task)).all():
        uid = f"task:{t.id}"
        seen.add(uid)
        body = " ".join(x for x in [t.details or "", f"with {t.people}" if t.people else ""] if x)
        _, changed = put(session, uid, kind="task" if t.kind.value != "event" else "event",
                         title=t.title, body=body, source="tasks",
                         data={"status": t.status.value, "task_kind": t.kind.value, "priority": t.priority,
                               "due_at": as_utc(t.due_at).isoformat() if t.due_at else None, "task_id": t.id},
                         tags=t.people or "")
        written += changed
    for rec in session.exec(select(MemoryRecord).where(MemoryRecord.source.in_(["graph", "tasks"]),  # type: ignore[attr-defined]
                                                       MemoryRecord.deleted == False)).all():  # noqa: E712
        if rec.uid not in seen:
            delete(session, rec.uid)
            written += 1
    session.commit()
    if written:
        log.info("memory: mirrored %d changed records", written)
    return written


def mirror_now() -> int:
    with Session(engine) as session:
        return mirror(session)
