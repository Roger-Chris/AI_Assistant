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


def test_clean_summary_rejects_headings_and_preamble():
    assert graph.clean_summary("**Extract Key Information:**") is None
    assert graph.clean_summary("Here is the sentence:") is None
    assert graph.clean_summary("## Profile\n\n**Roger Christopher** is a CSBS student at Rajalakshmi "
                               "Institute building robotics and IoT systems.") == (
        "Roger Christopher is a CSBS student at Rajalakshmi Institute building robotics and IoT systems.")
    assert graph.clean_summary("Roger is a CSBS student at RIT who builds drones and IoT devices.").startswith("Roger is a")


def test_relations_survive_a_truncated_answer(fake_llm):
    """The seed failure: the model hit the token ceiling mid-relations. Salvage must still
    link what completed, and the drop must be logged rather than silent."""
    wipe()
    fake_llm.raw = ('{"entities": [{"name": "Rajalakshmi Institute of Technology", "kind": "org",'
                    ' "summary": "his college", "aliases": ["RIT"]},'
                    ' {"name": "Zapprise", "kind": "project", "summary": "co-founded venture", "aliases": []}],'
                    ' "relations": [["Kishaunjith", "studies_at", "Rajalakshmi Institute of Technology"],'
                    ' ["Kishaunjith", "works_on", "Zappr')
    with Session(engine) as s:
        res = asyncio.run(graph.extract(s, "cv", source="resume"))
        assert res["entities"] == 2
        assert res["relations"] == 1          # the completed one linked
        o = graph.owner(s)
        assert any("studies at Rajalakshmi" in r for r in graph._neighbours(s, o, limit=5))


def test_parenthetical_names_are_one_entity():
    """The seed produced 'IIT PALS' and 'IIT PALS (IIT Madras)' as two orgs."""
    wipe()
    with Session(engine) as s:
        a = graph.upsert_entity(s, name="IIT PALS", kind="org")
        b = graph.upsert_entity(s, name="IIT PALS (IIT Madras)", kind="org")
        s.commit()
        assert a.id == b.id
        assert a.name == "IIT PALS"
        assert "iit-madras" in a.aliases
        # and the bracketed form is findable
        assert graph.describe(s, "IIT Madras").startswith("<b>IIT PALS</b>")


def test_merge_duplicates_folds_older_rows(fake_llm):
    """Rows written before the rule existed still merge, keeping edges."""
    wipe()
    with Session(engine) as s:
        o = graph.ensure_owner(s)
        keep = graph.upsert_entity(s, name="Indian Space Association", kind="org")
        s.commit()
        dup = Entity(kind="org", key="indian-space-association-isa", name="Indian Space Association (ISA)",
                     summary="internship")
        s.add(dup)
        s.commit()
        graph.link(s, o, "member_of", dup)
        s.commit()
        assert graph.merge_duplicates(s) == 1
        s.expire_all()
        assert len(s.exec(__import__("sqlmodel").select(Entity)).all()) == 2  # owner + the one org
        assert any("member of Indian Space Association" in r for r in graph._neighbours(s, o, limit=5))
        assert s.get(Entity, keep.id).summary == "internship"
