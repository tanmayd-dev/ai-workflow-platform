"""
Idempotent seed data creation.

Creates the five RBAC roles (get-or-create), the sample admin user, and grants
that user the ``admin`` role. Optionally get-or-creates the demo DOMAIN_RULE
logics that ``scripts/demo.py`` references (rule names matched EXACTLY), so a
freshly seeded system can run the offline-capable demo unchanged.

Re-running the script is safe: existing rows are reused and nothing is
duplicated (a rule that already exists by name is left untouched; a missing
password is never reset on an existing admin).

Callable entry points for other consumers (e.g. ``docker/entrypoint.py``):

- ``bootstrap(email, password, include_demo_rules)`` — one-shot full seeding,
  opens its own session and returns a summary dict.
- ``seed_roles()`` / ``ensure_admin(email, password)`` /
  ``grant_role(email, role_name)`` / ``seed_demo_rules(created_by_email)``
  — small sync helpers, each opening its own short-lived session.

CLI behavior is preserved: ``python scripts/seed.py`` seeds
``admin@acmebank.com / admin123`` and prints the documented credentials.
"""
import sys
import os

# Add repository root to path
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import asyncio
import concurrent.futures
import secrets
from uuid import uuid4
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from app.core.database import get_session_maker
from app.core.security import get_password_hash
from app.models.rule import Rule, RuleVersion
from app.models.user import User, Role, UserRole

DEFAULT_ADMIN_EMAIL = "admin@acmebank.com"
DEFAULT_ADMIN_PASSWORD = "admin123"

ROLES = {
    "admin": "Superuser; bypasses every role check",
    "author": "Drafts workflows, authors rules, uploads knowledge documents",
    "approver": "Approves workflow definitions and human-gated runs",
    "executor": "Starts and replays workflow runs",
    "auditor": "Read-only access to definitions, runs, and audit trails",
}

# DOMAIN_RULE steps in scripts/demo.py reference rules by EXACT name; these
# definitions mirror scripts/demo.py's RULES block so the demo works against
# seeded state. Get-or-create by name keeps the seeder tolerant when the rules
# already exist (created by the API or a previous seed run).
DEMO_RULES = {
    "demo_high_risk_rule": {
        "name": "demo_high_risk_rule",
        "description": "High-risk transactions (risk_score > 0.8) are escalated",
        "domain": "demo",
        "logic_json": {
            "condition": {"field": "risk_score", "op": ">", "value": 0.8},
            "result": "ESCALATE_HIGH_RISK",
        },
    },
    "demo_high_value_rule": {
        "name": "demo_high_value_rule",
        "description": "High-value transactions get a secondary confirmation",
        "domain": "demo",
        "logic_json": {
            "condition": {"field": "amount", "op": ">", "value": 10000},
            "result": "HIGH_VALUE_CONFIRMED",
        },
    },
    "demo_standard_rule": {
        "name": "demo_standard_rule",
        "description": "Low-risk transactions proceed on the standard track",
        "domain": "demo",
        "logic_json": {
            "condition": {"field": "risk_score", "op": "<=", "value": 0.8},
            "result": "PROCEED_STANDARD",
        },
    },
}

_FALSY = {"0", "false", "no", "off", ""}


def env_flag(name: str, default: bool) -> bool:
    """Read a boolean env flag (defaulted per the caller's contract)."""
    raw = os.environ.get(name)
    if raw is None:
        return default
    return raw.strip().strip("'\"").lower() not in _FALSY


# ---------------------------------------------------------------------------
# Async primitives (operate on a caller-provided session)
# ---------------------------------------------------------------------------

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


async def seed_roles_in_session(db) -> dict[str, Role]:
    """Get-or-create every RBAC role; returns {role_name: Role}."""
    print("  Ensuring roles...")
    roles = {}
    for name, description in ROLES.items():
        roles[name] = await get_or_create_role(db, name, description)
    return roles


async def ensure_admin_in_session(
    db, email: str, password: str
) -> tuple[User, bool]:
    """Get-or-create the admin user; returns (user, created).

    Never resets the password recorded for an existing user — inserting one
    must not silently take over an account that already exists.
    """
    print("  Ensuring admin user...")
    result = await db.execute(select(User).where(User.email == email))
    user = result.scalar_one_or_none()
    if user is None:
        user = User(
            id=uuid4(),
            email=email,
            hashed_password=get_password_hash(password),
            full_name="Admin User",
            is_active=True,
        )
        db.add(user)
        await db.flush()
        print(f"  + user '{email}'")
        return user, True
    print(f"  = user '{email}' already exists (credentials unchanged)")
    return user, False


async def grant_role_in_session(db, user: User, role: Role) -> bool:
    """Link a user to a role if not already linked; returns True when linked now."""
    existing_link = await db.execute(
        select(UserRole).where(
            UserRole.user_id == user.id,
            UserRole.role_id == role.id,
        )
    )
    if existing_link.scalar_one_or_none() is not None:
        print(f"  = '{user.email}' already has role '{role.name}'")
        return False
    db.add(UserRole(id=uuid4(), user_id=user.id, role_id=role.id))
    print(f"  + granted '{role.name}' to '{user.email}'")
    return True


async def get_or_create_demo_rule(
    db, spec: dict, created_by
) -> tuple[Rule, bool]:
    """Get-or-create one demo rule (+ active version 1); tolerant to races."""
    name = spec["name"]
    result = await db.execute(select(Rule).where(Rule.name == name))
    rule = result.scalar_one_or_none()
    if rule is not None:
        print(f"  = rule '{name}' already exists (active version reused)")
        return rule, False

    rule = Rule(
        id=uuid4(),
        name=name,
        description=spec.get("description"),
        domain=spec["domain"],
    )
    db.add(rule)
    rule_version = RuleVersion(
        id=uuid4(),
        rule_id=rule.id,
        version_number=1,
        logic_json=spec["logic_json"],
        is_active=True,
        created_by=created_by,
    )
    db.add(rule_version)
    try:
        await db.flush()
        print(f"  + rule '{name}' (active version 1)")
        return rule, True
    except IntegrityError:
        # Lost a race with the rules API or a parallel seed: reuse the winner.
        await db.rollback()
        result = await db.execute(select(Rule).where(Rule.name == name))
        rule = result.scalar_one()
        print(f"  = rule '{name}' already exists (created concurrently)")
        return rule, False


async def seed_demo_rules_in_session(db, created_by) -> list[str]:
    """Get-or-create every demo DOMAIN_RULE referenced by scripts/demo.py."""
    print("  Ensuring demo rules...")
    created = []
    for spec in DEMO_RULES.values():
        _, created_now = await get_or_create_demo_rule(db, spec, created_by)
        if created_now:
            created.append(spec["name"])
    return created


async def run_seed(
    db,
    email: str = DEFAULT_ADMIN_EMAIL,
    password: str = DEFAULT_ADMIN_PASSWORD,
    include_demo_rules: bool = True,
) -> dict:
    """Converge all seed data inside the given session (caller commits).

    Returns a summary dict describing what this call did.
    """
    roles = await seed_roles_in_session(db)
    admin_user, admin_created = await ensure_admin_in_session(db, email, password)
    await grant_role_in_session(db, admin_user, roles["admin"])

    rules_created: list[str] = []
    if include_demo_rules:
        rules_created = await seed_demo_rules_in_session(db, admin_user.id)

    await db.commit()

    return {
        "roles": sorted(roles),
        "admin_email": email,
        "admin_created": admin_created,
        "rules_created": rules_created,
    }


# ---------------------------------------------------------------------------
# Sync one-shot helpers (own session, own short-lived event loop)
# ---------------------------------------------------------------------------

def _run_async(coro):
    """Run a coroutine even when an event loop is already active somewhere.

    The engine uses NullPool, so connections are not bound to a shared loop
    (the same trick tests/conftest.py uses for its grant_role helper).
    """
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return asyncio.run(coro)
    with concurrent.futures.ThreadPoolExecutor(max_workers=1) as executor:
        return executor.submit(asyncio.run, coro).result()


def bootstrap(
    email: str = DEFAULT_ADMIN_EMAIL,
    password: str = DEFAULT_ADMIN_PASSWORD,
    *,
    include_demo_rules: bool | None = None,
) -> dict:
    """One-shot seeding used by the container entrypoint and this CLI.

    Opens its own session, converges roles / admin / demo rules, commits.
    ``include_demo_rules`` defaults to the SEED_DEMO_RULES env flag (true).
    """
    if include_demo_rules is None:
        include_demo_rules = env_flag("SEED_DEMO_RULES", True)

    async def _run() -> dict:
        session_maker = get_session_maker()
        async with session_maker() as db:
            return await run_seed(
                db,
                email=email,
                password=password,
                include_demo_rules=include_demo_rules,
            )

    return _run_async(_run())


def seed_roles() -> list[str]:
    """Get-or-create the RBAC roles; returns the role names."""
    async def _run() -> list[str]:
        session_maker = get_session_maker()
        async with session_maker() as db:
            roles = await seed_roles_in_session(db)
            await db.commit()
            return sorted(roles)

    return _run_async(_run())


def ensure_admin(email: str = DEFAULT_ADMIN_EMAIL,
                 password: str = DEFAULT_ADMIN_PASSWORD) -> tuple[str, bool]:
    """Get-or-create the admin user (no reset); returns (email, created)."""
    summary = bootstrap(email=email, password=password, include_demo_rules=False)
    return summary["admin_email"], summary["admin_created"]


def grant_role(email: str, role_name: str = "admin") -> str:
    """Grant a role to a registered user (get-or-create the role row)."""
    name = role_name.strip()
    key = name.lower()
    description = ROLES.get(key)

    async def _run() -> str:
        session_maker = get_session_maker()
        async with session_maker() as db:
            user = (
                await db.execute(select(User).where(User.email == email))
            ).scalar_one_or_none()
            if user is None:
                raise LookupError(f"user {email} does not exist (register first)")

            role = await get_or_create_role(db, name, description or "Granted role")

            await grant_role_in_session(db, user, role)
            await db.commit()
            return f"granted role '{role.name}' to {email}"

    return _run_async(_run())


def seed_demo_rules(created_by_email: str = DEFAULT_ADMIN_EMAIL) -> list[str]:
    """Get-or-create the demo rules; returns the names created by this call."""
    async def _run() -> list[str]:
        session_maker = get_session_maker()
        async with session_maker() as db:
            user = (
                await db.execute(select(User).where(User.email == created_by_email))
            ).scalar_one_or_none()
            if user is None:
                raise LookupError(
                    f"user {created_by_email} does not exist (seed the admin first)"
                )
            created = await seed_demo_rules_in_session(db, user.id)
            await db.commit()
            return created

    return _run_async(_run())


def generate_password() -> str:
    """A single-use strong password for the first admin (printed once)."""
    return secrets.token_urlsafe(16)


def main():
    """CLI: seed roles, the documented admin user, and (SEED_DEMO_RULES) demo rules."""
    print("Creating seed data...")
    summary = bootstrap(
        email=DEFAULT_ADMIN_EMAIL,
        password=DEFAULT_ADMIN_PASSWORD,
        include_demo_rules=env_flag("SEED_DEMO_RULES", True),
    )

    print("\n✓ Seed data ready!")
    print("\nRoles: " + ", ".join(summary["roles"]))
    print("\nSample User:")
    print(f"  - {DEFAULT_ADMIN_EMAIL} / {DEFAULT_ADMIN_PASSWORD} (admin)")
    if summary["rules_created"]:
        print("\nDemo rules created: " + ", ".join(summary["rules_created"]))
    print("\nGrant more roles with: python scripts/assign_role.py --email <email> --role <role>")


if __name__ == "__main__":
    try:
        main()
    except Exception as e:
        print(f"\n✗ Error creating seed data: {e}")
        import traceback
        traceback.print_exc()
        raise
