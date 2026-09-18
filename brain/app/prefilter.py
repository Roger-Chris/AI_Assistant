"""Cheap, rule-based triage that runs BEFORE any LLM call.

Returns a reason string if the message is obviously not worth a model call, else None.
Filtered messages are still stored (context) and tagged with the reason so misfires are auditable.
"""
from __future__ import annotations

import re
from typing import Optional

from .config import settings

# pure acknowledgements / greetings / fillers (English + common Tanglish), matched whole
_CHITCHAT = {
    "ok", "okay", "okk", "k", "kk", "okie", "oki", "hmm", "hm", "hmmm", "mm", "ya", "yeah", "yes", "yep",
    "no", "nope", "sure", "fine", "done", "nice", "cool", "great", "good", "super", "wow", "lol", "lmao",
    "haha", "hahaha", "hehe", "thanks", "thank you", "thx", "ty", "tq", "welcome", "np", "bye", "gn",
    "gm", "good morning", "good night", "good evening", "good afternoon", "hi", "hii", "hiii", "hello",
    "hey", "yo", "sup", "congrats", "congratulations", "happy birthday", "hbd", "all the best", "atb",
    "seri", "sari", "aama", "ama", "illa", "vanakkam", "ok da", "ok bro", "ok pa", "ok ma", "ok sir",
    "ok mam", "ok ji", "sure da", "sure bro", "thanks da", "thanks bro", "haan", "nahi", "theek hai",
    "achha", "acha", "ji", "hmm ok", "ok ok", "okay okay", "yes sir", "no sir", "noted", "got it",
    "understood", "same", "same here", "me too", "+1", "true", "exactly", "ikr", "omg", "bruh",
}
_EMOJI_ONLY = re.compile(r"^[\W_\d]+$")  # no letters at all: emoji, punctuation, numbers only
_URL_ONLY = re.compile(r"^\s*(https?://\S+\s*)+$", re.I)

# anything that smells like time, commitment, or a request — used only as a GROUP gate
_SIGNAL = re.compile(
    r"\b(\d{1,2}[:.]\d{2}|\d{1,2}\s?(am|pm)|\d{1,2}[/-]\d{1,2}|"
    r"today|tomorrow|tonight|tmrw|tmr|yesterday|monday|tuesday|wednesday|thursday|friday|saturday|sunday|"
    r"mon|tue|wed|thu|fri|sat|sun|week|month|morning|evening|afternoon|noon|night|"
    r"deadline|due|submit|submission|send|share|forward|upload|fill|register|apply|attend|bring|"
    r"meeting|meet|call|class|lab|exam|test|quiz|viva|review|presentation|ppt|report|assignment|"
    r"project|task|work|urgent|asap|important|reminder|remind|please|pls|plz|kindly|"
    r"can you|could you|will you|need|needed|required|must|should|have to|"
    r"before|after|by|at|on|until|till|last date|extended|postponed|cancelled|canceled|shifted|"
    r"venue|room|hall|link|zoom|gmeet|teams|"
    r"everyone|all|team|@)\b",
    re.I,
)


def _norm(text: str) -> str:
    t = text.strip().lower()
    t = re.sub(r"[\W_]+$", "", t)  # trailing punctuation / emoji
    t = re.sub(r"^[\W_]+", "", t)
    return re.sub(r"\s+", " ", t)


def owner_mentioned(text: str, sender_is_me: bool = False) -> bool:
    if sender_is_me:
        return False
    names = [n for n in re.split(r"[\s,]+", settings.owner_name.lower()) if len(n) >= 3]
    aliases = [a.strip().lower() for a in settings.owner_aliases.split(",") if a.strip()]
    low = text.lower()
    return any(n in low for n in names + aliases)


def prefilter(text: str, *, is_group: bool, from_me: bool, forwarded: bool = False,
              media_type: Optional[str] = None) -> Optional[str]:
    """Return a skip reason, or None to let the LLM see it."""
    t = _norm(text)
    if not t:
        return "empty"
    if len(t) <= 2 and not t.isalpha():
        return "too-short"
    if t in _CHITCHAT:
        return "chitchat"
    if _EMOJI_ONLY.match(t):
        return "emoji-only"
    if len(t.split()) <= 2 and any(t.startswith(w) for w in ("ok", "hmm", "haha", "lol", "thanks", "gm", "gn")):
        return "chitchat"
    if _URL_ONLY.match(text):
        return "url-only"
    if media_type == "audio" and text.startswith("[voice note"):
        return "voice-note-untranscribed"
    if is_group:
        if forwarded:
            return "forwarded-in-group"
        if settings.group_keyword_gate and not owner_mentioned(text, from_me) and not _SIGNAL.search(text):
            return "group-no-signal"
    return None
