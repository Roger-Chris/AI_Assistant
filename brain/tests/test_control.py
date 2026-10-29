"""The app's controls: chats and mute, brief on demand, model key usage, graph."""
from __future__ import annotations

from sqlmodel import Session, select

from app.db import Alert, Chat, engine


def test_chats_list_and_mute(client):
    with Session(engine) as s:
        c = Chat(chat_id="zzctl-group@g.us", name="Zzctl noisy group", is_group=True, message_count=99999)
        s.add(c)
        s.commit()
        pk = c.id
    chats = client.get("/chats", params={"limit": 5}).json()
    assert chats[0]["name"] == "Zzctl noisy group" and chats[0]["muted"] is False
    assert client.post(f"/chats/{pk}/mute", json={"muted": True}).json()["muted"] is True
    with Session(engine) as s:
        assert s.get(Chat, pk).muted
    assert client.post("/chats/999999/mute", json={"muted": True}).status_code == 404
    with Session(engine) as s:
        s.delete(s.get(Chat, pk))
        s.commit()


def test_brief_now_arrives_as_an_alert(client):
    assert client.post("/brief").json() == {"ok": True}
    with Session(engine) as s:
        assert s.exec(select(Alert).where(Alert.kind == "brief")).first()


def test_usage_and_graph(client):
    assert "report" in client.get("/llm/usage").json()
    g = client.get("/graph").json()
    assert "entities" in g and "relations" in g


def test_control_needs_the_token(client):
    for path in ("/chats", "/llm/usage", "/graph"):
        assert client.get(path, headers={"Authorization": "Bearer nope"}).status_code == 401
    assert client.post("/brief", headers={"Authorization": "Bearer nope"}).status_code == 401
