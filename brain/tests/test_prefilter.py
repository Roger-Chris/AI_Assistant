import time

from app.prefilter import prefilter


def test_chitchat_and_emoji_skip():
    assert prefilter("ok", is_group=False, from_me=False) == "chitchat"
    assert prefilter("Thanks da!!", is_group=False, from_me=False) == "chitchat"
    assert prefilter("👍👍", is_group=False, from_me=False) in ("emoji-only", "too-short", "empty")
    assert prefilter("haha nice", is_group=False, from_me=False) == "chitchat"
    assert prefilter("https://x.com/abc", is_group=False, from_me=False) == "url-only"


def test_dm_with_content_passes():
    assert prefilter("can u check the doc i sent", is_group=False, from_me=False) is None


def test_group_gate():
    assert prefilter("lol that meme", is_group=True, from_me=False) == "group-no-signal"
    assert prefilter("submit the form by tomorrow 5pm", is_group=True, from_me=False) is None
    assert prefilter("Kishaunjith can u bring the board", is_group=True, from_me=False) is None
    assert prefilter("submit by tomorrow", is_group=True, from_me=False, forwarded=True) == "forwarded-in-group"


def test_muted_chat_is_stored_but_not_triaged(client, fake_llm):
    from tests.test_pipeline import wa
    chat = {"chat_id": "noisy@g.us", "chat_name": "Memes", "is_group": True}
    fake_llm.script["submit the form"] = {"action": "new", "kind": "task", "title": "Submit form", "confidence": 0.9}
    client.post("/ingest/whatsapp", json=wa("please submit the form by tomorrow", **chat))
    before = len(fake_llm.calls)
    # find the chat id via the Chat table and mute it
    from sqlmodel import Session, select

    from app.db import Chat, engine
    with Session(engine) as s:
        c = s.exec(select(Chat).where(Chat.chat_id == "noisy@g.us")).one()
        c.muted = True
        s.add(c)
        s.commit()
    r = client.post("/ingest/whatsapp", json=wa("please submit the other form by friday", **chat))
    assert r.json()["skipped"] == "muted-chat"
    assert len(fake_llm.calls) == before  # no model call
    _ = time  # keep import used for wa()
