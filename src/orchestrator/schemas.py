from __future__ import annotations

import uuid
from enum import Enum
from typing import Any, Literal

from pydantic import BaseModel, Field


class QualityCheck(BaseModel):
    """How a node output is validated before we move to the next node."""

    type: Literal["llm_eval", "script", "none"] = "llm_eval"
    criteria: str = ""


class NodeType(str, Enum):
    execute = "execute"   # run tools/LLM to produce output
    reflect = "reflect"   # evaluate prior execute node output for pass/fail
    format = "format"     # synthesize accumulated context into final report
    branch = "branch"     # routing-only node, no execution


class EdgeCondition(str, Enum):
    on_pass = "on_pass"   # traverse when source node passed
    on_fail = "on_fail"   # traverse when source node failed
    default = "default"   # unconditional fallthrough when no conditional edge matched


class GraphNode(BaseModel):
    node_id: str = Field(default_factory=lambda: f"node_{uuid.uuid4().hex[:8]}")
    node_type: NodeType = NodeType.execute
    node_type_ref: str | None = None   # ID of the NodeTypeDef used to create this node
    title: str
    description: str
    tools: list[str] = Field(default_factory=list)
    model: str = ""
    inputs: dict[str, Any] = Field(default_factory=dict)
    quality_check: QualityCheck = Field(default_factory=QualityCheck)


class GraphEdge(BaseModel):
    edge_id: str = Field(default_factory=lambda: f"edge_{uuid.uuid4().hex[:8]}")
    from_node: str
    to_node: str
    condition: EdgeCondition = EdgeCondition.default
    max_traversals: int = Field(default=3, ge=0)  # 0 = unlimited


class ExecutionPlan(BaseModel):
    plan_id: str = Field(default_factory=lambda: uuid.uuid4().hex)
    blueprint_type: str
    objective: str
    nodes: list[GraphNode]
    edges: list[GraphEdge] = Field(default_factory=list)
    entry_node: str = ""  # node_id; if empty, use nodes[0]
    output_format: str = "markdown_report"
    llm_modification_rationale: str = ""  # LLM fills this to explain structural changes
    owner: str | None = None
    session_id: str | None = None


class NodeTypeDef(BaseModel):
    """Reusable named node template stored in the node type library."""

    type_id: str = Field(default_factory=lambda: f"nt_{uuid.uuid4().hex[:8]}")
    name: str                                          # human display name
    classification: Literal["execute", "branch"] = "execute"
    tools: list[str] = Field(default_factory=list)    # for execute classification
    model: str = ""
    quality_check_type: Literal["llm_eval", "script", "none"] = "llm_eval"  # for branch


class NodeResult(BaseModel):
    node_id: str
    status: Literal["pending", "running", "passed", "failed", "skipped"]
    output: str = ""
    error: str = ""
    attempts: int = 0


# Backward-compatible alias so existing serialised data and imports still work
StepResult = NodeResult


class DispatchRequest(BaseModel):
    plan: ExecutionPlan


class DispatchResponse(BaseModel):
    run_id: str
    status: Literal["queued"] = "queued"
