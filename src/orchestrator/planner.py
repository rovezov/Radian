from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass
from pathlib import Path

from src.llm_core import llm_call_async
from src.orchestrator.blueprints import all_blueprints, get_blueprint
from src.orchestrator.schemas import ExecutionPlan, GraphNode, GraphEdge, NodeType, EdgeCondition

logger = logging.getLogger(__name__)

_BUILTIN_TOOLS = {
    "web_search",
    "trigger_research",
    "manage_research",
    "read_file",
    "write_file",
    "bash",
    "python",
    "manage_notes",
    "manage_memory",
    "list_sessions",
    "manage_calendar",
    "read_email",
    "list_emails",
}

_NODE_TYPES_PATH = Path("data") / "node_types.json"


def _load_node_type_defs() -> list[dict]:
    try:
        if not _NODE_TYPES_PATH.exists():
            return []
        raw = json.loads(_NODE_TYPES_PATH.read_text(encoding="utf-8"))
        return [x for x in raw if isinstance(x, dict)] if isinstance(raw, list) else []
    except Exception:
        return []


def _allowed_tools_for_plan() -> set[str]:
    """Return the union of builtin tools and every tool referenced in user node types."""
    tools = set(_BUILTIN_TOOLS)
    for nt in _load_node_type_defs():
        for t in (nt.get("tools") or []):
            if t and isinstance(t, str):
                tools.add(t.strip())
    return tools


def _node_types_context_block() -> str:
    """Build a human-readable block describing the user-defined node type library."""
    defs = _load_node_type_defs()
    if not defs:
        return ""
    lines = ["AVAILABLE NODE TYPE TEMPLATES (prefer these when they fit the task):"]
    for nt in defs:
        tid = nt.get("type_id", "")
        name = nt.get("name", tid)
        classif = nt.get("classification", "execute")
        tools = nt.get("tools") or []
        tools_str = ", ".join(tools) if tools else "none"
        qc = nt.get("quality_check_type", "llm_eval")
        lines.append(
            f'- type_id="{tid}" name="{name}" classification={classif} tools=[{tools_str}] quality_check_type={qc}'
        )
    lines.append(
        "When you use one of these templates for a node, set node_type_ref to its type_id "
        "and copy its tools list into the node's tools array."
    )
    return "\n".join(lines)


@dataclass
class PlanningSession:
    active: bool = False
    current_plan: ExecutionPlan | None = None
    blueprint_name: str | None = None


_planning_sessions: dict[str, PlanningSession] = {}


async def _classify_complex(
    message: str,
    endpoint_url: str,
    model: str,
    headers: dict | None,
) -> bool:
    msg = (message or "").strip()
    # Simple length-based fallback when LLM is unavailable.
    if not endpoint_url or not model:
        return len(msg) > 180

    prompt = (
        "Classify user objective complexity for planning orchestration. "
        "Reply with exactly one token: COMPLEX or SIMPLE.\n\n"
        f"User request:\n{msg}"
    )
    try:
        out = await llm_call_async(
            endpoint_url,
            model,
            [
                {"role": "system", "content": "You classify requests for planning depth."},
                {"role": "user", "content": prompt},
            ],
            headers=headers,
            temperature=0,
            max_tokens=8,
            timeout=15,
        )
        token = (out or "").strip().upper()
        if "COMPLEX" in token:
            return True
        if "SIMPLE" in token:
            return False
    except Exception:
        logger.debug("Complexity classifier failed; using length heuristic", exc_info=True)
    return len(msg) > 180


async def _select_blueprint(
    message: str,
    endpoint_url: str,
    model: str,
    headers: dict | None,
) -> str:
    blueprints = all_blueprints()
    if not blueprints:
        return ""

    # Build a name -> description list for the LLM to choose from.
    bp_descriptions = "\n".join(
        f"- {bp.name}: {bp.description or '(no description)'}"
        for bp in blueprints
    )
    names = {bp.name for bp in blueprints}

    if not endpoint_url or not model:
        # No LLM available: return the first blueprint.
        return next(iter(names))

    prompt = (
        "Select the best execution blueprint for the user's objective.\n\n"
        f"Available blueprints:\n{bp_descriptions}\n\n"
        "Reply with the exact blueprint name only (one word, lowercase).\n\n"
        f"Objective:\n{message}"
    )
    try:
        out = await llm_call_async(
            endpoint_url,
            model,
            [
                {"role": "system", "content": "You select workflow blueprints by matching objectives to descriptions."},
                {"role": "user", "content": prompt},
            ],
            headers=headers,
            temperature=0,
            max_tokens=16,
            timeout=15,
        )
        choice = (out or "").strip().lower()
        if choice in names:
            return choice
    except Exception:
        logger.debug("Blueprint selection failed; using first available", exc_info=True)
    # Fallback: return first blueprint.
    return next(iter(names))


def _plan_for_llm(plan: ExecutionPlan) -> dict:
    """Serialize a plan into an LLM-safe dict.

    Strips backend-internal fields (plan_id, owner, session_id, node_type_ref,
    inputs) and keeps quality_check only on branch nodes, so the LLM sees a
    clean, semantically readable document.
    """
    nodes = []
    for node in plan.nodes:
        n: dict = {
            "node_id": node.node_id,
            "node_type": node.node_type.value if hasattr(node.node_type, "value") else str(node.node_type),
            "title": node.title,
            "description": node.description,
            "tools": list(node.tools or []),
            "model": node.model or "",
        }
        node_type_str = n["node_type"]
        if node_type_str == "branch":
            n["quality_check"] = {
                "type": node.quality_check.type,
                "criteria": node.quality_check.criteria,
            }
        nodes.append(n)

    edges = []
    for edge in plan.edges:
        edges.append({
            "edge_id": edge.edge_id,
            "from_node": edge.from_node,
            "to_node": edge.to_node,
            "condition": edge.condition.value if hasattr(edge.condition, "value") else str(edge.condition),
            "max_traversals": edge.max_traversals,
        })

    return {
        "blueprint_type": plan.blueprint_type,
        "objective": plan.objective,
        "entry_node": plan.entry_node,
        "output_format": plan.output_format,
        "llm_modification_rationale": "",
        "nodes": nodes,
        "edges": edges,
    }


def _clean_llm_json(s: str) -> str:
    """Fix the most common LLM JSON output defects before strict parsing."""
    # Remove trailing commas before } or ]
    s = re.sub(r",\s*([}\]])", r"\1", s)
    # Python literals → JSON
    s = re.sub(r"\bNone\b", "null", s)
    s = re.sub(r"\bTrue\b", "true", s)
    s = re.sub(r"\bFalse\b", "false", s)
    # Quote unquoted enum values for known fields that the LLM sometimes writes bare.
    # e.g.  "condition": on_pass  →  "condition": "on_pass"
    # The regex only matches bare word tokens (no leading "), so quoted values are safe.
    for field in ("condition", "node_type", "output_format", "type", "status"):
        s = re.sub(
            rf'("{field}"\s*:\s*)([a-zA-Z_][a-zA-Z0-9_]*)',
            r'\1"\2"',
            s,
        )
    return s


def _extract_json_payload(raw: str) -> str | None:
    txt = (raw or "").strip()
    if not txt:
        return None
    # Step 1: find the first fenced block anywhere in the text.
    # Non-greedy on the fence content so we grab only the FIRST block.
    fence = re.search(r"```(?:json|radian-plan)?\s*([\s\S]*?)\s*```", txt, flags=re.IGNORECASE)
    if fence:
        content = fence.group(1).strip()
        # Step 2: extract the outermost { ... } from within the fence using GREEDY
        # match so we get the full nested object, not just the first closing brace.
        m = re.search(r"(\{[\s\S]*\})", content)
        if m:
            return m.group(1)
    # Fall back: grab the outermost { ... } block in the full text (greedy).
    m2 = re.search(r"(\{[\s\S]*\})", txt)
    if m2:
        return m2.group(1)
    return None


_JSON_SCHEMA_BLOCK = """\
Output the plan as a JSON code block using this schema:
```json
{
  "blueprint_type": "<copy from current plan>",
  "objective": "<user objective, verbatim>",
  "entry_node": "<node_id of first node>",
  "output_format": "markdown_report",
  "llm_modification_rationale": "<brief explanation of what you changed and why>",
  "nodes": [
    {
      "node_id": "<semantic_snake_case e.g. reframe_objective, execute_task, quality_check_branch>",
      "node_type": "execute",
      "title": "<short action-oriented title>",
      "description": "<what this node does, specific to the objective>",
      "tools": ["<tool_name>"],
      "model": ""
    }
  ],
  "edges": [
    {
      "edge_id": "<semantic_snake_case e.g. reframe_to_execute, qc_fail_to_retry>",
      "from_node": "<node_id>",
      "to_node": "<node_id>",
      "condition": "default",
      "max_traversals": 0
    }
  ]
}
```
For branch nodes only, add: "quality_check": {"type": "llm_eval", "criteria": "<PASS condition>"}
max_traversals: use 0 (unlimited) on all edges from execute/reflect/format nodes.
Use a small integer (1-3) ONLY on edges from branch nodes to cap retry loops."""

_DESIGN_RULES_BLOCK = """\
NODE TYPES (node_type field values):
- execute: Run tools or LLM to produce output for one step.
- reflect: Evaluate the previous node output for quality — returns pass/fail, no tools needed.
- format: Synthesize all accumulated context into a polished final report. The final node on the
  success path must always be a format node.
- branch: Routing-only node dedicated to quality evaluation — no tool execution, directs flow
  via on_pass/on_fail edges based on its quality_check criteria.

EDGE CONDITIONS:
- default: Unconditional — always traverse. Use on edges from execute/reflect/format nodes.
- on_pass: Traverse only when the source branch node passed its quality_check.
- on_fail: Traverse only when the source branch node failed its quality_check.

max_traversals:
- Set to 0 (unlimited) on all edges FROM execute, reflect, and format nodes.
- Set to a small integer (1-3) ONLY on edges FROM branch nodes to cap retry loops.

DESIGN RULES:
- Use semantic snake_case node_ids (e.g. reframe_objective, execute_task, quality_check_branch).
- Use semantic snake_case edge_ids (e.g. reframe_to_execute, qc_fail_to_retry).
- For quality gating: execute -> branch -> (on_pass: format | on_fail: retry node).
- Every branch node must have both an on_pass and an on_fail outbound edge.
- Use tools only where needed; reasoning/writing nodes need no tools.
- trigger_research and manage_research are for deep multi-source research only \
(financial analysis, academic research, comprehensive comparisons). \
For simple web lookups use web_search.
- Keep model as empty string."""


async def _plan_with_llm(
    base_plan: ExecutionPlan,
    endpoint_url: str,
    model: str,
    headers: dict | None,
    refinement_hint: str | None = None,
) -> ExecutionPlan:
    if not endpoint_url or not model:
        return base_plan

    allowed_tools = _allowed_tools_for_plan()
    allowed_tools_text = ", ".join(sorted(allowed_tools))
    node_types_block = _node_types_context_block()
    is_refinement = bool(refinement_hint and refinement_hint.strip())

    if is_refinement:
        system_msg = (
            "You are an orchestration planner. The user has reviewed an execution plan and provided targeted feedback. "
            "Apply exactly what the user asked for — no more, no less. "
            "If the user asks to ADD a node, add it. "
            "If the user asks to REMOVE a node, remove it. "
            "If the user asks to CHANGE something, change only that thing. "
            "Do NOT touch anything the user did not explicitly mention. "
            "Preserve existing node_id and edge_id values; assign new stable IDs only for newly added nodes/edges. "
            "Output the complete revised plan as a JSON code block."
        )
        user_msg = (
            f"USER FEEDBACK (apply exactly these changes, nothing else):\n{refinement_hint.strip()}\n\n"
            "CURRENT PLAN (start from this — modify only what the feedback requests):\n"
            f"```json\n{json.dumps(_plan_for_llm(base_plan), indent=2)}\n```\n\n"
            f"AVAILABLE TOOLS: {allowed_tools_text}\n\n"
            + (_DESIGN_RULES_BLOCK + "\n\n")
            + (node_types_block + "\n\n" if node_types_block else "")
            + "Make exactly the changes the user described. "
            "Every node and edge not mentioned in the feedback must be copied verbatim. "
            "Then output the complete updated plan (all nodes, all edges).\n\n"
            + _JSON_SCHEMA_BLOCK
        )
    else:
        system_msg = (
            "You are an orchestration planner. Given a user objective and a blueprint plan, "
            "adapt the plan to specifically accomplish that objective. "
            "Think through what each step should actually do and what tools are needed, "
            "then output the complete adapted plan as a JSON code block."
        )
        user_msg = (
            f"USER OBJECTIVE:\n{base_plan.objective}\n\n"
            "BLUEPRINT TO ADAPT:\n"
            f"```json\n{json.dumps(_plan_for_llm(base_plan), indent=2)}\n```\n\n"
            f"AVAILABLE TOOLS: {allowed_tools_text}\n\n"
            + (_DESIGN_RULES_BLOCK + "\n\n")
            + (node_types_block + "\n\n" if node_types_block else "")
            + "Think through what the objective requires — what should each node actually do? "
            "Do the tools match the task? Should any nodes be added or removed? "
            "Then output the complete adapted plan.\n\n"
            + _JSON_SCHEMA_BLOCK
        )

    try:
        out = await llm_call_async(
            endpoint_url,
            model,
            [
                {"role": "system", "content": system_msg},
                {"role": "user", "content": user_msg},
            ],
            headers=headers,
            temperature=0.3,
            max_tokens=3200,
            timeout=60,
        )
        payload = _extract_json_payload(out or "")
        if not payload:
            logger.warning("_plan_with_llm: no JSON found in LLM output. Raw: %s", (out or "")[:400])
            return base_plan
        try:
            data = json.loads(_clean_llm_json(payload))
        except json.JSONDecodeError as exc:
            logger.warning("_plan_with_llm: JSON parse error (%s). Payload: %s", exc, payload[:400])
            return base_plan
        try:
            plan = ExecutionPlan.model_validate(data)
        except Exception as exc:
            logger.warning("_plan_with_llm: Pydantic validation error (%s). Data: %s", exc, str(data)[:400])
            return base_plan
        if not plan.nodes:
            logger.warning("_plan_with_llm: LLM returned plan with no nodes; falling back to base plan")
            return base_plan
        # Restore fields the LLM doesn't emit.
        plan.owner = base_plan.owner
        plan.session_id = base_plan.session_id
        # Ensure entry_node is set.
        if not plan.entry_node and plan.nodes:
            plan.entry_node = plan.nodes[0].node_id
        # Strip tools that are not in the allowed set (includes user node type tools).
        for node in plan.nodes:
            node.tools = [t for t in (node.tools or []) if t in allowed_tools]
        return plan
    except Exception:
        logger.warning("_plan_with_llm: LLM plan generation failed; falling back to base plan", exc_info=True)
        return base_plan


def _format_plan_card_text(plan: ExecutionPlan) -> str:
    return (
        "I drafted an execution plan. Review it below, then click Approve and Execute if it looks right.\n\n"
        "```radian-plan\n"
        f"{json.dumps(plan.model_dump(), indent=2)}\n"
        "```"
    )



async def maybe_handle_planning_turn(
    session_id: str,
    owner: str | None,
    message: str,
    endpoint_url: str,
    model: str,
    headers: dict | None,
    selected_blueprint: str | None = None,
    force_plan: bool = False,
) -> dict | None:
    cur = _planning_sessions.get(session_id)
    selected_key = (selected_blueprint or "").strip().lower()

    # Switching the selected blueprint starts a fresh plan instead of refining
    # the prior plan from a different blueprint.
    if cur and cur.active and cur.current_plan is not None and selected_key:
        cur_bp = (cur.blueprint_name or cur.current_plan.blueprint_type or "").strip().lower()
        if cur_bp and cur_bp != selected_key:
            cur = None
            _planning_sessions.pop(session_id, None)

    # Existing planning session: treat this turn as iterative refinement.
    if cur and cur.active and cur.current_plan is not None:
        base = cur.current_plan.model_copy(deep=True)
        updated = await _plan_with_llm(
            base,
            endpoint_url,
            model,
            headers,
            refinement_hint=message,
        )
        if updated is base:
            updated = base
        cur.current_plan = updated
        _planning_sessions[session_id] = cur
        return {
            "plan": updated,
            "response_text": _format_plan_card_text(updated),
            "is_refinement": True,
        }

    is_complex = True if force_plan else await _classify_complex(message, endpoint_url, model, headers)
    if not is_complex:
        return None

    bp_name = selected_key or await _select_blueprint(message, endpoint_url, model, headers)
    bp = get_blueprint(bp_name)
    if bp is None:
        all_bps = all_blueprints()
        bp = all_bps[0] if all_bps else None
    if bp is None:
        return None

    base_plan = bp.fill(objective=message.strip(), owner=owner, session_id=session_id)
    final_plan = await _plan_with_llm(base_plan, endpoint_url, model, headers)

    _planning_sessions[session_id] = PlanningSession(
        active=True,
        current_plan=final_plan,
        blueprint_name=final_plan.blueprint_type,
    )

    return {
        "plan": final_plan,
        "response_text": _format_plan_card_text(final_plan),
        "is_refinement": False,
    }


def clear_planning_session(session_id: str) -> None:
    _planning_sessions.pop(session_id, None)
