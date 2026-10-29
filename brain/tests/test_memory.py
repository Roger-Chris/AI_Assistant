"""Shared memory: change log, last-writer-wins, tombstones, search, mirrors and the importer."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

from sqlmodel import Session, select

from app import graph, memory
from app.db import Edge, Task, TaskKind, TaskStatus, engine


def _iso(minutes: int = 0) -> str:
    return (datetime.now(timezone.utc) + timedelta(minutes=minutes)).isoformat()


def test_changes_are_ordered_and_unchanged_content_is_not_rewritten():
    with Session(engine) as s:
        a, changed_a = memory.put(s, "note:zz-order-a", kind="note", title="first zzorder")
        b, _ = memory.put(s, "note:zz-order-b", kind="note", title="second zzorder")
        s.commit()
        assert changed_a and b.seq > a.seq
        same, changed = memory.put(s, "note:zz-order-a", kind="note", title="first zzorder")
        s.commit()
        assert not changed and same.seq == a.seq
        page = memory.changes(s, since=a.seq - 1, limit=1)
        assert [r["uid"] for r in page["records"]] == ["note:zz-order-a"] and page["more"]
        rest = memory.changes(s, since=page["last_seq"])
        assert "note:zz-order-b" in [r["uid"] for r in rest["records"]]


def test_batch_newer_wins_older_is_ignored(client):
    uid = "fact:zz-lww"
    r = client.post("/memory/batch", json={"device": "omen", "records": [
        {"uid": uid, "kind": "fact", "title": "laptop wrote this", "updated_at": _iso(-10)}]})
    assert r.json()["results"][0]["accepted"]
    # an older edit from the Mac arrives late: ignored
    r = client.post("/memory/batch", json={"device": "mac", "records": [
        {"uid": uid, "kind": "fact", "title": "stale mac edit", "updated_at": _iso(-20)}]})
    refused = r.json()["results"][0]
    assert not refused["accepted"] and refused["record"]["title"] == "laptop wrote this"
    # a newer one wins
    r = client.post("/memory/batch", json={"device": "mac", "records": [
        {"uid": uid, "kind": "fact", "title": "newer mac edit", "updated_at": _iso(0)}]})
    assert r.json()["results"][0]["accepted"]
    rec = client.get(f"/memory/record/{uid}").json()
    assert rec["title"] == "newer mac edit" and rec["device"] == "mac"


def test_batch_rejects_bad_records_without_failing_the_rest(client):
    r = client.post("/memory/batch", json={"device": "omen", "records": [
        {"uid": "x:zz-bad", "kind": "spaceship", "title": "nope", "updated_at": _iso()},
        {"uid": "note:zz-good", "kind": "note", "title": "fine", "updated_at": _iso()}]})
    res = r.json()["results"]
    assert not res[0]["accepted"] and "kind" in res[0]["error"]
    assert res[1]["accepted"]


def test_delete_is_a_tombstone_that_syncs_and_hides_from_search(client):
    client.post("/memory/remember", json={"text": "zzquokka likes coffee", "kind": "fact"})
    hit = client.get("/memory/search", params={"q": "zzquokka"}).json()
    assert hit
    uid = hit[0]["uid"]
    head = client.get("/memory/stats").json()["head"]
    assert client.delete(f"/memory/record/{uid}").json()["deleted"]
    changed = client.get("/memory/changes", params={"since": head}).json()["records"]
    assert any(r["uid"] == uid and r["deleted"] for r in changed)
    assert client.get("/memory/search", params={"q": "zzquokka"}).json() == []


def test_search_all_words_then_any_word_and_kind_filter(client):
    client.post("/memory/batch", json={"device": "omen", "records": [
        {"uid": "project:zz-evtol", "kind": "project", "title": "Zzheavy coaxial eVTOL",
         "body": "X8 airframe, tethered thrust tests", "updated_at": _iso()},
        {"uid": "file:zz-thrust", "kind": "file", "title": "zzthrust_test.xlsx",
         "body": "thrust readings", "data": {"device": "omen", "path": "D:/evtol/zzthrust_test.xlsx"},
         "updated_at": _iso()}]})
    both = client.get("/memory/search", params={"q": "tethered thrust"}).json()
    assert both[0]["uid"] == "project:zz-evtol"
    anyw = client.get("/memory/search", params={"q": "zzthrust unknownword"}).json()
    assert [h["uid"] for h in anyw] == ["file:zz-thrust"]
    question = client.get("/memory/search", params={"q": "which file has the zzthrust readings"}).json()
    assert question[0]["uid"] == "file:zz-thrust"  # question words are not searched for
    files = client.get("/memory/search", params={"q": "thrust", "kind": "file"}).json()
    assert all(h["kind"] == "file" for h in files) and files


def test_mirror_copies_graph_and_tasks_and_tombstones_what_disappeared():
    with Session(engine) as s:
        a = graph.upsert_entity(s, name="Zzmirror Person", kind="person", summary="lab partner")
        b = graph.upsert_entity(s, name="Zzmirror Project", kind="project")
        s.commit()
        graph.link(s, a, "works_on", b)
        t = Task(kind=TaskKind.task, status=TaskStatus.open, title="zzmirror send the report", people="Zzmirror Person")
        s.add(t)
        s.commit()
        memory.mirror(s)
        assert memory.get(s, "person:zzmirror-person")["body"] == "lab partner"
        rel = memory.get(s, "relation:zzmirror-person~works_on~zzmirror-project")
        assert rel and rel["data"]["dst"] == "project:zzmirror-project"
        task = memory.get(s, f"task:{t.id}")
        assert task["data"]["status"] == "open"
        assert memory.mirror(s) == 0  # nothing changed: nothing written

        t.status = TaskStatus.done
        s.add(t)
        for e in s.exec(select(Edge).where(Edge.src_id == a.id)).all():
            s.delete(e)
        s.commit()
        assert memory.mirror(s) >= 2
        assert memory.get(s, f"task:{t.id}")["data"]["status"] == "done"
        assert memory.get(s, "relation:zzmirror-person~works_on~zzmirror-project")["deleted"]


def test_about_gathers_relations_and_facts(client):
    with Session(engine) as s:
        a = graph.upsert_entity(s, name="Zzabout Jayesh", kind="person")
        b = graph.upsert_entity(s, name="Zzabout Patent", kind="project")
        s.commit()
        graph.link(s, a, "co_inventor_of", b)
        s.commit()
        memory.mirror(s)
    client.post("/memory/remember", json={"text": "Prefers calls after 6 pm", "kind": "fact",
                                          "subject": "Zzabout Jayesh"})
    got = client.get("/memory/about", params={"name": "zzabout jayesh"}).json()
    assert got["record"]["uid"] == "person:zzabout-jayesh"
    assert got["relations"][0]["dst"] == "project:zzabout-patent"
    assert got["linked"][0]["body"] == "Prefers calls after 6 pm"
    assert client.get("/memory/about", params={"name": "nobody-zz-at-all"}).status_code == 404


def test_import_markdown_project_with_decisions_is_idempotent(client):
    md = ("---\naliases: zzX8, zz air ambulance drone\ntags: hardware\nstatus: tethered\n---\n"
          "# Zzimport eVTOL\nHeavy-lift X8 coaxial.\n\n## Decisions\n- Coaxial X8 for redundancy\n"
          "- Tether only until ESC thermals are fixed\n\n## Notes\n- not a decision\nRelated: [[Zzimport Battery Sim]]\n")
    files = [{"name": "evtol.md", "text": md}]
    out = client.post("/memory/import", json={"files": files}).json()["imported"][0]
    assert out == {"uid": "project:zzimport-evtol", "title": "Zzimport eVTOL", "decisions": 2, "links": 1}
    head = client.get("/memory/stats").json()["head"]
    client.post("/memory/import", json={"files": files})
    assert client.get("/memory/stats").json()["head"] == head  # same content: no new writes
    about = client.get("/memory/about", params={"name": "zzx8"}).json()  # found by alias
    assert about["record"]["data"]["status"] == "tethered"
    assert {r["kind"] for r in about["linked"]} == {"decision"}
    assert about["relations"] == [{"src": "project:zzimport-battery-sim", "rel": "related_to",
                                   "dst": "project:zzimport-evtol",
                                   "title": "Zzimport Battery Sim related to Zzimport eVTOL"}]


def test_memory_needs_the_token(client):
    assert client.get("/memory/stats", headers={"Authorization": "Bearer nope"}).status_code == 401
