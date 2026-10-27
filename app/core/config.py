from functools import lru_cache

from pydantic_settings import BaseSettings
from sqlalchemy.engine import make_url

# Defaults for local (non-container) runs. Containers override these with
# absolute paths under the mounted data volumes; see docker-compose.yml.
DEFAULT_DATABASE_URL = "sqlite+aiosqlite:///./workflow_db.sqlite"
DEFAULT_CHROMA_DB_DIR = "./chroma_db"
DEFAULT_DOCUMENTS_DIR = "./documents"

# Async drivers used by the app cannot be used by Alembic's synchronous
# migration runner, so map them to their sync counterparts.
_SYNC_DRIVER_MAP = {
    "sqlite+aiosqlite": "sqlite",
    "postgresql+asyncpg": "postgresql",
    "postgresql+psycopg_async": "postgresql+psycopg",
}


def to_sync_database_url(url: str) -> str:
    """Return a synchronous SQLAlchemy URL derived from the app's async URL."""
    parsed = make_url(url)
    sync_driver = _SYNC_DRIVER_MAP.get(parsed.drivername)
    if sync_driver:
        parsed = parsed.set(drivername=sync_driver)
    return parsed.render_as_string(hide_password=False)


class Settings(BaseSettings):
    """Application settings loaded from environment variables."""

    # API Configuration
    PROJECT_NAME: str = "AI Workflow Orchestration System"
    VERSION: str = "1.0.0"
    API_V1_STR: str = "/api/v1"

    # OpenRouter Configuration
    OPENROUTER_API_KEY: str
    OPENROUTER_MODEL: str = "openrouter/pony-alpha"
    OPENROUTER_BASE_URL: str = "https://openrouter.ai/api/v1"

    # Database Configuration
    DATABASE_URL: str = DEFAULT_DATABASE_URL
    # Local/dev convenience: create missing tables on startup. Containers run
    # Alembic from the entrypoint and set this to false to avoid duplicate work.
    AUTO_CREATE_SCHEMA: bool = True

    # Security
    SECRET_KEY: str
    ALGORITHM: str = "HS256"
    ACCESS_TOKEN_EXPIRE_MINUTES: int = 30

    # Vector Store
    CHROMA_DB_DIR: str = DEFAULT_CHROMA_DB_DIR
    EMBEDDING_MODEL: str = "sentence-transformers/all-MiniLM-L6-v2"

    # Best-effort RAG context retrieval inside AI nodes
    ENABLE_RAG: bool = True

    # Documents Storage
    DOCUMENTS_DIR: str = DEFAULT_DOCUMENTS_DIR

    class Config:
        env_file = ".env"
        case_sensitive = True
        # Ignore unrelated keys (e.g. container-only APP_UID/APP_GID) instead
        # of failing to start when .env is shared with docker-compose.
        extra = "ignore"


@lru_cache()
def get_settings() -> Settings:
    """Get cached settings instance."""
    return Settings()
