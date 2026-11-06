# AI Agent & Machine Identity Registry

## 1. Machine Identifiers
Permanent physical and host development machine identifiers:

| Machine ID | Machine Description | Operating System | Host / Device Model | Status |
|---|---|---|---|---|
| `MACHINE-01` | Primary Linux development workstation | Linux | Designated Linux Workstation | Active |
| `MACHINE-02` | HP Omen development laptop | Windows 11 Home (Build 10.0.26200) | HP OMEN Gaming Laptop 16-ap0xxx | Active (Current) |

> [!NOTE]
> Identifiers are permanently bound to specific machines. If a new machine joins the development cluster, allocate `MACHINE-03`.

---

## 2. AI Development Agent Registry

| Agent ID | Agent Attribution Name | Tool | Model | Registered Machine | Status |
|---|---|---|---|---|---|
| `AGENT-001` | `AI-AGENT-001-GEMINI` | Gemini CLI / IDE | Inherited / Gemini CLI | `MACHINE-01` | Active |
| `AGENT-002` | `AI-AGENT-002-GEMINI` | Antigravity AI Coding Assistant | Gemini 3.8 Flash (High) | `MACHINE-02` | Active (Current) |
| `AGENT-003` | `AI-AGENT-003-CLAUDE` | Claude Code | Claude 3.5 Sonnet / 3.7 Sonnet | `MACHINE-02` | Registered |

---

## 3. Git Author Attribution Configuration
Git author attribution is configured locally in each repository without changing global human credentials:
- **Git Author Name:** `AI-AGENT-002-GEMINI`
- **Git Author Email:** `166780669+Roger-Chris@users.noreply.github.com`
