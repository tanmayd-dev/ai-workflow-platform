from logging.config import fileConfig
from sqlalchemy import engine_from_config, pool
from alembic import context
from dotenv import load_dotenv
import sys
import os

# Add parent directory to path to import app modules
sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

# Load .env for local runs; already-set environment variables (containers) win.
load_dotenv()

# Import Base (does not trigger async engine creation)
from app.core.config import DEFAULT_DATABASE_URL, to_sync_database_url
from app.db.base import Base

# Import all models to register them with Base.metadata
from app.models.user import User, Role, UserRole
from app.models.workflow import WorkflowDefinition, WorkflowStep, PromptTemplate
from app.models.rule import Rule, RuleVersion
from app.models.execution import WorkflowRun, NodeExecution, AuditLog
from app.models.document import Document

# Alembic Config object
config = context.config

# The app runs on an async driver; Alembic migrations run synchronously. Derive
# the URL from DATABASE_URL so the container entrypoint and the app agree.
configured_url = (
    os.environ.get("DATABASE_URL")
    or config.get_main_option("sqlalchemy.url")
    or DEFAULT_DATABASE_URL
)
config.set_main_option(
    "sqlalchemy.url",
    to_sync_database_url(configured_url).replace("%", "%%"),
)

# Setup logging
if config.config_file_name is not None:
    fileConfig(config.config_file_name)

# Set target metadata
target_metadata = Base.metadata


def run_migrations_offline() -> None:
    """Run migrations in 'offline' mode."""
    url = config.get_main_option("sqlalchemy.url")
    context.configure(
        url=url,
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
        render_as_batch=True,  # For SQLite
    )

    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    """Run migrations in 'online' mode."""
    connectable = engine_from_config(
        config.get_section(config.config_ini_section, {}),
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
    )

    with connectable.connect() as connection:
        context.configure(
            connection=connection,
            target_metadata=target_metadata,
            render_as_batch=True,  # For SQLite
        )

        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
