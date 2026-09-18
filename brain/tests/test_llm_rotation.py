import json

import httpx
import pytest

from app import llm as llm_mod
from app.llm import LLM, KeyRing, Profile


def test_keyring_round_robin_and_cooldown():
    ring = KeyRing(["a", "b", "c"])
    assert [ring.next()[1] for _ in range(4)] == ["a", "b", "c", "a"]
    ring.penalise(1)  # "b" is cooling
    assert [ring.next()[1] for _ in range(3)] == ["c", "a", "c"]


@pytest.mark.asyncio
async def test_rotates_on_429(monkeypatch):
    llm_mod.LLM.override = None  # bypass the fake for this test
    seen = []

    def handler(request: httpx.Request) -> httpx.Response:
        key = request.headers["authorization"].split()[1]
        seen.append(key)
        if key == "k1":
            return httpx.Response(429, json={"error": "quota"})
        return httpx.Response(200, json={"choices": [{"message": {"content": json.dumps({"ok": key})}}],
                                         "usage": {"prompt_tokens": 1, "completion_tokens": 1}})

    real_client = httpx.AsyncClient
    monkeypatch.setattr(httpx, "AsyncClient",
                        lambda **kw: real_client(transport=httpx.MockTransport(handler), **kw))
    llm_mod._rings.clear()
    llm = LLM(Profile("t", "https://example.invalid/v1", ["k1", "k2"], "m"))
    out = await llm.complete_json([{"role": "user", "content": "hi"}])
    assert out == {"ok": "k2"}
    assert seen == ["k1", "k2"]
    # k1 is now cooling, so the next call goes straight to k2
    out = await llm.complete_json([{"role": "user", "content": "hi"}])
    assert out == {"ok": "k2"}
    assert seen[-1] == "k2"
