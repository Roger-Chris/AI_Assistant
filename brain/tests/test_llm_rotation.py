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


@pytest.mark.asyncio
async def test_retries_transient_503_then_succeeds(monkeypatch):
    """Gemini answers 503 when the model is busy — that is not the key's fault."""
    llm_mod.LLM.override = None
    seen = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request.headers["authorization"].split()[1])
        if len(seen) < 3:
            return httpx.Response(503, text="The model is overloaded. Please try again later.")
        return httpx.Response(200, json={"choices": [{"message": {"content": '{"ok": true}'}}], "usage": {}})

    real = httpx.AsyncClient
    monkeypatch.setattr(httpx, "AsyncClient",
                        lambda **kw: real(transport=httpx.MockTransport(handler), **kw))
    monkeypatch.setattr(llm_mod, "BACKOFF_SECONDS", 0.001)
    llm_mod._rings.clear()
    llm = LLM(Profile("t2", "https://example.invalid/v1", ["k1", "k2"], "m"))
    assert await llm.complete_json([{"role": "user", "content": "hi"}]) == {"ok": True}
    assert len(seen) == 3
    # a busy model must not bench the keys — both stay usable
    assert llm_mod._rings["t2"].cooling == {}


def test_salvages_json_the_model_cut_off():
    """Real failure: a resume produced more entities than max_tokens allowed, so the relations
    array was cut mid-element and the whole answer was lost."""
    from app.llm import extract_json
    cut = ('{"entities": [{"name": "RIT", "kind": "org", "summary": "his college", "aliases": []},'
           ' {"name": "Zapprise", "kind": "project", "summary": "co-founded", "aliases": []}],'
           ' "relations": [["Roger", "studies_at", "RIT"], ["Roger", "works_on", "Zapp')
    out = extract_json(cut)
    assert [e["name"] for e in out["entities"]] == ["RIT", "Zapprise"]
    assert out["relations"] == [["Roger", "studies_at", "RIT"]]  # the half-written one is dropped

    # cut inside an entity object -> keep the entities that completed
    cut2 = '{"entities": [{"name": "RIT", "kind": "org"}, {"name": "Zap", "kind": "proj'
    assert [e["name"] for e in extract_json(cut2)["entities"]] == ["RIT"]

    # a complete answer is untouched
    assert extract_json('```json\n{"entities": [], "relations": []}\n```') == {"entities": [], "relations": []}


@pytest.mark.asyncio
async def test_usage_report_counts_every_key(monkeypatch):
    """The AI Studio dashboard is per Google Cloud project, so it cannot answer 'is the
    rotation working'. The ring counts calls itself."""
    llm_mod.LLM.override = None

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"choices": [{"message": {"content": "{}"}}],
                                         "usage": {"prompt_tokens": 10, "completion_tokens": 5}})

    real = httpx.AsyncClient
    monkeypatch.setattr(httpx, "AsyncClient",
                        lambda **kw: real(transport=httpx.MockTransport(handler), **kw))
    llm_mod._rings.clear()
    monkeypatch.setattr(llm_mod.settings, "classifier_api_keys", "kA,kB,kC")
    monkeypatch.setattr(llm_mod.settings, "planner_api_keys", "")
    monkeypatch.setattr(llm_mod.settings, "planner_api_key", "")
    llm = LLM(llm_mod.classifier_profile())
    for _ in range(6):
        await llm.complete_json([{"role": "user", "content": "hi"}])
    ring = llm_mod._rings["classifier"]
    assert ring.calls == {0: 2, 1: 2, 2: 2}
    assert ring.tokens == {0: 30, 1: 30, 2: 30}
    report = llm_mod.usage_report()
    assert "2 calls, 30 tokens" in report and report.count("calls") == 3


@pytest.mark.asyncio
async def test_thinks_with_chatgpt_through_the_hub_and_falls_back_to_gemini(monkeypatch):
    """PLANNER_BASE_URL=hub + PLANNER_MODEL=codex: your ChatGPT session via the hub's /v1;
    when that fails, the fallback (Gemini by API) answers instead."""
    llm_mod.LLM.override = None
    seen = []
    hub_up = {"ok": True}

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        seen.append((request.url.host, body["model"], request.headers["authorization"].split()[1]))
        if request.url.host == "127.0.0.1" and not hub_up["ok"]:
            return httpx.Response(502, json={"error": {"message": "codex: usage limit"}})
        return httpx.Response(200, json={"choices": [{"message": {"content": f"from {body['model']}"}}], "usage": {}})

    real = httpx.AsyncClient
    monkeypatch.setattr(httpx, "AsyncClient", lambda **kw: real(transport=httpx.MockTransport(handler), **kw))
    monkeypatch.setattr(llm_mod, "BACKOFF_SECONDS", 0.001)
    s = llm_mod.settings
    monkeypatch.setattr(s, "hub_url", "http://127.0.0.1:4317")
    monkeypatch.setattr(s, "orc_token", "")
    monkeypatch.setattr(s, "planner_base_url", "hub")
    monkeypatch.setattr(s, "planner_model", "codex")
    monkeypatch.setattr(s, "planner_api_key", "")
    monkeypatch.setattr(s, "planner_api_keys", "")
    monkeypatch.setattr(s, "planner_fallback_base_url", "https://generativelanguage.googleapis.com/v1beta/openai")
    monkeypatch.setattr(s, "planner_fallback_api_keys", "g1")
    monkeypatch.setattr(s, "planner_fallback_model", "gemini-3.8-flash")
    llm_mod._rings.clear()

    p = llm_mod.planner_profile()
    assert p.base_url == "http://127.0.0.1:4317/v1" and p.model == "raw:codex" and p.min_timeout >= 180
    assert await LLM(p).complete([{"role": "user", "content": "hi"}]) == "from raw:codex"
    assert seen == [("127.0.0.1", "raw:codex", "loopback")]

    hub_up["ok"] = False
    assert await LLM(p).complete([{"role": "user", "content": "hi"}]) == "from gemini-3.8-flash"
    assert seen[-1] == ("generativelanguage.googleapis.com", "gemini-3.8-flash", "g1")
