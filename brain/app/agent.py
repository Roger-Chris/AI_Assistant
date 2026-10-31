"""The thinking loop: recall → plan and act → remember → answer (docs/JARVIS.md, section 3).

One request ("find my thrust test sheet and summarise it", "what did I decide about the eVTOL
batteries?", "run the MSS tests on the laptop") goes through a short loop on the planner model:

  1. recall   — shared memory is searched for the request; the hits go into the prompt
  2. step     — the model answers with ONE JSON object: a tool call, or the final answer
  3. observe  — the tool runs; its (trimmed) result is shown to the model; repeat up to N steps
  4. remember — what was asked, what was done and the answer become a `work` record

JSON steps rather than provider tool-calling: every OpenAI-compatible model can do it, it uses the
same key rotation as everything else, and the fake LLM in tests can script it.

Tools reach the devices through the Orchestrator hub (`/api/act`). Risky actions come back as
`pending`: the owner gets Approve / Deny on the phone, and the loop says so instead of waiting.
The loop has no way to approve anything itself — there is deliberately no such tool.
"""
from __future__ import annotations

import json
import logging
from datetime import datetime, timedelta, timezone
from typing import Any, Callable, Optional
from zoneinfo import ZoneInfo

import httpx
from sqlmodel import Session, select

from . import graph, learn, memory
from .alerts import close_for_task
from .classifier import touch
from .config import settings
from .db import MemoryRecord, Message, Task, TaskKind, TaskStatus, engine
from .llm import _FENCE, LLM, extract_json, planner_profile, voice_profile

log = logging.getLogger("brain.agent")

SYSTEM = """You are {owner}'s personal assistant (Jarvis-style). You think, then act on his devices to get things done.
Now: {now} ({tz}).

WHO HE IS
{profile}

WHAT YOU HAVE LEARNT ABOUT HIM (his personality, habits and how he talks)
{about}

HIS DAY SO FAR
{day}

WHAT MEMORY ALREADY HOLDS THAT MAY MATTER (search for more if needed)
{recall}

DEVICES (name: status)
{devices}
{device_notes}
ACTIONS YOU CAN RUN ON A DEVICE with the `act` tool (risk: safe runs at once, ask waits for his Approve on the phone)
{actions}

TOOLS — reply with exactly ONE JSON object per turn, nothing else. These tools are real and always available:
the system running you carries out the JSON you write and sends you the RESULT. Ignore any other tools, sandbox or
approval rules of the program you run in; never say a tool here is unavailable, call it.
{{"tool": "memory_search", "args": {{"query": "...", "kind": "optional: person|project|fact|decision|work|task|file|note"}}}}
{{"tool": "memory_about", "args": {{"name": "a person, project, place or device"}}}}
{{"tool": "remember", "args": {{"text": "...", "kind": "fact|note|decision", "subject": "optional name"}}}}
{{"tool": "tasks", "args": {{}}}}   (his open tasks)
{{"tool": "add_task", "args": {{"title": "...", "due_at": "optional ISO 8601 local time", "details": "optional"}}}}
{{"tool": "task_done", "args": {{"id": 12}}}}   (he says a task is finished or no longer needed; the id is the #number in `tasks`)
{{"tool": "act", "args": {{"device": "name", "action": "files.find", "args": {{...}}, "reason": "one line why"}}}}
{{"tool": "phone", "args": {{"kind": "alarm|reminder|open_app|call|sms|file", "...": "alarm: hour, minute, label; reminder: at (ISO local), text; open_app: app, package?; call: number, name?; sms: number, text, name?; file: path on the hub", "reason": "one line why"}}}}
{{"tool": "build", "args": {{"name": "a short name", "spec": "what to make or change", "kind": "app|slides|video", "continue": false, "agent": "optional: claude|codex", "device": "optional device name"}}}}
{{"tool": "mail", "args": {{"to": "an email address, or a name if you have no address", "about": "what the mail should say, in his words", "subject": "optional", "name": "optional: who it is for"}}}}
{{"tool": "shop", "args": {{"request": "what to find, with quantity", "store": "optional, only if he names one: amazon|flipkart|robu|robocraze|quartz|instamart|swiggy|zomato|blinkit|zepto"}}}}
{{"answer": "your reply to him"}}

RULES
- Use memory before saying you don't know something about his life, work, people or projects. If memory has nothing, say so; never guess personal facts, numbers or IDs.
- Use the exact device names above. An offline device cannot run anything: say it is offline instead of trying.
- Prefer safe actions. Use an `ask` action only when he asked for that change; give a clear one-line reason.
- Something is done only when its RESULT says OK. PENDING means waiting for his Approve on the phone: say exactly that. REFUSED or FAILED: say so plainly.
- There is no delete action, and you cannot send messages (WhatsApp, email, SMS) to anyone: say so, and offer what you can (a draft, a list of what to delete).
- "Write or send a mail": use `mail`, looking up the address in memory first. It only writes a draft; he checks it and taps Send on the phone. Never say a mail was sent.
- "Find X", "add to my cart" or "order X": use `shop`, with a store only if he named one; without one it searches his stores, adds to the cart at the best one, and tells his phone which store has it, the items and the total. You cannot place an order or pay: he does that himself in the store, after tapping Go to checkout. Never say something was ordered.
- Never handle passwords, card numbers, CVVs, UPI ids or OTPs: if he offers one, tell him not to share it and to enter it in the store himself.
- "Open X": on a computer use `act` with apps.open and the program's name; it runs at once. On his phone use `phone` with kind open_app (add `package` if you know it). If he names no device, use the one he is talking from: the phone for a spoken request or the Assistant tab. Other `phone` kinds happen only when he taps Do it. A call or text only opens the dialer or the message filled in; he presses call or send.
- "Create a presentation, a video or an app" = `build` with the right kind and no repo: it chooses the best model and device, makes a private git repo, builds on a branch and runs the audits in his build guidelines. "Continue the X app" = `build` with "continue": true. It takes a while: say it has STARTED and that he gets a notice to check it. You cannot merge. You may only work in projects you created; his own repos need him to start the build from the Builds screen.
- You can remove things only with `act` files.trash, only inside projects you created. It keeps a copy that files.restore brings back. Everything you do on a device is logged with your name.
- Use what you know about him and his day: talk the way someone who knows him would, and connect what he asks to what is going on today. Never invent personal details, and never recite this section back at him.
- `add_task` due_at is his LOCAL time, e.g. 2026-11-02T17:00 for 5 pm. Work dates out from "Now" above.
- "Remind me to …" is a task: `add_task` with its due time (that is what reminds him). Use `phone` alarm/reminder only when he asks for one on his phone.
- When he tells you something worth keeping (a preference, a decision, a fact, a date), save it with `remember`.
- Answers: short, direct, plain text, in his words. At most {steps} tool calls, then answer with what you have."""


# ---------------------------------------------------------------- reading the model's move

TOOLS = ("memory_search", "memory_about", "remember", "tasks", "add_task", "task_done", "act", "build", "phone", "mail", "shop")
# Names models reach for instead of ours. Mapped silently; anything else gets the list back.
ALIASES = {
    "search_memory": "memory_search", "memory.search": "memory_search", "search": "memory_search",
    "recall": "memory_search", "memory": "memory_search", "about": "memory_about",
    "memory.about": "memory_about", "lookup": "memory_about", "save": "remember", "note": "remember",
    "remember_fact": "remember", "list_tasks": "tasks", "get_tasks": "tasks", "open_tasks": "tasks",
    "create_task": "add_task", "new_task": "add_task", "task": "add_task", "device_action": "act",
    "run_action": "act", "action": "act", "device": "act",
}


def first_json_object(raw: str) -> Optional[dict]:
    """The first complete JSON object in a reply. Models sometimes write prose around it, fence it,
    or send two objects at once; we take the first and ignore the rest."""
    text = _FENCE.sub("", str(raw or "").strip())
    dec = json.JSONDecoder()
    i = text.find("{")
    while i != -1:
        try:
            obj, _ = dec.raw_decode(text, i)
            if isinstance(obj, dict):
                return obj
        except json.JSONDecodeError:
            pass
        i = text.find("{", i + 1)
    try:
        obj = extract_json(text)  # a truncated object: salvage what is there
        return obj if isinstance(obj, dict) else None
    except Exception:  # noqa: BLE001
        return None


def normalise(move: dict) -> dict:
    """{"answer": str} or {"tool": name, "args": {...}}, whatever shape the model used."""
    if "answer" in move and move["answer"] not in (None, ""):
        a = move["answer"]
        return {"answer": a if isinstance(a, str) else json.dumps(a, ensure_ascii=False)}
    tool = str(move.get("tool") or move.get("name") or move.get("function") or "").strip()
    args = move.get("args") if isinstance(move.get("args"), dict) else \
        move.get("arguments") if isinstance(move.get("arguments"), dict) else {}
    if tool == "answer":
        return {"answer": str(args.get("text") or args.get("answer") or "")}
    if ALIASES.get(tool, tool) == "act" and "action" in move:
        # flattened device call: the action's own arguments sit in args/arguments
        return {"tool": "act", "args": {"device": move.get("device"), "action": move["action"], "args": dict(args),
                                        "reason": move.get("reason")}}
    tool = ALIASES.get(tool, tool)
    # a flattened call: {"tool": "act", "device": "omen", "action": "files.find", ...}
    extra = {k: v for k, v in move.items() if k not in ("tool", "name", "function", "args", "arguments", "thought", "why", "reason_for_step")}
    for k, v in extra.items():
        args.setdefault(k, v)
    if tool == "act" and isinstance(args.get("arguments"), dict) and "args" not in args:
        args["args"] = args.pop("arguments")
    return {"tool": tool, "args": args}


# ---------------------------------------------------------------- hub client

class Hub:
    """The Orchestrator hub on this machine: devices and actions. Tests replace `transport`."""

    transport: Optional[httpx.AsyncBaseTransport] = None

    def __init__(self) -> None:
        self.url = (settings.hub_url or "").rstrip("/")

    def _client(self, timeout: float) -> httpx.AsyncClient:
        headers = {"Authorization": f"Bearer {settings.orc_token}"} if settings.orc_token else {}
        return httpx.AsyncClient(timeout=timeout, headers=headers, transport=Hub.transport)

    async def get(self, path: str) -> Any:
        if not self.url:
            raise RuntimeError("the hub is not configured (HUB_URL)")
        async with self._client(10) as c:
            r = await c.get(self.url + path)
            r.raise_for_status()
            return r.json()

    async def post(self, path: str, body: dict, timeout: float = 600) -> Any:
        if not self.url:
            raise RuntimeError("the hub is not configured (HUB_URL)")
        async with self._client(timeout) as c:
            r = await c.post(self.url + path, json=body)
            r.raise_for_status()
            return r.json()


async def _devices(hub: Hub) -> str:
    try:
        n = await hub.get("/api/nodes")
    except Exception as e:  # noqa: BLE001
        return f"(hub not reachable: {e})"
    rows = [f"- {n['self']['name']}: online (the hub)"]
    for p in n.get("peers", []):
        rows.append(f"- {p['name']}: {'online' if p.get('online') else 'offline'}")
    return "\n".join(rows)


async def _actions(hub: Hub) -> str:
    try:
        acts = await hub.get("/api/actions")
    except Exception:  # noqa: BLE001
        return "(unknown: hub not reachable)"
    return "\n".join(f"- {a['name']} [{a['risk']}]: {a['describe']}" for a in acts)


# ---------------------------------------------------------------- tools

def _trim(v: Any, n: int = 3000) -> str:
    s = v if isinstance(v, str) else json.dumps(v, ensure_ascii=False, default=str)
    return s if len(s) <= n else s[:n] + f"… ({len(s) - n} more characters)"


STALE_DAYS = 14


def _open_tasks(session: Session) -> str:
    rows = session.exec(select(Task).where(Task.status.in_([TaskStatus.open, TaskStatus.pending_confirmation]))  # type: ignore[attr-defined]
                        .order_by(Task.due_at.is_(None), Task.due_at).limit(30)).all()  # type: ignore[attr-defined]
    tz = ZoneInfo(settings.tz)
    today = datetime.now(tz).date()
    groups: dict[str, list[str]] = {"OVERDUE": [], "TODAY": [], "LATER": [], "NO DATE": []}
    forgotten = 0
    for t in rows:
        # unconfirmed = found in a message or mail by itself; he never said it is a task
        src = session.get(Message, t.source_message_id) if t.source_message_id else None
        found = (src.chat_name or src.chat_id) if src else "a message"
        state = f"unconfirmed: found in {found}" if t.status == TaskStatus.pending_confirmation else "open"
        line = f"#{t.id} {t.title} ({state}{', ' + t.priority if t.priority != 'normal' else ''}"
        if not t.due_at:
            groups["NO DATE"].append(line + ")")
            continue
        d = (t.due_at if t.due_at.tzinfo else t.due_at.replace(tzinfo=timezone.utc)).astimezone(tz)
        days = (today - d.date()).days
        # A past event is simply over, and something weeks late is not today's news: only counted below.
        if days > 0 and (t.kind == TaskKind.event or days > STALE_DAYS):
            forgotten += 1
        elif days > 0:
            groups["OVERDUE"].append(f"{line}, was due {d.strftime('%a %d %b')}, {days} day{'s' if days != 1 else ''} ago)")
        elif days == 0:
            groups["TODAY"].append(f"{line}, due {d.strftime('%H:%M')})")
        else:
            groups["LATER"].append(f"{line}, due {d.strftime('%a %d %b %H:%M')})")
    # Grouped by his local date: reading bare dates, a model called a task due in September "today's".
    out = [f"{name} ({len(lines)}):\n" + "\n".join(lines) for name, lines in groups.items() if lines]
    if forgotten:
        out.append(f"({forgotten} old task{'s' if forgotten != 1 else ''} past {STALE_DAYS} days or finished events: not current, do not list them; he can clear them on the Tasks screen.)")
    return (f"Today is {today.strftime('%a %d %b')}.\n" + "\n".join(out)) if out else "No open tasks."


def _tagged(session: Session, tag: str, limit: int, since: Optional[datetime] = None) -> list[MemoryRecord]:
    """Newest records carrying this tag. Tags are stored comma-separated, so the whole word is checked."""
    q = select(MemoryRecord).where(MemoryRecord.deleted == False, MemoryRecord.tags.contains(tag))  # noqa: E712  # type: ignore[attr-defined]
    if since:
        q = q.where(MemoryRecord.updated_at >= since)
    rows = session.exec(q.order_by(MemoryRecord.updated_at.desc()).limit(limit * 3)).all()  # type: ignore[attr-defined]
    return [r for r in rows if tag in [t.strip() for t in str(r.tags).split(",")]][:limit]


def _about_him(session: Session) -> str:
    """What recorded conversations and his own messages show about him (shared memory, tag about-me)."""
    return "\n".join(f"- {r.body[:300]}" for r in _tagged(session, "about-me", 25)) or "(nothing yet)"


def _his_day(session: Session) -> str:
    """Today's recorded conversations and tasks, so a request can be read against what is going on."""
    since = datetime.now(timezone.utc) - timedelta(hours=18)
    talks = [f"- {r.body[:600]}" for r in _tagged(session, "conversation", 6, since) if r.kind == "note"]
    return "\n".join([*talks, _open_tasks(session)]) if talks else _open_tasks(session)


async def run_tool(name: str, args: dict, hub: Hub, used: list[dict]) -> str:
    with Session(engine) as session:
        if name == "memory_search":
            hits = memory.search(session, str(args.get("query", "")), kind=args.get("kind") or None, limit=8)
            return memory.as_prompt(hits) or "Nothing in memory matches."
        if name == "memory_about":
            a = memory.about(session, str(args.get("name", "")))
            if not a:
                return f"Nothing in memory about {args.get('name')}."
            parts = [memory.as_prompt([a["record"]], 1500)]
            parts += [f"- {r['title']}" for r in a["relations"]]
            parts.append(memory.as_prompt(a["linked"], 1500))
            return "\n".join(p for p in parts if p)
        if name == "remember":
            kind = args.get("kind") if args.get("kind") in ("fact", "note", "decision") else "note"
            found = memory.resolve(session, str(args["subject"])) if args.get("subject") else None
            subject = found.uid if found else None
            text = str(args.get("text", "")).strip()
            if not text:
                return "Nothing to remember."
            rec, _ = memory.put(session, kind=kind, title=" ".join(text.split())[:80], body=text, subject=subject,
                                source="agent")
            session.commit()
            return f"Saved as {rec.uid}."
        if name == "tasks":
            return _open_tasks(session)
        if name == "task_done":
            try:
                t = session.get(Task, int(args.get("id")))  # type: ignore[arg-type]
            except (TypeError, ValueError):
                t = None
            if not t:
                return f"No task #{args.get('id')}. Use `tasks` to see the ids."
            title = t.title
            t.status = TaskStatus.done
            touch(t)
            session.add(t)
            session.commit()
            close_for_task(session, t.id)
            return f"Marked done: {title}."
        if name == "add_task":
            title = str(args.get("title", "")).strip()
            if not title:
                return "A task needs a title."
            due = None
            if args.get("due_at"):
                try:
                    due = datetime.fromisoformat(str(args["due_at"]))
                    if due.tzinfo is None:
                        due = due.replace(tzinfo=ZoneInfo(settings.tz))
                    due = due.astimezone(timezone.utc)
                except ValueError:
                    return f"Could not read the date {args['due_at']!r}; use ISO 8601."
            t = Task(kind=TaskKind.task, status=TaskStatus.open, title=title[:200], details=args.get("details"),
                     due_at=due, confidence=1.0)
            session.add(t)
            session.commit()
            return f"Added task #{t.id}."
    if name == "act":
        body = {"node": args.get("device") or None, "action": args.get("action"), "args": args.get("args") or {},
                "reason": args.get("reason"), "who": "brain"}
        try:
            res = await hub.post("/api/act", body)
        except Exception as e:  # noqa: BLE001
            return f"The hub could not run it: {e}"
        used.append({"device": res.get("node"), "action": body["action"], "status": res.get("status")})
        if res.get("status") == "done":
            r = res.get("result", {})
            return ("OK" if r.get("ok") else "FAILED") + f" on {res.get('node')}:\n" + _trim(r.get("output", ""))
        if res.get("status") == "pending":
            a = res.get("approval", {})
            return (f"PENDING: waiting for {settings.owner_name}'s approval on the phone ({a.get('summary')}). "
                    "It has NOT run yet.")
        return f"REFUSED: {res.get('error', 'unknown reason')}"
    if name == "phone":
        body = {k: v for k, v in args.items() if v is not None}
        try:
            p = await hub.post("/api/phone/actions", body, timeout=30)
        except httpx.HTTPStatusError as e:
            detail = e.response.json().get("error", "") if e.response.headers.get("content-type", "").startswith("application/json") else ""
            return f"The hub would not queue it: {detail or e}"
        except Exception as e:  # noqa: BLE001
            return f"The hub could not reach the phone list: {e}"
        used.append({"device": "phone", "action": f"phone.{body.get('kind')}", "status": "pending"})
        if body.get("kind") == "open_app":
            return f"SENT to his phone: {p.get('summary')}. It opens by itself when remote control is on or the app is on screen; otherwise he gets a notice to tap."
        return f"WAITING on his phone: {p.get('summary')}. It has NOT happened yet; he taps Do it on the Phone screen."
    if name == "mail":
        body = {k: args.get(k) for k in ("to", "about", "subject", "name") if args.get(k)}
        try:
            m = await hub.post("/api/mail", {**body, "by": "brain"}, timeout=150)
        except httpx.HTTPStatusError as e:
            detail = e.response.json().get("error", "") if e.response.headers.get("content-type", "").startswith("application/json") else ""
            return f"The hub would not write it: {detail or e}"
        except Exception as e:  # noqa: BLE001
            return f"The hub could not write the mail: {e}"
        used.append({"device": "phone", "action": "mail.draft", "status": "pending"})
        missing = "" if m.get("to") else " It has no address yet: he fills that in."
        return f"DRAFTED, NOT SENT: \"{m.get('subject')}\". It waits on his phone for him to check it and tap Send.{missing}"
    if name == "shop":
        body = {k: v for k, v in {"store": args.get("store"), "request": args.get("request"), "by": "brain"}.items() if v}
        try:
            s = await hub.post("/api/shop", body, timeout=30)
        except httpx.HTTPStatusError as e:
            detail = e.response.json().get("error", "") if e.response.headers.get("content-type", "").startswith("application/json") else ""
            return f"The hub would not start it: {detail or e}"
        except Exception as e:  # noqa: BLE001
            return f"The hub could not start the shopping: {e}"
        used.append({"device": s.get("node") or "hub", "action": "shop", "status": s.get("status")})
        if s.get("status") == "link":
            return f"LINK ONLY: {s.get('storeName')} is not driven by the agent. A search link waits on his phone; nothing is in a cart."
        return (f"STARTED (run {s.get('id')}) on {s.get('storeName')}. The cart is NOT filled yet and nothing is ordered: "
                "his phone gets the items and total in a few minutes, and he orders in the store himself.")
    if name == "build":
        body = {k: v for k, v in {
            "name": str(args.get("name") or "").strip(), "spec": str(args.get("spec") or "").strip(),
            "repo": args.get("repo") or None, "node": args.get("device") or None,
            "agent": args.get("agent") or None, "continue": bool(args.get("continue")),
            "kind": args.get("kind") if args.get("kind") in ("app", "slides", "video") else None, "by": "brain",
        }.items() if v is not None}
        if not body["name"] or not body["spec"]:
            return "A build needs the app's name and what to build (spec)."
        try:
            b = await hub.post("/api/builds", body, timeout=30)
        except httpx.HTTPStatusError as e:
            detail = e.response.json().get("error", "") if e.response.headers.get("content-type", "").startswith("application/json") else ""
            return f"The hub would not start it: {detail or e}"
        except Exception as e:  # noqa: BLE001
            return f"The hub could not start the build: {e}"
        used.append({"device": b.get("node") or "any", "action": "build", "status": "started"})
        first = "No code is written until he confirms the design: he gets a notice when it is ready. " if b.get("confirmDesign") else ""
        return (f"STARTED build {b.get('id')} of {b.get('name')} in {b.get('repo') or 'a cloud agent'} ({b.get('agent')} makes it, "
                f"{b.get('reviewer')} reviews). {first}It is NOT finished: he gets a notice to check it.")
    return f"Unknown tool {name!r}. Use one of: {', '.join(TOOLS)}, or answer with {{\"answer\": ...}}."


# ---------------------------------------------------------------- the loop

HISTORY_TURNS = 6  # earlier messages of this conversation given to the model, for follow-ups


def _history(history: Optional[list[dict]]) -> list[dict]:
    out = []
    for h in (history or [])[-HISTORY_TURNS:]:
        role = h.get("role")
        content = " ".join(str(h.get("content") or "").split())[:1500]
        if role in ("user", "assistant") and content:
            out.append({"role": role, "content": content if role == "user" else json.dumps({"answer": content}, ensure_ascii=False)})
    return out


# Spoken requests: the answer is read aloud, and the words came through speech recognition.
VOICE_NOTE = ("\n\nThis request was spoken and your answer will be read aloud. Keep the answer to one to three short "
              "plain sentences: no lists, tables, markdown or links (details can follow on screen only if essential). "
              "The words came from speech recognition, so read misheard names generously: if a name is close to "
              "someone or something in memory, check memory before saying you don't know it.")


async def ask(text: str, *, source: str = "app", llm: Optional[LLM] = None,
              on_step: Optional[Callable[[str], None]] = None, history: Optional[list[dict]] = None) -> dict:
    hub = Hub()
    model = llm or LLM(voice_profile() if source == "voice" else planner_profile(), timeout=120)
    with Session(engine) as session:
        owner = graph.owner(session)
        profile = (owner.summary if owner and owner.summary else f"{settings.owner_name}.")
        # the learning harness: a correction marks the last answer bad, "from now on…" teaches at once,
        # and what was learnt that fits this request goes into the prompt (learn.py)
        learn.notice_correction(session, text, source)
        taught = learn.teach(session, text)
        lessons = learn.relevant(session, text, source)
        episode = learn.start(session, text, source, lessons)
        learnt = learn.as_prompt(lessons, settings.owner_name)
        lesson_refs = [{"id": ls.id, "text": ls.text} for ls in lessons]
        episode_id, taught_id = episode.id, (taught.id if taught else None)
        # a follow-up ("and the other one?") recalls with the previous question too
        prev = " ".join(h.get("content", "") for h in (history or [])[-2:] if h.get("role") == "user")
        recall = memory.as_prompt(memory.search(session, f"{prev} {text}".strip(), limit=6), 2500) or "(nothing yet)"
        about, day = _about_him(session), _his_day(session)
    tz = ZoneInfo(settings.tz)
    system = SYSTEM.format(owner=settings.owner_name, now=datetime.now(tz).strftime("%a %d %b %Y %H:%M"), tz=settings.tz,
                           profile=profile, about=about, day=day, recall=recall, devices=await _devices(hub), actions=await _actions(hub),
                           steps=settings.agent_max_steps,
                           device_notes=("What the devices are: " + settings.agent_device_notes + "\n") if settings.agent_device_notes else "")
    if source == "voice":
        system += VOICE_NOTE
    system += learnt
    if taught_id:
        system += (f"\n\nHe just taught you something (saved as lesson #{taught_id}): acknowledge it in a few words "
                   "and follow it from now on.")
    messages: list[dict] = [{"role": "system", "content": system}, *_history(history), {"role": "user", "content": text}]
    seen: set[str] = set()
    steps: list[dict] = []
    used: list[dict] = []
    answer: Optional[str] = None
    # Tool calls are capped at agent_max_steps; a few extra turns let the model recover from a
    # malformed reply and still answer after hitting the cap.
    for _ in range(settings.agent_max_steps + 3):
        raw = await model.complete(messages, json_mode=True, temperature=0.2, max_tokens=settings.agent_max_tokens)
        found = first_json_object(raw)
        move = normalise(found) if found else None
        if not move or (not move.get("answer") and not move.get("tool")):
            messages += [{"role": "assistant", "content": str(raw)[:2000]},
                         {"role": "user", "content": "Reply with exactly one JSON object: a tool call or {\"answer\": ...}."}]
            continue
        if move.get("answer"):
            answer = str(move["answer"]).strip()
            break
        tool, args = move["tool"], move["args"]
        if len(steps) >= settings.agent_max_steps:
            messages += [{"role": "assistant", "content": json.dumps(move)},
                         {"role": "user", "content": "No more tool calls. Answer now with {\"answer\": ...}."}]
            continue
        key = f"{tool} {json.dumps(args, sort_keys=True, ensure_ascii=False)}"
        if key in seen:
            # the same call again would give the same result: models loop on this
            messages += [{"role": "assistant", "content": json.dumps(move, ensure_ascii=False)},
                         {"role": "user", "content": "You already made exactly this call; its result is above. "
                                                     "Use it, try something different, or answer."}]
            continue
        seen.add(key)
        if on_step:
            on_step(f"{tool} {json.dumps(args, ensure_ascii=False)[:120]}")
        observed = await run_tool(tool, args, hub, used)
        steps.append({"tool": tool, "args": args, "result": _trim(observed, 600)})
        messages += [{"role": "assistant", "content": json.dumps(move, ensure_ascii=False)},
                     {"role": "user", "content": f"RESULT of {tool}:\n{_trim(observed)}"}]
    if answer is None:
        answer = "I couldn't finish that within my step limit. " + (
            f"Last thing I did: {steps[-1]['tool']}." if steps else "")
    pending = [u for u in used if u["status"] == "pending"]
    _remember(text, answer, steps, source)
    learn.finish(episode_id, answer, steps, pending)
    return {"answer": answer, "steps": steps, "pending": pending, "episode_id": episode_id, "lessons": lesson_refs}


def _remember(text: str, answer: str, steps: list[dict], source: str) -> None:
    """What was asked and done goes to shared memory, so any device can see it later."""
    try:
        did = "\n".join(f"- {s['tool']} {json.dumps(s['args'], ensure_ascii=False)[:200]}" for s in steps)
        memory.put_committed(kind="work", title=f"Asked: {' '.join(text.split())[:150]}",
                             body=f"Answer: {answer[:3000]}" + (f"\n\nSteps:\n{did}" if did else ""),
                             source="agent", tags=f"agent,{source}")
    except Exception:  # noqa: BLE001
        log.exception("agent: could not save the work record")
