from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass

from src.llm_core import llm_call_async
from src.orchestrator.blueprints import all_blueprints, get_blueprint
from src.orchestrator.schemas import ExecutionPlan, PlanStep, QualityCheck

logger = logging.getLogger(__name__)

_RESEARCH_KEYWORDS = (
    "research",
    "investigate",
    "best",
    "compare",
    "sources",
    "deep dive",
    "market",
    "cards",
)

_PYTHON_CALC_KEYWORDS = (
    "python",
    "calculate",
    "calculation",
    "compute",
    "estimated value",
    "estimate value",
    "value gain",
    "yearly value",
    "model scenario",
)

_TOOL_NONE_MARKERS = {
    "",
    "none",
    "no_tool",
    "no_tools",
    "no-tool",
    "no-tools",
    "null",
    "n/a",
    "na",
    "auto",
    "default",
}

_TOOL_PREFIXES = {
    "tool",
    "tools",
    "autotool",
    "autotools",
    "model",
    "models",
}

_ALLOWED_STEP_TOOLS = {
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

_WRITING_STEP_HINTS = (
    "write",
    "draft",
    "story",
    "concept",
    "idea",
    "summarize",
    "select",
    "choose",
)

_CALC_HINTS = (
    "calculate",
    "calculation",
    "compute",
    "script",
    "python",
    "analysis code",
)


@dataclass
class PlanningSession:
    active: bool = False
    current_plan: ExecutionPlan | None = None
    blueprint_name: str | None = None


_planning_sessions: dict[str, PlanningSession] = {}


def _heuristic_complex(message: str) -> bool:
    msg = (message or "").strip().lower()
    if not msg:
        return False
    if len(msg) > 180:
        return True
    needles = (
        "build",
        "implement",
        "integrate",
        "plan",
        "phases",
        "multi-step",
        "roadmap",
        "orchestrate",
        "end-to-end",
    )
    return any(n in msg for n in needles)


def _choose_blueprint_heuristic(message: str) -> str:
    msg = (message or "").lower()
    scored: list[tuple[int, str]] = []
    for bp in all_blueprints():
        hits = sum(1 for k in (bp.trigger_keywords or []) if k and k in msg)
        scored.append((hits, bp.name))
    scored.sort(reverse=True)
    if scored and scored[0][0] > 0:
        return scored[0][1]
    if scored:
        return scored[0][1]
    return ""


async def _classify_complex(
    message: str,
    endpoint_url: str,
    model: str,
    headers: dict | None,
) -> bool:
    if not endpoint_url or not model:
        return _heuristic_complex(message)

    prompt = (
        "Classify user objective complexity for planning orchestration. "
        "Reply with exactly one token: COMPLEX or SIMPLE.\n\n"
        f"User request:\n{message}"
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
        logger.debug("Complexity classifier failed; using heuristic", exc_info=True)
    return _heuristic_complex(message)


async def _select_blueprint(
    message: str,
    endpoint_url: str,
    model: str,
    headers: dict | None,
) -> str:
    if not endpoint_url or not model:
        return _choose_blueprint_heuristic(message)

    names = {bp.name for bp in all_blueprints()}
    if not names:
        return ""
    bp_list = ", ".join(sorted(names))
    prompt = (
        "Select the best execution blueprint for this objective. "
        f"Allowed values: {bp_list}. Reply with one value only.\n\n"
        f"Objective:\n{message}"
    )
    try:
        out = await llm_call_async(
            endpoint_url,
            model,
            [
                {"role": "system", "content": "You map objectives to workflow blueprints."},
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
        logger.debug("Blueprint selection failed; using heuristic", exc_info=True)
    return _choose_blueprint_heuristic(message)


def _extract_json_payload(raw: str) -> str | None:
    txt = (raw or "").strip()
    if not txt:
        return None
    if txt.startswith("```"):
        m = re.search(r"```(?:json|radian-plan)?\s*(\{[\s\S]*\})\s*```", txt, flags=re.IGNORECASE)
        if m:
            return m.group(1)
    m2 = re.search(r"(\{[\s\S]*\})", txt)
    if m2:
        return m2.group(1)
    return None


def _normalize_tool_token(raw: str | None) -> str | None:
    token = str(raw or "").strip().lower()
    if not token:
        return None
    token = token.replace(" ", "_")
    if ":" in token:
        prefix, suffix = token.split(":", 1)
        if prefix.strip() in _TOOL_PREFIXES:
            token = suffix.strip()
    if token in _TOOL_NONE_MARKERS:
        return None
    return token or None


def _normalize_step_tools(step: PlanStep) -> list[str]:
    out: list[str] = []
    seen: set[str] = set()
    for raw in (step.tools or []):
        token = _normalize_tool_token(raw)
        if not token:
            continue
        if token in {"deep_research", "deepresearch", "research", "research_job", "research_report"}:
            for mapped in ("trigger_research", "manage_research"):
                if mapped not in seen:
                    seen.add(mapped)
                    out.append(mapped)
            continue
        if token.startswith("python_script") or token in {"python-script", "pandas"}:
            token = "python"
        if token not in _ALLOWED_STEP_TOOLS:
            continue
        if token in seen:
            continue
        seen.add(token)
        out.append(token)
    return out


def _is_writing_step(step: PlanStep) -> bool:
    text = f"{step.title} {step.description}".lower()
    return any(hint in text for hint in _WRITING_STEP_HINTS)


def _is_calc_step(step: PlanStep) -> bool:
    text = f"{step.title} {step.description}".lower()
    return any(hint in text for hint in _CALC_HINTS)


def _step_similarity_signature(step: PlanStep) -> set[str]:
    text = f"{step.title} {step.description}".lower()
    words = re.findall(r"[a-z0-9_]+", text)
    stop = {
        "the", "a", "an", "and", "to", "of", "for", "with", "based", "on", "using",
        "step", "task", "current", "today", "create", "generate", "write", "story",
    }
    return {w for w in words if len(w) > 2 and w not in stop}


def _merge_duplicate_steps(steps: list[PlanStep]) -> list[PlanStep]:
    if len(steps) < 2:
        return steps
    merged: list[PlanStep] = []
    for step in steps:
        if not merged:
            merged.append(step)
            continue
        prev = merged[-1]
        a = _step_similarity_signature(prev)
        b = _step_similarity_signature(step)
        overlap = len(a & b)
        union = len(a | b) or 1
        jaccard = overlap / union
        if jaccard >= 0.7:
            # Keep the earlier step and enrich tools if the later one adds value.
            prev_tools = _normalize_step_tools(prev)
            cur_tools = _normalize_step_tools(step)
            prev.tools = list(dict.fromkeys(prev_tools + cur_tools))
            continue
        merged.append(step)
    return merged


def _sanitize_plan(plan: ExecutionPlan, refinement_hint: str | None = None) -> ExecutionPlan:
    refined = plan.model_copy(deep=True)
    hint = str(refinement_hint or "").lower()
    challenge_mode = any(k in hint for k in ("same", "duplicate", "why", "need", "unnecessary", "redundant"))

    sanitized_steps: list[PlanStep] = []
    for step in (refined.steps or []):
        tools = _normalize_step_tools(step)

        # Recover tool labels accidentally emitted into `model`.
        model_raw = str(step.model or "").strip()
        model_low = model_raw.lower()
        if model_low.startswith(("autotools:", "tools:", "tool:")):
            leaked_tool = _normalize_tool_token(model_raw)
            if leaked_tool and leaked_tool in _ALLOWED_STEP_TOOLS and leaked_tool not in tools:
                tools.append(leaked_tool)
            step.model = ""
        elif model_low in _TOOL_NONE_MARKERS:
            step.model = ""

        # Writing/concept/selection steps should not require infra tools unless
        # the step explicitly asks for calculations/code execution.
        if _is_writing_step(step) and not _is_calc_step(step):
            tools = [t for t in tools if t not in {"python", "bash", "manage_notes", "manage_memory", "write_file"}]

        # When user questions duplication/tool use, minimize tool load further.
        if challenge_mode and _is_writing_step(step):
            tools = [t for t in tools if t in {"web_search", "trigger_research", "manage_research", "read_file"}]

        # Strip trigger_research/manage_research unless the objective genuinely needs
        # deep multi-minute research. For simple tasks (creative writing, short story,
        # quick lookup) a plain web_search is sufficient and much faster.
        obj_text = str(plan.objective or "").lower()
        if not _objective_needs_deep_research(obj_text):
            needs_deep = {"trigger_research", "manage_research"}
            if needs_deep & set(tools):
                # Replace with web_search if this is a search/lookup step, else remove
                step_low = f"{step.title} {step.description}".lower()
                if any(k in step_low for k in ("search", "find", "gather", "research", "look", "fetch", "trend")):
                    tools = [t for t in tools if t not in needs_deep]
                    if "web_search" not in tools:
                        tools.insert(0, "web_search")
                else:
                    tools = [t for t in tools if t not in needs_deep]

        # Writing steps and pure reasoning steps (no tools) don't need LLM quality
        # evaluation — there's no PASS/FAIL for a story or a plan.
        if not tools or (_is_writing_step(step) and not _is_calc_step(step)):
            step.quality_check = QualityCheck(type="none")

        step.tools = tools
        sanitized_steps.append(step)

    refined.steps = _merge_duplicate_steps(sanitized_steps)
    if len(refined.steps) > 1:
        for idx, step in enumerate(refined.steps):
            if idx == 0:
                step.depends_on = []
            elif not step.depends_on:
                step.depends_on = [refined.steps[idx - 1].step_id]
    return refined


async def _plan_with_llm(
    base_plan: ExecutionPlan,
    endpoint_url: str,
    model: str,
    headers: dict | None,
    refinement_hint: str | None = None,
) -> ExecutionPlan:
    if not endpoint_url or not model:
        return base_plan

    refinement_block = ""
    if refinement_hint and refinement_hint.strip():
        refinement_block = (
            "\n\nUser refinement to incorporate:\n"
            f"{refinement_hint.strip()}\n"
            "Update objective and steps accordingly. Do not append a literal 'User refinement' step."
        )

    prompt = (
        "You are filling a structured execution plan JSON. "
        "Return JSON ONLY, no markdown. Keep schema-compatible fields.\n\n"
        "Requirements:\n"
        "- Make steps specific to the objective, not generic placeholders.\n"
        "- If the user asked to revise a prior plan, address that feedback explicitly in the revised steps.\n"
        "- Avoid duplicate adjacent steps; each step must have a distinct purpose.\n"
        "- Prefer the minimum tool set needed per step; no tools is valid for reasoning/writing steps.\n"
        "- Include meaningful step titles and descriptions.\n"
        "- Include concrete tools per step where useful.\n"
        "- trigger_research and manage_research are ONLY for deep multi-source research that takes several minutes "
        "(e.g. financial analysis, academic literature, comprehensive market comparison). "
        "For simple information lookup use web_search instead.\n"
        "- Use ONLY real tool names in step.tools, chosen from this list: "
        "web_search, trigger_research, manage_research, read_file, write_file, bash, python, manage_notes, manage_memory, "
        "list_sessions, manage_calendar, read_email, list_emails.\n"
        "- Do NOT invent pseudo-tools (no filenames, no package names such as pandas).\n"
        "- Keep model empty string unless there is a guaranteed concrete model id.\n"
        "- Keep existing step_id values stable when feasible.\n\n"
        "Base plan JSON:\n"
        f"{base_plan.model_dump_json(indent=2)}"
        f"{refinement_block}"
    )

    try:
        out = await llm_call_async(
            endpoint_url,
            model,
            [
                {"role": "system", "content": "Produce strict JSON output."},
                {"role": "user", "content": prompt},
            ],
            headers=headers,
            temperature=0.2,
            max_tokens=1800,
            timeout=35,
        )
        payload = _extract_json_payload(out or "")
        if not payload:
            return base_plan
        plan = ExecutionPlan.model_validate_json(payload)
        if not plan.steps:
            return base_plan
        return _sanitize_plan(plan, refinement_hint=refinement_hint)
    except Exception:
        return base_plan


def _objective_needs_deep_research(objective: str) -> bool:
    text = str(objective or "").strip().lower()
    if not text:
        return False
    return any(k in text for k in _RESEARCH_KEYWORDS)


def _objective_needs_python_calc(objective: str) -> bool:
    text = str(objective or "").strip().lower()
    if not text:
        return False
    return any(k in text for k in _PYTHON_CALC_KEYWORDS)


def _plan_has_tool(plan: ExecutionPlan, tool_name: str) -> bool:
    target = str(tool_name or "").strip().lower()
    if not target:
        return False
    for step in (plan.steps or []):
        for t in (step.tools or []):
            if str(t or "").strip().lower() == target:
                return True
    return False


def _ensure_research_and_calc_steps(plan: ExecutionPlan) -> ExecutionPlan:
    """Inject required deep-research/python steps when user objective clearly asks for them."""
    obj = str(plan.objective or "")
    needs_research = _objective_needs_deep_research(obj)
    needs_python = _objective_needs_python_calc(obj)

    # Ensure an explicit deep research step exists for research-heavy objectives.
    if needs_research and not _plan_has_tool(plan, "trigger_research"):
        insert_at = 1 if len(plan.steps or []) >= 1 else 0
        plan.steps.insert(
            insert_at,
            PlanStep(
                title="Run deep research",
                description=(
                    "Use deep research to gather and verify high-signal sources relevant to the objective. "
                    "Capture key facts, constraints, and trade-offs with citations."
                ),
                tools=["trigger_research", "manage_research"],
                max_retries=1,
            ),
        )

    # Ensure a concrete Python computation step exists when the user asks for calculations.
    if needs_python and not _plan_has_tool(plan, "python"):
        insert_at = max(0, len(plan.steps) - 1)
        plan.steps.insert(
            insert_at,
            PlanStep(
                title="Compute value scenarios in Python",
                description=(
                    "Run Python calculations for estimated annual value and scenario comparisons based on the "
                    "collected evidence and stated spending profile."
                ),
                tools=["python"],
                max_retries=2,
            ),
        )

    return _sanitize_plan(plan)


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
        updated = _ensure_research_and_calc_steps(updated)
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
    final_plan = _ensure_research_and_calc_steps(final_plan)

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
