from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine, async_sessionmaker, AsyncEngine
from typing import AsyncGenerator
from app.core.config import get_settings
from app.db.base import Base

settings = get_settings()

# Global variables for lazy initialization
_engine: AsyncEngine | None = None
_async_session_maker: async_sessionmaker | None = None


def get_engine() -> AsyncEngine:
    """Get or create the async engine (lazy initialization)."""
    global _engine
    if _engine is None:
        _engine = create_async_engine(
            settings.DATABASE_URL,
            echo=True,
            future=True
        )
    return _engine


def get_session_maker() -> async_sessionmaker:
    """Get or create the session maker (lazy initialization)."""
    global _async_session_maker
    if _async_session_maker is None:
        _async_session_maker = async_sessionmaker(
            get_engine(),
            class_=AsyncSession,
            expire_on_commit=False,
            autocommit=False,
            autoflush=False
        )
    return _async_session_maker


async def get_db() -> AsyncGenerator[AsyncSession, None]:
    """Dependency to get database session."""
    async_session_maker = get_session_maker()
    async with async_session_maker() as session:
        try:
            yield session
        finally:
            await session.close()


async def init_db():
    """Initialize database tables."""
    engine = get_engine()
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
