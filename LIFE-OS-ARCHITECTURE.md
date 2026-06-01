# Life OS — System Architecture Document
> Version 1.1 | Infrastructure & Orchestrator Layer Only — updated after simplification pass
> Agents, skills, and domain modules are provisioned via orchestrator tasks, not this document.

---

## Table of Contents
1. [Design Philosophy](#1-design-philosophy)
2. [Architecture Decision Records](#2-architecture-decision-records)
3. [System Overview](#3-system-overview)
4. [Full Software Stack](#4-full-software-stack)
5. [Repository Structure](#5-repository-structure)
6. [Security & Permission Model](#6-security--permission-model)
7. [Orchestrator Design](#7-orchestrator-design)
8. [Skill Registry & Exponential Growth](#8-skill-registry--exponential-growth)
9. [Observability Design](#9-observability-design)
10. [Data Architecture](#10-data-architecture)
11. [Global Access Architecture](#11-global-access-architecture)
12. [Known Failure Modes & Mitigations](#12-known-failure-modes--mitigations)
13. [Windows → Ubuntu Migration Path](#13-windows--ubuntu-migration-path)
14. [Scalability Roadmap](#14-scalability-roadmap)

---

## 1. Design Philosophy

### Core Principles

**1. Everything is a container.** No service runs bare-metal. Every component — backend, frontend, models, databases, monitoring — runs in Docker. This is what makes the Windows-to-Ubuntu migration a single `git clone` + `./setup.sh`.

**2. Open-source first, API fallback.** Local models (via Ollama) handle all routine tasks. Claude/OpenAI APIs are invoked only when a task explicitly exceeds local model capability — routed by the orchestrator's capability-assessment logic, never by default.

**3. The orchestrator is a governor, not a doer.** The orchestrator receives tasks, plans, delegates to specialized agents, reviews outputs, and loops until completion. It never directly executes domain logic itself. This separation is critical for auditability and role-scoped security.

**4. Skills compound over time.** Every tool an agent writes that succeeds more than once becomes a permanent, callable skill available to future agents. The system grows more capable with every task.

**5. You remain in control.** All agent actions are logged. Destructive operations (write to filesystem, call external APIs with auth tokens, spend money) require explicit human approval unless whitelisted. The dashboard is the single pane of glass for this.

### On Multica

Multica (`multica-ai/multica`) was evaluated. It is a well-designed platform for coordinating *coding agents* (Claude Code, Codex) on a kanban board with skill compounding. It is not used as the core of this system for three reasons:
- It is purpose-built for software development tasks; Life OS needs life-domain orchestration (health, finance, research)
- It has no role-based permission model for domain-scoped agents
- It introduces a Go + PostgreSQL + Next.js stack that we are already building natively

However, multica's skill-compounding pattern and board-style activity feed are excellent ideas and are implemented natively in this architecture.

---

## 2. Architecture Decision Records

### ADR-001: LangGraph over CrewAI, AutoGen, or raw LangChain

**Decision:** Use LangGraph as the orchestration framework.

**Rationale:**
- LangGraph uses a **stateful directed graph** model. Each node is a function; edges are conditional transitions. This models the orchestrator's "plan → delegate → review → loop" cycle precisely.
- Built-in **human-in-the-loop** checkpointing. The graph can pause at any node and wait for your approval before continuing — essential for the "review before executing" requirement.
- Built-in **persistence** via checkpointers. If the system restarts mid-task, the graph resumes exactly where it left off.
- LangGraph is the production standard at Anthropic, LangChain, and major enterprise deployments.

CrewAI is simpler but lacks the checkpoint/resumption model and has weak support for complex conditional routing. AutoGen is powerful but has a steep configuration surface and less predictable control flow.

### ADR-002: FastAPI over Django, Flask, or Node

**Decision:** Python + FastAPI for the backend.

**Rationale:**
- All AI/ML libraries (LangGraph, LangChain, Ollama Python client, NumPy) are Python-native. A Python backend means agents and API share a single process space — no serialization overhead.
- FastAPI has native async support, which is critical for concurrent agent execution.
- FastAPI's automatic OpenAPI docs make the API self-documenting from day one.
- WebSocket support is first-class — needed for real-time dashboard streaming.

### ADR-003: Next.js + shadcn/ui for the Dashboard

**Decision:** Next.js 14 (App Router) with shadcn/ui components and Tailwind CSS.

**Rationale:**
- Server Components allow the dashboard to stream data without heavy client-side JS.
- shadcn/ui provides production-quality, fully customizable components with no vendor lock-in.
- Single codebase runs as a Docker container, accessible globally via Cloudflare Tunnel.
- The chat interface and the agent board can coexist in a single Next.js app without architectural gymnastics.

### ADR-004: PostgreSQL + pgvector over MongoDB or ChromaDB

**Decision:** PostgreSQL as the single source of truth, with pgvector extension for vector search.

**Rationale:**
- A single database for relational data (tasks, agents, permissions, skill registry) AND vector embeddings (long-term memory, document search) eliminates the operational overhead of running two separate databases.
- pgvector is production-proven and trivially added to a standard Postgres container.
- Tasks, agent memory, and skill records are all relational by nature — a document store would require denormalizing them.

### ADR-005: Redis for Real-Time Messaging

**Decision:** Redis for pub/sub.

**Rationale:**
- Redis pub/sub powers real-time WebSocket events to the dashboard — every lifecycle state change the orchestrator makes is published to a Redis channel, which the FastAPI WebSocket endpoint forwards to the browser.
- Redis also provides a simple distributed lock mechanism to prevent two agents from executing the same task simultaneously.

Note: Celery was removed. The orchestrator now runs inline within the HTTP request rather than as a background Celery task, eliminating a class of distributed-state bugs.

### ADR-006: Cloudflare Tunnel for Global Access (No open ports)

**Decision:** Cloudflare Tunnel (cloudflared) for remote access.

**Rationale:**
- Zero open inbound ports. All traffic is outbound-initiated from the machine to Cloudflare's edge.
- Free tier supports unlimited bandwidth and custom domains.
- Cloudflare Access adds identity-aware access control (your email as identity provider) with zero configuration.
- Trivially runs as a Docker container alongside the rest of the stack.

### ADR-007: Docker-in-Docker (DinD) for Agent Code Sandboxing

**Decision:** Agents execute code in isolated Docker containers spawned by the host daemon.

**Rationale:**
- Code written by agents runs in an ephemeral container with no access to host filesystem, network, or environment variables unless explicitly granted.
- The container is destroyed after execution. No persistent state escapes.
- This is the standard production approach (GitHub Actions, Replit, etc.) — safe, auditable, portable.

---

## 3. System Overview

```
┌─────────────────────────────────────────────────────────────────┐
│                        YOU (anywhere in world)                   │
│              browser / mobile / SSH via Cloudflare Tunnel        │
└───────────────────────────┬─────────────────────────────────────┘
                            │ HTTPS + WSS
                    ┌───────▼────────┐
                    │  Cloudflare    │
                    │  Tunnel Edge   │
                    └───────┬────────┘
                            │ encrypted tunnel (outbound from machine)
┌───────────────────────────▼─────────────────────────────────────┐
│                      LIFE OS HOST MACHINE                        │
│                                                                  │
│  ┌──────────────┐    ┌──────────────────────────────────────┐   │
│  │  Next.js     │    │           FastAPI Backend             │   │
│  │  Dashboard   │◄──►│                                       │   │
│  │  :3000       │    │  ┌─────────────────────────────────┐ │   │
│  └──────────────┘    │  │        LangGraph Orchestrator    │ │   │
│                      │  │  ┌──────────┐  ┌─────────────┐  │ │   │
│  ┌──────────────┐    │  │  │  Planner │  │   Reviewer  │  │ │   │
│  │   Ollama     │◄───│  │  └────┬─────┘  └──────▲──────┘  │ │   │
│  │  :11434      │    │  │       │  delegate       │ output  │ │   │
│  │  (30B / 70B) │    │  │  ┌────▼─────────────────────┐    │ │   │
│  └──────────────┘    │  │  │    Agent Executor Pool   │    │ │   │
│                      │  │  │  (LangGraph subgraphs)   │    │ │   │
│  ┌──────────────┐    │  │  └────┬─────────────────────┘    │ │   │
│  │  PostgreSQL  │◄───│  │       │                           │ │   │
│  │  + pgvector  │    │  └───────┼───────────────────────────┘ │   │
│  └──────────────┘    │          │ spawns                       │   │
│                      │  ┌───────▼──────────────────────────┐  │   │
│  ┌──────────────┐    │  │    Docker Sandbox (DinD)          │  │   │
│  │    Redis     │◄───│  │    Ephemeral code execution       │  │   │
│  │  (queue/pub) │    │  │    → Skill registry on success    │  │   │
│  └──────────────┘    │  └──────────────────────────────────┘  │   │
│                      └──────────────────────────────────────────┘   │
│                                                                  │
│  ┌────────────────────────────────────────────────────────────┐ │
│  │  Prometheus + Grafana (self-monitoring :9090 / :3001)      │ │
│  └────────────────────────────────────────────────────────────┘ │
└─────────────────────────────────────────────────────────────────┘
```

---

## 4. Full Software Stack

### 4.1 Infrastructure Layer

| Component | Tool | Version | Purpose |
|-----------|------|---------|---------|
| Container Runtime | Docker Desktop (Win) / Docker CE (Ubuntu) | 25+ | All services run here |
| Orchestration | Docker Compose v2 | 2.x | Multi-container lifecycle |
| OS (dev) | Windows 11 + WSL2 | – | Development environment |
| OS (prod) | Ubuntu 22.04 LTS | – | Target hardware environment |
| Reverse Proxy | Traefik | 3.x | Internal routing between containers |
| Global Tunnel | Cloudflare Tunnel (`cloudflared`) | latest | Zero-trust remote access |

### 4.2 Backend

| Component | Tool | Version | Repo/Source |
|-----------|------|---------|-------------|
| API Framework | FastAPI | 0.115+ | `fastapi.tiangolo.com` |
| ASGI Server | Uvicorn | 0.30+ | Runs FastAPI |
| Orchestration | LangGraph | 0.2+ | `github.com/langchain-ai/langgraph` |
| LLM Abstraction | LangChain | 0.3+ | `github.com/langchain-ai/langchain` |
| WebSocket | FastAPI native | – | Real-time dashboard events |
| Auth | python-jose + passlib | – | JWT tokens |
| DB ORM | SQLAlchemy 2.0 (async) | 2.x | Postgres access |
| Config | Pydantic Settings | 2.x | .env → typed config |
| HTTP Client | httpx | 0.27+ | Agent web access |
| Code Sandbox | Docker SDK for Python | 7.x | Spawn/manage DinD containers |

### 4.3 AI / Model Layer

| Component | Tool | Purpose |
|-----------|------|---------|
| Local Model Server | Ollama | Runs quantized models, OpenAI-compatible API |
| Primary Reasoning | `qwen3.5:4b-q4_K_M` | Default model, fits in 8GB VRAM |
| Deep Reasoning | `sam860/deepseek-r1-0528-qwen3:8b` | Extended reasoning tasks |
| Embeddings | `nomic-embed-text` via Ollama | Vector embeddings for skill search |
| API Fallback | Anthropic Claude API | Hard tasks exceeding local model capability |
| API Fallback | OpenAI API | Optional secondary fallback |

### 4.4 Data Layer

| Component | Tool | Purpose |
|-----------|------|---------|
| Primary Database | PostgreSQL 16 + pgvector | Relational data + vector similarity search |
| Cache / Event Bus | Redis 7 | Pub/sub for live dashboard events, distributed locks |
| Secrets | `.env` + Docker secrets | Credential management |

### 4.5 Frontend

| Component | Tool | Purpose |
|-----------|------|---------|
| Framework | Next.js 14 (App Router) | Dashboard + chat UI |
| UI Components | shadcn/ui + Radix UI | Production-quality accessible components |
| Styling | Tailwind CSS | Utility-first, no runtime CSS |
| State Management | Zustand | Lightweight, no boilerplate |
| Real-time | native WebSocket client | Agent event streaming |
| Charts/Metrics | Recharts | Performance dashboards |
| Icons | Lucide React | Consistent iconography |

### 4.6 Observability

| Component | Tool | Purpose |
|-----------|------|---------|
| Metrics Scraping | Prometheus | Collects all service metrics |
| Metrics Visualization | Grafana | Dashboards for system health |
| FastAPI Metrics | `prometheus-fastapi-instrumentator` | Auto-instruments all API endpoints |
| Log Aggregation | Loki (optional, phase 2) | Structured log search |
| Distributed Tracing | LangSmith (optional) | LangGraph trace visualization |

---

## 5. Repository Structure

```
life-os/
├── README.md
├── ARCHITECTURE.md               ← this document
├── docker-compose.yml            ← production services
├── docker-compose.dev.yml        ← dev overrides (hot reload, debug ports)
├── .env.example                  ← all required env vars with descriptions
├── setup.ps1                     ← Windows one-command setup script
├── setup.sh                      ← Ubuntu one-command setup script
│
├── backend/
│   ├── Dockerfile
│   ├── requirements.txt
│   └── app/
│       ├── main.py               ← FastAPI app entry point
│       ├── config.py             ← Pydantic settings
│       │
│       ├── api/                  ← REST + WebSocket routes
│       │   ├── auth.py
│       │   ├── tasks.py
│       │   ├── agents.py
│       │   ├── skills.py
│       │   ├── approvals.py
│       │   └── ws.py             ← WebSocket endpoint
│       │
│       ├── orchestrator/         ← LangGraph graphs
│       │   ├── graph.py          ← Main orchestrator graph definition
│       │   ├── nodes.py          ← Graph node functions (plan, delegate, review)
│       │   ├── state.py          ← Typed state schema
│       │   ├── schemas.py        ← Pydantic output schemas for LLM calls
│       │   └── checkpointer.py   ← Postgres checkpointer config
│       │
│       ├── skills/               ← Skill registry
│       │   ├── registry.py       ← Skill CRUD + call tracking
│       │   ├── sandbox.py        ← Docker code execution
│       │   └── validator.py      ← Test + promote skills to permanent
│       │
│       ├── models/               ← SQLAlchemy DB models
│       │   ├── task.py
│       │   ├── agent.py
│       │   ├── skill.py
│       │   ├── event.py
│       │   ├── approval.py
│       │   └── user.py
│       │
│       └── core/
│           ├── events.py         ← Redis pub/sub publisher
│           ├── llm.py            ← LLM provider abstraction (Ollama / Claude / OAI)
│           ├── database.py       ← Async SQLAlchemy engine + session
│           ├── redis_client.py   ← Redis connection + publish_event helper
│           └── metrics.py        ← Prometheus metrics definitions
│
├── frontend/
│   ├── Dockerfile
│   ├── package.json
│   └── src/
│       ├── app/
│       │   ├── layout.tsx
│       │   ├── page.tsx          ← Dashboard home
│       │   ├── chat/page.tsx     ← Task assignment chat (synchronous HTTP lifecycle)
│       │   ├── agents/page.tsx   ← Active agent monitor
│       │   ├── tasks/page.tsx    ← Kanban task board
│       │   ├── skills/page.tsx   ← Skill registry browser
│       │   ├── approvals/page.tsx ← Pending approval queue
│       │   └── metrics/page.tsx  ← System performance
│       │
│       ├── components/
│       │   ├── AuthGuard.tsx     ← JWT auth wrapper
│       │   ├── Sidebar.tsx       ← Navigation sidebar
│       │   └── ui/               ← shadcn/ui components
│       │
│       └── lib/
│           ├── api.ts            ← Typed API client
│           ├── websocket.ts      ← WebSocket connection manager
│           ├── store.ts          ← Zustand global state
│           └── utils.ts          ← Shared utilities
│
└── services/
    ├── postgres/
    │   └── init.sql              ← pgvector extension + initial schema
    ├── redis/
    │   └── redis.conf
    ├── prometheus/
    │   └── prometheus.yml        ← scrape configs
    ├── grafana/
    │   ├── dashboards/
    │   │   └── life-os.json      ← pre-built dashboard
    │   └── provisioning/
    ├── ollama/
    │   └── pull-models.sh        ← pulls required models on first run (bind-mounted ro)
    └── cloudflared/
        └── config.yml.example
```

---

## 6. Security & Permission Model

> **Implementation status:** The role definitions below are the target model. Runtime enforcement via `PermissionEnforcer` is planned for Phase 4 when domain agents are added. The current codebase enforces auth at the HTTP layer (JWT) only.

### 6.1 Role Definitions

Agents are assigned a role at creation. Roles are immutable at runtime — an agent cannot elevate its own permissions.

```python
# permissions/roles.py

ROLES = {
    "orchestrator": {
        "can_read":  ["*"],           # reads everything
        "can_write": ["tasks", "events", "agent_assignments"],
        "can_spawn": True,            # can create sub-agents
        "can_call_external_api": False,  # still needs human approval
        "can_write_filesystem": False,
        "can_execute_code": False,    # delegates to executor agents
    },
    "researcher": {
        "can_read":  ["tasks", "skills", "memory.research"],
        "can_write": ["memory.research", "skills"],
        "can_spawn": False,
        "can_call_external_api": True,   # read-only web access
        "can_write_filesystem": False,
        "can_execute_code": True,        # sandboxed only
    },
    "executor": {
        "can_read":  ["tasks", "skills"],
        "can_write": ["tasks.output", "skills"],
        "can_spawn": False,
        "can_call_external_api": False,
        "can_write_filesystem": True,    # sandboxed only
        "can_execute_code": True,
    },
    "health_agent": {
        "can_read":  ["memory.health", "skills"],
        "can_write": ["memory.health"],
        "can_spawn": False,
        "can_call_external_api": True,   # health APIs only (allowlist)
        "can_write_filesystem": False,
        "can_execute_code": False,
        # CANNOT read: memory.finance, memory.personal, tasks of other domains
    },
    "finance_agent": {
        "can_read":  ["memory.finance", "skills"],
        "can_write": ["memory.finance"],
        "can_spawn": False,
        "can_call_external_api": True,   # finance APIs only (allowlist)
        "can_write_filesystem": False,
        "can_execute_code": False,
        # CANNOT read: memory.health, memory.personal
    },
}
```

### 6.2 Permission Enforcement

Every agent method call that touches data is wrapped by the permission enforcer:

```python
# permissions/enforcer.py
class PermissionEnforcer:
    def check(self, agent_role: str, action: str, resource: str) -> bool:
        role_config = ROLES.get(agent_role)
        if not role_config:
            return False
        # Check read/write access to resource namespace
        # Log every access check to the audit table
        # Raise PermissionDenied if not authorized
```

### 6.3 Network Isolation

Docker Compose creates separate networks:
- `internal` — backend, postgres, redis, ollama (no external access)
- `external` — only cloudflared, Traefik have access to both networks
- Agent containers spawned for code execution are created with `--network none` unless the task explicitly requires internet access (researcher role only)

### 6.4 Human Approval Gates

Operations in the following categories require your explicit approval via the dashboard before execution:

- External API calls with write permissions (posting to social media, sending emails)
- Any financial transaction
- File writes outside the designated `/data/outputs` directory
- Spawning new permanent agent roles
- Promoting a skill from "candidate" to "permanent"

---

## 7. Orchestrator Design

### 7.1 LangGraph State Schema

```python
# orchestrator/state.py
from typing import TypedDict, Optional

class OrchestratorState(TypedDict):
    # Core task identity
    task_id: str
    user_request: str

    # Planning conversation (plain list of LangChain messages)
    planning_conversation: list
    clarification_round: int
    plan_draft: Optional[list]       # [{step, agent_role, instructions, estimated_complexity}]
    plan_approved: bool
    plan_action: Optional[str]       # pre-classified: "approved" | "changes" | "rejected"

    # Locked execution plan
    plan: Optional[list]
    current_step: int

    # Execution phase
    delegated_to: Optional[str]
    agent_output: Optional[str]

    # Review phase
    review_result: Optional[str]     # "approved" | "needs_revision" | "failed"
    revision_count: int

    # Complexity
    task_complexity: Optional[str]   # "trivial" | "standard" | "complex"
```

### 7.2 Orchestrator Graph

The orchestrator uses a two-phase model inspired by Gemini's Deep Research flow: a **collaborative planning phase** where the orchestrator and user negotiate the plan together before anything executes, followed by the **execution phase** that only begins after explicit user approval.

```
                        ┌──────────┐
                        │  START   │ (user submits task)
                        └────┬─────┘
                             ▼
                     ┌───────────────┐
                     │  CLARIFY      │ orchestrator asks targeted questions:
                     │               │ scope, constraints, preferences,
                     │               │ success criteria, things to avoid
                     └───────┬───────┘
                             ▼
                     ┌───────────────┐
                     │  AWAIT_USER   │ ← graph pauses here (LangGraph interrupt)
                     │  RESPONSE     │   user replies via chat in dashboard
                     └───────┬───────┘
                             ▼
              ┌──────────────────────────────┐
              │  sufficient_context?          │
              │  NO → back to CLARIFY         │ (max 3 rounds, then proceed anyway)
              │  YES → DRAFT_PLAN             │
              └──────────────┬───────────────┘
                             ▼
                     ┌───────────────┐
                     │  DRAFT_PLAN   │ orchestrator produces a structured plan draft:
                     │               │ numbered steps, assigned agent roles,
                     │               │ estimated complexity, what each step will DO
                     │               │ and what it will NOT do — streamed to chat
                     └───────┬───────┘
                             ▼
                     ┌───────────────┐
                     │  AWAIT_PLAN   │ ← graph pauses (LangGraph interrupt)
                     │  APPROVAL     │   user sees plan in chat with:
                     │               │   [Approve] [Request Changes] buttons
                     └───────┬───────┘
                             ▼
              ┌──────────────────────────────┐
              │  user_response?               │
              │  APPROVED → lock plan,        │
              │             set plan_approved │
              │             → DELEGATE        │
              │  CHANGES    → incorporate     │
              │             feedback,         │
              │             → DRAFT_PLAN      │ (re-draft with changes, no limit)
              └──────────────┬───────────────┘
                             │ (approved path)
                             ▼
                        ┌──────────┐
                        │ DELEGATE │ assign current step to agent by role
                        └────┬─────┘
                             ▼
                        ┌──────────┐
                        │ EXECUTE  │ agent subgraph runs (async)
                        └────┬─────┘
                             ▼
                        ┌──────────┐
                        │  REVIEW  │ orchestrator checks output vs plan step
                        └────┬─────┘
                             ▼
              ┌──────────────────────────────┐
              │  review_result?               │
              │  approved → next step or END  │
              │  needs_revision → DELEGATE    │ (max 3 retries)
              │  failed → REPORT_FAILURE      │
              └──────────────────────────────┘
                             ▼
                        ┌──────────┐
                        │   END    │ result streamed to dashboard
                        └──────────┘
```

#### Planning Conversation Design

The `CLARIFY` node uses the orchestrator LLM to ask focused, non-redundant questions. It should ask at most 2–3 questions per round, not a wall of text. The goal is to surface:

- **Scope boundaries** — what is explicitly in and out of scope
- **Constraints** — time, tools, APIs, formats the user cares about
- **Success criteria** — what does "done" look like to the user
- **Preferences** — style, verbosity, structure of outputs

The `DRAFT_PLAN` node produces a plan formatted for human readability in the chat UI — not just internal JSON. Each step is rendered as a numbered card showing: what the agent will do, which agent role handles it, and any external calls or file writes it will make (so the user can see the blast radius before approving).

The `AWAIT_PLAN_APPROVAL` interrupt is a hard stop. The graph will never transition to `DELEGATE` unless `plan_approved = True`. This is enforced both in the graph edge condition and in the `delegate_node` itself as a guard assertion.

#### Fast-Track for Simple Tasks

If the orchestrator's initial analysis classifies a task as `complexity = trivial` (e.g. "what is 2+2", "format this text"), the `CLARIFY` phase is skipped entirely and the plan draft is presented immediately with a single confirmation step rather than a full back-and-forth.

### 7.3 LLM Routing Logic

The orchestrator uses a tiered model selection strategy:

```python
# core/llm.py

def select_model(task_complexity: str, task_type: str) -> LLM:
    """
    Route to the cheapest/fastest model that can handle the task.
    """
    if task_type == "routing" or task_complexity == "trivial":
        return ollama("qwen3.5:4b-q4_K_M")   # default fast model
    
    elif task_complexity in ("medium", "planning"):
        return ollama("qwen3.5:4b-q4_K_M")   # same model, no context switch
    
    elif task_complexity == "deep_reasoning":
        return ollama("sam860/deepseek-r1-0528-qwen3:8b")
    
    elif task_complexity == "frontier":
        # Local models flagged this as beyond their capability
        return claude_api("claude-opus-4-6")  # API fallback
```

---

## 8. Skill Registry & Exponential Growth

### 8.1 Skill Lifecycle

```
Agent writes Python code
        │
        ▼
Sandbox execution (Docker container, --network none)
        │
        ├── FAILS → logged, not saved
        │
        └── SUCCEEDS
                │
                ▼
        Skill promoted to "candidate"
        (human review required in dashboard)
                │
                ▼
        Human approves in dashboard
                │
                ▼
        Skill saved to PostgreSQL skills table
        + embedding generated (nomic-embed-text)
        + stored in pgvector index
                │
                ▼
        Future agents can find this skill via
        semantic search: "find skills similar to
        'fetch stock price from Yahoo Finance'"
```

### 8.2 Skill Schema

```sql
CREATE TABLE skills (
    id          UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    name        TEXT NOT NULL,
    description TEXT NOT NULL,
    code        TEXT NOT NULL,           -- the Python function
    language    TEXT DEFAULT 'python',
    author_agent TEXT,                   -- which agent wrote it
    embedding   vector(768),             -- nomic-embed-text embedding
    call_count  INTEGER DEFAULT 0,       -- usage tracking
    success_rate FLOAT DEFAULT 1.0,
    tags        TEXT[],
    created_at  TIMESTAMPTZ DEFAULT NOW(),
    last_used   TIMESTAMPTZ,
    status      TEXT DEFAULT 'active'    -- active | deprecated | candidate
);
```

### 8.3 Skill Discovery

When an agent starts a task, it first queries the skill registry:

```python
async def find_relevant_skills(task_description: str, limit: int = 5):
    embedding = await embed(task_description)
    return await db.execute("""
        SELECT name, description, code, success_rate
        FROM skills
        WHERE status = 'active'
        ORDER BY embedding <=> $1
        LIMIT $2
    """, embedding, limit)
```

If a relevant skill exists with `success_rate > 0.8`, the agent uses it directly. If no skill exists or all relevant skills have low success rates, the agent writes new code.

---

## 9. Observability Design

### 9.1 What the Dashboard Shows

**Agent Monitor (real-time)**
- Every active agent: name, role, current task, elapsed time, model being used
- Agent status: `idle | planning | executing | awaiting_review | awaiting_approval | error`
- Live token stream from the current agent's LLM call

**Task Board (Kanban)**
- Columns: `Inbox | Planning | In Progress | Review | Done | Failed`
- Each card: task description, assigned agent, time in current state, step count
- Click any card to see full execution trace

**Skill Registry**
- All permanent skills, searchable
- Call count, success rate, last used
- "Candidate skills" awaiting your approval

**System Metrics**
- GPU VRAM usage (via `nvidia-smi` scraper)
- RAM usage per service
- Ollama model load time, tokens/second per model
- Task completion rate, average task duration
- Redis queue depth (backlog indicator)

### 9.2 Event System

Every state change in the system publishes a structured event to Redis:

```python
# core/events.py

@dataclass
class SystemEvent:
    event_type: str    # agent.started | agent.completed | task.state_changed
                       # skill.created | approval.required | error
    agent_id: str
    task_id: str
    payload: dict
    timestamp: datetime

async def publish_event(event: SystemEvent):
    await redis.publish("life-os:events", event.json())
```

The FastAPI WebSocket endpoint subscribes to this channel and forwards all events to connected browsers. This is how the dashboard updates in real time without polling.

### 9.3 Prometheus Metrics

```python
# core/metrics.py  — custom metrics exposed at /metrics

active_agents = Gauge("life_os_active_agents_total", "Currently running agents")
tasks_total = Counter("life_os_tasks_total", "Tasks by status", ["status"])
tokens_per_second = Histogram("life_os_tokens_per_second", "LLM throughput", ["model"])
skill_calls = Counter("life_os_skill_calls_total", "Skill invocations", ["skill_name", "outcome"])
task_duration = Histogram("life_os_task_duration_seconds", "Task completion time", ["complexity"])
api_fallback_calls = Counter("life_os_api_fallback_calls_total", "External API fallback calls", ["provider"])
```

Grafana dashboards are pre-provisioned from `services/grafana/dashboards/life-os.json`.

---

## 10. Data Architecture

### 10.1 Database Schema (Core Tables)

```sql
-- Tasks
CREATE TABLE tasks (
    id          UUID PRIMARY KEY,
    title       TEXT NOT NULL,
    description TEXT,
    status      TEXT DEFAULT 'inbox',   -- inbox|planning|running|review|done|failed
    priority    INTEGER DEFAULT 0,
    created_by  TEXT DEFAULT 'user',
    assigned_to TEXT,                   -- agent_id
    parent_id   UUID REFERENCES tasks(id),  -- subtask hierarchy
    metadata    JSONB,
    created_at  TIMESTAMPTZ DEFAULT NOW(),
    updated_at  TIMESTAMPTZ DEFAULT NOW()
);

-- Agents (registered agent instances, not running processes)
CREATE TABLE agents (
    id          UUID PRIMARY KEY,
    name        TEXT NOT NULL,
    role        TEXT NOT NULL,
    model       TEXT,                   -- which Ollama model this agent uses
    status      TEXT DEFAULT 'idle',
    current_task_id UUID REFERENCES tasks(id),
    total_tasks_completed INTEGER DEFAULT 0,
    created_at  TIMESTAMPTZ DEFAULT NOW()
);

-- Events (immutable audit log)
CREATE TABLE events (
    id          UUID PRIMARY KEY,
    event_type  TEXT NOT NULL,
    agent_id    UUID,
    task_id     UUID,
    payload     JSONB,
    created_at  TIMESTAMPTZ DEFAULT NOW()
);

-- Agent Memory (long-term)
CREATE TABLE memories (
    id          UUID PRIMARY KEY,
    agent_role  TEXT,                   -- memory is scoped to role, not instance
    content     TEXT NOT NULL,
    embedding   vector(768),
    importance  FLOAT DEFAULT 0.5,
    created_at  TIMESTAMPTZ DEFAULT NOW(),
    last_accessed TIMESTAMPTZ
);

-- Approval Queue
CREATE TABLE approvals (
    id          UUID PRIMARY KEY,
    task_id     UUID REFERENCES tasks(id),
    action_type TEXT,
    action_payload JSONB,
    status      TEXT DEFAULT 'pending', -- pending|approved|rejected
    created_at  TIMESTAMPTZ DEFAULT NOW(),
    resolved_at TIMESTAMPTZ
);
```

---

## 11. Global Access Architecture

### 11.1 Cloudflare Tunnel Setup

```yaml
# services/cloudflared/config.yml
tunnel: <YOUR_TUNNEL_ID>
credentials-file: /etc/cloudflared/<YOUR_TUNNEL_ID>.json

ingress:
  # Dashboard (authenticated via Cloudflare Access)
  - hostname: os.yourdomain.com
    service: http://frontend:3000
  
  # API (for mobile app / external integrations)
  - hostname: api.yourdomain.com
    service: http://backend:8000
    originRequest:
      noTLSVerify: false
  
  # SSH access
  - hostname: ssh.yourdomain.com
    service: ssh://host.docker.internal:22
  
  # Grafana metrics (restrict to your IP via Cloudflare Access policy)
  - hostname: metrics.yourdomain.com
    service: http://grafana:3001
  
  - service: http_status:404
```

### 11.2 Access Security

- All subdomains are protected by **Cloudflare Access** — requires your email verification on every new device
- Dashboard has its own JWT authentication layer (even if someone gets past Cloudflare Access)
- API keys for Ollama, Postgres, and Redis are never exposed outside the `internal` Docker network
- SSH is tunneled through Cloudflare — no SSH port open on the machine

---

## 12. Known Failure Modes & Mitigations

| Failure Mode | Likelihood | Impact | Mitigation |
|---|---|---|---|
| Orchestrator LLM produces malformed plan JSON | High (initially) | Medium — task fails, no damage | Pydantic validation on all LLM outputs. Retry with corrective prompt up to 3 times. |
| Agent stuck in infinite revision loop | Medium | Low — wastes resources | Hard `max_revisions = 3` limit in graph. Auto-escalate to human after limit. |
| Docker sandbox container not destroyed | Low | High — resource leak | `docker run --rm` flag + watchdog job that kills containers older than 10 min. |
| Skill with bug promoted to permanent | Low | Medium — future agents use broken skill | Mandatory human review before promotion. `success_rate` tracking auto-deprecates skills below 0.5. |
| pgvector returns wrong skill (semantic mismatch) | Medium | Low — agent falls back to writing new code | Agent validates skill output before using. Low confidence → write fresh. |
| Ollama GPU OOM during concurrent model loads | Medium | High — inference stops | Ollama keeps only 1 large model loaded. 3B router always resident. Queue system prevents concurrent 70B + 32B load. |
| Cloudflare Tunnel drops | Low | High — no remote access | `cloudflared` runs with `--autorestart`. UPS prevents power-cut tunnel drops. Local LAN access always available. |
| Agent writes code that deletes files | Low | Critical | Sandbox has `--read-only` root filesystem. Writes only to `/tmp` inside container, destroyed on exit. |
| Redis message queue fills up (backlog) | Low | Medium — tasks stall | Prometheus alert when queue depth > 50. Celery worker autoscaling (future). |
| LLM API fallback cost runaway | Low | Medium — unexpected charges | Hard daily spend limit via Anthropic/OpenAI dashboard. Orchestrator logs every API call. Alert if >$5/day. |

---

## 13. Windows → Ubuntu Migration Path

### Design Guarantees

Every service runs in a Docker container. The entire system state lives in:
1. Docker volumes (Postgres data, Redis data) — portable via `docker volume`
2. The git repository (all code, config, compose files)
3. `.env` file (secrets — **never committed**)

### Migration Steps (when Ubuntu hardware is ready)

```bash
# On Windows machine — export data volumes
docker run --rm -v life-os_postgres_data:/data -v $(pwd):/backup \
  alpine tar czf /backup/postgres_backup.tar.gz /data

docker run --rm -v life-os_redis_data:/data -v $(pwd):/backup \
  alpine tar czf /backup/redis_backup.tar.gz /data

# Copy to Ubuntu machine
scp -r postgres_backup.tar.gz redis_backup.tar.gz user@ubuntu-box:~/

# On Ubuntu machine
git clone https://github.com/you/life-os.git
cd life-os
cp .env.example .env    # fill in your secrets
./setup.sh              # installs Docker, pulls images, restores volumes, starts services

# Restore volumes
docker run --rm -v life-os_postgres_data:/data -v ~/:/backup \
  alpine tar xzf /backup/postgres_backup.tar.gz

# Done — system is live
```

### Windows-Specific Notes

- Docker Desktop for Windows with WSL2 backend is required
- GPU passthrough for Ollama requires WSL2 + NVIDIA Container Toolkit for WSL2
- All paths in docker-compose use Linux-style paths (works in WSL2 / Docker Desktop)
- `setup.ps1` handles Windows prerequisites automatically

---

## 14. Scalability Roadmap

### Phase 1 — Current (1× RTX 3090)
- 30B model fully in VRAM at 25-35 tok/s
- 70B model split GPU/RAM at 6-12 tok/s
- Single-machine, all services co-located

### Phase 2 — Add Second GPU (2× RTX 3090 = 48GB VRAM)
- 70B model fully in VRAM at 25-30 tok/s
- Parallel model hosting: 32B + 7B simultaneously
- No software changes required — Ollama handles multi-GPU automatically

### Phase 3 — Add Model-Dedicated Machine
- Separate small server runs Ollama exclusively
- Main machine freed for agents, Docker, services
- Update `OLLAMA_HOST` env var — single config change

### Phase 4 — Agent Parallelism
- Current: agents run sequentially within a task
- Future: LangGraph `Send()` API for parallel subgraph execution
- Multiple agents work on different steps simultaneously
- Redis queue handles parallelism coordination

### Phase 5 — Fine-Tuning
- Collect successful task traces
- Fine-tune a small model (7B) on your personal task patterns
- Replace the 3B router with your fine-tuned model
- True personalization — a model that knows how you think
