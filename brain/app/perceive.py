"""Perceive: turn media into text before classification.

  voice note -> transcript
  image      -> short description + any text in it (timetables, posters, notes)

Provider order: a live worker advertising `stt` (Phase 4, Whisper on the Omen) for audio, then
Gemini's native multimodal endpoint using the classifier's key ring. The result is written into
the message's `text`, so the classifier never knows the message wasn't typed.
"""
from __future__ import annotations

import base64
import logging
from typing import Optional

import httpx
from sqlmodel import Session

from .config import settings
from .llm import LLM, _ring, classifier_profile
from .routers.workers import find_alive

log = logging.getLogger("brain.perceive")

AUDIO_PROMPT = ("Transcribe this WhatsApp voice note verbatim in the language(s) spoken "
                "(Tamil/Tanglish/Hindi/English mixes are common; keep them as spoken, romanised). "
                "Output only the transcript, no preamble.")
IMAGE_PROMPT = ("This image was shared in a WhatsApp chat. In 1-3 lines, say what it is and copy out "
                "any text that matters (dates, times, venues, names, deadlines, instructions). "
                "If it's a meme/sticker/selfie with no useful text, reply exactly: [no actionable content]")


def _model() -> str:
    return settings.perceive_model or settings.classifier_model


async def _gemini_native(mime: str, data: bytes, prompt: str) -> str:
    """Gemini generateContent with inline media. Uses the same rotating keys as the classifier."""
    p = classifier_profile()
    ring = _ring(p)
    if not len(ring):
        raise RuntimeError("perceive: no API key")
    base = p.base_url.split("/openai")[0].rstrip("/")  # .../v1beta
    url = f"{base}/models/{_model()}:generateContent"
    body = {"contents": [{"parts": [
        {"inline_data": {"mime_type": mime, "data": base64.b64encode(data).decode()}},
        {"text": prompt},
    ]}], "generationConfig": {"temperature": 0.1, "maxOutputTokens": 600}}
    async with httpx.AsyncClient(timeout=120) as client:
        for _ in range(len(ring)):
            idx, key = ring.next()
            r = await client.post(url, json=body, headers={"x-goog-api-key": key})
            if r.status_code == 429:
                ring.penalise(idx)
                continue
            if r.status_code >= 400:
                log.error("perceive: HTTP %s: %s", r.status_code, r.text[:300])
            r.raise_for_status()
            parts = r.json()["candidates"][0]["content"]["parts"]
            out = "".join(pt.get("text", "") for pt in parts).strip()
            log.info("perceive %s via gemini key=#%d -> %d chars", mime, idx + 1, len(out))
            return out
    raise RuntimeError("perceive: all keys rate-limited")


async def _openai_compat(mime: str, data: bytes, prompt: str, kind: str) -> str:
    """Fallback for non-Gemini OpenAI-compatible endpoints (images only)."""
    if kind != "image":
        raise RuntimeError("audio needs Gemini or a worker")
    b64 = base64.b64encode(data).decode()
    msgs = [{"role": "user", "content": [
        {"type": "text", "text": prompt},
        {"type": "image_url", "image_url": {"url": f"data:{mime};base64,{b64}"}},
    ]}]
    return (await LLM(classifier_profile()).complete(msgs, max_tokens=400)).strip()


async def _worker_stt(session: Session, mime: str, data: bytes) -> Optional[str]:
    w = find_alive(session, "stt")
    if not w:
        return None
    try:
        async with httpx.AsyncClient(timeout=120) as client:
            r = await client.post(f"{w.url.rstrip('/')}/transcribe",
                                  files={"file": ("audio", data, mime)},
                                  headers={"Authorization": f"Bearer {settings.brain_token}"})
            r.raise_for_status()
            return r.json().get("text", "").strip() or None
    except Exception as e:  # noqa: BLE001
        log.warning("worker stt failed (%s); falling back to cloud", e)
        return None


async def perceive(session: Session, kind: str, mime: str, data: bytes) -> Optional[str]:
    """kind: 'audio' | 'image'. Returns text, or None if perception is off/unavailable."""
    if not settings.perceive_enabled:
        return None
    if len(data) > settings.perceive_max_bytes:
        log.info("perceive: media too large (%d bytes), skipping", len(data))
        return None
    if kind == "audio":
        text = await _worker_stt(session, mime, data)
        if text:
            return text
    prompt = AUDIO_PROMPT if kind == "audio" else IMAGE_PROMPT
    if "generativelanguage.googleapis.com" in settings.classifier_base_url:
        return await _gemini_native(mime, data, prompt)
    return await _openai_compat(mime, data, prompt, kind)
