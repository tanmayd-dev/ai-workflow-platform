"""
Idempotent seed data creation.

Creates the five RBAC roles (get-or-create), the sample admin user, and grants
that user the ``admin`` role. Re-running the script is safe: existing rows are
reused and nothing is duplicated.
"""
import sys
import os

# Add repository root to path
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import asyncio
from uuid import uuid4
from sqlalchemy import func, select
from app.core.database import get_session_maker
from app.core.security import get_password_hash
from app.models.user import User, Role, UserRole

ADMIN_EMAIL = "admin@acmebank.com"
ADMIN_PASSWORD = "admin123"

ROLES = {
    "admin": "Superuser; bypasses every role check",
    "author": "Drafts workflows, authors rules, uploads knowledge documents",
    "approver": "Approves workflow definitions and human-gated runs",
    "executor": "Starts and replays workflow runs",
    "auditor": "Read-only access to definitions, runs, and audit trails",
}


async def get_or_create_role(db, name: str, description: str) -> Role:
    """Return the role with this name (case-insensitive), creating it if absent."""
    result = await db.execute(
        select(Role).where(func.lower(Role.name) == name.lower())
    )
    role = result.scalar_one_or_none()
    if role is None:
        role = Role(id=uuid4(), name=name, description=description)
        db.add(role)
        await db.flush()
        print(f"  + role '{name}'")
    else:
        print(f"  = role '{role.name}' already exists")
    return role


async def get_or_create_admin_user(db) -> User:
    """Return the sample admin user, creating it with the documented password if absent."""
    result = await db.execute(select(User).where(User.email == ADMIN_EMAIL))
    user = result.scalar_one_or_none()
    if user is None:
        user = User(
            id=uuid4(),
            email=ADMIN_EMAIL,
            hashed_password=get_password_hash(ADMIN_PASSWORD),
            full_name="Admin User",
            is_active=True,
        )
        db.add(user)
        await db.flush()
        print(f"  + user '{ADMIN_EMAIL}'")
    else:
        print(f"  = user '{ADMIN_EMAIL}' already exists (credentials unchanged)")
    return user


async def main():
    """Create roles, the admin user, and the admin role assignment."""
    print("Creating seed data...")

    async_session_maker = get_session_maker()
    async with async_session_maker() as db:
        try:
            print("  Ensuring roles...")
            roles = {}
            for name, description in ROLES.items():
                roles[name] = await get_or_create_role(db, name, description)

            print("  Ensuring admin user...")
            admin_user = await get_or_create_admin_user(db)

            print("  Ensuring admin role assignment...")
            existing_link = await db.execute(
                select(UserRole).where(
                    UserRole.user_id == admin_user.id,
                    UserRole.role_id == roles["admin"].id,
                )
            )
            if existing_link.scalar_one_or_none() is None:
                db.add(UserRole(
                    id=uuid4(),
                    user_id=admin_user.id,
                    role_id=roles["admin"].id,
                ))
                print("  + granted 'admin' to the admin user")
            else:
                print("  = admin user already has 'admin'")

            await db.commit()

            print("\n✓ Seed data ready!")
            print("\nRoles: " + ", ".join(sorted(roles)))
            print("\nSample User:")
            print(f"  - {ADMIN_EMAIL} / {ADMIN_PASSWORD} (admin)")
            print("\nGrant more roles with: python scripts/assign_role.py --email <email> --role <role>")

        except Exception as e:
            print(f"\n✗ Error creating seed data: {e}")
            import traceback
            traceback.print_exc()
            await db.rollback()
            raise

if __name__ == "__main__":
    asyncio.run(main())
