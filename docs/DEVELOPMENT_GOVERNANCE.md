# Development Governance & Git Workflow Specification

## 1. Branching Strategy — GitHub Flow
All development repositories follow GitHub Flow:
- **Protected Branch:** `main` (production-stable; direct pushes prohibited).
- **Task Branches:**
  - `feature/<issue-id>-<description>`
  - `fix/<issue-id>-<description>`
  - `docs/<issue-id>-<description>`
  - `test/<issue-id>-<description>`

Every pull request into `main` requires:
1. Reference to an Issue or approved task.
2. Machine & Agent attribution in description.
3. Successful CI validation runs.
4. Clean secret scan verification.
5. Explicit human review and approval.

---

## 2. Commit Standards & Git Trailers
Commit message subject line:
`[AGENT-ID][MACHINE-ID][PROJECT] TYPE: Description`

Types: `feat`, `fix`, `docs`, `style`, `refactor`, `test`, `chore`.

Required Git trailers:
```text
Agent-ID: AGENT-002
Machine-ID: MACHINE-02
Agent-Tool: Antigravity
Task-ID: <TASK-OR-ISSUE-ID>
```

---

## 3. Secret Protection Standards
1. Never commit credentials, personal access tokens, private keys, or `.env` files.
2. Ensure `.gitignore` explicitly excludes `.env*`, build outputs, and local credential caches.
3. GitHub push protection must be active on all remotes.
