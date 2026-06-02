from __future__ import annotations

import uuid
from typing import Any, Literal

from pydantic import BaseModel, Field


class QualityCheck(BaseModel):
    """How a step output is validated before we move to the next step."""

    type: Literal["llm_eval", "script", "none"] = "llm_eval"
    criteria: str = ""


class PlanStep(BaseModel):
    step_id: str = Field(default_factory=lambda: f"step_{uuid.uuid4().hex[:8]}")
    title: str
    description: str
    tools: list[str] = Field(default_factory=list)
    model: str = ""
    inputs: dict[str, Any] = Field(default_factory=dict)
    quality_check: QualityCheck = Field(default_factory=QualityCheck)
    max_retries: int = 2
    depends_on: list[str] = Field(default_factory=list)


class ExecutionPlan(BaseModel):
    plan_id: str = Field(default_factory=lambda: uuid.uuid4().hex)
    blueprint_type: str
    objective: str
    steps: list[PlanStep]
    output_format: str = "markdown_report"
    owner: str | None = None
    session_id: str | None = None


class StepResult(BaseModel):
    step_id: str
    status: Literal["pending", "running", "passed", "failed", "skipped"]
    output: str = ""
    error: str = ""
    attempts: int = 0


class DispatchRequest(BaseModel):
    plan: ExecutionPlan


class DispatchResponse(BaseModel):
    run_id: str
    status: Literal["queued"] = "queued"
