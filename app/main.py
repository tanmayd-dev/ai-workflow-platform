"""Main FastAPI application."""
import logging
from contextlib import asynccontextmanager
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from app.core.config import get_settings, parse_cors_origins
from app.core.database import get_session_maker, init_db
from app.services.executor import sweep_stale_running_runs
from app.api.v1.api import api_router

logger = logging.getLogger(__name__)

settings = get_settings()


async def _startup_database_setup() -> None:
    """Initialize schema (opt-in) and reap runs orphaned by a previous process."""
    if settings.AUTO_CREATE_SCHEMA:
        try:
            await init_db()
        except Exception as e:
            print(f"Note: Database initialization skipped or tables already exist: {e}")
    else:
        print("Schema initialization skipped (AUTO_CREATE_SCHEMA=false); "
              "Alembic migrations are run by the container entrypoint.")

    # Crash recovery: RUNNING runs with a NULL or expired lease were owned by a
    # process that is gone, so they can never reach a terminal status on their
    # own. Mark them FAILED durably (the sweep leaves PAUSED runs and runs with
    # a live lease alone). The count is logged for operators.
    session_maker = get_session_maker()
    async with session_maker() as db:
        swept = await sweep_stale_running_runs(db)
    if swept:
        logger.warning("Startup sweep: marked %d stale RUNNING run(s) as FAILED", swept)
    else:
        logger.info("Startup sweep: no stale RUNNING runs found")


@asynccontextmanager
async def lifespan(fastapi_app: FastAPI):
    """App lifespan: initialize the database, then serve until shutdown."""
    await _startup_database_setup()
    print(f"✓ {settings.PROJECT_NAME} started successfully")
    print(f"✓ API docs available at http://localhost:8000/docs")
    yield


# Create FastAPI app
app = FastAPI(
    title=settings.PROJECT_NAME,
    version=settings.VERSION,
    description="AI-Governed Workflow Orchestration System for Regulated Banking Operations",
    lifespan=lifespan,
)

# CORS is opt-in: set CORS_ORIGINS to a CSV of explicit origins. parse_cors_origins
# drops wildcard ("*") entries, so CORSMiddleware is never configured with
# allow_origins=["*"] and allow_credentials=True (an invalid combination).
cors_origins = parse_cors_origins(settings.CORS_ORIGINS)
if cors_origins:
    app.add_middleware(
        CORSMiddleware,
        allow_origins=cors_origins,
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )

# Include API routers
app.include_router(api_router, prefix=settings.API_V1_STR)

# Admin console UI (single page; one include line — everything self-contained in app/ui)
from app.ui.router import router as ui_router  # noqa: E402
app.include_router(ui_router)


@app.get("/")
async def root():
    """Root endpoint."""
    return {
        "name": settings.PROJECT_NAME,
        "version": settings.VERSION,
        "status": "operational",
        "docs_url": f"{settings.API_V1_STR}/docs"
    }


@app.get("/health")
async def health_check():
    """Health check endpoint."""
    return {"status": "healthy"}


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(
        "app.main:app",
        host="0.0.0.0",
        port=8000,
        reload=True
    )
