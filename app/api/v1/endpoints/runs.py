"""Workflow execution endpoints."""
import logging
from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, and_
from sqlalchemy.orm import selectinload
from typing import List
from uuid import uuid4, UUID

from app.core.database import get_db
from app.api.v1.endpoints.auth import get_current_active_user
from app.models.user import User
from app.models.workflow import WorkflowDefinition, WorkflowStatus
from app.models.execution import WorkflowRun, NodeExecution, RunStatus
from app.schemas import RunCreate, RunResponse, NodeExecutionResponse
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
