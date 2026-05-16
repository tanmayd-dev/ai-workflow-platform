"""Workflow execution endpoints."""
from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, and_
from sqlalchemy.orm import selectinload
from typing import List
from uuid import uuid4, UUID

from app.core.database import get_db
from app.models.workflow import WorkflowDefinition, WorkflowStatus
from app.models.execution import WorkflowRun, NodeExecution, RunStatus
from app.schemas import RunCreate, RunResponse, NodeExecutionResponse

router = APIRouter()


@router.post("/", response_model=RunResponse, status_code=201)
async def create_run(
    run_data: RunCreate,
    db: AsyncSession = Depends(get_db)
):
    """
    Start a workflow execution run.
    
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
    
    # TODO: Get user from auth
    user_id = uuid4()
    
    # Create run
    run = WorkflowRun(
        id=uuid4(),
        workflow_id=run_data.workflow_id,
        account_id=run_data.account_id,
        status=RunStatus.RUNNING,
        current_step_index=0,
        triggered_by=user_id,
        trigger_reason=run_data.trigger_reason
    )
    
    db.add(run)
    await db.commit()
    await db.refresh(run)
    
    # TODO: Trigger actual LangGraph execution
    # For now, just return the created run
    
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
    db: AsyncSession = Depends(get_db)
):
    """
    Replay a completed run for verification.
    
    - Creates new run with same inputs
    - Re-executes all nodes
    - Compares outputs with original
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
    
    # TODO: Get user from auth
    user_id = uuid4()
    
    # Create replay run
    replay_run = WorkflowRun(
        id=uuid4(),
        workflow_id=original_run.workflow_id,
        account_id=original_run.account_id,
        status=RunStatus.RUNNING,
        current_step_index=0,
        triggered_by=user_id,
        trigger_reason=f"Replay of run {run_id}"
    )
    
    db.add(replay_run)
    await db.commit()
    await db.refresh(replay_run)
    
    # TODO: Trigger LangGraph execution with replay flag
    
    return replay_run
