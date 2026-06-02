# Radian: Orchestrator + Blueprints (Repo-Integrated Spec)

> Scope for this implementation:
> 1. Chat-to-Task Orchestrator (plan in chat, execute in background)
> 2. Blueprint system (typed execution templates)
>
> Out of scope:
> - Context pipeline / memory tier redesign
> - Tool-building system
> - Standalone engineering project spaces
>
> This version is aligned to the current Radian codebase layout and conventions.

---

## 0. Critical Integration Corrections

This section replaces assumptions from the generic spec with actual Radian constraints.

1. Database model path is `core/database.py`, not `src/database/models.py`.
2. API routers live under `routes/`, not `src/api/`.
3. A `task_runs` table already exists for `ScheduledTask` runs:
   - `core.database.TaskRun`
   - It must not be repurposed for orchestrator execution state.
4. Scheduler is class-based in `src/task_scheduler.py` (`TaskScheduler`), already running a polling loop.
5. The app currently uses polling for tasks UI (`static/js/tasks.js`), not WebSocket task streams.
6. Existing task APIs are already mounted at `/api/tasks` via `routes/task_routes.py`.
   - New orchestrator APIs must use a separate prefix to avoid collisions.

---

## 1. Architecture Overview

Orchestrator remains two-phase, but integrated with current Radian flow.

Phase 1: Planning (synchronous, chat)
- User asks for a complex objective.
- Planner classifies intent, selects a blueprint, produces an `ExecutionPlan` JSON.
- Chat renders a plan card with step list.
- User can refine plan via follow-up messages.
- User clicks Approve and backend enqueues run immediately.

Phase 2: Execution (async, scheduler-driven)
- `TaskScheduler` picks queued orchestrator runs.
- Orchestrator engine executes step-by-step with reflection/retries.
- Run status is persisted in DB.
- Frontend polls status endpoint (v1).

V1 transport decision:
- Use HTTP polling first (consistent with existing task UI).
- WebSocket streaming is optional V1.1 after baseline stability.

---

## 2. File and Module Structure (Radian-Actual)

Create:

```text
src/
├── orchestrator/
│   ├── __init__.py
│   ├── engine.py                 # LangGraph compile + state transitions
│   ├── nodes.py                  # execute_step, reflect, retry, finalize
│   ├── planner.py                # classify/select/generate/modify plans
│   ├── schemas.py                # Pydantic schemas
│   ├── storage.py                # DB helpers for orchestrator runs
│   └── blueprints/
│       ├── __init__.py
│       ├── base.py
│       ├── research.py
│       └── coding.py
routes/
└── orchestrator_routes.py        # /api/orchestrator/* endpoints
static/js/
└── orchestratorPlanCard.js       # parse/render radian-plan block + approve action
```

Modify (minimal and targeted):
- `core/database.py`
- `app.py`
- `src/task_scheduler.py`
- `routes/chat_routes.py`
- `static/js/chatRenderer.js`
- `static/js/tasks.js` (queue tab wiring from prior plan)

Do not create `src/api/tasks.py` or `src/database/models.py` in this repository.

---

## 3. Data Models (No collision with existing task_runs)

### 3.1 Pydantic Schemas

Location: `src/orchestrator/schemas.py`

Keep the same schema family from the previous draft:
- `QualityCheck`
- `PlanStep`
- `ExecutionPlan`
- `StepResult`
- `DispatchRequest`
- `DispatchResponse`

Small additions for integration:
- Add `owner: str | None = None` to dispatch payload server-side fill only.
- Add `session_id: str | None = None` to persist originating chat session.

### 3.2 SQLAlchemy Models

Location: `core/database.py`

Do not alter existing `TaskRun` (`task_runs`) semantics.

Add a new table for orchestrator executions, for example `orchestrator_runs`:

```python
class OrchestratorRun(Base):
    __tablename__ = "orchestrator_runs"

    id = Column(String, primary_key=True, index=True, default=lambda: uuid.uuid4().hex)
    owner = Column(String, nullable=True, index=True)
    session_id = Column(String, ForeignKey("sessions.id", ondelete="SET NULL"), nullable=True, index=True)

    created_at = Column(DateTime, default=datetime.utcnow, nullable=False)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow, nullable=False)

    plan_json = Column(Text, nullable=False)
    status = Column(String, nullable=False, default="queued")  # queued|running|completed|failed
    current_step_index = Column(Integer, nullable=False, default=0)

    results_json = Column(Text, nullable=True)
    final_output = Column(Text, nullable=True)
    error_message = Column(Text, nullable=True)

    __table_args__ = (
        Index("ix_orchestrator_runs_owner_status", "owner", "status"),
        Index("ix_orchestrator_runs_status_created", "status", "created_at"),
    )
```

Migration approach in this repo:
- Follow existing startup migration pattern in `core/database.py`.
- Add `_migrate_add_orchestrator_runs_table()` and call it from `init_db()`.
- Do not introduce Alembic for this change.

---

## 4. Blueprint System

Location: `src/orchestrator/blueprints/`

Use the same template design from the earlier draft with these constraints:

1. Tool names must exist in current tool ecosystem:
   - Built-ins from `src/agent_tools.py` / `src/tool_index.py`
   - MCP tools only when connected
2. Avoid hard-coding unavailable tools like `file_reader` if not present.
3. Blueprints should default to tools that are already stable in this codebase:
   - Research: `web_search`, `read_file` (if local files involved), no invented tool tags
   - Coding: run through orchestrator execution wrapper that calls existing agent loop, not direct shell APIs

Blueprint files:
- `base.py`
- `research.py`
- `coding.py`
- registry in `__init__.py`

---

## 5. Planner (Chat-side, Phase 1)

Location: `src/orchestrator/planner.py`

### 5.1 Intent Classification

Use a cheap model via existing LLM wrapper and classify as `COMPLEX` or `SIMPLE`.

Use existing endpoint/model resolution patterns from current code:
- Prefer task/utility endpoint fallback chain already used by scheduler where practical.

### 5.2 Blueprint Selection and Plan Generation

Use LLM-generated selection with guarded fallback to `research`.

Plan generation returns strict `ExecutionPlan` JSON.
- Validate with `ExecutionPlan.model_validate_json(...)`.
- On parse failure, retry once with repair prompt.

### 5.3 Plan Modification

When planning session is active, follow-up messages mutate current plan instead of starting over.

### 5.4 Session State

Track planning state per chat session.

Integration recommendation:
- Persist in memory map keyed by session id first.
- Optional persistence in session metadata can be added later.

Data shape:

```python
class PlanningSession(BaseModel):
    active: bool = False
    current_plan: ExecutionPlan | None = None
    blueprint_name: str | None = None
```

### 5.5 Chat Integration Points

Primary integration point is `routes/chat_routes.py` (`/api/chat_stream`).

Flow:
1. If no active planning session:
   - classify
   - SIMPLE => existing path unchanged
   - COMPLEX => generate plan and emit plan card block
2. If planning session active:
   - process as modification unless explicit approval event

Approval transport:
- Add dedicated endpoint in `routes/orchestrator_routes.py`:
  - `POST /api/orchestrator/dispatch`
- Frontend button should call this directly with current plan JSON.
- Do not rely on NLP to interpret button clicks.

---

## 6. Execution Engine (Phase 2)

Location: `src/orchestrator/engine.py` and `src/orchestrator/nodes.py`

Use LangGraph `StateGraph`, but do not bypass existing Radian tool execution behavior.

Key integration rule:
- For step execution, route through existing agent runtime pattern where possible, not a new parallel tool stack.
- Reuse task scheduler/agent execution behavior as reference (`TaskScheduler._run_agent_loop`).

State fields should include:
- `run_id`
- `plan`
- `current_step_index`
- `step_results`
- `retry_counts`
- `status`
- `error_log`

Node set:
- `router`
- `execute_step`
- `reflect`
- `handle_failure`
- `finalize`

Retry behavior:
- Compare retries against each step's `max_retries`.
- On exhaustion, fail run and finalize.

---

## 7. API Surface (New, isolated prefix)

Create router in `routes/orchestrator_routes.py` with prefix `/api/orchestrator`.

Required endpoints:

1. `POST /api/orchestrator/dispatch`
- Body: `DispatchRequest`
- Inserts `orchestrator_runs` row with `status="queued"`
- Returns `{ run_id, status: "queued" }`

2. `GET /api/orchestrator/runs/{run_id}`
- Returns run status, step results, output, error

3. `GET /api/orchestrator/runs`
- Returns latest runs for current owner (limit + status filters)

4. `POST /api/orchestrator/runs/{run_id}/cancel` (optional v1)
- Marks queued/running run as failed with cancellation reason

Auth and ownership:
- Use existing user resolution helper (`get_current_user` pattern used across routes)
- Owner-scope all queries

Mount in `app.py` with other routers.

---

## 8. Scheduler Integration

Modify `src/task_scheduler.py`.

Do not replace existing scheduled-task flow.

Add:
- `process_queued_orchestrator_runs()` method inside scheduler loop
- Called from `_loop()` alongside existing `_check_due_tasks()`
- Process one queued run at a time initially

Suggested behavior:
1. Fetch oldest `orchestrator_runs.status == "queued"`
2. Atomically set to `running`
3. Build initial graph state from `plan_json`
4. `await ORCHESTRATOR_GRAPH.ainvoke(state)`
5. Persist progress/final status
6. On unhandled exception, mark failed with error

Concurrency:
- Keep serial execution for v1 to match current scheduler safety posture

---

## 9. Frontend Integration

### 9.1 Plan card rendering

Update `static/js/chatRenderer.js` to parse fenced block:

```text
```radian-plan
{ ...ExecutionPlan json... }
```
```

Render card with:
- blueprint type
- objective
- ordered steps
- Approve & Execute button

### 9.2 Approval action

Create `static/js/orchestratorPlanCard.js` helper:
- Parses JSON
- POSTs to `/api/orchestrator/dispatch`
- Shows queued run id and links user to Tasks queue tab

### 9.3 Queue visibility

Extend existing tasks UI in `static/js/tasks.js`:
- Keep current Scheduled tab unchanged
- Add Queue tab that reads `/api/orchestrator/runs`
- Poll every 5 seconds while tab is open

No WebSocket required for v1.

---

## 10. Dependencies

Add to `requirements.txt`:

```text
langgraph>=0.2.0
langchain-core>=0.2.0
```

Keep all existing dependencies unchanged.

---

## 11. Implementation Order (Repo-safe)

1. Schemas
- `src/orchestrator/schemas.py`

2. DB model + migration
- Add `OrchestratorRun` in `core/database.py`
- Add `_migrate_add_orchestrator_runs_table()` in startup migration chain

3. Blueprints
- `src/orchestrator/blueprints/*`

4. Planner
- `src/orchestrator/planner.py`

5. Routes
- `routes/orchestrator_routes.py`
- Mount in `app.py`

6. Engine
- `src/orchestrator/nodes.py`
- `src/orchestrator/engine.py`

7. Scheduler hookup
- `src/task_scheduler.py` queued-run processor

8. Frontend
- `static/js/chatRenderer.js` plan block parse
- `static/js/orchestratorPlanCard.js`
- `static/js/tasks.js` queue tab polling

9. End-to-end testing
- Complex chat prompt => plan card
- Approve => queued row
- Scheduler executes => completed/failed persisted
- Queue tab updates status

---

## 12. Acceptance Criteria

1. No regressions in existing scheduled tasks (`/api/tasks` continues to work).
2. No DB conflict with existing `task_runs` table.
3. Complex objectives can be planned in-chat and approved.
4. Approved plans run asynchronously and survive chat closure.
5. User can monitor orchestrator runs via queue UI polling.
6. Owner scoping is enforced on all orchestrator run APIs.

---

## 13. Explicit Non-goals (for this implementation)

Do not implement in this pass:
- Context store redesign
- Tool marketplace/custom tool authoring
- Project workspaces/venture layer
- Global real-time WebSocket bus for all subsystems

This spec is intentionally scoped to an integration-safe orchestrator foundation inside current Radian architecture.
