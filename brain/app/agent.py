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
from datetime import datetime, timezone
from typing import Any, Callable, Optional
from zoneinfo import ZoneInfo

import httpx
from sqlmodel import Session, select

from . import graph, memory
from .config import settings
from .db import Task, TaskKind, TaskStatus, engine
from .llm import _FENCE, LLM, extract_json, planner_profile

log = logging.getLogger("brain.agent")

SYSTEM = """You are {owner}'s personal assistant (Jarvis-style). You think, then act on his devices to get things done.
Now: {now} ({tz}).

WHO HE IS
{profile}

WHAT MEMORY ALREADY HOLDS THAT MAY MATTER (search for more if needed)
{recall}

DEVICES (name: status)
{devices}
{device_notes}
ACTIONS YOU CAN RUN ON A DEVICE with the `act` tool (risk: safe runs at once, ask waits for his Approve on the phone)
{actions}

TOOLS — reply with exactly ONE JSON object per turn, nothing else:
{{"tool": "memory_search", "args": {{"query": "...", "kind": "optional: person|project|fact|decision|work|task|file|note"}}}}
{{"tool": "memory_about", "args": {{"name": "a person, project, place or device"}}}}
{{"tool": "remember", "args": {{"text": "...", "kind": "fact|note|decision", "subject": "optional name"}}}}
{{"tool": "tasks", "args": {{}}}}   (his open tasks)
{{"tool": "add_task", "args": {{"title": "...", "due_at": "optional ISO 8601 local time", "details": "optional"}}}}
{{"tool": "act", "args": {{"device": "name", "action": "files.find", "args": {{...}}, "reason": "one line why"}}}}
{{"tool": "build", "args": {{"name": "the app's name", "spec": "what to build or change", "repo": "git folder on the hub's machine (not needed when continuing)", "continue": false, "agent": "optional: claude|codex", "device": "optional device name"}}}}
{{"answer": "your reply to him"}}

RULES
- Use memory before saying you don't know something about his life, work, people or projects. If memory has nothing, say so; never guess personal facts, numbers or IDs.
- Use the exact device names above. An offline device cannot run anything: say it is offline instead of trying.
- Prefer safe actions. Use an `ask` action only when he asked for that change; give a clear one-line reason.
- Something is done only when its RESULT says OK. PENDING means waiting for his Approve on the phone: say exactly that. REFUSED or FAILED: say so plainly.
- There is no delete action, and you cannot send messages (WhatsApp, email, SMS) to anyone: say so, and offer what you can (a draft, a list of what to delete).
- Never handle passwords.
- `build` makes or changes an app on its own git branch (coding agent, tests, a second model's review); it never touches main. "Continue the X app" = `build` with "continue": true. It takes a while: say it has STARTED and that he gets a notice with a preview to check and Merge on the phone. You cannot merge.
- `add_task` due_at is his LOCAL time, e.g. 2026-11-02T17:00 for 5 pm. Work dates out from "Now" above.
- When he tells you something worth keeping (a preference, a decision, a fact, a date), save it with `remember`.
- Answers: short, direct, plain text, in his words. At most {steps} tool calls, then answer with what you have."""


# ---------------------------------------------------------------- reading the model's move

TOOLS = ("memory_search", "memory_about", "remember", "tasks", "add_task", "act", "build")
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


def _open_tasks(session: Session) -> str:
    rows = session.exec(select(Task).where(Task.status.in_([TaskStatus.open, TaskStatus.pending_confirmation]))  # type: ignore[attr-defined]
                        .order_by(Task.due_at.is_(None), Task.due_at).limit(30)).all()  # type: ignore[attr-defined]
    tz = ZoneInfo(settings.tz)

    def due(t: Task) -> str:
        if not t.due_at:
            return "no date"
        d = t.due_at if t.due_at.tzinfo else t.due_at.replace(tzinfo=timezone.utc)
        return d.astimezone(tz).strftime("%a %d %b %H:%M")

    return "\n".join(f"#{t.id} {t.title} (due {due(t)}, {'unconfirmed' if t.status == TaskStatus.pending_confirmation else 'open'}"
                     f"{', ' + t.priority if t.priority != 'normal' else ''})" for t in rows) or "No open tasks."


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
                "reason": args.get("reason")}
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
    if name == "build":
        body = {k: v for k, v in {
            "name": str(args.get("name") or "").strip(), "spec": str(args.get("spec") or "").strip(),
            "repo": args.get("repo") or None, "node": args.get("device") or None,
            "agent": args.get("agent") or None, "continue": bool(args.get("continue")),
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
        return (f"STARTED build {b.get('id')} of {b.get('name')} in {b.get('repo')} ({b.get('agent')} codes, "
                f"{b.get('reviewer')} reviews). It is NOT finished: he gets a notice to check the preview and Merge.")
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
    model = llm or LLM(planner_profile(), timeout=120)
    with Session(engine) as session:
        owner = graph.owner(session)
        profile = (owner.summary if owner and owner.summary else f"{settings.owner_name}.")
        # a follow-up ("and the other one?") recalls with the previous question too
        prev = " ".join(h.get("content", "") for h in (history or [])[-2:] if h.get("role") == "user")
        recall = memory.as_prompt(memory.search(session, f"{prev} {text}".strip(), limit=6), 2500) or "(nothing yet)"
    tz = ZoneInfo(settings.tz)
    system = SYSTEM.format(owner=settings.owner_name, now=datetime.now(tz).strftime("%a %d %b %Y %H:%M"), tz=settings.tz,
                           profile=profile, recall=recall, devices=await _devices(hub), actions=await _actions(hub),
                           steps=settings.agent_max_steps,
                           device_notes=("What the devices are: " + settings.agent_device_notes + "\n") if settings.agent_device_notes else "")
    if source == "voice":
        system += VOICE_NOTE
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
    return {"answer": answer, "steps": steps, "pending": pending}


def _remember(text: str, answer: str, steps: list[dict], source: str) -> None:
    """What was asked and done goes to shared memory, so any device can see it later."""
    try:
        did = "\n".join(f"- {s['tool']} {json.dumps(s['args'], ensure_ascii=False)[:200]}" for s in steps)
        memory.put_committed(kind="work", title=f"Asked: {' '.join(text.split())[:150]}",
                             body=f"Answer: {answer[:3000]}" + (f"\n\nSteps:\n{did}" if did else ""),
                             source="agent", tags=f"agent,{source}")
    except Exception:  # noqa: BLE001
        log.exception("agent: could not save the work record")
