from __future__ import annotations

import json
import logging
import uuid
from datetime import datetime

from core.database import ChatMessage as DbChatMessage
from core.database import OrchestratorRun, Session as DbSession, SessionLocal
from src.orchestrator.schemas import ExecutionPlan, StepResult

logger = logging.getLogger(__name__)


def create_run(plan: ExecutionPlan) -> OrchestratorRun:
    db = SessionLocal()
    try:
        run = OrchestratorRun(
            owner=plan.owner,
            session_id=plan.session_id,
            plan_json=plan.model_dump_json(),
            status="queued",
            current_step_index=0,
            results_json=json.dumps([]),
        )
        db.add(run)
        db.commit()
        db.refresh(run)
        return run
    finally:
        db.close()


def get_run(run_id: str) -> OrchestratorRun | None:
    db = SessionLocal()
    try:
        return db.query(OrchestratorRun).filter(OrchestratorRun.id == run_id).first()
    finally:
        db.close()


def update_run_status(run_id: str, status: str, error_message: str | None = None) -> None:
    db = SessionLocal()
    try:
        run = db.query(OrchestratorRun).filter(OrchestratorRun.id == run_id).first()
        if not run:
            return
        run.status = status
        run.updated_at = datetime.utcnow()
        if error_message is not None:
            run.error_message = error_message
        db.commit()
    finally:
        db.close()


def parse_plan(run: OrchestratorRun) -> ExecutionPlan:
    return ExecutionPlan.model_validate_json(run.plan_json)


def parse_results_json(results_json: str | None) -> list[StepResult]:
    if not results_json:
        return []
    try:
        raw = json.loads(results_json)
        return [StepResult.model_validate(r) for r in raw]
    except Exception:
        return []


def persist_progress(
    run_id: str,
    *,
    status: str | None = None,
    current_step_index: int | None = None,
    results: list[StepResult] | None = None,
    final_output: str | None = None,
    error_message: str | None = None,
) -> None:
    db = SessionLocal()
    try:
        run = db.query(OrchestratorRun).filter(OrchestratorRun.id == run_id).first()
        if not run:
            return
        if status is not None:
            run.status = status
        if current_step_index is not None:
            run.current_step_index = current_step_index
        if results is not None:
            run.results_json = json.dumps([r.model_dump() for r in results])
        if final_output is not None:
            run.final_output = final_output
        if error_message is not None:
            run.error_message = error_message
        run.updated_at = datetime.utcnow()
        db.commit()
    finally:
        db.close()


def append_completion_to_session_chat(
    run_id: str,
    *,
    session_id: str | None,
    objective: str,
    blueprint_type: str,
    final_output: str,
    report_output: str | None = None,
) -> None:
    """Write the orchestrator final output into its source chat session once.

    Idempotency is enforced by checking for an existing assistant message that
    carries this run id in structured metadata.
    """
    sid = str(session_id or "").strip()
    body = str(final_output or "").strip()
    report = str(report_output or "").strip()
    if not sid or not body:
        return

    db = SessionLocal()
    try:
        run_key = str(run_id or "").strip()
        if not run_key:
            return

        existing = db.query(DbChatMessage).filter(
            DbChatMessage.session_id == sid,
            DbChatMessage.role == "assistant",
            DbChatMessage.meta_data.like(f'%"orchestrator_run_id":"{run_key}"%'),
        ).first()
        if existing:
            return

        now = datetime.utcnow()
        metadata = {
            "source": "orchestrator",
            "orchestrator_run_id": run_key,
            "objective": objective,
            "blueprint_type": blueprint_type,
            "orchestrator_report": report,
            "orchestrator_raw_output": body,
            "posted_at": now.isoformat(),
        }
        content = report or (
            "## Orchestrator Report\n\n"
            f"### Objective\n{objective or 'n/a'}\n\n"
            f"### Blueprint\n{blueprint_type or 'n/a'}\n\n"
            f"### Findings\n{body}"
        )
        db.add(DbChatMessage(
            id=str(uuid.uuid4()),
            session_id=sid,
            role="assistant",
            content=content,
            meta_data=json.dumps(metadata, separators=(",", ":")),
            timestamp=now,
        ))

        db_session = db.query(DbSession).filter(DbSession.id == sid).first()
        if db_session:
            db_session.message_count = int(db_session.message_count or 0) + 1
            db_session.last_accessed = now
            db_session.last_message_at = now

        db.commit()
    except Exception as e:
        db.rollback()
        logger.warning("Failed to post orchestrator completion to chat run_id=%s session_id=%s: %s", run_id, sid, e)
    finally:
        db.close()
