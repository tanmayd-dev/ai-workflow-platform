from pydantic_settings import BaseSettings
from functools import lru_cache


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
    DATABASE_URL: str = "sqlite+aiosqlite:///./workflow_db.sqlite"
    
    # Security
    SECRET_KEY: str
    ALGORITHM: str = "HS256"
    ACCESS_TOKEN_EXPIRE_MINUTES: int = 30
    
    # Vector Store
    CHROMA_DB_DIR: str = "./chroma_db"
    EMBEDDING_MODEL: str = "sentence-transformers/all-MiniLM-L6-v2"
    
    # Documents Storage
    DOCUMENTS_DIR: str = "./documents"
    
    class Config:
        env_file = ".env"
        case_sensitive = True


@lru_cache()
def get_settings() -> Settings:
    """Get cached settings instance."""
    return Settings()
