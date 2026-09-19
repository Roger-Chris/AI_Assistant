"""Provider-agnostic LLM client.

Everything speaks the OpenAI chat-completions dialect, which Gemini (AI Studio),
OpenRouter, OpenAI, Groq and local servers (Ollama, vLLM) all expose. Two profiles:

  classifier — cheap, high volume (every WhatsApp message)
  planner    — strong, low volume (daily brief, drafting)

Each profile can carry several API keys (comma-separated in .env). Calls round-robin across
them, and a key that returns 429 / quota-exhausted is skipped for a cool-down so free-tier
limits are spread across all keys instead of one key hitting the wall.
"""
from __future__ import annotations

import asyncio
import itertools
import json
import logging
import re
import time
from dataclasses import dataclass
from typing import Any, Optional

import httpx

from .config import settings

log = logging.getLogger("brain.llm")

COOLDOWN_SECONDS = 60.0
TRANSIENT = {500, 502, 503, 504, 529}  # provider hiccups: retry, don't bench the key
MAX_ATTEMPTS = 4
BACKOFF_SECONDS = 1.5


@dataclass
class Profile:
    name: str
    base_url: str
    api_keys: list[str]
    model: str


class KeyRing:
    """Round-robin over keys with a per-key cool-down after a rate-limit response."""

    def __init__(self, keys: list[str]):
        self.keys = [k for k in keys if k]
        self._cycle = itertools.cycle(range(len(self.keys))) if self.keys else None
        self.cooling: dict[int, float] = {}
        # the AI Studio dashboard is per Google Cloud project, so keys from different projects
        # never show up on one page. Count here instead: this is the honest per-key tally.
        self.calls: dict[int, int] = {}
        self.tokens: dict[int, int] = {}

    def __len__(self) -> int:
        return len(self.keys)

    def next(self) -> tuple[int, str]:
        """Next usable key; if all are cooling, the one whose cool-down ends soonest."""
        if not self.keys:
            raise RuntimeError("no API key configured")
        now = time.monotonic()
        for _ in range(len(self.keys)):
            i = next(self._cycle)  # type: ignore[arg-type]
            if self.cooling.get(i, 0) <= now:
                return i, self.keys[i]
        i = min(self.cooling, key=self.cooling.get)  # type: ignore[arg-type]
        return i, self.keys[i]

    def penalise(self, i: int, seconds: float = COOLDOWN_SECONDS) -> None:
        self.cooling[i] = time.monotonic() + seconds

    def record(self, i: int, tokens: int) -> None:
        self.calls[i] = self.calls.get(i, 0) + 1
        self.tokens[i] = self.tokens.get(i, 0) + tokens


_rings: dict[str, KeyRing] = {}


def _ring(profile: Profile) -> KeyRing:
    ring = _rings.get(profile.name)
    if ring is None or ring.keys != [k for k in profile.api_keys if k]:
        ring = _rings[profile.name] = KeyRing(profile.api_keys)
    return ring


def _split(keys: str) -> list[str]:
    return [k.strip() for k in keys.split(",") if k.strip()]


def classifier_profile() -> Profile:
    keys = _split(settings.classifier_api_keys) or _split(settings.classifier_api_key)
    return Profile("classifier", settings.classifier_base_url, keys, settings.classifier_model)


def planner_profile() -> Profile:
    keys = _split(settings.planner_api_keys) or _split(settings.planner_api_key)
    return Profile("planner", settings.planner_base_url, keys, settings.planner_model)


_FENCE = re.compile(r"^```(?:json)?\s*|\s*```$", re.MULTILINE)


def salvage_json(text: str) -> Any:
    """Parse JSON that the model cut off mid-array (it hit the token ceiling).

    Walks the text tracking brackets and strings, remembers the last point where a complete
    element ended, truncates there and closes what is still open. Losing the tail of a list
    beats losing the whole answer."""
    depth: list[str] = []
    safe = -1
    in_str = esc = False
    for i, ch in enumerate(text):
        if in_str:
            if esc:
                esc = False
            elif ch == "\\":
                esc = True
            elif ch == '"':
                in_str = False
            continue
        if ch == '"':
            in_str = True
        elif ch in "[{":
            depth.append(ch)
        elif ch in "]}":
            if depth:
                depth.pop()
            if depth:
                safe = i + 1  # a complete element inside a container just ended
    if safe <= 0:
        raise ValueError("nothing salvageable")
    head = text[:safe].rstrip().rstrip(",")
    # close whatever is still open, innermost first
    d: list[str] = []
    in_str = esc = False
    for ch in head:
        if in_str:
            if esc:
                esc = False
            elif ch == "\\":
                esc = True
            elif ch == '"':
                in_str = False
            continue
        if ch == '"':
            in_str = True
        elif ch in "[{":
            d.append(ch)
        elif ch in "]}" and d:
            d.pop()
    return json.loads(head + "".join("]" if c == "[" else "}" for c in reversed(d)))


def extract_json(text: str) -> Any:
    """Tolerant JSON parse: strips code fences, leading/trailing prose, and truncated tails."""
    cleaned = _FENCE.sub("", text.strip())
    try:
        return json.loads(cleaned)
    except json.JSONDecodeError:
        pass
    start, end = cleaned.find("{"), cleaned.rfind("}")
    if start != -1 and end > start:
        try:
            return json.loads(cleaned[start : end + 1])
        except json.JSONDecodeError:
            pass
    if start != -1:
        out = salvage_json(cleaned[start:])
        log.warning("recovered a truncated JSON answer (%d chars) — raise max_tokens", len(cleaned))
        return out
    raise ValueError("no JSON object in the answer")


def _is_quota_error(r: httpx.Response) -> bool:
    if r.status_code == 429:
        return True
    if r.status_code == 403 and "quota" in r.text.lower():
        return True
    return False


class LLM:
    """Thin async client. Tests inject a fake via `LLM.override`."""

    override: Optional["LLM"] = None  # set in tests

    def __init__(self, profile: Profile, timeout: float = 60.0):
        self.profile = profile
        self.timeout = timeout

    async def complete(self, messages: list[dict], *, json_mode: bool = False,
                       temperature: float = 0.1, max_tokens: int = 800) -> str:
        if LLM.override is not None:
            return await LLM.override.complete(messages, json_mode=json_mode,
                                               temperature=temperature, max_tokens=max_tokens)
        p = self.profile
        ring = _ring(p)
        if not len(ring):
            raise RuntimeError(f"{p.name}: no API key configured")
        body: dict[str, Any] = {
            "model": p.model,
            "messages": messages,
            "temperature": temperature,
            "max_tokens": max_tokens,
        }
        if json_mode:
            body["response_format"] = {"type": "json_object"}
        url = p.base_url.rstrip("/") + "/chat/completions"

        last: Optional[httpx.Response] = None
        attempts = max(len(ring), MAX_ATTEMPTS)
        async with httpx.AsyncClient(timeout=self.timeout) as client:
            for attempt in range(attempts):
                idx, key = ring.next()
                headers = {"Authorization": f"Bearer {key}"}
                if "openrouter" in p.base_url:
                    headers["HTTP-Referer"] = "https://github.com/Roger-Chris/AI_Assistant"
                    headers["X-Title"] = "AI_Assistant brain"
                r = await client.post(url, json=body, headers=headers)
                if r.status_code == 400 and json_mode and "response_format" in body:
                    # some providers reject response_format; retry relying on the prompt
                    body.pop("response_format", None)
                    r = await client.post(url, json=body, headers=headers)
                if _is_quota_error(r):
                    ring.penalise(idx)
                    log.warning("%s: key #%d rate-limited (%s); rotating (%d/%d)",
                                p.name, idx + 1, r.status_code, attempt + 1, attempts)
                    last = r
                    continue
                if r.status_code in TRANSIENT:
                    # the model is busy, not the key — wait a little and try again
                    last = r
                    if attempt < attempts - 1:
                        wait = BACKOFF_SECONDS * (2 ** attempt)
                        log.warning("%s: HTTP %s from %s (busy); retrying in %.1fs (%d/%d)",
                                    p.name, r.status_code, p.model, wait, attempt + 1, attempts)
                        await asyncio.sleep(wait)
                    continue
                if r.status_code >= 400:
                    log.error("%s: HTTP %s from %s model=%s: %s", p.name, r.status_code,
                              p.base_url, p.model, r.text[:300])
                r.raise_for_status()
                data = r.json()
                usage = data.get("usage", {})
                ring.record(idx, (usage.get("prompt_tokens") or 0) + (usage.get("completion_tokens") or 0))
                log.info("llm %s model=%s key=#%d in=%s out=%s", p.name, p.model, idx + 1,
                         usage.get("prompt_tokens"), usage.get("completion_tokens"))
                return data["choices"][0]["message"]["content"]
        assert last is not None
        last.raise_for_status()
        raise RuntimeError("unreachable")

    async def complete_json(self, messages: list[dict], **kw) -> Any:
        text = await self.complete(messages, json_mode=True, **kw)
        return extract_json(text)


def usage_report() -> str:
    """Per-key call and token counts since the brain last started. Answers "is rotation
    actually spreading the load", which the AI Studio dashboard cannot: it is scoped to one
    Google Cloud project, so keys from other projects look unused there."""
    if not _rings:
        return "No model calls yet."
    now = time.monotonic()
    out = []
    for prof in (classifier_profile(), planner_profile()):
        ring = _rings.get(prof.name)
        if not ring or not len(ring):
            continue
        rows = []
        for i in range(len(ring)):
            cool = ring.cooling.get(i, 0) - now
            rows.append(f"  #{i + 1} {ring.keys[i][-6:]}: {ring.calls.get(i, 0)} calls, "
                        f"{ring.tokens.get(i, 0)} tokens" + (f" (cooling {cool:.0f}s)" if cool > 0 else ""))
        out.append(f"{prof.name} ({prof.model}), {len(ring)} keys:\n" + "\n".join(rows))
    return "\n\n".join(out) or "No model calls yet."
