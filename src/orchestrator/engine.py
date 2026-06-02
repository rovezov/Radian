from __future__ import annotations

import logging
from typing import TypedDict

from src.orchestrator import storage
from src.orchestrator.nodes import execute_step, format_final_report, handle_failure, reflect
from src.orchestrator.schemas import ExecutionPlan, StepResult

logger = logging.getLogger(__name__)


class OrchestratorState(TypedDict):
    run_id: str
    plan: ExecutionPlan
    current_step_index: int
    step_results: list[StepResult]
    retry_counts: dict[str, int]
    status: str
    error_log: list[str]


def compile_graph():
    """Optional LangGraph compile hook.

    V1 uses an explicit async loop for reliability. We still expose this helper
    so LangGraph can be switched on without touching call sites.
    """
    try:
        from langgraph.graph import END, StateGraph

        graph = StateGraph(OrchestratorState)
        graph.add_node("router", lambda s: s)
        graph.add_node("execute_step", lambda s: s)
        graph.add_node("reflect", lambda s: s)
        graph.add_node("handle_failure", lambda s: s)
        graph.add_node("finalize", lambda s: s)
        graph.set_entry_point("router")
        graph.add_edge("router", "execute_step")
        graph.add_edge("execute_step", "reflect")
        graph.add_edge("reflect", "handle_failure")
        graph.add_edge("handle_failure", "finalize")
        graph.add_edge("finalize", END)
        return graph.compile()
    except Exception:
        return None


async def execute_orchestrator_run(run_id: str) -> None:
    run = storage.get_run(run_id)
    if not run:
        return

    if run.status in {"failed", "completed", "cancelled"}:
        return

    plan = storage.parse_plan(run)
    results = storage.parse_results_json(run.results_json)

    state: OrchestratorState = {
        "run_id": run_id,
        "plan": plan,
        "current_step_index": run.current_step_index or 0,
        "step_results": results,
        "retry_counts": {},
        "status": "running",
        "error_log": [],
    }

    storage.persist_progress(run_id, status="running")

    try:
        while state["current_step_index"] < len(plan.steps):
            step = plan.steps[state["current_step_index"]]
            attempts = state["retry_counts"].get(step.step_id, 0)

            output = await execute_step(
                step,
                objective=plan.objective,
                owner=plan.owner,
                session_id=plan.session_id or run_id,
                prior_step_outputs=[
                    {
                        "step_id": result.step_id,
                        "title": next(
                            (candidate.title for candidate in plan.steps if candidate.step_id == result.step_id),
                            result.step_id,
                        ),
                        "output": result.output,
                    }
                    for result in state["step_results"]
                    if result.output.strip()
                ],
            )
            passed, verdict = await reflect(
                step,
                output,
                owner=plan.owner,
                session_id=plan.session_id or run_id,
            )

            if passed:
                state["step_results"].append(
                    StepResult(
                        step_id=step.step_id,
                        status="passed",
                        output=output,
                        error="",
                        attempts=attempts + 1,
                    )
                )
                state["current_step_index"] += 1
                storage.persist_progress(
                    run_id,
                    status="running",
                    current_step_index=state["current_step_index"],
                    results=state["step_results"],
                )
                continue

            # Failed this attempt
            state["retry_counts"][step.step_id] = attempts + 1
            should_retry = await handle_failure(state["retry_counts"][step.step_id], step.max_retries)
            if should_retry:
                state["error_log"].append(verdict)
                storage.persist_progress(
                    run_id,
                    status="running",
                    current_step_index=state["current_step_index"],
                    results=state["step_results"],
                    error_message=verdict,
                )
                continue

            state["step_results"].append(
                StepResult(
                    step_id=step.step_id,
                    status="failed",
                    output=output,
                    error=verdict,
                    attempts=state["retry_counts"][step.step_id],
                )
            )
            storage.persist_progress(
                run_id,
                status="failed",
                current_step_index=state["current_step_index"],
                results=state["step_results"],
                final_output="",
                error_message=verdict,
            )
            return

        final_output = "\n\n".join(
            r.output.strip() for r in state["step_results"] if r.output.strip()
        )
        report_output = await format_final_report(
            final_output,
            plan.objective,
            plan.blueprint_type,
            owner=plan.owner,
            session_id=plan.session_id or run_id,
        )
        storage.persist_progress(
            run_id,
            status="completed",
            current_step_index=state["current_step_index"],
            results=state["step_results"],
            final_output=final_output,
            error_message="",
        )
        storage.append_completion_to_session_chat(
            run_id,
            session_id=plan.session_id,
            objective=plan.objective,
            blueprint_type=plan.blueprint_type,
            final_output=final_output,
            report_output=report_output,
        )
    except Exception as e:
        logger.exception("Orchestrator run failed: %s", run_id)
        storage.persist_progress(
            run_id,
            status="failed",
            current_step_index=state["current_step_index"],
            results=state["step_results"],
            error_message=str(e),
        )
        raise
