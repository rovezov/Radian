# Radian Expansion Plan
> Version 1.0 — Built on top of Radian's existing tech stack (FastAPI, SQLite, ChromaDB, Docker Compose)
> Adapted from Life-OS architecture vision to fit Radian's foundations.

---

## What This Document Covers

Five new capability areas, phased for safe incremental delivery:

1. **Orchestrator Mode** — chat-driven task planning and background execution
2. **Personal Context Store** — structured domain data accessible to agents
3. **Code Sandbox** — isolated code execution for the LLM agent
4. **Unified Tool Registry** — one place for all tools (built-ins, skills, MCP, custom code)
5. **Project Spaces** — persistent git-backed workspaces for software the LLM builds

---

## Current State: What Already Exists (Do Not Rebuild)

| Feature Area | What Radian Already Has |
|---|---|
| Task queue | `ScheduledTask` + `TaskRun` models in DB, `task_scheduler.py` runs them |
| Session modes | `Session.mode` field already accepts `"agent"` \| `"chat"` \| `"research"` |
| Password vault | `vault_routes.py` — Bitwarden/Vaultwarden CLI bridge (do not repurpose) |
| Personal files | `personal_routes.py` + `data/personal_docs/` with RAG indexing |
| Tool indexing | `src/tool_index.py` already embeds built-in tools into ChromaDB |
| MCP servers | `mcp_routes.py` + `McpServer` model with stdio/SSE transport |
| Skills (procedures) | `data/skills/<category>/<name>/SKILL.md` — disk-based, no DB registry |
| Shell access | `shell_routes.py` — admin-only, direct host execution, no isolation |
| AI personas | `CrewMember` model — named LLM personas with custom tool sets |

---

## Architecture Decisions (Radian-Compatible)

These are the key design choices that adapt Life-OS ideas to Radian's stack:

| Life-OS Used | Radian Equivalent | Rationale |
|---|---|---|
| LangGraph | Extend `agent_loop.py` with a planner wrapper | LangGraph is a heavy dependency. A lightweight task-planner built on the existing loop is sufficient and easier to reason about for a personal system. |
| PostgreSQL + pgvector | SQLite + ChromaDB (already in place) | SQLite handles hundreds of thousands of rows fine for a personal system. pgvector becomes relevant if multiple users or very large skill/context datasets are needed — revisit in v2. |
| Redis pub/sub | In-memory event bus + SSE (existing WebSocket path) | Radian already streams over SSE/WebSocket. Redis adds operational complexity for a solo deployment. |
| MinIO | Local filesystem (existing `data/` layout) | MinIO is overkill for a personal data store. Filesystem with metadata in SQLite is sufficient. Add MinIO only if files grow past local disk limits. |
| Docker-in-Docker | Docker SDK (`docker` Python library) via mounted socket | The Docker socket is already accessible in the container (required for GPU). DinD adds unnecessary overhead. |

---

## Engineering Flags ⚠️

These are issues to design around before building each phase:

**Flag 1: SQLite write contention**
Background task execution + orchestrator planning + user chat all write to the same SQLite DB simultaneously. SQLite in WAL mode handles concurrent reads fine but serializes writes. Under load this causes `database is locked` errors.
→ **Mitigation:** Verify `PRAGMA journal_mode=WAL` and `PRAGMA busy_timeout=5000` are set on every connection. Already partially handled in Radian — confirm and document it. If contention becomes real, the task runner can have its own SQLite connection with longer timeouts.

**Flag 2: Docker socket security**
Mounting `/var/run/docker.sock` into the sandbox (Feature 3) means a compromised sandbox container can control the Docker daemon. This is acceptable for a personal system where only the admin user can trigger sandbox execution.
→ **Mitigation:** Sandbox creation is gated to admin + agent loopback calls only (same restriction as `shell_routes.py`). Future option: use `gVisor` or rootless Docker for stronger isolation if this runs on an internet-exposed host.

**Flag 3: Context window limits for orchestrator tasks**
"Pass full context to agent" (Phase 1 design) will eventually hit LLM context limits once the personal context store (Feature 2) and project spaces (Feature 5) grow large. A 7B model at 32k context runs out fast.
→ **Mitigation:** Phase 1 intentionally keeps this simple (full context dump). Phase 2+ adds relevance-filtered context injection via ChromaDB semantic search. The architecture should use a `context_builder` abstraction from the start so Phase 1's "dump everything" can be replaced without touching the orchestrator logic.

**Flag 4: Existing vault naming collision**
`vault_routes.py` and the word "vault" in Radian refer to Bitwarden password management. The personal context store (Feature 2) must use a different name.
→ **Decision:** Call it **Context Store** everywhere (routes: `/api/context`, DB table: `context_entries`). Do not use "vault", "data vault", or "personal vault" to avoid confusion.

**Flag 5: Skills vs. Tools naming**
Radian currently has two separate concepts that Life-OS calls "tools":
- **Skills** = SKILL.md instruction documents (how to do something; not executable code)
- **Tools** = callable functions (built-ins, MCP tools, code that runs)

The new Tool Registry (Feature 4) unifies *executable* tools. Skills stay as instruction documents but gain DB-backed indexing and can optionally link to a Tool that implements them.
→ **Decision:** Do not rename existing skills. Add a new `Tool` concept for executable code. A Skill can reference a Tool ID as its implementation.

**Flag 6: Sandbox vs. shell — hybrid execution model**
Two execution paths, each with a clear trigger:
- **Shell** (`shell_routes.py`) — used when the user is present and explicitly approves the command. Fast, full access, reviewed by a human before it runs.
- **Sandbox** (`SandboxExecutor`) — used for all unsupervised execution: orchestrator background tasks, tool test runs, and any agent code that runs without the user in the loop.

→ **Decision:** The orchestrator task runner always uses the sandbox. Shell is only invoked when a user manually triggers a command and confirms it. Never route unsupervised agent output to the host shell.

---

## Phase 1 — Orchestrator Mode

**Goal:** Add an orchestrator mode to the chat tab where users describe problems, the system proposes a structured task, and approved tasks run in the background.

### New DB Model: `AgentTask`

```
id               (int, PK)
owner            (str, user who created the task)
title            (str, short task name)
description      (text, what it will do and what it won't)
context          (text, assembled context at proposal time)
steps            (JSON, ordered list of plain-English steps)
status           (str: "draft" | "pending_approval" | "queued" | "running" | "done" | "failed")
approval_status  (str: "pending" | "approved" | "rejected")
result           (text, final output)
error            (text, error message if failed)
model            (str)
endpoint_url     (str)
session_id       (int, FK → Session — the chat that created this task)
created_at       (datetime)
started_at       (datetime)
finished_at      (datetime)
run_count        (int)
```

### New Session Mode: `"orchestrator"`

Extend `Session.mode` to accept `"orchestrator"`. The chat tab renders differently in this mode — proposals appear as structured cards above the chat input, not as plain prose.

### Orchestrator Flow

```
User describes problem in chat
        │
        ▼
OrchestratorPlanner generates structured proposal:
  - title (short task name)
  - description (what it will do, what it won't do)
  - steps (ordered list, plain English)
  - context_needed (list of domains to pull from Context Store)
  - estimated_complexity ("simple" | "medium" | "complex")
        │
        ▼
Proposal rendered as card in UI
[Approve]  [Refine]  [Reject]
        │
        ▼ (approved)
AgentTask written to DB with status="queued"
        │
        ▼
Background task runner picks it up (uses existing task_scheduler.py infrastructure)
Executes via existing agent_loop with enriched context:
  - Task description + steps
  - Relevant context entries (from Context Store once Phase 2 is built)
  - Relevant skills (semantic search on tool_index)
  All code execution within the task runs through SandboxExecutor — no host shell access
        │
        ▼
Result written to AgentTask.result
Session receives completion message with summary + link to full result
```

### Phase 1 Execution Model (Intentionally Simple)

The task runner invokes the existing `agent_loop` with:
- The full task description + steps as the initial user message
- All relevant memories fetched via existing memory system
- All relevant skills from `tool_index.py`
- (If Phase 2 context store exists) relevant context entries

This means Phase 1 orchestration is just a structured wrapper around the existing agent — no new LLM routing, no multi-agent delegation. The `task_runner.py` is designed as a pipeline so multi-stage execution can be added later without touching routes or DB models.

### What Phase 1 Does NOT Include (Intentionally Deferred)

- Multi-stage execution (read docs → plan → execute → quality check)
- Agent role delegation
- Approval gates for individual steps within a task

### Task Queue Visibility UI

The existing **Tasks page** (sidebar rail button) already shows scheduled tasks and their run history. Extend it with a second tab to surface `AgentTask` queue status alongside scheduled tasks — one place for all task visibility.

**"Scheduled" tab (existing):** Cron/event-triggered tasks and their `TaskRun` history. Unchanged.

**"Queue" tab (new):** All `AgentTask` records for the current user, sorted by `created_at` descending.

Each row shows:
- Task title + truncated description
- Status badge: `queued` / `running` / `done` / `failed`
- Originating session link (click to jump back to the chat that created it)
- Started / finished timestamps
- Expand row → full result text or error message

**Live updates:** The tab polls `GET /api/orchestrator/tasks` every 5 seconds while open so status badges update without a page refresh. Running tasks show a spinner.

### Files

**New:**
- `src/orchestrator.py` — `OrchestratorPlanner` class; builds proposal from conversation, formats structured output
- `routes/orchestrator_routes.py` — REST routes for task CRUD, approve/reject, result retrieval
- `src/task_runner.py` — Background executor that picks up `AgentTask` records with `status="queued"` and runs them via agent loop

**Modified:**
- `core/database.py` — Add `AgentTask` model
- `src/task_scheduler.py` — Hook `task_runner.py` into the existing scheduler poll loop
- `app.py` — Register orchestrator routes
- `static/js/tasks.js` — Add "Queue" tab rendering `AgentTask` rows with status badges and polling

---

## Phase 2 — Personal Context Store

**Goal:** A structured, domain-tagged data store that agents can query for relevant background context (goals, finances, preferences, standing rules, etc.).

### New DB Model: `ContextEntry`

```
id            (int, PK)
owner         (str)
domain        (str, free-form: "finance", "health", "goals", etc.)
title         (str)
content       (text)
tags          (JSON array)
is_pinned     (bool — always inject into agent context regardless of relevance)
embedding_id  (str, ChromaDB document ID)
created_at    (datetime)
updated_at    (datetime)
```

**ChromaDB collection:** `context_entries`

### Key Behaviors

**Pinned entries** always inject into agent context regardless of semantic relevance. Use for standing rules: "always prefer libraries over custom code," "my monthly cloud budget is $50."

**Domain tagging** is free-form text — no predefined categories. User types any domain name. The Context Store creates it on demand.

**Agent access** via two paths:
1. Orchestrator task runner pulls pinned + semantically relevant entries before running a task
2. Agent tool `context_search(query, domain?)` for inline retrieval during a chat session

### Disambiguation from Existing Features

| Route | Purpose | Status |
|---|---|---|
| `vault_routes.py` | Bitwarden password manager | Unchanged |
| `personal_routes.py` | Uploaded files with RAG indexing | Unchanged |
| `context_routes.py` | Structured facts, goals, preferences | **New** |

### Files

**New:**
- `routes/context_routes.py` — CRUD for context entries + semantic search endpoint
- `src/context_manager.py` — Interface between routes and ChromaDB; handles embedding on create/update

**Modified:**
- `core/database.py` — Add `ContextEntry` model
- `src/orchestrator.py` — Pull relevant context entries before task execution
- `src/agent_tools.py` — Add `context_search` tool
- `app.py` — Register context routes

---

## Phase 3 — Code Sandbox

**Goal:** Isolated, safe code execution for the LLM agent. Replaces using the host shell for agent-generated code.

### New Python Dependency

`docker` library (Docker SDK for Python) — added to `requirements.txt`

### `SandboxExecutor` Design

Located at `services/sandbox/executor.py`:

```python
class SandboxExecutor:
    def run(
        self,
        code: str,
        language: str,           # "python" | "bash" | "node"
        timeout: int = 30,
        network: bool = False,   # True only for researcher-type tasks
        mount_project: str | None = None,  # project workspace path (Phase 5)
    ) -> SandboxResult
```

### Execution Model

- Spawns an ephemeral Docker container via Docker SDK using the host's Docker daemon
- Container images: `python:3.12-slim`, `alpine:latest`, `node:20-slim`
- Resource limits: 512 MB RAM, 1 CPU, no network by default
- Timeout enforced via `container.wait(timeout=N)` + `container.stop()`
- Container removed after execution (`remove=True`)
- Stdout + stderr captured and returned as `SandboxResult`

### Integration with Agent

Two execution paths based on whether a human is approving in real time:

| Trigger | Path | Rationale |
|---|---|---|
| User manually runs a command and confirms it | `shell_routes.py` (host shell) | Human in the loop; fast; full access |
| Orchestrator background task (unsupervised) | `SandboxExecutor` (isolated container) | No human review; must be contained |
| Agent `code_execution` tool during chat | `SandboxExecutor` by default | LLM-generated code; treat as untrusted |

- `shell_routes.py` stays unchanged — admin-only, requires explicit user action
- Sandbox is the default for all agent-initiated code execution regardless of whether sandbox is toggled in Settings; the toggle controls whether the agent can run code *at all*, not which path it takes

### Project Task Integration (Phase 5 prep)

When a sandbox call includes `mount_project=<workspace_path>`, the container mounts the project directory at `/workspace` so code can read/write project files without escaping isolation.

### Files

**New:**
- `services/sandbox/executor.py` — `SandboxExecutor` class
- `services/sandbox/__init__.py`

**Modified:**
- `src/agent_tools.py` — Route `python`/`bash` tool blocks through `SandboxExecutor` when enabled
- `docker-compose.yml` — Mount Docker socket: `/var/run/docker.sock:/var/run/docker.sock`
- `requirements.txt` — Add `docker` dependency

---

## Phase 4 — Unified Tool Registry

**Goal:** A single searchable registry that unifies all executable tools available to the system.

### New DB Model: `ToolEntry`

```
id              (int, PK)
owner           (str)
name            (str)
description     (text)
type            (str: "builtin" | "skill_impl" | "mcp" | "custom_code")
category        (str)
code            (text, executable code for custom_code type)
code_language   (str: "python" | "bash" | "node")
config          (JSON)
tags            (JSON)
call_count      (int)
success_count   (int)
fail_count      (int)
is_public       (bool)
is_enabled      (bool)
embedding_id    (str, ChromaDB document ID)
created_at      (datetime)
updated_at      (datetime)
last_used_at    (datetime)
```

**ChromaDB collection:** `tool_registry`

### Migration of Existing Tools (Non-Destructive)

| Tool Type | Migration Approach |
|---|---|
| Built-in tools | Already in ChromaDB via `tool_index.py`. Wrap with unified query interface — no migration needed. |
| SKILL.md files | Stay on disk. Registry adds a DB record per skill with its description embedded. |
| MCP servers | Already in `McpServer` table. Registry indexes their tool descriptions when a server connects. |
| Custom code tools | New — stored as code in `ToolEntry.code`, executed via sandbox (Phase 3). |

### Agent Integration

`src/tool_index.py` (existing) already feeds built-in tools to the agent. Extend it to query the full registry. When the orchestrator assembles task context, it selects the most relevant tools by semantic search and injects their descriptions into the system prompt.

### Tools Page UI

Four sections in a dedicated page (moves MCP servers out of Settings):
1. **Built-in tools** — read-only, from `tool_index.py`
2. **Skills** — browse/edit/test existing SKILL.md files
3. **MCP Servers** — moved from Settings into Tools page
4. **Custom Code Tools** — create/edit/test inline code tools, runs via sandbox

### Success Rate Tracking

Each tool call outcome (success/fail) updates `call_count`, `success_count`, `fail_count`. Tools with success rate below 40% get flagged in the UI. Agent gets a low-confidence warning when selecting unreliable tools.

### Files

**New:**
- `routes/tools_routes.py` — Tool CRUD, test runner, semantic search
- `src/tool_registry.py` — Unified query interface across all tool types

**Modified:**
- `core/database.py` — Add `ToolEntry` model
- `src/tool_index.py` — Extend to query the full registry
- `app.py` — Register tools routes

---

## Phase 5 — Software Project Spaces

**Goal:** Persistent, git-tracked workspaces where the LLM can build software. Projects can later be spun into separate services or integrated into Radian itself.

### New DB Model: `Project`

```
id                   (int, PK)
owner                (str)
name                 (str)
description          (text)
status               (str: "active" | "archived" | "building")
workspace_path       (str, relative path under data/projects/)
default_branch       (str)
last_commit_hash     (str)
last_commit_message  (str)
agent_task_id        (int, FK → AgentTask — null if created manually)
tech_stack           (JSON: ["python", "fastapi", etc.])
readme_summary       (text, auto-generated from README or agent summary)
created_at           (datetime)
updated_at           (datetime)
```

### Workspace Layout on Disk

```
data/
  projects/
    <project_id>/
      .git/
      .agent.context       ← project brief injected into agent context
      src/
      README.md
      ...
```

### `WorkspaceManager` Operations

Located at `services/projects/manager.py`:

```python
create(name, description, tech_stack) → Project    # git init, write .agent.context
get_file_tree(project_id) → list[str]               # git ls-files
get_git_log(project_id, n=10) → str                 # recent commit summaries
get_diff(project_id, n_commits=1) → str             # git diff HEAD~n
read_file(project_id, path) → str                   # safe read (no path traversal)
write_file(project_id, path, content)               # write + git add (commit on demand)
commit(project_id, message)                         # git commit
```

### Integration with Orchestrator (Phase 1) and Sandbox (Phase 3)

When an `AgentTask` is associated with a project:
1. Task runner injects project context (`.agent.context` + recent commits + file tree) into the task prompt
2. Sandbox mounts the project workspace at `/workspace`
3. After execution, file changes in the mounted directory are committed automatically using the task summary as the commit message

### LLM Coding Flow

```
User creates project or task references an existing project
        │
        ▼
Orchestrator proposes task plan including project context
        │ (user approves)
        ▼
Task runner:
  1. Build context: project brief + recent commits + relevant tools + pinned context entries
  2. Run agent loop with assembled context
  3. Agent writes code via code_execution tool (Phase 3) with project mount
  4. Each successful execution round → auto-commit to project workspace
  5. Task completes → Project.last_commit updated, .agent.context refreshed with summary
```

### Projects Page UI

- Card per project: name, status badge, last commit message, tech stack tags
- Expand card: file tree, recent git log, last agent task result
- Actions: **[New Task →]** (creates `AgentTask` scoped to this project), **[Open Workspace]** (read-only file browser), **[View Diff]**
- No in-browser code editor in Phase 1 — file browser is read-only. Editing happens via agent task or direct filesystem access.

### Files

**New:**
- `services/projects/manager.py` — `WorkspaceManager` class with git operations
- `routes/project_routes.py` — Project CRUD, file browser, git diff, git log, status
- `src/project_context.py` — Builds context block from project for injection into agent calls

**Modified:**
- `core/database.py` — Add `Project` model
- `src/task_runner.py` — Inject project context when task has a `project_id`
- `app.py` — Register project routes
- `docker-compose.yml` — Ensure `data/projects/` is on a persistent volume mount

---

## Phase Dependencies

```
Phase 1 (Orchestrator)  ← start immediately, no blockers
        │
        ├─── Phase 2 (Context Store)  ← parallel with Phase 1; feeds into Phase 1's task runner
        │
        ├─── Phase 3 (Sandbox)        ← parallel; needed by Phase 5, not Phase 1 or 2
        │
        ├─── Phase 4 (Tool Registry)  ← after Phase 1 done
        │
        └─── Phase 5 (Projects)       ← after Phases 1 + 3 done
```

Phases 1, 2, and 3 can be built simultaneously.
Phase 4 requires Phase 1 complete (to test tool selection in task context).
Phase 5 requires Phases 1 and 3 complete.

---

## Full File Map

### New Files Per Phase

| Phase | New File | Purpose |
|---|---|---|
| 1 | `src/orchestrator.py` | Task proposal generation, context assembly |
| 1 | `routes/orchestrator_routes.py` | Task CRUD, approve/reject, result retrieval |
| 1 | `src/task_runner.py` | Background AgentTask executor |
| 2 | `routes/context_routes.py` | Context entry CRUD + semantic search |
| 2 | `src/context_manager.py` | ChromaDB interface for context embeddings |
| 3 | `services/sandbox/executor.py` | Docker-based code sandbox |
| 3 | `services/sandbox/__init__.py` | Package init |
| 4 | `routes/tools_routes.py` | Tool CRUD, test runner, semantic search |
| 4 | `src/tool_registry.py` | Unified query interface across all tool types |
| 5 | `services/projects/manager.py` | Git workspace management |
| 5 | `routes/project_routes.py` | Project CRUD, file browser, git operations |
| 5 | `src/project_context.py` | Project context builder for agent injection |

### Modified Files Per Phase

| Phase | Modified File | What Changes |
|---|---|---|
| 1 | `core/database.py` | Add `AgentTask` model |
| 1 | `src/task_scheduler.py` | Hook `task_runner.py` into scheduler poll loop |
| 1 | `app.py` | Register orchestrator routes |
| 1 | `static/js/tasks.js` | Add "Queue" tab for `AgentTask` visibility |
| 2 | `core/database.py` | Add `ContextEntry` model |
| 2 | `src/orchestrator.py` | Pull relevant context before task execution |
| 2 | `src/agent_tools.py` | Add `context_search` tool |
| 2 | `app.py` | Register context routes |
| 3 | `src/agent_tools.py` | Route code blocks through sandbox when enabled |
| 3 | `docker-compose.yml` | Mount Docker socket into radian container |
| 3 | `requirements.txt` | Add `docker` SDK dependency |
| 4 | `core/database.py` | Add `ToolEntry` model |
| 4 | `src/tool_index.py` | Extend to query full registry |
| 4 | `app.py` | Register tools routes |
| 5 | `core/database.py` | Add `Project` model |
| 5 | `src/task_runner.py` | Inject project context when task has project_id |
| 5 | `app.py` | Register project routes |
| 5 | `docker-compose.yml` | Add `data/projects/` persistent volume mount |

---

## Future Expansion (Architecture Supports, Not In Scope Now)

**Multi-stage task execution (expand Phase 1)**
Replace Phase 1's single agent loop in `task_runner.py` with a pipeline:
```
Stage 1: Read relevant context entries + project state
Stage 2: Discover available tools (Phase 4 registry)
Stage 3: Devise plan (sub-proposals, verifiable steps)
Stage 4: Execute (Phase 3 sandbox, Phase 5 workspace)
Stage 5: Quality check (run tests, verify outputs)
Stage 6: Commit + summarize
```
All routes, DB models, and UI built in Phase 1 remain unchanged — this is a purely internal change to `task_runner.py`.

**Agent role delegation**
Once multi-stage execution is stable, each stage can be delegated to a role-scoped agent using the existing `CrewMember` model.

**Tool success-rate auto-deprecation (extend Phase 4)**
Tools with success rate below a threshold for 30+ days get automatically disabled and flagged for review.

**Project deployment (extend Phase 5)**
`POST /api/projects/{id}/deploy` — runs tests, builds Docker image, `docker-compose up` inside the project workspace. The sandbox executor already has the Docker SDK available.

**PostgreSQL migration (if needed)**
If SQLite write contention (Flag 1) becomes a real problem or the system grows to multiple users, migrate to PostgreSQL. All SQLAlchemy models use standard types — migration is a connection string change + schema export. ChromaDB stays unchanged.

---

## Verification Checklist

### Phase 1
- [ ] New chat session can be created with `mode = "orchestrator"`
- [ ] User message in orchestrator mode returns a structured proposal card, not plain prose
- [ ] Proposal includes: title, description, ordered steps, complexity estimate
- [ ] **Approve** button creates an `AgentTask` record with `status = "queued"`
- [ ] Background runner picks up queued tasks and executes via agent loop
- [ ] Task result written to `AgentTask.result`, session notified on completion
- [ ] **Reject** sets `approval_status = "rejected"` and returns to refinement conversation
- [ ] Tasks page "Queue" tab shows all `AgentTask` records with correct status badges
- [ ] Clicking the session link on a queued task navigates to the originating chat
- [ ] Running tasks show a spinner; status updates without a full page refresh (5s poll)

### Phase 2
- [ ] `POST /api/context` creates entry with embedding stored in ChromaDB
- [ ] `GET /api/context?domain=finance` returns only entries in that domain
- [ ] `POST /api/context/search` with a query returns semantically ranked results
- [ ] Pinned entries appear in agent context without needing semantic match
- [ ] Orchestrator task execution includes relevant context entries in prompt

### Phase 3
- [ ] `docker` library available in container (`pip show docker`)
- [ ] Sandbox spawns container, runs Python code, returns stdout/stderr
- [ ] Container is destroyed after execution (`docker ps` shows no leftover containers)
- [ ] Timeout enforced: process killed after 30 seconds
- [ ] Agent `python` tool blocks route through sandbox when sandbox is enabled in Settings
- [ ] Host shell (`shell_routes.py`) is unchanged and still works for admin

### Phase 4
- [ ] `GET /api/tools` returns built-ins, skills, MCP tools, and custom code tools in one response
- [ ] `POST /api/tools/search` with a query returns semantically ranked tools
- [ ] Custom code tool can be created, saved to DB, and executed via sandbox
- [ ] Tool call count and success/fail tallied after each agent use
- [ ] Tools page in UI shows all four sections with working test buttons

### Phase 5
- [ ] `POST /api/projects` creates directory under `data/projects/<id>/` with `.git/` initialized
- [ ] `GET /api/projects/{id}/files` returns git-tracked file tree
- [ ] `GET /api/projects/{id}/log` returns recent commits
- [ ] Agent task scoped to a project receives project context in its system prompt
- [ ] Code written by agent (via sandbox with project mount) appears in git log after task
- [ ] `.agent.context` file updated with task summary after each completed run
