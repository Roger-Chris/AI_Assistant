"""Memory graph: dedup, context injection, extraction."""
import asyncio

from sqlmodel import Session

from app import graph
from app.db import Entity, Message, engine, utcnow


def wipe():
    from sqlmodel import select
    from app.db import Edge, Mention
    with Session(engine) as s:
        for model in (Mention, Edge, Entity):
            for row in s.exec(select(model)).all():
                s.delete(row)
        s.commit()


def msg(**kw):
    base = dict(source="whatsapp", external_id=f"g-{utcnow().timestamp()}", chat_id="x@s.whatsapp.net",
                chat_name="Jayesh Daga", sender_id="x@s.whatsapp.net", sender_name="Jayesh Daga",
                from_me=False, is_group=False, text="hi", sent_at=utcnow())
    base.update(kw)
    return Message(**base)


def test_entities_dedup_on_name_and_alias():
    wipe()
    with Session(engine) as s:
        a = graph.upsert_entity(s, name="Jayesh Daga", kind="person")
        b = graph.upsert_entity(s, name="jayesh  daga", kind="person")  # same slug
        s.commit()
        assert a.id == b.id and b.mention_count == 2
        graph.upsert_entity(s, name="Jayesh Daga", aliases=["jd", "9198@s.whatsapp.net"])
        s.commit()
        c = graph.upsert_entity(s, name="JD", kind="person")  # matches by alias
        s.commit()
        assert c.id == a.id


def test_context_names_owner_and_sender():
    wipe()
    with Session(engine) as s:
        o = graph.ensure_owner(s)
        o.summary = "final-year CSBS student at Sathyabama, builds IoT and eVTOL projects"
        s.add(o)
        sih = graph.upsert_entity(s, name="SIH 2026", kind="project", summary="hackathon he is entering")
        s.commit()
        graph.link(s, o, "works_on", sih)
        jd = graph.upsert_entity(s, name="Jayesh Daga", kind="person", summary="classmate and co-inventor")
        s.commit()
        graph.link(s, jd, "works_on", sih)
        s.commit()
        out = graph.context_for(s, msg())
    assert "final-year CSBS student" in out
    assert "works on SIH 2026" in out
    assert "Jayesh Daga" in out and "co-inventor" in out


def test_context_is_empty_when_graph_is_bare():
    wipe()
    with Session(engine) as s:
        assert graph.context_for(s, msg(chat_name="Unknown", sender_name="Unknown")) == ""


def test_extract_folds_entities_and_relations(fake_llm):
    wipe()
    fake_llm.default = {
        "entities": [
            {"name": "Sathyabama Institute", "kind": "org", "summary": "his college", "aliases": ["SIST"]},
            {"name": "FactoryOS", "kind": "project", "summary": "MSME hackathon project", "aliases": []},
            {"name": "Om Kumar C U", "kind": "person", "summary": "professor, patent co-inventor", "aliases": []},
        ],
        "relations": [["Kishaunjith", "studies_at", "Sathyabama Institute"],
                      ["Kishaunjith", "works_on", "FactoryOS"],
                      ["Om Kumar C U", "advisor_of", "Kishaunjith"],
                      ["nonexistent", "works_on", "also missing"]],
    }
    with Session(engine) as s:
        res = asyncio.run(graph.extract(s, "cv text", source="resume"))
        assert res == {"entities": 3, "relations": 3}  # the bogus 4th is dropped
        o = graph.owner(s)
        rel = graph._neighbours(s, o, limit=10)
        assert any("studies at Sathyabama" in r for r in rel)
        assert any("advisor of this" in r for r in rel)
        # aliases let the college be found by its short name
        assert graph.describe(s, "SIST").startswith("<b>Sathyabama Institute</b>")
        assert "FactoryOS" in graph.to_mermaid(s)
