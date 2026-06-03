from __future__ import annotations

from abc import ABC, abstractmethod

from src.orchestrator.schemas import EdgeCondition, ExecutionPlan, GraphEdge, GraphNode


class BlueprintTemplate(ABC):
    name: str
    display_name: str
    description: str

    @abstractmethod
    def template_nodes(self) -> list[GraphNode]:
        raise NotImplementedError

    def template_edges(self) -> list[GraphEdge]:
        """Return default linear edges connecting template nodes in order."""
        nodes = self.template_nodes()
        edges: list[GraphEdge] = []
        for i in range(len(nodes) - 1):
            edges.append(GraphEdge(
                from_node=nodes[i].node_id,
                to_node=nodes[i + 1].node_id,
                condition=EdgeCondition.default,
            ))
        return edges

    def to_dict(self) -> dict:
        return {
            "name": self.name,
            "display_name": self.display_name,
            "description": self.description,
            "nodes": [n.model_dump() for n in self.template_nodes()],
            "edges": [e.model_dump() for e in self.template_edges()],
        }

    def fill(self, objective: str, owner: str | None = None, session_id: str | None = None) -> ExecutionPlan:
        nodes = self.template_nodes()
        edges = self.template_edges()
        return ExecutionPlan(
            blueprint_type=self.name,
            objective=objective,
            nodes=nodes,
            edges=edges,
            entry_node=nodes[0].node_id if nodes else "",
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
        nodes: list[GraphNode] | None = None,
        edges: list[GraphEdge] | None = None,
    ) -> None:
        self.name = (name or "").strip().lower()
        self.display_name = (display_name or name or "").strip()
        self.description = (description or "").strip()
        self._nodes = [n.model_copy(deep=True) for n in (nodes or [])]
        self._edges = [e.model_copy(deep=True) for e in (edges or [])]

    def template_nodes(self) -> list[GraphNode]:
        return [n.model_copy(deep=True) for n in self._nodes]

    def template_edges(self) -> list[GraphEdge]:
        if self._edges:
            return [e.model_copy(deep=True) for e in self._edges]
        return super().template_edges()

    def to_dict(self) -> dict:
        data = super().to_dict()
        data["name"] = self.name
        data["display_name"] = self.display_name
        data["description"] = self.description
        return data
