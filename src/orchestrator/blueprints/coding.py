from __future__ import annotations

from src.orchestrator.blueprints.base import BlueprintTemplate
from src.orchestrator.schemas import PlanStep, QualityCheck


class CodingBlueprint(BlueprintTemplate):
    name = "coding"
    display_name = "Code Implementation"
    description = "Analyze, implement, and validate a code change safely."
    trigger_keywords = [
        "implement",
        "fix",
        "refactor",
        "build",
        "code",
        "feature",
        "bug",
    ]

    def template_steps(self) -> list[PlanStep]:
        return [
            PlanStep(
                title="Understand codebase context",
                description="Locate relevant files, constraints, and expected behavior.",
                tools=["read_file"],
                quality_check=QualityCheck(type="none"),
                max_retries=1,
            ),
            PlanStep(
                title="Implement change",
                description="Apply minimal edits to satisfy the objective.",
                tools=["read_file", "write_file"],
                quality_check=QualityCheck(
                    type="llm_eval",
                    criteria="PASS only if implementation details map to objective requirements.",
                ),
                max_retries=2,
            ),
            PlanStep(
                title="Validate and summarize",
                description="Run available checks and summarize outcomes and risks.",
                tools=["read_file"],
                quality_check=QualityCheck(
                    type="llm_eval",
                    criteria="PASS only if validation status and any residual risk are explicit.",
                ),
                max_retries=1,
            ),
        ]
