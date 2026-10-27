"""
Shared pytest configuration.

This module configures environment variables BEFORE any app module is imported
(pytest loads conftest.py first), so `app.core.config.get_settings()` sees the
test configuration everywhere:

- a throwaway SQLite database under tests/
- isolation from the developer's ChromaDB/document directories
- fake OpenRouter/secret credentials (no real API calls in tests)
"""
import os
import shutil
import asyncio
import concurrent.futures
from pathlib import Path
from uuid import uuid4

import pytest
import pytest_asyncio

TESTS_DIR = Path(__file__).parent
TEST_DB = TESTS_DIR / "test_workflow_db.sqlite"
TEST_CHROMA_DIR = TESTS_DIR / ".test_chroma_db"
TEST_DOCS_DIR = TESTS_DIR / ".test_documents"

TEST_DB.unlink(missing_ok=True)
shutil.rmtree(TEST_CHROMA_DIR, ignore_errors=True)
shutil.rmtree(TEST_DOCS_DIR, ignore_errors=True)

os.environ["OPENROUTER_API_KEY"] = "test-openrouter-key"
os.environ["SECRET_KEY"] = "test-secret-key-for-jwt-signing"
os.environ["DATABASE_URL"] = f"sqlite+aiosqlite:///{TEST_DB}"
os.environ["CHROMA_DB_DIR"] = str(TEST_CHROMA_DIR)
os.environ["DOCUMENTS_DIR"] = str(TEST_DOCS_DIR)
os.environ.setdefault("ENABLE_RAG", "false")


@pytest_asyncio.fixture(autouse=True)
async def _reset_database():
    """Create tables before each test, wipe all rows after it."""
    from sqlalchemy import delete
    from app.core.database import init_db, get_session_maker
    from app.db.base import Base

    await init_db()
    yield
    session_maker = get_session_maker()
    async with session_maker() as session:
        for table in reversed(Base.metadata.sorted_tables):
            await session.execute(delete(table))
        await session.commit()


ROLE_DESCRIPTIONS = {
    "admin": "Superuser; bypasses every role check",
    "author": "Drafts workflows, authors rules, uploads knowledge documents",
    "approver": "Approves workflow definitions and human-gated runs",
    "executor": "Starts and replays workflow runs",
    "auditor": "Read-only access to definitions, runs, and audit trails",
}


async def assign_role(email: str, role_name: str, *, remove: bool = False) -> str:
    """Test helper: grant/revoke a role for a registered user.

    The role row is get-or-created case-insensitively, so tests can grant
    ``AUTHOR`` to prove role names are matched case-insensitively.
    """
    from sqlalchemy import func, select
    from app.core.database import get_session_maker
    from app.models.user import Role, User, UserRole

    name = role_name.strip()
    key = name.lower()

    session_maker = get_session_maker()
    async with session_maker() as session:
        user = (
            await session.execute(select(User).where(User.email == email))
        ).scalar_one_or_none()
        if user is None:
            raise AssertionError(f"test user {email} does not exist")

        role = (
            await session.execute(select(Role).where(func.lower(Role.name) == key))
        ).scalar_one_or_none()

        if role is None:
            if remove:
                return f"{email} already lacks role '{name}'"
            role = Role(
                id=uuid4(),
                name=name,
                description=ROLE_DESCRIPTIONS.get(key),
            )
            session.add(role)
            await session.flush()

        link = (
            await session.execute(
                select(UserRole).where(
                    UserRole.user_id == user.id,
                    UserRole.role_id == role.id,
                )
            )
        ).scalar_one_or_none()

        if remove:
            if link is None:
                return f"{email} already lacks role '{role.name}'"
            await session.delete(link)
            await session.commit()
            return f"revoked role '{role.name}' from {email}"

        if link is not None:
            return f"{email} already has role '{role.name}'"

        session.add(UserRole(id=uuid4(), user_id=user.id, role_id=role.id))
        await session.commit()
        return f"granted role '{role.name}' to {email}"


def grant_role(email: str, role_name: str = "admin") -> str:
    """Sync wrapper usable from both sync and pytest-asyncio async tests.

    Async tests already have a running loop, so the short-lived DB write is
    executed by ``asyncio.run`` on a worker thread instead; the engine uses
    ``NullPool``, so no loop-bound connection is shared.
    """
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return asyncio.run(assign_role(email, role_name))

    with concurrent.futures.ThreadPoolExecutor(max_workers=1) as executor:
        return executor.submit(
            asyncio.run, assign_role(email, role_name)
        ).result()


@pytest.fixture
def mock_llm(monkeypatch):
    """Replace LLMService with a deterministic fake (no OpenRouter calls)."""
    async def fake_generate_completion(
        self,
        system_prompt: str,
        user_prompt: str,
        context=None,
        temperature: float = 0.7,
        max_tokens: int = 2000,
    ):
        return {
            "content": "MOCK-LLM ANALYSIS: transaction risk assessed",
            "prompt_snapshot": f"SYSTEM:{system_prompt} USER:{user_prompt} CONTEXT:{context}",
            "model_name": "mock-model",
            "temperature": temperature,
            "usage": {"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15},
        }

    monkeypatch.setattr(
        "app.services.llm_service.LLMService.generate_completion",
        fake_generate_completion,
    )
