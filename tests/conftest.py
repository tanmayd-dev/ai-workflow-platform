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
from pathlib import Path

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
