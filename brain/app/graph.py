"""The memory graph: who the owner is, who he deals with, and what he's working on.

Three sources fill it, cheapest first:

  chats    — every WhatsApp contact and group, and every email sender, becomes a node for free
  resume   — one planner pass over the owner's CV seeds him, his college, his projects, his people
  messages — a nightly planner pass over the day's kept messages adds entities and relations

One thing uses it on the hot path: `context_for()` puts two or three lines into the classifier
prompt — who the owner is, and who this sender is to him — which is what lets the model tell a
programme he belongs to from a webinar someone is selling him.

Entities dedup on `key`, a slug of the name, plus an alias list. No embeddings, nothing fuzzy:
a wrong merge is worse than a duplicate, and duplicates are easy to merge later.
"""
from __future__ import annotations

import json
import logging
import re
from datetime import timedelta
from typing import Iterable, Optional

from sqlmodel import Session, or_, select

from .config import settings
from .db import Edge, Entity, Mention, Message, Task, utcnow
from .llm import LLM, classifier_profile, planner_profile

log = logging.getLogger("brain.graph")

KINDS = {"person", "org", "project", "place", "thing", "event_series"}
_SLUG = re.compile(r"[^a-z0-9]+")
_TITLE_NOISE = re.compile(r"\s*\((mail|whatsapp)\)\s*$", re.I)


def slug(name: str) -> str:
    return _SLUG.sub("-", _TITLE_NOISE.sub("", name or "").strip().lower()).strip("-")[:80]


# ---------------------------------------------------------------- store

def upsert_entity(session: Session, *, name: str, kind: str = "person", summary: Optional[str] = None,
                  aliases: Iterable[str] = (), source: str = "derived", is_owner: bool = False) -> Optional[Entity]:
    key = slug(name)
    if not key or kind not in KINDS:
        return None
    e = session.exec(select(Entity).where(Entity.key == key)).first()
    if not e:
        # an alias of something we already know?
        for cand in session.exec(select(Entity).where(Entity.aliases.contains(key))).all():  # type: ignore[union-attr]
            if key in cand.aliases.split(","):
                e = cand
                break
    if not e:
        e = Entity(kind=kind, key=key, name=name.strip()[:120], source=source, is_owner=is_owner)
    known = [a for a in e.aliases.split(",") if a]
    for a in aliases:
        a = slug(a)
        if a and a != e.key and a not in known:
            known.append(a)
    e.aliases = ",".join(known[:20])
    if summary and (not e.summary or (source in ("resume", "manual") and e.summary != summary)):
        e.summary = summary[:300]
    if is_owner:
        e.is_owner = True
    if source in ("resume", "manual"):
        e.source = source
    e.mention_count += 1
    e.last_seen = utcnow()
    session.add(e)
    return e


def link(session: Session, src: Entity, rel: str, dst: Entity, note: Optional[str] = None,
         evidence_message_id: Optional[int] = None) -> Optional[Edge]:
    if not src or not dst or src.id == dst.id:
        return None
    session.flush()
    e = session.exec(select(Edge).where(Edge.src_id == src.id, Edge.rel == rel, Edge.dst_id == dst.id)).first()
    if e:
        e.weight += 1
    else:
        e = Edge(src_id=src.id, rel=rel[:40], dst_id=dst.id, note=(note or None),
                 evidence_message_id=evidence_message_id)
    session.add(e)
    return e


def owner(session: Session) -> Optional[Entity]:
    return session.exec(select(Entity).where(Entity.is_owner == True)).first()  # noqa: E712


def ensure_owner(session: Session) -> Entity:
    o = owner(session)
    if not o:
        o = upsert_entity(session, name=settings.owner_name, kind="person", is_owner=True, source="manual",
                          aliases=[a for a in settings.owner_aliases.split(",") if a.strip()])
        session.commit()
    return o  # type: ignore[return-value]


# ---------------------------------------------------------------- hot path

def entity_for_chat(session: Session, msg: Message) -> Optional[Entity]:
    """The person or group this message came from, as a node (created on first sight)."""
    if msg.is_self:
        return None
    if msg.is_group:
        name = msg.chat_name or msg.chat_id.split("@")[0]
        return upsert_entity(session, name=name, kind="org", source="chat", aliases=[msg.chat_id])
    name = (msg.sender_name or msg.chat_name or msg.sender_id.split("@")[0])
    return upsert_entity(session, name=_TITLE_NOISE.sub("", name), kind="person", source="chat",
                         aliases=[msg.sender_id])


def _neighbours(session: Session, e: Entity, limit: int = 4) -> list[str]:
    out = []
    for edge in session.exec(select(Edge).where(or_(Edge.src_id == e.id, Edge.dst_id == e.id))
                             .order_by(Edge.weight.desc()).limit(limit)).all():  # type: ignore[union-attr]
        other_id = edge.dst_id if edge.src_id == e.id else edge.src_id
        other = session.get(Entity, other_id)
        if not other:
            continue
        arrow = f"{edge.rel.replace('_', ' ')} {other.name}" if edge.src_id == e.id \
            else f"{other.name} {edge.rel.replace('_', ' ')} this"
        out.append(arrow)
    return out


def context_for(session: Session, msg: Message) -> str:
    """Two or three lines for the classifier prompt. Empty string when the graph knows nothing."""
    lines = []
    o = owner(session)
    if o and o.summary:
        lines.append(f"{o.name}: {o.summary}")
        rel = _neighbours(session, o, limit=6)
        if rel:
            lines.append("He is involved in: " + "; ".join(rel))
    who = entity_for_chat(session, msg) if not msg.is_self else None
    if who and who.id and (who.summary or who.mention_count > 1):
        bits = [f"This {'group' if who.kind == 'org' else 'sender'} — {who.name}"]
        if who.summary:
            bits.append(who.summary)
        rel = _neighbours(session, who, limit=3)
        if rel:
            bits.append("(" + "; ".join(rel) + ")")
        lines.append(": ".join(bits[:2]) + (" " + bits[2] if len(bits) > 2 else ""))
    return "\n".join(lines)


# ---------------------------------------------------------------- extraction

EXTRACT_PROMPT = """You maintain a memory graph for {owner}'s personal assistant.
From the material below, list the PEOPLE, ORGANISATIONS, PROJECTS and PLACES that matter to {owner}, and how they relate.

Rules:
- Only entities with a real connection to {owner}: people he talks to or works with, organisations he belongs to or deals with, projects/competitions/programmes he works on, places he studies or works at.
- Skip: companies that only market to him, news subjects, public figures he merely reads about, generic words ("college", "the team").
- `name`: how {owner} would say it. `kind`: person | org | project | place | thing | event_series.
- `summary`: ONE short line saying what this is TO HIM ("classmate, co-inventor on the water-metering patent"), not a general description. Leave "" if unsure.
- `relations`: [src, rel, dst] using names from your own entity list. Use these rels: works_on, member_of, studies_at, teaches_at, advisor_of, knows, colleague_of, organises, part_of, located_in, uses.
- Prefer few, certain entities over many guesses. If nothing qualifies, return empty lists.

Answer with ONLY JSON: {{"entities": [{{"name": "...", "kind": "...", "summary": "...", "aliases": ["..."]}}], "relations": [["src", "rel", "dst"]]}}

MATERIAL:
{material}"""


async def _ask(messages: list[dict], **kw):
    """Planner first; if it is down or busy, the cheap model does this job well enough."""
    try:
        return await LLM(planner_profile()).complete_json(messages, **kw)
    except Exception as e:  # noqa: BLE001
        log.warning("planner unavailable for the graph (%s); falling back to the classifier model", e)
        return await LLM(classifier_profile()).complete_json(messages, **kw)


async def extract(session: Session, material: str, *, source: str = "derived",
                  evidence_message_id: Optional[int] = None) -> dict:
    """Run a model over some text and fold the result into the graph."""
    o = ensure_owner(session)
    prompt = EXTRACT_PROMPT.format(owner=settings.owner_name, material=material[:24000])
    try:
        data = await _ask([{"role": "user", "content": prompt}], max_tokens=2000)
    except Exception:  # noqa: BLE001
        log.exception("graph extraction failed")
        return {"entities": 0, "relations": 0}
    if not isinstance(data, dict):
        return {"entities": 0, "relations": 0}

    by_name: dict[str, Entity] = {}
    for raw in data.get("entities") or []:
        if not isinstance(raw, dict) or not raw.get("name"):
            continue
        e = upsert_entity(session, name=str(raw["name"]), kind=str(raw.get("kind") or "person"),
                          summary=(raw.get("summary") or None), aliases=raw.get("aliases") or [],
                          source=source)
        if e:
            by_name[slug(str(raw["name"]))] = e
            if evidence_message_id:
                session.flush()
                session.add(Mention(entity_id=e.id, message_id=evidence_message_id))
    session.flush()

    owner_tokens = set(o.key.split("-")) | {t for a in o.aliases.split(",") for t in a.split("-") if t}

    def find(n: str) -> Optional[Entity]:
        k = slug(n)
        if not k:
            return None
        if k == o.key or k in o.aliases.split(","):
            return o
        hit = by_name.get(k) or session.exec(select(Entity).where(Entity.key == k)).first()
        if hit:
            return hit
        # the model often calls the owner by part of his name ("Roger" for "Roger Christopher")
        toks = {t for t in k.split("-") if t}
        if toks and toks <= owner_tokens:
            return o
        return None

    rels = 0
    for r in data.get("relations") or []:
        if not (isinstance(r, (list, tuple)) and len(r) == 3):
            continue
        a, rel, b = (str(x) for x in r)
        ea, eb = find(a), find(b)
        if ea and eb and link(session, ea, rel, eb, evidence_message_id=evidence_message_id):
            rels += 1
    session.commit()
    log.info("graph: +%d entities, +%d relations (%s)", len(by_name), rels, source)
    return {"entities": len(by_name), "relations": rels}


_MD_EDGE = re.compile(r"^[\s>#*_`-]+|[\s*_`]+$")
_MD_INLINE = re.compile(r"(\*\*|__|`)")


def clean_summary(raw: str) -> Optional[str]:
    """Models sometimes answer with a heading or a preamble ("**Extract Key Information:**").
    Take the first line that reads like a sentence about a person, or give up."""
    for line in (raw or "").splitlines():
        line = _MD_INLINE.sub("", _MD_EDGE.sub("", line)).strip()
        if len(line) < 25 or line.endswith(":"):
            continue
        low = line.lower()
        if low.startswith(("here", "sure", "okay", "summary", "answer", "output", "json", "extract")):
            continue
        return line[:300]
    return None


async def seed_from_resume(session: Session, text: str) -> dict:
    """One pass over the owner's CV. Also (re)writes his own one-line summary."""
    o = ensure_owner(session)
    res = await extract(session, f"{settings.owner_name}'s CV:\n\n{text}", source="resume")
    try:
        raw = await LLM(planner_profile()).complete(
            [{"role": "user", "content": "Write ONE sentence, under 30 words, saying who this person is: "
              "what they study, where, and the two or three things they are best known for. "
              "Reply with the sentence only — no heading, no preamble, no markdown.\n\n"
              f"{text[:12000]}"}], max_tokens=120)
    except Exception as e:  # noqa: BLE001
        log.warning("planner unavailable for the owner summary (%s); using the classifier model", e)
        try:
            raw = await LLM(classifier_profile()).complete(
                [{"role": "user", "content": "One sentence, under 30 words: who is this person? "
                  f"Sentence only.\n\n{text[:12000]}"}], max_tokens=120)
        except Exception:  # noqa: BLE001
            log.exception("owner summary failed")
            raw = ""
    line = clean_summary(raw)
    if line:
        o.summary = line
        o.source = "resume"
        session.add(o)
        session.commit()
    else:
        log.warning("owner summary unusable, left unchanged: %r", (raw or "")[:120])
    return res


async def nightly() -> dict:
    """Learn from the day's kept messages: the ones that became tasks, plus anything non-noise."""
    from .db import engine

    with Session(engine) as session:
        since = utcnow() - timedelta(days=1)
        msgs = session.exec(
            select(Message).where(Message.received_at >= since, Message.classified == True,  # noqa: E712
                                  Message.text != "")
            .order_by(Message.sent_at).limit(settings.graph_daily_messages)
        ).all()
        keep = [m for m in msgs if "prefilter" not in (m.classification or "")]
        if not keep:
            return {"entities": 0, "relations": 0}
        chunks, buf, size = [], [], 0
        for m in keep:
            who = "ME" if m.from_me else (m.sender_name or "them")
            line = f"[{m.sent_at:%d %b}] {who} in {m.chat_name or m.chat_id}: {m.text[:400]}"
            if size + len(line) > 12000:
                chunks.append(buf)
                buf, size = [], 0
            buf.append(line)
            size += len(line)
        if buf:
            chunks.append(buf)
        total = {"entities": 0, "relations": 0}
        for ch in chunks[:settings.graph_daily_chunks]:
            r = await extract(session, "\n".join(ch), source="derived")
            total["entities"] += r["entities"]
            total["relations"] += r["relations"]
        return total


# ---------------------------------------------------------------- queries

def describe(session: Session, name: str) -> str:
    """For Telegram /who — what the graph knows about someone, and what is open with them."""
    k = slug(name)
    e = session.exec(select(Entity).where(or_(Entity.key == k, Entity.aliases.contains(k),  # type: ignore[union-attr]
                                              Entity.name.ilike(f"%{name}%")))).first()  # type: ignore[union-attr]
    if not e:
        return f"Nothing in the graph for “{name}”."
    out = [f"<b>{e.name}</b> ({e.kind})" + (f"\n{e.summary}" if e.summary else "")]
    rel = _neighbours(session, e, limit=8)
    if rel:
        out.append("Links: " + "; ".join(rel))
    open_tasks = session.exec(
        select(Task).join(Mention, Mention.task_id == Task.id).where(Mention.entity_id == e.id).limit(5)  # type: ignore[arg-type]
    ).all()
    byname = session.exec(select(Task).where(Task.people.contains(e.name), Task.status.in_(["open", "pending_confirmation"]))  # type: ignore[union-attr,attr-defined]
                          .limit(5)).all()
    tasks = {t.id: t for t in list(open_tasks) + list(byname)}.values()
    if tasks:
        out.append("Open with them:\n" + "\n".join(f"#{t.id} {t.title}" for t in tasks))
    out.append(f"Seen {e.mention_count}×, last {e.last_seen:%d %b}.")
    return "\n".join(out)


def stats(session: Session) -> dict:
    kinds: dict[str, int] = {}
    for e in session.exec(select(Entity)).all():
        kinds[e.kind] = kinds.get(e.kind, 0) + 1
    edges = len(session.exec(select(Edge)).all())
    return {"entities": kinds, "relations": edges}


def to_mermaid(session: Session, limit: int = 40) -> str:
    """The graph as a diagram, for eyeballing what it has learned."""
    ents = session.exec(select(Entity).order_by(Entity.mention_count.desc()).limit(limit)).all()  # type: ignore[union-attr]
    ids = {e.id for e in ents}
    shape = {"person": ('("', '")'), "org": ('["', '"]'), "project": ('{{"', '"}}'),
             "place": ('(["', '"])'), "thing": ('["', '"]'), "event_series": ('["', '"]')}
    lines = ["flowchart LR"]
    for e in ents:
        a, b = shape.get(e.kind, ('["', '"]'))
        lines.append(f'  n{e.id}{a}{e.name[:28]}{b}')
    for edge in session.exec(select(Edge).order_by(Edge.weight.desc()).limit(limit * 2)).all():  # type: ignore[union-attr]
        if edge.src_id in ids and edge.dst_id in ids:
            lines.append(f"  n{edge.src_id} -->|{edge.rel.replace('_', ' ')}| n{edge.dst_id}")
    return "\n".join(lines)


def export(session: Session) -> str:
    ents = session.exec(select(Entity)).all()
    edges = session.exec(select(Edge)).all()
    by_id = {e.id: e.name for e in ents}
    return json.dumps({
        "entities": [{"name": e.name, "kind": e.kind, "summary": e.summary, "aliases": e.aliases,
                      "mentions": e.mention_count, "source": e.source} for e in ents],
        "relations": [[by_id.get(x.src_id), x.rel, by_id.get(x.dst_id), x.weight] for x in edges],
    }, indent=2, ensure_ascii=False)
