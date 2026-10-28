"""Workflow definition endpoints."""
import logging
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select
from sqlalchemy.orm import selectinload
from typing import List, Optional
from uuid import uuid4, UUID

from app.core.database import get_db
from app.core.rbac import require_roles
from app.api.v1.endpoints.auth import get_current_active_user
from app.models.user import User
from app.models.execution import AuditLog
from app.models.workflow import (
    WorkflowDefinition,
    WorkflowStatus,
    WorkflowStep,
)
from app.schemas import WorkflowCreate, WorkflowResponse, WorkflowStepCreate
from app.services.workflow_validator import validate_workflow_for_approval

logger = logging.getLogger(__name__)

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
    
    # Validate BEFORE flipping the status: a broken definition (no steps,
    # duplicate order_index/names, unresolvable rule references, bad branch
    # wiring) must fail the gate here, not on its first execution.
    try:
        await validate_workflow_for_approval(workflow, db)
    except ValueError as exc:
        # The workflow stays DRAFT: nothing below this point mutated it.
        logger.warning(
            "Workflow %s failed approval validation by %s: %s", workflow.id, user_id, exc
        )
        raise HTTPException(
            status_code=422,
            detail=f"Workflow does not pass approval validation: {exc}",
        )
    
    from datetime import datetime
    workflow.status = WorkflowStatus.APPROVED
    workflow.approved_by = user_id
    workflow.approved_at = datetime.utcnow()
    
    await db.commit()
    await db.refresh(workflow)
    
    return workflow


class WorkflowUpdate(BaseModel):
    """Payload for PATCH /workflows/{id} (partial edit of a DRAFT workflow).

    Every field is optional; omitted fields are left untouched. ``steps``, when
    present, is a full replacement using the same WorkflowStepCreate shape as
    POST /draft. Defined locally (not in app.schemas) to keep the P1b change
    area isolated.
    """

    name: Optional[str] = None
    description: Optional[str] = None
    steps: Optional[List[WorkflowStepCreate]] = None


@router.patch("/{workflow_id}", response_model=WorkflowResponse)
async def update_draft_workflow(
    workflow_id: UUID,
    workflow_data: WorkflowUpdate,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(require_roles("author"))
):
    """
    Edit a DRAFT workflow definition.

    - Requires the author role (admin bypasses)
    - Optional full replacement of name, description, and steps (the same
      WorkflowStepCreate shape as POST /draft); omitted fields are untouched
    - Only permitted while status == DRAFT (409 otherwise)

    RULING (P1b): the PATCH path reuses the approval validator as a pre-check
    and returns 422 on failure, so a draft can never be edited into a broken
    state that would later fail (or mis-pass) the approval gate.
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
        raise HTTPException(status_code=409, detail="Only DRAFT workflows can be edited")

    user_id = current_user.id

    # Apply the edits on the managed instance first, then validate the
    # resulting definition. On a validation failure we roll back so neither
    # the field edits nor the step replacement survive the request.
    if workflow_data.name is not None:
        workflow.name = workflow_data.name
    if workflow_data.description is not None:
        workflow.description = workflow_data.description

    if workflow_data.steps is not None:
        workflow.steps = [
            WorkflowStep(
                id=uuid4(),
                workflow_id=workflow.id,
                order_index=step_data.order_index,
                step_type=step_data.step_type,
                name=step_data.name,
                description=step_data.description,
                config_json=step_data.config_json,
            )
            for step_data in workflow_data.steps
        ]

    try:
        await validate_workflow_for_approval(workflow, db)
    except ValueError as exc:
        # Log and capture context BEFORE the rollback: rollback expires the
        # session-managed instances and touching workflow.id afterwards would
        # trigger a synchronous refresh inside the async response path.
        logger.warning(
            "Draft workflow %s edit by %s rejected: %s", workflow.id, user_id, exc
        )
        await db.rollback()
        raise HTTPException(
            status_code=422,
            detail=f"Workflow does not pass edit validation: {exc}",
        )

    db.add(
        AuditLog(
            entity_type="workflow",
            entity_id=workflow.id,
            action="updated",
            actor_id=user_id,
            actor_role="author",
            details={
                "fields": workflow_data.model_dump(exclude_unset=True),
            },
        )
    )

    await db.commit()
    await db.refresh(workflow)

    # Reload with the replacement steps so the response carries them.
    result = await db.execute(
        select(WorkflowDefinition)
        .options(selectinload(WorkflowDefinition.steps))
        .where(WorkflowDefinition.id == workflow.id)
    )
    return result.scalar_one()
