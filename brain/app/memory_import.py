"""Import notes into shared memory: one markdown file per project (or person, or place).

    python -m app.memory_import notes/projects/*.md          # kind=project
    python -m app.memory_import --kind person people/*.md

File format (everything optional except some text):

    ---
    title: X8 coaxial eVTOL
    aliases: eVTOL, air ambulance drone
    tags: hardware, aerospace
    status: tethered tests
    ---
    # X8 coaxial eVTOL            (used as the title when there is no front matter title)
    Free text about the project...

    ## Decisions
    - Coaxial X8 over a quad for redundancy
    - Tethered tests only until the ESC thermal issue is fixed

The file becomes one record (`project:<slug>`); each bullet under a "Decisions" heading also
becomes a `decision` record about it, so "why did we pick X" finds the decision directly.
Re-importing the same file updates the records instead of adding new ones.
"""
from __future__ import annotations

import argparse
import hashlib
import re
from pathlib import Path

from sqlmodel import Session

from . import memory
from .db import engine, init_db

_FRONT = re.compile(r"\A---\s*\n(.*?)\n---\s*\n", re.S)
_H1 = re.compile(r"^#\s+(.+)$", re.M)
_SECTION = re.compile(r"^##\s+(.+)$", re.M)
_BULLET = re.compile(r"^\s*[-*]\s+(.+)$", re.M)


def parse(name: str, text: str) -> dict:
    meta: dict[str, str] = {}
    m = _FRONT.match(text)
    if m:
        for line in m.group(1).splitlines():
            if ":" in line:
                k, v = line.split(":", 1)
                meta[k.strip().lower()] = v.strip()
        text = text[m.end():]
    h1 = _H1.search(text)
    title = meta.get("title") or (h1.group(1).strip() if h1 else Path(name).stem.replace("_", " ").replace("-", " "))
    decisions: list[str] = []
    sections = list(_SECTION.finditer(text))
    for i, s in enumerate(sections):
        if "decision" in s.group(1).lower():
            end = sections[i + 1].start() if i + 1 < len(sections) else len(text)
            decisions += [b.strip() for b in _BULLET.findall(text[s.end():end]) if b.strip()]
    return {"title": title, "body": text.strip(), "meta": meta, "decisions": decisions}


def import_markdown(session: Session, name: str, text: str, *, kind: str = "project", device: str = "brain") -> dict:
    p = parse(name, text)
    meta = p["meta"]
    uid = memory.named_uid(kind, p["title"])
    data = {k: v for k, v in meta.items() if k not in ("title", "aliases", "tags")}
    data["file"] = name
    memory.put(session, uid, kind=kind, title=p["title"], body=p["body"], data=data,
               aliases=[memory.slug(a) for a in meta.get("aliases", "").split(",") if a.strip()],
               tags=meta.get("tags", ""), source="import", device=device)
    for d in p["decisions"]:
        did = f"decision:{memory.slug(p['title'])[:40]}-{hashlib.sha1(d.encode()).hexdigest()[:10]}"
        memory.put(session, did, kind="decision", title=d[:120], body=d, subject=uid, source="import", device=device)
    return {"uid": uid, "title": p["title"], "decisions": len(p["decisions"])}


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("files", nargs="+")
    ap.add_argument("--kind", default="project")
    args = ap.parse_args()
    init_db()
    with Session(engine) as session:
        for f in args.files:
            path = Path(f)
            r = import_markdown(session, path.name, path.read_text(encoding="utf-8"), kind=args.kind)
            print(f"{r['uid']}  ({r['decisions']} decisions)")
        session.commit()


if __name__ == "__main__":
    main()
