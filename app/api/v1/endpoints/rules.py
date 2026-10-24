"""Rule management endpoints."""
import logging
from typing import List, Optional
from uuid import UUID, uuid4

from fastapi import APIRouter, Depends, HTTPException, Response, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.api.v1.endpoints.auth import get_current_active_user
from app.core.database import get_db
from app.models.rule import Rule, RuleVersion
from app.models.user import User
from app.schemas import (
    RuleCreate,
    RuleResponse,
    RuleVersionCreate,
    RuleVersionResponse,
)

logger = logging.getLogger(__name__)

router = APIRouter()


@router.post("", response_model=RuleResponse, status_code=status.HTTP_201_CREATED)
@router.post("/", response_model=RuleResponse, status_code=status.HTTP_201_CREATED, include_in_schema=False)
async def create_rule(
    rule_data: RuleCreate,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_active_user),
):
    """
    Create a new rule and its initial version (v1).

    - Requires JWT authentication
    - Body carries name, description, domain, and logic_json (or initial_logic)
    - Creates Rule + RuleVersion (version_number=1, is_active=True, created_by=current user id)
    - Returns 409 Conflict if a rule with this name already exists in the domain
    """
    # Check for existing rule with same name
    existing_result = await db.execute(
        select(Rule).where(Rule.name == rule_data.name)
    )
    existing_rule = existing_result.scalar_one_or_none()
    if existing_rule:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"Rule with name '{rule_data.name}' already exists in domain '{existing_rule.domain}'",
        )

    rule = Rule(
        id=uuid4(),
        name=rule_data.name,
        description=rule_data.description,
        domain=rule_data.domain,
    )
    db.add(rule)

    version = RuleVersion(
        id=uuid4(),
        rule_id=rule.id,
        version_number=1,
        logic_json=rule_data.logic_json,
        is_active=True,
        created_by=current_user.id,
    )
    db.add(version)

    await db.commit()

    # Reload rule with its versions eagerly loaded
    result = await db.execute(
        select(Rule)
        .options(selectinload(Rule.versions))
        .where(Rule.id == rule.id)
    )
    created_rule = result.scalar_one()
    return created_rule


@router.get("", response_model=List[RuleResponse])
@router.get("/", response_model=List[RuleResponse], include_in_schema=False)
async def list_rules(
    domain: Optional[str] = None,
    db: AsyncSession = Depends(get_db),
):
    """
    List rules including their versions.
    Optionally filter by domain.
    """
    query = select(Rule).options(selectinload(Rule.versions)).order_by(Rule.created_at.desc())
    if domain:
        query = query.where(Rule.domain == domain)

    result = await db.execute(query)
    return result.scalars().all()


@router.get("/{rule_id}", response_model=RuleResponse)
async def get_rule(
    rule_id: UUID,
    db: AsyncSession = Depends(get_db),
):
    """
    Get a single rule by ID including its versions.
    """
    result = await db.execute(
        select(Rule)
        .options(selectinload(Rule.versions))
        .where(Rule.id == rule_id)
    )
    rule = result.scalar_one_or_none()
    if not rule:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Rule with id '{rule_id}' not found",
        )
    return rule


@router.post(
    "/{rule_id}/versions",
    response_model=RuleVersionResponse,
    status_code=status.HTTP_201_CREATED,
)
async def create_rule_version(
    rule_id: UUID,
    version_data: RuleVersionCreate,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_active_user),
):
    """
    Add a new version for an existing rule.

    - Requires JWT authentication
    - Assigns incremented version_number and is_active=True
    - Flips all older versions to is_active=False (only one active version per rule)
    """
    result = await db.execute(
        select(Rule)
        .options(selectinload(Rule.versions))
        .where(Rule.id == rule_id)
    )
    rule = result.scalar_one_or_none()
    if not rule:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Rule with id '{rule_id}' not found",
        )

    current_versions = rule.versions or []
    max_version = max((v.version_number for v in current_versions), default=0)
    next_version = max_version + 1

    # Only one active version per rule: flip older active versions to inactive
    for v in current_versions:
        if v.is_active:
            v.is_active = False

    new_version = RuleVersion(
        id=uuid4(),
        rule_id=rule.id,
        version_number=next_version,
        logic_json=version_data.logic_json,
        is_active=True,
        created_by=current_user.id,
    )
    db.add(new_version)

    await db.commit()
    await db.refresh(new_version)
    return new_version


@router.delete("/{rule_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_rule(
    rule_id: UUID,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_active_user),
):
    """
    Delete a rule and all of its versions.

    Explicit deletion strategy:
    Hard DELETE of the Rule record, cascading to all associated RuleVersion records.
    Requires authenticated user (JWT).
    """
    result = await db.execute(
        select(Rule)
        .options(selectinload(Rule.versions))
        .where(Rule.id == rule_id)
    )
    rule = result.scalar_one_or_none()
    if not rule:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Rule with id '{rule_id}' not found",
        )

    await db.delete(rule)
    await db.commit()
    return Response(status_code=status.HTTP_204_NO_CONTENT)
