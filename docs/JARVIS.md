# Jarvis: one brain, shared memory, many hands

Decided 2026-10-29. This is the plan the code follows; `CLAUDE.md` describes what is built.

## What the owner asked for

- An assistant that **thinks** and **uses his devices** to get things done.
- The most important part: **one memory pool shared by every device**.
- Memory holds: people and relationships; tasks, promises and events; past work and decisions;
  files and notes on every device; details of his old projects.
- Actions: run commands and scripts; move files between devices; control desktop apps; phone
  actions; hand work to his AI tools (Claude Code, ChatGPT/Codex, Gemini, Antigravity).
- Approval: a fixed **safe list runs on its own**; everything else asks on the phone first.
- Master copy of memory on the always-on Linux machine; laptop and Mac keep an offline copy.

## Split of responsibilities

```
                    ┌──────────────── Linux machine (always on) ────────────────┐
 WhatsApp, Gmail ──▶│ BRAIN (Python, AI_Assistant/brain)                         │
 Telegram, phone    │  • memory master  (memory_record + change log, FTS search) │
                    │  • thinking loop  (recall → plan → act → remember)          │
                    │  • perception, tasks, alerts, watcher (as today)           │
                    │            ▲ 127.0.0.1:8000                                │
                    │ HUB (Node, Orchestrator) ── the way in from other devices  │
                    └────────────┬───────────────────────────────────────────────┘
                                 │ Tailscale
          ┌──────────────────────┼──────────────────────────┐
   HP Omen node            Mac mini node              Redmi (phone app)
   • memory replica        • memory replica           • chat, approvals, alarms
   • actions: shell,       • actions                  • phone actions
     files, apps, GPU      • AI CLIs
   • AI CLIs: claude, codex, gemini/agy (Antigravity)
   • MCP server → every AI tool on the device reads/writes the shared memory
```

- **Brain = memory and judgement.** It already holds the people graph, tasks and messages and has
  the Gemini keys. The memory master and the thinking loop live here.
- **Every device = hands.** Every device already runs the same Orchestrator server (`orc serve`)
  and they find each other over Tailscale (`src/cluster`). Device actions, the offline memory copy
  and the MCP server are added to that server, so nothing new has to be installed per device.
- The brain stays on 127.0.0.1. Devices reach memory through the Linux hub (`/node/memory/*`,
  proxied to the brain with `BRAIN_TOKEN`), the same way the phone already reaches the brain.

## 1. Shared memory

### One record table, one change log

`memory_record` (brain SQLite):

| column | meaning |
|---|---|
| `uid` | globally unique id (`<kind>:<slug>` for named things, uuid for the rest) — created on any device |
| `kind` | `person` `org` `project` `place` `device` `relation` `fact` `note` `decision` `work` `task` `event` `file` |
| `title`, `body` | what a person would read; searched |
| `data` | JSON for kind-specific fields (relation: `src`,`rel`,`dst`; file: `device`,`path`,`size`,`mtime`,`hash`; task: `status`,`due_at`…) |
| `tags`, `aliases` | comma-separated; searched |
| `subject` | uid this record is about (a fact about a person, a decision in a project) |
| `source`, `device` | where it came from (`whatsapp`, `gmail`, `cv`, `run`, `manual`, `import`, `files`…) and which device wrote it |
| `updated_at`, `deleted` | last-writer-wins; deletes are tombstones so they sync |
| `seq` | position in the brain's change log, bumped on every accepted write |

Plus an FTS5 index over title/body/tags/aliases.

### Sync (offline-first)

- `GET /memory/changes?since=<seq>` → everything written after `seq` (paged). A replica stores
  the last `seq` it saw and pulls every minute and on reconnect.
- `POST /memory/batch` → a device pushes the records it wrote. Same `uid`: newer `updated_at` wins
  (ties: the brain's copy). The reply says which were accepted and their new `seq`.
- A replica writes locally first and queues the push, so the laptop remembers while offline and
  catches up when the hub is back. Nothing is ever lost by a failed push; it retries.

### Where memory comes from

- **Mirrors of what the brain already knows:** graph entities and edges → `person/org/project…` and
  `relation` records; tasks → `task` records (refreshed when they change). Existing tables stay as
  they are; the mirror keeps the classifier and watcher untouched.
- **The hub's run memory:** every finished Orchestrator run → a `work` record (task, result,
  your corrections).
- **WhatsApp people graph** (hub) → people, terms of address, groups.
- **Files:** each device indexes the folders you choose (path, size, time, hash, and the first part
  of text documents) → `file` records. "Where is my eVTOL thrust test sheet?" works from any device.
- **Old projects:** an importer for markdown notes (one file per project) plus his GitHub repos'
  READMEs → `project` records with facts and decisions.
- **Things said to the assistant:** "remember that…" → `fact`/`note`; the thinking loop writes
  what it did and what it decided as `work`/`decision`.

### Reading memory

- `GET /memory/search?q=&kind=&limit=` (FTS, ranked; semantic search can be added later with
  Gemini embeddings without changing the API).
- `GET /memory/about/<name or uid>` → the record, its relations, facts about it, recent work.
- **MCP server on every device** (`orc mcp`): tools `memory_search`, `memory_about`,
  `memory_remember` backed by the local replica. Register it once in Claude Code, Codex, Gemini CLI
  and Antigravity, and every AI tool on every device shares the same memory.

## 2. Hands: device actions

`POST /node/act` on every device, `{action, args}` → result. Actions are plain functions with a
**risk level**:

- **safe** (runs on its own): memory search/read; list/find/stat/read files inside allowed roots;
  system info (disk, battery, GPU, running apps); a fixed list of read-only commands (`git status`,
  `git log`, `nvidia-smi`, `tailscale status`, `df`…); asking an AI tool a read-only question.
- **ask** (needs the phone): any other shell command; writing, copying, moving files; opening or
  closing apps; desktop control; AI tool runs that change files; phone actions that ring or open
  things; anything sent to other people.
- **never**: deleting outside the assistant's own folders, sending WhatsApp messages (the bridge
  stays read-only), entering passwords.

The safe list lives in config (`actions.safe`), so it can be widened later without code changes.
An `ask` action creates an approval; the phone shows Approve / Deny; it runs only after Approve
and expires after 30 minutes.

AI tools are actions too: `ai.run {tool: claude|codex|gemini|antigravity, task, workspace,
write?}`. They use the subscriptions' CLIs that the Orchestrator already drives, on whichever
device has that tool logged in.

Desktop control starts with: open/close/focus an app, open a file or URL, screenshot. Anything
fiddlier is handed to an AI tool with computer control on that device.

Phone actions arrive as push data the app executes: set an alarm or timer, schedule a reminder,
open a URL or app, show a note. These need an app update.

## 3. Thinking loop (brain)

`POST /agent/ask {text, from}` — used by Telegram, the phone app's chat and the call screen.

1. **Recall:** search memory for the request (people, projects, recent work, open tasks).
2. **Plan and act:** a tool-calling loop on the planner model (Gemini) with tools: `memory_search`,
   `memory_about`, `remember`, `tasks`, `devices`, `act(device, action, args)`, `ai_run(...)`,
   `ask_owner`. At most N steps; every `ask` action waits for approval.
3. **Remember:** what was asked, what was done, the outcome → `work` record; decisions →
   `decision` records.
4. **Answer** on the channel it came from.

## Build order

| Phase | What | Where | State |
|---|---|---|---|
| M1 | `memory_record` + change log + FTS + `/memory/*` API; mirrors from graph and tasks; project importer | brain | built 29 Oct |
| M2 | memory replica + sync + `/node/memory/*` proxy + MCP server; run memory → brain | hub | built 29 Oct |
| A1 | `/node/act` with risk levels, safe list, approvals on the phone; shell, files, system, ai.run | hub + app | built 29 Oct |
| T1 | thinking loop `/agent/ask`; Telegram free text; hub `/api/assistant/ask` | brain + hub | built 29 Oct (app chat screen still to wire) |
| A2 | desktop app control, file index, file transfer between devices | hub | next |
| P1 | phone actions (alarm, reminder, open) | mobile app | next |

Verified end to end on 29 Oct with the real brain and hub (stand-in model): a safe search ran on
its own, a file write waited for Approve and ran once, a replayed approval was refused, and the
work record synced into the brain's memory.
