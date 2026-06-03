from __future__ import annotations

import logging
from typing import TypedDict

from src.orchestrator import storage
from src.orchestrator.nodes import execute_step, format_final_report, reflect
from src.orchestrator.schemas import (
    EdgeCondition,
    ExecutionPlan,
    GraphEdge,
    NodeResult,
    NodeType,
)

logger = logging.getLogger(__name__)

_MAX_TOTAL_NODES = 50


class OrchestratorState(TypedDict):
    run_id: str
    plan: ExecutionPlan
    current_node_id: str
    context: list[NodeResult]           # append-only global context
    edge_traversal_counts: dict[str, int]
    status: str
    error_log: list[str]


def compile_graph():
    """Optional LangGraph compile hook.

    Actual execution uses the custom graph traversal loop below. This helper
    allows LangGraph to be switched on without touching call sites.
    """
    try:
        from langgraph.graph import END, StateGraph

        graph = StateGraph(OrchestratorState)
        graph.add_node("execute", lambda s: s)
        graph.add_node("reflect", lambda s: s)
        graph.add_node("format", lambda s: s)
        graph.add_node("branch", lambda s: s)
        graph.set_entry_point("execute")
        graph.add_edge("execute", "reflect")
        graph.add_edge("reflect", "format")
        graph.add_edge("format", END)
        return graph.compile()
    except Exception:
        return None


def _build_edges_from(edges: list[GraphEdge]) -> dict[str, list[GraphEdge]]:
    out: dict[str, list[GraphEdge]] = {}
    for edge in edges:
        out.setdefault(edge.from_node, []).append(edge)
    return out


def _find_next_node(
    edges_from: dict[str, list[GraphEdge]],
    current_node_id: str,
    passed: bool,
    traversal_counts: dict[str, int],
) -> str | None:
    """Return the node_id to visit next, or None if the graph terminates here."""
    edges = edges_from.get(current_node_id, [])
    want = EdgeCondition.on_pass if passed else EdgeCondition.on_fail

    # Prefer conditional edge that matches the current pass/fail status.
    for edge in edges:
        if edge.condition == want:
            count = traversal_counts.get(edge.edge_id, 0)
            if edge.max_traversals == 0 or count < edge.max_traversals:
                traversal_counts[edge.edge_id] = count + 1
                return edge.to_node

    # Fall back to the unconditional default edge.
    for edge in edges:
        if edge.condition == EdgeCondition.default:
            count = traversal_counts.get(edge.edge_id, 0)
            if edge.max_traversals == 0 or count < edge.max_traversals:
                traversal_counts[edge.edge_id] = count + 1
                return edge.to_node

    return None


async def execute_orchestrator_run(run_id: str) -> None:
    run = storage.get_run(run_id)
    if not run:
        return

    if run.status in {"failed", "completed", "cancelled"}:
        return

    plan = storage.parse_plan(run)
    context = storage.parse_results_json(run.results_json)

    nodes_by_id = {n.node_id: n for n in plan.nodes}
    edges_from = _build_edges_from(plan.edges)

    entry = plan.entry_node or (plan.nodes[0].node_id if plan.nodes else "")
    if not entry:
        storage.persist_progress(run_id, status="failed", error_message="No nodes in plan")
        return

    # Restore position: if we already have results for some nodes, start from
    # the last unvisited node (for crash recovery).
    visited_ids = {r.node_id for r in context}
    current_node_id = entry
    for node in plan.nodes:
        if node.node_id not in visited_ids:
            current_node_id = node.node_id
            break

    state: OrchestratorState = {
        "run_id": run_id,
        "plan": plan,
        "current_node_id": current_node_id,
        "context": context,
        "edge_traversal_counts": {},
        "status": "running",
        "error_log": [],
    }

    storage.persist_progress(run_id, status="running")

    total_nodes_visited = 0
    format_output: str = ""

    try:
        while state["current_node_id"] and total_nodes_visited < _MAX_TOTAL_NODES:
            node_id = state["current_node_id"]
            node = nodes_by_id.get(node_id)
            if node is None:
                logger.warning("Node %s not found in plan; stopping traversal", node_id)
                break

            total_nodes_visited += 1

            # --- Execute the node by type ---
            result = NodeResult(node_id=node_id, status="running")

            if node.node_type == NodeType.execute:
                prior_outputs = [
                    {"node_id": r.node_id, "output": r.output}
                    for r in state["context"]
                    if r.output.strip()
                ]
                raw_output = await execute_step(
                    node,
                    objective=plan.objective,
                    owner=plan.owner,
                    session_id=plan.session_id or run_id,
                    prior_node_outputs=prior_outputs,
                )
                passed = bool(raw_output and raw_output.strip())
                result.output = raw_output or ""
                result.status = "passed" if passed else "failed"
                result.attempts = 1

            elif node.node_type == NodeType.reflect:
                # Find the most recent non-reflect output in context.
                last_output = ""
                for r in reversed(state["context"]):
                    prev_node = nodes_by_id.get(r.node_id)
                    if prev_node and prev_node.node_type != NodeType.reflect and r.output.strip():
                        last_output = r.output
                        break
                passed, verdict = await reflect(
                    node,
                    last_output,
                    owner=plan.owner,
                    session_id=plan.session_id or run_id,
                )
                result.output = verdict
                result.status = "passed" if passed else "failed"
                result.attempts = 1

            elif node.node_type == NodeType.format:
                accumulated = "\n\n".join(
                    r.output.strip() for r in state["context"] if r.output.strip()
                )
                format_output = await format_final_report(
                    accumulated,
                    plan.objective,
                    plan.blueprint_type,
                    owner=plan.owner,
                    session_id=plan.session_id or run_id,
                )
                result.output = format_output
                result.status = "passed"
                result.attempts = 1
                passed = True

            elif node.node_type == NodeType.branch:
                # Routing-only node â€” no execution, always passes.
                result.status = "passed"
                passed = True

            else:
                logger.warning("Unknown node_type %s for node %s; skipping", node.node_type, node_id)
                result.status = "skipped"
                passed = True

            # Append to append-only global context.
            state["context"].append(result)
            storage.persist_progress(
                run_id,
                status="running",
                current_step_index=total_nodes_visited,
                results=state["context"],
            )

            # Traverse to next node.
            next_id = _find_next_node(
                edges_from,
                node_id,
                passed,
                state["edge_traversal_counts"],
            )
            state["current_node_id"] = next_id or ""

        # --- Finalize ---
        # If no format node was visited, synthesize a final report now.
        if not format_output:
            accumulated = "\n\n".join(
                r.output.strip() for r in state["context"] if r.output.strip()
            )
            format_output = await format_final_report(
                accumulated,
                plan.objective,
                plan.blueprint_type,
                owner=plan.owner,
                session_id=plan.session_id or run_id,
            )

        storage.persist_progress(
            run_id,
            status="completed",
            current_step_index=total_nodes_visited,
            results=state["context"],
            final_output=format_output,
            error_message="",
        )
        storage.append_completion_to_session_chat(
            run_id,
            session_id=plan.session_id,
            objective=plan.objective,
            blueprint_type=plan.blueprint_type,
            final_output=format_output,
            report_output=format_output,
        )

    except Exception as e:
        logger.exception("Orchestrator run failed: %s", run_id)
        storage.persist_progress(
            run_id,
            status="failed",
            current_step_index=total_nodes_visited,
            results=state["context"],
            error_message=str(e),
        )
        raise

