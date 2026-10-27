"""Workflow definition endpoints."""
from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select
from sqlalchemy.orm import selectinload
from typing import List
from uuid import uuid4, UUID

from app.core.database import get_db
from app.core.rbac import require_roles
from app.api.v1.endpoints.auth import get_current_active_user
from app.models.user import User
from app.models.workflow import WorkflowDefinition, WorkflowStep, WorkflowStatus
from app.schemas import WorkflowCreate, WorkflowResponse

router = APIRouter()


@router.post("/draft", response_model=WorkflowResponse, status_code=201)
async def create_draft_workflow(
    workflow_data: WorkflowCreate,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(require_roles("author"))
):
    """
    Create a draft workflow definition.

    - Requires the author role (admin bypasses)
    - Status: DRAFT
    - Mutable until approved
    """
    user_id = current_user.id
    
    workflow = WorkflowDefinition(
        id=uuid4(),
        name=workflow_data.name,
        description=workflow_data.description,
        domain=workflow_data.domain,
        version=1,
        status=WorkflowStatus.DRAFT,
        created_by=user_id
    )
    
    db.add(workflow)
    
    # Add steps
    for step_data in workflow_data.steps:
        step = WorkflowStep(
            id=uuid4(),
            workflow_id=workflow.id,
            order_index=step_data.order_index,
            step_type=step_data.step_type,
            name=step_data.name,
            description=step_data.description,
            config_json=step_data.config_json
        )
        db.add(step)
    
    await db.commit()
    await db.refresh(workflow)
    
    # Reload with steps
    result = await db.execute(
        select(WorkflowDefinition)
        .options(selectinload(WorkflowDefinition.steps))
        .where(WorkflowDefinition.id == workflow.id)
    )
    workflow_with_steps = result.scalar_one()
    
    return workflow_with_steps


@router.get("/", response_model=List[WorkflowResponse])
async def list_workflows(
    domain: str = None,
    status: WorkflowStatus = None,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_active_user)
):
    """List workflows with optional filters (any authenticated user)."""
    query = select(WorkflowDefinition).options(selectinload(WorkflowDefinition.steps))
    
    if domain:
        query = query.where(WorkflowDefinition.domain == domain)
    if status:
        query = query.where(WorkflowDefinition.status == status)
    
    result = await db.execute(query)
    workflows = result.scalars().all()
    return workflows


@router.get("/{workflow_id}", response_model=WorkflowResponse)
async def get_workflow(
    workflow_id: UUID,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_active_user)
):
    """Get workflow by ID with all steps (any authenticated user)."""
    result = await db.execute(
        select(WorkflowDefinition)
        .options(selectinload(WorkflowDefinition.steps))
        .where(WorkflowDefinition.id == workflow_id)
    )
    workflow = result.scalar_one_or_none()
    
    if not workflow:
        raise HTTPException(status_code=404, detail="Workflow not found")
    
    return workflow


@router.post("/{workflow_id}/approve", response_model=WorkflowResponse)
async def approve_workflow(
    workflow_id: UUID,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(require_roles("approver"))
):
    """
    Approve a workflow definition.

    - Requires the approver role (admin bypasses)
    - Changes status to APPROVED
    - Workflow becomes immutable
    - Sets approved_by and approved_at
    """
    result = await db.execute(
        select(WorkflowDefinition)
        .options(selectinload(WorkflowDefinition.steps))
        .where(WorkflowDefinition.id == workflow_id)
    )
    workflow = result.scalar_one_or_none()
    
    if not workflow:
        raise HTTPException(status_code=404, detail="Workflow not found")
    
    if workflow.status != WorkflowStatus.DRAFT:
        raise HTTPException(status_code=400, detail="Only DRAFT workflows can be approved")
    
    user_id = current_user.id
    
    from datetime import datetime
    workflow.status = WorkflowStatus.APPROVED
    workflow.approved_by = user_id
    workflow.approved_at = datetime.utcnow()
    
    await db.commit()
    await db.refresh(workflow)
    
    return workflow
