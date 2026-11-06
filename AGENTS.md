# AGENTS.md — AI Development Governance & Code Collaboration Rules

## 1. Overview & Authority
This document defines the common development agreement for all AI coding agents working on this repository across distributed development environments (e.g., `MACHINE-01` Linux and `MACHINE-02` Windows).

All AI agents must strictly comply with the following mandatory operational rules.

---

## 2. Mandatory Rules for AI Agents

### Rule 1: Understand before changing
Before modifying code:
- Read this `AGENTS.md`.
- Read `README.md` and repository architectural documentation.
- Inspect relevant modules and directory structures.
- Review recent Git history and active Git status.
- Confirm assigned issue or task scope.

### Rule 2: Scope ownership
- Work strictly within assigned task scope.
- Never modify unrelated files or libraries.
- If another agent or user has active changes on shared files, coordinate before making edits.

### Rule 3: Preserve existing functionality
- Never remove, rename, or alter existing features without explicit authorization.
- Never perform unrelated refactoring.
- Never silently modify API contracts, payload schemas, or function signatures.

### Rule 4: Shared interfaces
Before modifying:
- API contracts and schemas
- Database structures or migrations
- Shared configurations or environment variable structures
- Common data models and authentication mechanisms
- Global build scripts and dependencies
Document proposed changes and request explicit user/owner approval before proceeding.

### Rule 5: Testing
- Run all existing tests relevant to changed modules before submitting work.
- Never modify tests solely to make a broken implementation pass.
- If pre-existing failures are discovered, document them transparently; never suppress test outputs.

### Rule 6: Git isolation
- Never commit or develop directly on `main`.
- Create a dedicated task branch following GitHub Flow: `feature/<task-id>-<description>`, `fix/<task-id>-<description>`, `docs/<task-id>-<description>`.
- Never force push (`--force`) to shared branches.
- Never overwrite or discard another agent's work.

### Rule 7: Documentation
- Update relevant documentation when implementation changes require it.
- Never delete or truncate existing documentation without prior review.

### Rule 8: Security & Secret Protection
Never commit:
- API keys or tokens (e.g., `ghp_`, `gho_`, `AIza`, AWS keys, OpenAI keys)
- Passwords, database connection credentials, or private certificates
- Local environment files (`.env`, `.env.local`)
- Production secrets
Never expose credentials in logs, commit messages, or PR descriptions.

### Rule 9: Standardized Final Report
After every completed task, report:
1. Agent ID & Machine ID
2. Git branch & Task/Issue ID
3. Files added, modified, or deleted
4. Implementation summary
5. Tests executed and validation results
6. Commit hash and pull request URL

---

## 3. Mandatory Commit Format
Every commit created by an AI agent must use:
```text
[AGENT-ID][MACHINE-ID][PROJECT] TYPE: Description

Body explaining changes and motivation.

Agent-ID: AGENT-002
Machine-ID: MACHINE-02
Agent-Tool: Antigravity
Task-ID: <TASK-OR-ISSUE-ID>
```
