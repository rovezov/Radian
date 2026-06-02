from src.orchestrator.planner import _sanitize_plan
from src.orchestrator.schemas import ExecutionPlan, PlanStep


def _mk_plan(*steps: PlanStep) -> ExecutionPlan:
    return ExecutionPlan(
        blueprint_type="basic-plan",
        objective="Write a short story that sounds cute based on what is trending today",
        steps=list(steps),
    )


def test_sanitize_recovers_tool_leaked_into_model_field():
    plan = _mk_plan(
        PlanStep(
            step_id="s1",
            title="Trend research",
            description="Research current trends.",
            tools=[],
            model="autotools:web_search",
        )
    )

    out = _sanitize_plan(plan)
    assert out.steps[0].model == ""
    assert out.steps[0].tools == ["web_search"]


def test_sanitize_removes_unneeded_tools_from_writing_steps():
    plan = _mk_plan(
        PlanStep(
            step_id="s1",
            title="Generate story concept",
            description="Create a cute story idea based on trends.",
            tools=["manage_notes"],
            model="",
        ),
        PlanStep(
            step_id="s2",
            title="Write the story",
            description="Write a short and cute story.",
            tools=["python"],
            model="",
        ),
    )

    out = _sanitize_plan(plan, refinement_hint="Why is step 3 and 4 doing the same thing and why do you need manage notes and python?")
    assert out.steps[0].tools == []
    assert out.steps[1].tools == []


def test_sanitize_collapses_duplicate_adjacent_steps():
    plan = _mk_plan(
        PlanStep(
            step_id="s1",
            title="Write the story",
            description="Write a short and cute story based on trend.",
            tools=["python"],
            model="",
        ),
        PlanStep(
            step_id="s2",
            title="Write the story draft",
            description="Write a short and cute story based on current trend.",
            tools=["manage_notes"],
            model="",
        ),
    )

    out = _sanitize_plan(plan, refinement_hint="these steps look duplicate")
    assert len(out.steps) == 1


def test_sanitize_adds_linear_dependencies_when_missing():
    plan = _mk_plan(
        PlanStep(step_id="s1", title="Clarify", description="Interpret constraints", tools=[], model=""),
        PlanStep(step_id="s2", title="Research", description="Find trends", tools=["web_search"], model=""),
        PlanStep(step_id="s3", title="Write", description="Write story", tools=[], model=""),
    )

    out = _sanitize_plan(plan)
    assert out.steps[0].depends_on == []
    assert out.steps[1].depends_on == ["s1"]
    assert out.steps[2].depends_on == ["s2"]
