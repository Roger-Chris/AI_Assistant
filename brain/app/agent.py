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
from .llm import LLM, extract_json, planner_profile

log = logging.getLogger("brain.agent")

SYSTEM = """You are {owner}'s personal assistant (Jarvis-style). You think, then act on his devices to get things done.
Now: {now} ({tz}).

WHO HE IS
{profile}

WHAT MEMORY ALREADY HOLDS THAT MAY MATTER (search for more if needed)
{recall}

DEVICES (name: status)
{devices}

ACTIONS YOU CAN RUN ON A DEVICE with the `act` tool (risk: safe runs at once, ask waits for his Approve on the phone)
{actions}

TOOLS — reply with exactly ONE JSON object per turn, nothing else:
{{"tool": "memory_search", "args": {{"query": "...", "kind": "optional: person|project|fact|decision|work|task|file|note"}}}}
{{"tool": "memory_about", "args": {{"name": "a person, project, place or device"}}}}
{{"tool": "remember", "args": {{"text": "...", "kind": "fact|note|decision", "subject": "optional name"}}}}
{{"tool": "tasks", "args": {{}}}}   (his open tasks)
{{"tool": "add_task", "args": {{"title": "...", "due_at": "optional ISO 8601 local time", "details": "optional"}}}}
{{"tool": "act", "args": {{"device": "name", "action": "files.find", "args": {{...}}, "reason": "one line why"}}}}
{{"answer": "your reply to him"}}

RULES
- Use memory before saying you don't know something about his life, work, people or projects.
- Prefer safe actions. Ask for an `ask` action only when he asked for that change; give a clear one-line reason.
- An action that comes back pending is NOT done: tell him it is waiting for his approval on the phone.
- Never invent results. If a tool failed, say so plainly.
- Never send messages to other people, never handle passwords, never delete anything.
- When he tells you something worth keeping (a preference, a decision, a fact), save it with `remember`.
- Answers: short, direct, plain text. At most {steps} tool calls, then answer with what you have."""


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
    return "\n".join(f"#{t.id} {t.title} (due {t.due_at.isoformat() if t.due_at else 'no date'}, {t.status.value})"
                     for t in rows) or "No open tasks."


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
    return f"Unknown tool {name!r}."


# ---------------------------------------------------------------- the loop

async def ask(text: str, *, source: str = "app", llm: Optional[LLM] = None,
              on_step: Optional[Callable[[str], None]] = None) -> dict:
    hub = Hub()
    model = llm or LLM(planner_profile(), timeout=120)
    with Session(engine) as session:
        owner = graph.owner(session)
        profile = (owner.summary if owner and owner.summary else f"{settings.owner_name}.")
        recall = memory.as_prompt(memory.search(session, text, limit=6), 2500) or "(nothing yet)"
    tz = ZoneInfo(settings.tz)
    system = SYSTEM.format(owner=settings.owner_name, now=datetime.now(tz).strftime("%a %d %b %Y %H:%M"), tz=settings.tz,
                           profile=profile, recall=recall, devices=await _devices(hub), actions=await _actions(hub),
                           steps=settings.agent_max_steps)
    messages: list[dict] = [{"role": "system", "content": system}, {"role": "user", "content": text}]
    steps: list[dict] = []
    used: list[dict] = []
    answer: Optional[str] = None
    # Tool calls are capped at agent_max_steps; a few extra turns let the model recover from a
    # malformed reply and still answer after hitting the cap.
    for _ in range(settings.agent_max_steps + 3):
        raw = await model.complete(messages, json_mode=True, temperature=0.2, max_tokens=settings.agent_max_tokens)
        try:
            move = extract_json(raw)
        except Exception:  # noqa: BLE001
            move = None
        if not isinstance(move, dict):
            messages += [{"role": "assistant", "content": str(raw)[:2000]},
                         {"role": "user", "content": "Reply with exactly one JSON object: a tool call or {\"answer\": ...}."}]
            continue
        if move.get("answer"):
            answer = str(move["answer"]).strip()
            break
        tool = str(move.get("tool") or "")
        args = move.get("args") if isinstance(move.get("args"), dict) else {}
        if len(steps) >= settings.agent_max_steps:
            messages += [{"role": "assistant", "content": json.dumps(move)},
                         {"role": "user", "content": "No more tool calls. Answer now with {\"answer\": ...}."}]
            continue
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
