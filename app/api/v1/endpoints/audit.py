"""Audit log read endpoints."""
import logging
from typing import List, Optional
from uuid import UUID

from fastapi import APIRouter, Depends, Query
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.database import get_db
from app.core.rbac import require_roles
from app.models.execution import AuditLog
from app.models.user import User
from app.schemas import AuditLogResponse

router = APIRouter()

logger = logging.getLogger(__name__)


@router.get("", response_model=List[AuditLogResponse])
async def list_audit_logs(
    entity_type: Optional[str] = None,
    entity_id: Optional[UUID] = None,
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0),
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(require_roles("auditor", "admin"))
):
    """
    List audit log entries newest-first (auditor or admin role required).

    Filters:
    - ``entity_type``: e.g. ``run``, ``node``, ``workflow``
    - ``entity_id``: the audited entity's id

    ``limit`` caps the page (default 50, max 200) and ``offset`` pages through
    the newest-first ordering (timestamp desc, id desc as tiebreak).
    """
    query = select(AuditLog)

    if entity_type:
        query = query.where(AuditLog.entity_type == entity_type)
    if entity_id:
        query = query.where(AuditLog.entity_id == entity_id)

    query = query.order_by(
        AuditLog.timestamp.desc(), AuditLog.id.desc()
    ).limit(limit).offset(offset)

    result = await db.execute(query)
    return result.scalars().all()
