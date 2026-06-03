"""CRUD routes for the reusable node type library."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Optional

from fastapi import APIRouter, HTTPException, Request

from core.atomic_io import atomic_write_json
from src.auth_helpers import get_current_user
from src.orchestrator.schemas import NodeTypeDef

_DATA_DIR = Path("data")
_NODE_TYPES_PATH = _DATA_DIR / "node_types.json"


def _load_node_types() -> list[dict]:
    try:
        if not _NODE_TYPES_PATH.exists():
            return []
        raw = json.loads(_NODE_TYPES_PATH.read_text(encoding="utf-8"))
        return [x for x in raw if isinstance(x, dict)] if isinstance(raw, list) else []
    except Exception:
        return []


def _save_node_types(defs: list[dict]) -> None:
    _DATA_DIR.mkdir(parents=True, exist_ok=True)
    atomic_write_json(str(_NODE_TYPES_PATH), defs, indent=2)


def setup_node_type_routes() -> APIRouter:
    router = APIRouter(prefix="/api/node-types", tags=["node-types"])

    def _owner(request: Request) -> Optional[str]:
        return get_current_user(request)

    @router.get("")
    def list_node_types(request: Request):
        _ = _owner(request)
        return {"node_types": _load_node_types()}

    @router.put("/{type_id}")
    def upsert_node_type(type_id: str, request: Request, body: NodeTypeDef):
        _ = _owner(request)
        if not type_id or type_id != body.type_id:
            raise HTTPException(400, "type_id in URL must match body.type_id")
        defs = _load_node_types()
        kept = [d for d in defs if d.get("type_id") != type_id]
        entry = body.model_dump()
        kept.append(entry)
        _save_node_types(kept)
        return entry

    @router.delete("/{type_id}")
    def delete_node_type(type_id: str, request: Request):
        _ = _owner(request)
        defs = _load_node_types()
        updated = [d for d in defs if d.get("type_id") != type_id]
        if len(updated) == len(defs):
            raise HTTPException(404, "Node type not found")
        _save_node_types(updated)
        return {"deleted": type_id}

    return router
