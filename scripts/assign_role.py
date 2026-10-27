"""
Grant or revoke an RBAC role for a user without writing SQL.

Usage:
    python scripts/assign_role.py --email alice@acmebank.com --role author
    python scripts/assign_role.py --email alice@acmebank.com --role author --remove

Roles: admin, author, approver, executor, auditor (case-insensitive).

Idempotent: granting a role the user already has (or revoking one they do not
have) reports that and exits 0. Roles are get-or-created, so this works before
running scripts/seed.py too. Password hashes are never printed.
"""
import argparse
import asyncio
import os
import sys
from uuid import uuid4

# Add repository root to path
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from sqlalchemy import func, select
from app.core.database import get_session_maker
from app.models.user import Role, User, UserRole

ROLE_NAMES = ("admin", "author", "approver", "executor", "auditor")


async def assign_role(email: str, role_name: str, remove: bool = False) -> int:
    """Return a process exit code: 0 on success/no-op, 1 when the user is unknown."""
    normalized = role_name.strip().lower()

    session_maker = get_session_maker()
    async with session_maker() as db:
        user = (
            await db.execute(select(User).where(func.lower(User.email) == email.strip().lower()))
        ).scalar_one_or_none()
        if user is None:
            print(f"✗ No user with email '{email}'", file=sys.stderr)
            return 1

        role = (
            await db.execute(select(Role).where(func.lower(Role.name) == normalized))
        ).scalar_one_or_none()

        if role is None:
            if remove:
                print(f"= {email} does not have role '{normalized}' (role does not exist); nothing to do")
                return 0
            # Get-or-create keeps the CLI usable before seed.py has run.
            role = Role(id=uuid4(), name=normalized, description=None)
            db.add(role)
            await db.flush()
            print(f"+ created role '{normalized}'")

        link = (
            await db.execute(
                select(UserRole).where(
                    UserRole.user_id == user.id,
                    UserRole.role_id == role.id,
                )
            )
        ).scalar_one_or_none()

        if remove:
            if link is None:
                print(f"= {email} does not have role '{role.name}'; nothing to do")
                return 0
            await db.delete(link)
            await db.commit()
            print(f"✓ Revoked role '{role.name}' from {email}")
            return 0

        if link is not None:
            print(f"= {email} already has role '{role.name}'; nothing to do")
            return 0

        db.add(UserRole(id=uuid4(), user_id=user.id, role_id=role.id))
        await db.commit()
        print(f"✓ Granted role '{role.name}' to {email}")
        return 0


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Grant or revoke an RBAC role for a user.",
    )
    parser.add_argument("--email", required=True, help="User email address")
    parser.add_argument(
        "--role",
        required=True,
        type=str.lower,
        choices=ROLE_NAMES,
        help="Role to grant/revoke: " + ", ".join(ROLE_NAMES),
    )
    parser.add_argument(
        "--remove",
        action="store_true",
        help="Revoke the role instead of granting it",
    )
    args = parser.parse_args()

    return asyncio.run(assign_role(args.email, args.role, args.remove))


if __name__ == "__main__":
    sys.exit(main())
