"""Workflow execution endpoints."""
import logging
from datetime import datetime
from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, and_
from sqlalchemy.orm import selectinload
from typing import List, Optional
from uuid import uuid4, UUID

from app.core.database import get_db
from app.api.v1.endpoints.auth import get_current_active_user
from app.models.user import User
from app.models.workflow import WorkflowDefinition, WorkflowStatus
from app.models.execution import WorkflowRun, NodeExecution, RunStatus
from app.schemas import (
    RunCreate,
    RunResponse,
    NodeExecutionResponse,
    RunApprovalRequest,
    RunRejectRequest,
)
from app.services.executor import WorkflowExecutor

router = APIRouter()

logger = logging.getLogger(__name__)


@router.post("/", response_model=RunResponse, status_code=201)
async def create_run(
    run_data: RunCreate,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_active_user)
):
    """
    Start a workflow execution run.

    The run executes synchronously: once the workflow steps finish, the
    response carries the final run status. Long-running executions should be
    moved to a background task/queue later.

    Constraints:
    - Workflow must be APPROVED
    - Only one active run per account
    """
    # Verify workflow exists and is approved
    result = await db.execute(
        select(WorkflowDefinition).where(WorkflowDefinition.id == run_data.workflow_id)
    )
    workflow = result.scalar_one_or_none()

    if not workflow:
        raise HTTPException(status_code=404, detail="Workflow not found")

    if workflow.status != WorkflowStatus.APPROVED:
        raise HTTPException(status_code=400, detail="Only APPROVED workflows can be executed")

    # Check for existing active run for this account
    existing_run_result = await db.execute(
        select(WorkflowRun).where(
            and_(
                WorkflowRun.account_id == run_data.account_id,
                WorkflowRun.status.in_([RunStatus.RUNNING, RunStatus.PAUSED])
            )
        )
    )
    existing_run = existing_run_result.scalar_one_or_none()

    if existing_run:
        raise HTTPException(
            status_code=409,
            detail=f"Account {run_data.account_id} already has an active run: {existing_run.id}"
        )

    run = WorkflowRun(
        id=uuid4(),
        workflow_id=run_data.workflow_id,
        account_id=run_data.account_id,
        status=RunStatus.RUNNING,
        current_step_index=0,
        triggered_by=current_user.id,
        trigger_reason=run_data.trigger_reason
    )

    db.add(run)
    await db.commit()
    await db.refresh(run)

    # Execute the compiled LangGraph with state tracking. On failure the
    # executor marks the run FAILED and we return that row (500 would lose
    # the audit trail).
    executor = WorkflowExecutor()
    try:
        run = await executor.execute(
            workflow_id=run_data.workflow_id,
            input_data=run_data.input_data,
            account_id=run_data.account_id,
            db=db,
            run_id=run.id,
            triggered_by=current_user.id
        )
    except Exception:
        # The run row (and its audit trail) stays; surface WHY it failed.
        logger.exception("Run execution failed for run %s; returning FAILED/partial run row", run.id)
        await db.refresh(run)
        return run

    return run


@router.get("/{run_id}", response_model=RunResponse)
async def get_run(
    run_id: UUID,
    db: AsyncSession = Depends(get_db)
):
    """Get run details."""
    result = await db.execute(
        select(WorkflowRun).where(WorkflowRun.id == run_id)
    )
    run = result.scalar_one_or_none()

    if not run:
        raise HTTPException(status_code=404, detail="Run not found")

    return run


@router.post("/{run_id}/approve", response_model=RunResponse)
async def approve_run(
    run_id: UUID,
    approval_data: Optional[RunApprovalRequest] = None,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_active_user)
):
    """
    Approve a paused workflow run and resume execution from the approval node.

    Constraints:
    - Run must exist (404)
    - Run must be in PAUSED status (400)
    """
    result = await db.execute(
        select(WorkflowRun).where(WorkflowRun.id == run_id)
    )
    run = result.scalar_one_or_none()

    if not run:
        raise HTTPException(status_code=404, detail="Run not found")

    if run.status != RunStatus.PAUSED:
        raise HTTPException(
            status_code=400,
            detail=f"Run {run_id} is not in PAUSED status (current: {run.status})"
        )

    notes = approval_data.notes if approval_data and approval_data.notes else "Approved by human reviewer"
    approver = getattr(current_user, "email", str(current_user.id))
    verdict = {
        "approved": True,
        "approver": approver,
        "notes": notes,
        "timestamp": datetime.utcnow().isoformat()
    }

    executor = WorkflowExecutor()
    try:
        run = await executor.resume(
            run_id=run_id,
            verdict=verdict,
            db=db
        )
    except ValueError as ve:
        # Claim lost in a race (or raced past the pre-check): this request's
        # verdict did not apply — say so instead of pretending success.
        await db.refresh(run)
        raise HTTPException(status_code=409, detail=str(ve))
    except Exception:
        logger.exception("Resume execution failed for run %s; returning FAILED run row", run_id)
        await db.refresh(run)
        return run

    return run


@router.post("/{run_id}/reject", response_model=RunResponse)
async def reject_run(
    run_id: UUID,
    reject_data: Optional[RunRejectRequest] = None,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_active_user)
):
    """
    Reject a paused workflow run, marking it FAILED with a rejection reason.

    Constraints:
    - Run must exist (404)
    - Run must be in PAUSED status (400)
    """
    result = await db.execute(
        select(WorkflowRun).where(WorkflowRun.id == run_id)
    )
    run = result.scalar_one_or_none()

    if not run:
        raise HTTPException(status_code=404, detail="Run not found")

    if run.status != RunStatus.PAUSED:
        raise HTTPException(
            status_code=400,
            detail=f"Run {run_id} is not in PAUSED status (current: {run.status})"
        )

    reason = reject_data.get_reason() if reject_data else "Rejected by user"

    executor = WorkflowExecutor()
    try:
        run = await executor.reject(
            run_id=run_id,
            reason=reason,
            db=db,
            user=current_user
        )
    except ValueError as ve:
        # Claim lost in a race (or raced past the pre-check): this request's
        # rejection did not apply — say so instead of pretending success.
        await db.refresh(run)
        raise HTTPException(status_code=409, detail=str(ve))
    except Exception:
        # The run row carries the outcome; a mid-flight executor failure lands
        # here after refresh and reflects the row as it ended up.
        logger.exception("Rejection execution failed for run %s; returning FAILED run row", run_id)
        await db.refresh(run)
        return run

    return run


@router.get("/{run_id}/executions", response_model=List[NodeExecutionResponse])
async def get_run_executions(
    run_id: UUID,
    db: AsyncSession = Depends(get_db)
):
    """Get all node executions for a run (audit trail)."""
    result = await db.execute(
        select(NodeExecution)
        .where(NodeExecution.run_id == run_id)
        .order_by(NodeExecution.started_at)
    )
    executions = result.scalars().all()

    return executions


@router.post("/{run_id}/replay", response_model=RunResponse, status_code=201)
async def replay_run(
    run_id: UUID,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_active_user)
):
    """
    Replay a completed run for verification.

    - Re-executes all nodes with the original inputs
    - Returns a new run linked via `replayed_from_run_id`
    """
    # Get original run
    result = await db.execute(
        select(WorkflowRun)
        .options(selectinload(WorkflowRun.node_executions))
        .where(WorkflowRun.id == run_id)
    )
    original_run = result.scalar_one_or_none()

    if not original_run:
        raise HTTPException(status_code=404, detail="Run not found")

    if original_run.status != RunStatus.COMPLETED:
        raise HTTPException(status_code=400, detail="Can only replay completed runs")

    executor = WorkflowExecutor()
    replayed = await executor.replay(
        original_run_id=run_id,
        db=db,
        triggered_by=current_user.id
    )
    return replayed
