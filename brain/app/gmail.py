"""Gmail as a second inbound source.

Every authorised account (see google_auth.accounts) is polled every few minutes. Promotions,
social and forum categories are excluded at the query — they never reach the brain. What's left
becomes `Message` rows with source="email" and runs through the same gate + classifier as
WhatsApp, with an email-specific pre-filter for bulk senders that carry no bill/date/request.

Chat id for an email is the sender address (per account), so `/mute` in Telegram works on senders.
"""
from __future__ import annotations

import base64
import html
import json
import logging
import re
from datetime import datetime, timedelta, timezone
from email.utils import parseaddr
from typing import Any, Optional

from sqlmodel import Session, select

from .config import settings
from .db import Message, Note, engine
from .google_auth import accounts, load_credentials

log = logging.getLogger("brain.gmail")

_service_override: dict[str, Any] = {}  # tests: label -> fake service

QUERY = "in:inbox -category:promotions -category:social -category:forums"
_BULK_SENDER = re.compile(r"(no-?reply|noreply|do-?not-?reply|newsletter|notifications?|mailer|marketing|promo|info@|news@|updates?@)", re.I)
# Mass mail only gets through when it is about something Roger ALREADY has: money owed, or his own
# booking / registration / application / account. Generic "register now", "you're invited",
# "action required" is marketing and stays out.
_MINE = re.compile(
    r"(amount (due|payable)|payment (due|reminder|failed|received|overdue)|due date|overdue|outstanding"
    r"|last date (to|for)|invoice|e-?bill|\bbill\b.{0,40}\bdue\b|\bdue (on|by)\b|statement"
    r"|minimum due|\bemi\b|auto-?pay"
    r"|your (?:\w+\s+){0,3}(booking|order|registration|application|ticket|admission|result|account|subscription"
    r"|membership|verification|status|payment|policy|claim|appointment|interview|submission|enrolment"
    r"|enrollment|seat|slot|refund|invoice|bill|fees?|dues?))", re.I)

_TAG = re.compile(r"<[^>]+>")
_WS = re.compile(r"[ \t]+")


def service(label: str):
    if label in _service_override:
        return _service_override[label]
    from googleapiclient.discovery import build

    creds = load_credentials(label)
    if not creds:
        return None
    return build("gmail", "v1", credentials=creds, cache_discovery=False)


def _decode(data: str) -> str:
    return base64.urlsafe_b64decode(data + "=" * (-len(data) % 4)).decode("utf-8", "replace")


def _body(payload: dict) -> str:
    """Prefer text/plain; fall back to stripped HTML. Walks multipart recursively."""
    plain, htm = [], []

    def walk(p):
        mime = p.get("mimeType", "")
        data = p.get("body", {}).get("data")
        if data and mime == "text/plain":
            plain.append(_decode(data))
        elif data and mime == "text/html":
            htm.append(_decode(data))
        for c in p.get("parts", []) or []:
            walk(c)

    walk(payload)
    text = "\n".join(plain) if plain else _TAG.sub(" ", html.unescape("\n".join(htm)))
    text = re.sub(r"\n{3,}", "\n\n", _WS.sub(" ", text)).strip()
    return text


def _headers(payload: dict) -> dict:
    return {h["name"].lower(): h["value"] for h in payload.get("headers", [])}


def is_bulk(sender: str, has_unsubscribe: bool) -> bool:
    return bool(_BULK_SENDER.search(sender) or has_unsubscribe)


def email_prefilter(sender: str, subject: str, text: str, has_unsubscribe: bool) -> Optional[str]:
    """Drop bulk mail unless it looks like a bill, booking, event, deadline or a real request."""
    probe = f"{subject}\n{text[:1500]}"
    if (_BULK_SENDER.search(sender) or has_unsubscribe) and not _MINE.search(probe):
        return "email-bulk"
    if not subject and not text.strip():
        return "empty"
    return None


def parse(label: str, account_email: str, m: dict) -> tuple[Message, Optional[str]]:
    """Gmail message resource -> (Message row, skip reason or None)."""
    payload = m.get("payload", {})
    h = _headers(payload)
    name, addr = parseaddr(h.get("from", ""))
    addr = (addr or "unknown").lower()
    subject = h.get("subject", "").strip()
    body = _body(payload) or m.get("snippet", "")
    text = f"Subject: {subject}\n\n{body[:4000]}"
    ts = int(m.get("internalDate", "0")) // 1000
    from_me = addr == account_email.lower()
    msg = Message(
        source="email",
        external_id=f"gmail:{label}:{m['id']}",
        chat_id=f"email:{label}:{addr}",
        chat_name=f"{name or addr} (mail)",
        sender_id=addr,
        sender_name=name or addr,
        from_me=from_me,
        is_group=False,
        text=text,
        media_type=None,
        is_bulk=is_bulk(f"{name} {addr}", "list-unsubscribe" in h),
        sent_at=datetime.fromtimestamp(ts, tz=timezone.utc),
    )
    skip = email_prefilter(f"{name} {addr}", subject, body, "list-unsubscribe" in h)
    return msg, skip


def _last_key(label: str) -> str:
    return f"gmail_last_{label}"


async def poll_account(session: Session, label: str) -> dict:
    svc = service(label)
    if not svc:
        return {"skipped": "not authorised"}
    profile = svc.users().getProfile(userId="me").execute()
    account_email = profile.get("emailAddress", "")
    note = session.get(Note, _last_key(label))
    since = int(note.value) if note else int((datetime.now(timezone.utc) - timedelta(days=settings.gmail_backfill_days)).timestamp())
    q = f"{QUERY} after:{since}"
    ids: list[str] = []
    page = None
    while True:
        res = svc.users().messages().list(userId="me", q=q, maxResults=50, pageToken=page).execute()
        ids += [x["id"] for x in res.get("messages", [])]
        page = res.get("nextPageToken")
        if not page or len(ids) >= 200:
            break

    from .routers.ingest import _mark_skipped, _process, touch_chat

    stored = skipped = queued = 0
    newest = since
    for mid in reversed(ids):  # oldest first
        ext = f"gmail:{label}:{mid}"
        if session.exec(select(Message).where(Message.external_id == ext)).first():
            continue
        full = svc.users().messages().get(userId="me", id=mid, format="full").execute()
        msg, skip = parse(label, account_email, full)
        session.add(msg)
        chat = touch_chat(session, msg.chat_id, msg.chat_name, False, msg.sent_at)
        session.commit()
        session.refresh(msg)
        stored += 1
        newest = max(newest, int(msg.sent_at.timestamp()))
        if chat.muted:
            _mark_skipped(session, msg, "muted-chat")
            skipped += 1
        elif skip:
            _mark_skipped(session, msg, skip)
            skipped += 1
        else:
            queued += 1
            await _process(msg.id)
    session.merge(Note(key=_last_key(label), value=str(newest)))
    session.commit()
    if stored:
        log.info("gmail[%s]: %d new, %d filtered, %d classified", label, stored, skipped, queued)
    return {"stored": stored, "filtered": skipped, "classified": queued}


def record_poll(session: Session, label: str, error: Optional[str], now: Optional[datetime] = None) -> None:
    """The watcher's view of Gmail health: Note gmail_error_<label> holds {since, count, error}
    while polls fail, and is removed by the first poll that works."""
    key = f"gmail_error_{label}"
    note = session.get(Note, key)
    if error is None:
        if note:
            session.delete(note)
            session.commit()
        return
    now = now or datetime.now(timezone.utc)
    info = {"since": now.isoformat(), "count": 0}
    if note:
        try:
            info = json.loads(note.value)
        except ValueError:
            pass
    info["count"] = int(info.get("count", 0)) + 1
    info["error"], info["last"] = error[:300], now.isoformat()
    session.merge(Note(key=key, value=json.dumps(info)))
    session.commit()


async def poll_all() -> None:
    for label in accounts():
        with Session(engine) as session:
            try:
                res = await poll_account(session, label)
                record_poll(session, label, f"skipped: {res['skipped']}" if "skipped" in res else None)
            except Exception as e:  # noqa: BLE001
                log.exception("gmail poll failed for %s", label)
                session.rollback()
                record_poll(session, label, f"{type(e).__name__}: {e}")
