"""Routes for orchestrator plan dispatch and run monitoring."""

from __future__ import annotations

import json

from datetime import datetime
from typing import Any, Optional

from fastapi import APIRouter, HTTPException, Query, Request

from core.database import OrchestratorRun, SessionLocal
from src.auth_helpers import get_current_user
from src.orchestrator import blueprints as blueprint_store
from src.orchestrator import storage
from src.orchestrator.planner import clear_planning_session
from src.orchestrator.schemas import DispatchRequest, DispatchResponse


def _run_to_dict(run: OrchestratorRun) -> dict[str, Any]:
    try:
        results = json.loads(run.results_json) if run.results_json else []
    except Exception:
        results = []
    try:
        plan_raw = json.loads(run.plan_json) if run.plan_json else {}
    except Exception:
        plan_raw = {}
    plan = {
        "plan_id": plan_raw.get("plan_id"),
        "blueprint_type": plan_raw.get("blueprint_type"),
        "objective": plan_raw.get("objective"),
        "entry_node": plan_raw.get("entry_node", ""),
        "nodes": plan_raw.get("nodes") if isinstance(plan_raw.get("nodes"), list) else [],
        "edges": plan_raw.get("edges") if isinstance(plan_raw.get("edges"), list) else [],
    }
    return {
        "id": run.id,
        "owner": run.owner,
        "session_id": run.session_id,
        "status": run.status,
        "current_step_index": run.current_step_index or 0,
        "plan": plan,
        "results": results,
        "final_output": run.final_output,
        "error_message": run.error_message,
        "created_at": run.created_at.isoformat() + "Z" if run.created_at else None,
        "updated_at": run.updated_at.isoformat() + "Z" if run.updated_at else None,
    }


def setup_orchestrator_routes() -> APIRouter:
    router = APIRouter(prefix="/api/orchestrator", tags=["orchestrator"])

    def _owner(request: Request) -> Optional[str]:
        return get_current_user(request)

    @router.post("/dispatch", response_model=DispatchResponse)
    async def dispatch(request: Request, body: DispatchRequest):
        owner = _owner(request)
        plan = body.plan.model_copy(deep=True)
        plan.owner = owner
        if not plan.session_id:
            plan.session_id = None

        run = storage.create_run(plan)
        if plan.session_id:
            clear_planning_session(plan.session_id)
        return DispatchResponse(run_id=run.id, status="queued")

    @router.get("/blueprints")
    async def list_blueprints(request: Request):
        _ = _owner(request)
        return {"blueprints": blueprint_store.list_blueprint_dicts()}

    @router.put("/blueprints/{blueprint_name}")
    async def upsert_blueprint(request: Request, blueprint_name: str, body: dict[str, Any]):
        _ = _owner(request)
        name = (blueprint_name or "").strip().lower()
        if not name:
            raise HTTPException(400, "Blueprint name is required")

        payload = dict(body or {})
        payload["name"] = name
        if not payload.get("display_name"):
            payload["display_name"] = name

        try:
            saved = blueprint_store.upsert_custom_blueprint(payload)
        except ValueError as e:
            raise HTTPException(400, str(e))
        except Exception:
            raise HTTPException(500, "Failed to save blueprint")
        return {"ok": True, "blueprint": saved}

    @router.delete("/blueprints/{blueprint_name}")
    async def delete_blueprint(request: Request, blueprint_name: str):
        _ = _owner(request)
        name = (blueprint_name or "").strip().lower()
        if not name:
            raise HTTPException(400, "Blueprint name is required")
        deleted = blueprint_store.delete_custom_blueprint(name)
        if not deleted:
            raise HTTPException(404, "Blueprint not found")
        return {"ok": True}

    @router.get("/runs/{run_id}")
    async def get_run(request: Request, run_id: str):
        owner = _owner(request)
        db = SessionLocal()
        try:
            run = db.query(OrchestratorRun).filter(OrchestratorRun.id == run_id).first()
            if not run:
                raise HTTPException(404, "Run not found")
            if owner is not None and run.owner != owner:
                raise HTTPException(404, "Run not found")
            return _run_to_dict(run)
        finally:
            db.close()

    @router.get("/runs")
    async def list_runs(
        request: Request,
        limit: int = Query(20, ge=1, le=200),
        status: Optional[str] = Query(None),
    ):
        owner = _owner(request)
        db = SessionLocal()
        try:
            q = db.query(OrchestratorRun)
            if owner is not None:
                q = q.filter(OrchestratorRun.owner == owner)
            if status:
                q = q.filter(OrchestratorRun.status == status)
            rows = q.order_by(OrchestratorRun.created_at.desc()).limit(limit).all()
            return {"runs": [_run_to_dict(r) for r in rows]}
        finally:
            db.close()

    @router.post("/runs/{run_id}/cancel")
    async def cancel_run(request: Request, run_id: str):
        owner = _owner(request)
        db = SessionLocal()
        try:
            run = db.query(OrchestratorRun).filter(OrchestratorRun.id == run_id).first()
            if not run:
                raise HTTPException(404, "Run not found")
            if owner is not None and run.owner != owner:
                raise HTTPException(404, "Run not found")
            if run.status not in {"queued", "running"}:
                return {"ok": True, "status": run.status}

            run.status = "cancelled"
            run.error_message = "Cancelled by user"
            run.updated_at = datetime.utcnow()
            db.commit()
            return {"ok": True, "status": run.status}
        finally:
            db.close()

    return router
