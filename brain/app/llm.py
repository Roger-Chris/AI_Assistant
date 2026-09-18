"""Provider-agnostic LLM client.

Everything speaks the OpenAI chat-completions dialect, which Gemini (AI Studio),
OpenRouter, OpenAI, Groq and local servers (Ollama, vLLM) all expose. Two profiles:

  classifier — cheap, high volume (every WhatsApp message)
  planner    — strong, low volume (daily brief, drafting)

Swap providers by editing .env, never code.
"""
from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass
from typing import Any, Optional

import httpx

from .config import settings

log = logging.getLogger("brain.llm")


@dataclass
class Profile:
    name: str
    base_url: str
    api_key: str
    model: str


def classifier_profile() -> Profile:
    return Profile("classifier", settings.classifier_base_url, settings.classifier_api_key,
                   settings.classifier_model)


def planner_profile() -> Profile:
    return Profile("planner", settings.planner_base_url, settings.planner_api_key,
                   settings.planner_model)


_FENCE = re.compile(r"^```(?:json)?\s*|\s*```$", re.MULTILINE)


def extract_json(text: str) -> Any:
    """Tolerant JSON parse: strips code fences and leading/trailing prose."""
    cleaned = _FENCE.sub("", text.strip())
    try:
        return json.loads(cleaned)
    except json.JSONDecodeError:
        start, end = cleaned.find("{"), cleaned.rfind("}")
        if start != -1 and end > start:
            return json.loads(cleaned[start : end + 1])
        raise


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
        if not p.api_key:
            raise RuntimeError(f"{p.name}: no API key configured")
        body: dict[str, Any] = {
            "model": p.model,
            "messages": messages,
            "temperature": temperature,
            "max_tokens": max_tokens,
        }
        if json_mode:
            body["response_format"] = {"type": "json_object"}
        headers = {"Authorization": f"Bearer {p.api_key}"}
        if "openrouter" in p.base_url:
            headers["HTTP-Referer"] = "https://github.com/Roger-Chris/AI_Assistant"
            headers["X-Title"] = "AI_Assistant brain"
        url = p.base_url.rstrip("/") + "/chat/completions"
        async with httpx.AsyncClient(timeout=self.timeout) as client:
            r = await client.post(url, json=body, headers=headers)
            if r.status_code == 400 and json_mode:
                # some providers reject response_format; retry relying on the prompt
                body.pop("response_format", None)
                r = await client.post(url, json=body, headers=headers)
            r.raise_for_status()
            data = r.json()
        usage = data.get("usage", {})
        log.info("llm %s model=%s in=%s out=%s", p.name, p.model,
                 usage.get("prompt_tokens"), usage.get("completion_tokens"))
        return data["choices"][0]["message"]["content"]

    async def complete_json(self, messages: list[dict], **kw) -> Any:
        text = await self.complete(messages, json_mode=True, **kw)
        return extract_json(text)
