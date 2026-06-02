from __future__ import annotations

from abc import ABC, abstractmethod
from copy import deepcopy

from src.orchestrator.schemas import ExecutionPlan, PlanStep


class BlueprintTemplate(ABC):
    name: str
    display_name: str
    description: str
    trigger_keywords: list[str] = []

    @abstractmethod
    def template_steps(self) -> list[PlanStep]:
        raise NotImplementedError

    def to_dict(self) -> dict:
        return {
            "name": self.name,
            "display_name": self.display_name,
            "description": self.description,
            "trigger_keywords": list(self.trigger_keywords or []),
            "steps": [s.model_dump() for s in self.template_steps()],
        }

    def fill(self, objective: str, owner: str | None = None, session_id: str | None = None) -> ExecutionPlan:
        return ExecutionPlan(
            blueprint_type=self.name,
            objective=objective,
            steps=self.template_steps(),
            owner=owner,
            session_id=session_id,
        )


class GenericBlueprint(BlueprintTemplate):
    def __init__(
        self,
        *,
        name: str,
        display_name: str,
        description: str,
        trigger_keywords: list[str] | None = None,
        steps: list[PlanStep] | None = None,
    ) -> None:
        self.name = (name or "").strip().lower()
        self.display_name = (display_name or name or "").strip()
        self.description = (description or "").strip()
        self.trigger_keywords = [str(k).strip().lower() for k in (trigger_keywords or []) if str(k).strip()]
        self._steps = [s.model_copy(deep=True) for s in (steps or [])]

    def template_steps(self) -> list[PlanStep]:
        return [s.model_copy(deep=True) for s in self._steps]

    def to_dict(self) -> dict:
        data = super().to_dict()
        data["name"] = self.name
        data["display_name"] = self.display_name
        data["description"] = self.description
        data["trigger_keywords"] = deepcopy(self.trigger_keywords)
        return data
