from __future__ import annotations

from src.orchestrator.blueprints.base import BlueprintTemplate
from src.orchestrator.schemas import PlanStep, QualityCheck


class ResearchBlueprint(BlueprintTemplate):
    name = "research"
    display_name = "Research and Synthesis"
    description = "Gather sources, synthesize findings, and produce a concise report."
    trigger_keywords = [
        "research",
        "compare",
        "analyze",
        "investigate",
        "find sources",
        "summarize",
    ]

    def template_steps(self) -> list[PlanStep]:
        return [
            PlanStep(
                title="Scope and assumptions",
                description="Restate the objective, assumptions, and success criteria.",
                tools=["web_search"],
                quality_check=QualityCheck(
                    type="none",
                    criteria="",
                ),
                max_retries=1,
            ),
            PlanStep(
                title="Collect evidence",
                description="Collect high-signal references and capture key facts.",
                tools=["trigger_research", "manage_research", "web_search", "read_file"],
                quality_check=QualityCheck(
                    type="llm_eval",
                    criteria="PASS only if at least 3 concrete facts or references were produced.",
                ),
                max_retries=2,
            ),
            PlanStep(
                title="Synthesize answer",
                description="Produce a final summary with recommendations and caveats.",
                tools=[],
                quality_check=QualityCheck(
                    type="llm_eval",
                    criteria="PASS only if the output directly addresses the objective and includes caveats.",
                ),
                max_retries=1,
            ),
        ]
