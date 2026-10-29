"""Telegram long-poller: the interim remote until the Android app exists.

Handles: /start (learns your chat id), /tasks, /today, /done <id>, and the inline
Keep / Not-a-task buttons on new-task notifications. Runs as a background task in the app.
"""
from __future__ import annotations

import asyncio
import logging
from datetime import datetime
from zoneinfo import ZoneInfo

from sqlmodel import Session, select

from . import alerts, graph
from .classifier import touch
from .config import settings
from .db import Alert, Chat, Note, Task, TaskStatus, engine
from .notify import fmt_due, tg

log = logging.getLogger("brain.telegram")


def _list(session: Session, only_today: bool = False) -> str:
    q = select(Task).where(Task.status.in_([TaskStatus.open, TaskStatus.pending_confirmation]))  # type: ignore[attr-defined]
    tasks = session.exec(q.order_by(Task.due_at.is_(None), Task.due_at)).all()  # type: ignore[attr-defined]
    if only_today:
        tz = ZoneInfo(settings.tz)
        end = datetime.now(tz).replace(hour=23, minute=59, second=59)
        tasks = [t for t in tasks if t.due_at and
                 (t.due_at if t.due_at.tzinfo else t.due_at.replace(tzinfo=ZoneInfo("UTC"))) <= end]
    if not tasks:
        return "Nothing pending." if not only_today else "Nothing due today."
    lines = []
    for t in tasks:
        pend = " (unconfirmed)" if t.status == TaskStatus.pending_confirmation else ""
        lines.append(f"#{t.id} {t.title} — {fmt_due(t)}{pend}")
    return "\n".join(lines)


async def _handle_message(m: dict) -> None:
    chat_id = str(m["chat"]["id"])
    text = (m.get("text") or "").strip()
    with Session(engine) as session:
        if not settings.telegram_chat_id:
            session.merge(Note(key="telegram_chat_id", value=chat_id))
            session.commit()
        if text.startswith("/start"):
            reply = f"Connected. Your chat id is <code>{chat_id}</code> — put it in .env as TELEGRAM_CHAT_ID."
        elif text.startswith("/tasks"):
            reply = _list(session)
        elif text.startswith("/today"):
            reply = _list(session, only_today=True)
        elif text.startswith("/done"):
            try:
                tid = int(text.split()[1])
                t = session.get(Task, tid)
                assert t
                t.status = TaskStatus.done
                touch(t)
                session.add(t)
                session.commit()
                reply = f"Done: {t.title}"
            except (IndexError, ValueError, AssertionError):
                reply = "Usage: /done <id>"
        elif text.startswith("/brief"):
            from . import scheduler as _sched
            await _sched.daily_brief()
            return
        elif text.startswith("/who"):
            arg = text[4:].strip()
            reply = graph.describe(session, arg) if arg else "Usage: /who <name>"
        elif text.startswith("/graph"):
            st = graph.stats(session)
            kinds = ", ".join(f"{v} {k}" for k, v in sorted(st["entities"].items(), key=lambda x: -x[1])) or "nothing yet"
            o = graph.owner(session)
            reply = (f"Memory graph: {kinds}; {st['relations']} links.\n"
                     + (f"\nYou: {o.summary}" if o and o.summary else "\nNo profile yet — seed it from your CV."))
        elif text.startswith("/keys"):
            from .llm import usage_report
            reply = "<pre>" + usage_report() + "</pre>"
        elif text.startswith("/chats"):
            chats = session.exec(select(Chat).order_by(Chat.message_count.desc()).limit(40)).all()  # type: ignore[attr-defined]
            reply = "Chats by volume (id · name · msgs):\n" + "\n".join(
                f"{'🔇 ' if c.muted else ''}{c.id} · {(c.name or c.chat_id.split('@')[0])[:32]} · {c.message_count}"
                for c in chats) + "\n\n/mute <id>  /unmute <id>"
        elif text.startswith(("/mute", "/unmute")):
            try:
                cid = int(text.split()[1])
                c = session.get(Chat, cid)
                assert c
                c.muted = text.startswith("/mute")
                session.add(c)
                session.commit()
                reply = f"{'Muted' if c.muted else 'Unmuted'}: {c.name or c.chat_id}"
            except (IndexError, ValueError, AssertionError):
                reply = "Usage: /mute <id> or /unmute <id> (ids from /chats)"
        elif text.startswith("/") or not text:
            reply = ("Commands: /tasks, /today, /brief, /done &lt;id&gt;, /chats, /mute &lt;id&gt;, /unmute &lt;id&gt;, "
                     "/who &lt;name&gt;, /graph, /keys. Anything else you write goes to the assistant.")
        else:
            await _ask_agent(chat_id, text)
            return
    await tg("sendMessage", chat_id=chat_id, text=reply, parse_mode="HTML")


async def _ask_agent(chat_id: str, text: str) -> None:
    """Free text goes to the thinking loop, which can act on the owner's devices: only the chat in
    TELEGRAM_CHAT_ID may use it. (The chat id learned from /start is not enough: whoever wrote to
    the bot last would count as the owner.)"""
    if not settings.agent_enabled:
        await tg("sendMessage", chat_id=chat_id, text="The assistant is switched off (AGENT_ENABLED).")
        return
    if not settings.telegram_chat_id:
        await tg("sendMessage", chat_id=chat_id,
                 text=f"Set TELEGRAM_CHAT_ID={chat_id} in the brain's .env (if this is your chat) to talk to the assistant here.")
        return
    if chat_id != str(settings.telegram_chat_id):
        log.warning("telegram: free text from chat %s, which is not the owner's; ignored", chat_id)
        return
    from . import agent  # late: agent imports the LLM stack

    await tg("sendChatAction", chat_id=chat_id, action="typing")
    try:
        res = await agent.ask(text, source="telegram")
        reply = res["answer"]
        if res["pending"]:
            reply += "\n\n(Waiting for your Approve in the app.)"
    except Exception as e:  # noqa: BLE001
        log.exception("agent failed")
        reply = f"Sorry, that failed: {e}"
    await tg("sendMessage", chat_id=chat_id, text=reply[:4000])


_ACK_LABEL = {"done": "✅ Done", "snooze": "⏰ Snoozed", "dismiss": "🙈 Not important"}


def _ack_alert(data: str) -> str:
    """Buttons on alerts sent through Telegram: alert:<done|snooze|dismiss>:<id>."""
    _, action, aid = (data.split(":", 2) + ["", ""])[:3]
    with Session(engine) as session:
        a = session.get(Alert, int(aid)) if aid.isdigit() else None
        if not a or action not in alerts.ACTIONS:
            return ""
        alerts.ack(session, a, action)
    return _ACK_LABEL[action]


async def _handle_callback(cb: dict) -> None:
    data = cb.get("data", "")
    action, _, tid = data.partition(":")
    label = _ack_alert(data) if action == "alert" else ""
    with Session(engine) as session:
        t = session.get(Task, int(tid)) if tid.isdigit() and action != "alert" else None
        if t and action == "confirm":
            t.status = TaskStatus.open
            label = "✅ Kept"
        elif t and action == "reject":
            t.status = TaskStatus.rejected
            label = "❌ Dropped"
        elif t and action == "reopen":
            t.status = TaskStatus.open
            label = "↩️ Reopened"
        elif t and action == "done":
            t.status = TaskStatus.done
            label = "✅ Done"
        if t:
            touch(t)
            session.add(t)
            session.commit()
    await tg("answerCallbackQuery", callback_query_id=cb["id"], text=label or "?")
    msg = cb.get("message")
    if msg and label:
        await tg("editMessageText", chat_id=msg["chat"]["id"], message_id=msg["message_id"],
                 text=msg.get("text", "") + f"\n\n{label}")


async def poll_forever() -> None:
    if not settings.telegram_bot_token:
        log.info("telegram disabled (no token)")
        return
    offset = 0
    log.info("telegram poller started")
    while True:
        try:
            res = await tg("getUpdates", offset=offset, timeout=25, allowed_updates=["message", "callback_query"])
            for upd in (res or {}).get("result", []):
                offset = upd["update_id"] + 1
                if "message" in upd:
                    await _handle_message(upd["message"])
                elif "callback_query" in upd:
                    await _handle_callback(upd["callback_query"])
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001
            log.exception("telegram poll error; retrying in 5s")
            await asyncio.sleep(5)
