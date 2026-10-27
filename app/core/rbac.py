"""
Role-based access control (RBAC) dependencies.

Roles live in ``roles``/``user_roles``; ``require_roles(...)`` turns them into a
FastAPI dependency that raises 403 unless the caller holds one of the required
roles. ``admin`` is a superuser and always passes. Users with no role are
denied every guarded endpoint (no implicit/auto-assigned roles).

Role semantics (the endpoint matrix is documented in README.md):

- ``author``   — drafts workflows, authors rules, uploads knowledge documents.
- ``approver`` — approves workflow definitions and human-gated runs.
- ``executor`` — starts and replays workflow runs.
- ``auditor``  — read-only access to definitions, runs, and audit trails.
- ``admin``    — superuser; passes every role check.

Role names are compared case-insensitively.
"""
from typing import Annotated, Callable

from fastapi import Depends, HTTPException, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.api.v1.endpoints.auth import get_current_active_user
from app.core.database import get_db
from app.models.user import User, UserRole

ADMIN_ROLE = "admin"


def require_roles(*role_names: str) -> Callable[..., User]:
    """Build a dependency allowing active users with any of ``role_names``.

    The returned dependency loads ``User.user_roles`` → ``UserRole.role`` →
    ``Role.name`` eagerly (one user query + the selectinload query) instead of
    touching the relationships lazily, so guarding an endpoint adds no N+1
    query and works under async SQLAlchemy.
    """
    required = {name.strip().lower() for name in role_names if name and name.strip()}
    if not required:
        raise ValueError("require_roles() requires at least one role name")

    async def role_checker(
        current_user: Annotated[User, Depends(get_current_active_user)],
        db: Annotated[AsyncSession, Depends(get_db)],
    ) -> User:
        result = await db.execute(
            select(User)
            .options(selectinload(User.user_roles).selectinload(UserRole.role))
            .where(User.id == current_user.id)
        )
        user = result.scalar_one_or_none() or current_user

        granted = {
            link.role.name.strip().lower()
            for link in user.user_roles
            if link.role is not None and link.role.name
        }

        if ADMIN_ROLE in granted or granted & required:
            return user

        # Deliberately generic: the caller does not need to learn which roles
        # would have been sufficient.
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Insufficient permissions for this action",
        )

    return role_checker
