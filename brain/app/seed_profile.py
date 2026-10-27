"""Seed the memory graph from the owner's CV. Put the file at data/brain/profile/resume.docx,
then run once:

    deploy/linux/run brain python -m app.seed_profile ../data/brain/profile/resume.docx   # Linux
    docker compose run --rm brain python -m app.seed_profile /data/profile/resume.docx    # Docker

Reads .docx, .txt or .md. Everything it learns is visible with /who and /graph in Telegram, and
can be re-run safely: entities merge on name, they don't duplicate.
"""
from __future__ import annotations

import asyncio
import html
import re
import sys
import zipfile

from sqlmodel import Session

from . import graph
from .db import engine, init_db


def read_text(path: str) -> str:
    if path.lower().endswith(".docx"):
        with zipfile.ZipFile(path) as z:
            xml = z.read("word/document.xml").decode("utf-8", "replace")
        xml = re.sub(r"</w:p>", "\n", xml)
        xml = re.sub(r"<w:tab[^>]*/>", "\t", xml)
        text = html.unescape(re.sub(r"<[^>]+>", "", xml))
        return "\n".join(line.strip() for line in text.split("\n") if line.strip())
    with open(path, encoding="utf-8", errors="replace") as f:
        return f.read()


async def _run(path: str) -> int:
    text = read_text(path)
    if len(text) < 200:
        print(f"only {len(text)} characters read from {path} — is the file complete?")
        return 1
    init_db()
    with Session(engine) as session:
        res = await graph.seed_from_resume(session, text)
        o = graph.owner(session)
        print(f"seeded {res['entities']} entities and {res['relations']} relations")
        if o and o.summary:
            print(f"owner: {o.summary}")
        print(graph.stats(session))
    return 0


def main() -> int:
    if len(sys.argv) < 2:
        print("usage: python -m app.seed_profile <path to .docx|.txt|.md>")
        return 1
    return asyncio.run(_run(sys.argv[1]))


if __name__ == "__main__":
    sys.exit(main())
