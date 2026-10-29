"""Main FastAPI application."""
import logging
from contextlib import asynccontextmanager
from uuid import uuid4
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from sqlalchemy import text
from app.core.config import get_settings, parse_cors_origins
from app.core.database import get_session_maker, init_db
from app.core.logging import (
    RequestIdFilter,
    configure_logging,
    reset_request_id,
    set_request_id,
)
from app.services.executor import sweep_stale_running_runs
from app.api.v1.api import api_router

logger = logging.getLogger(__name__)

settings = get_settings()

# Header names. The token is a uuid4 hex; a caller-provided value is echoed
# so distributed traces can correlate across services.
REQUEST_ID_HEADER = "X-Request-ID"


async def _startup_database_setup() -> None:
    """Initialize schema (opt-in) and reap runs orphaned by a previous process."""
    if settings.AUTO_CREATE_SCHEMA:
        try:
            await init_db()
        except Exception as e:
            logger.warning("Database initialization skipped or tables already exist: %s", e)
    else:
        logger.info(
            "Schema initialization skipped (AUTO_CREATE_SCHEMA=false); "
            "Alembic migrations are run by the container entrypoint."
        )

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
    """App lifespan: configure logging, initialize the database, then serve."""
    configure_logging()
    await _startup_database_setup()
    logger.info("%s started successfully", settings.PROJECT_NAME)
    logger.info("API docs available at http://localhost:8000%s/docs", settings.API_V1_STR)
    yield


# Create FastAPI app
app = FastAPI(
    title=settings.PROJECT_NAME,
    version=settings.VERSION,
    description="AI-Governed Workflow Orchestration System for Regulated Banking Operations",
    lifespan=lifespan,
)


@app.middleware("http")
async def request_id_middleware(request, call_next):
    """Attach a request id: echo an incoming X-Request-ID or mint a uuid4 hex.

    The id is published on the response header and exposed to every log line
    via the contextvar read by RequestIdFilter.
    """
    incoming = (request.headers.get(REQUEST_ID_HEADER) or "").strip()
    request_id = incoming or uuid4().hex
    token = set_request_id(request_id)
    try:
        response = await call_next(request)
    finally:
        reset_request_id(token)
    response.headers[REQUEST_ID_HEADER] = request_id
    return response

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
    """Liveness probe: cheap and static; does NOT touch the DB."""
    return {"status": "healthy"}


async def _check_database() -> None:
    """Raise if the database is unreachable; return otherwise."""
    session_maker = get_session_maker()
    async with session_maker() as db:
        await db.execute(text("SELECT 1"))


def _get_cached_rag_service():
    """Return the live RAGService singleton if one exists, else None.

    Avoids importing the documents module when it has no service yet, so the
    readiness probe cannot trigger any heavy construction as a side effect.
    """
    try:
        from app.api.v1.endpoints import documents as documents_module
    except Exception:
        return None
    return getattr(documents_module, "rag_service", None)


def _probe_chroma() -> str:
    """Probe chroma if a client already exists, never force-loading the model.

    RAGService is being made lazy: constructing it may (re)load the embedding
    model, which is far too heavy for a readiness poll. So we only heartbeat a
    client that is already built. Both service shapes are handled:

    - eager: ``self.client`` is set in the constructor;
    - lazy: ``_client`` holds the client once built, ``_get_client`` builds it
      on demand (NOT called here — probing must stay side-effect free).
    """
    try:
        service = _get_cached_rag_service()
        if service is None:
            return "skipped"
        client = getattr(service, "client", None) or getattr(service, "_client", None)
        if client is None:
            return "skipped"
        # Cheap server heartbeat; no embedding model involved.
        client.get_version()
        return "ok"
    except Exception as e:
        return f"fail: {e}"


@app.get("/ready")
async def ready_check():
    """Readiness probe: real DB reachability + best-effort chroma heartbeat.

    /health answers "is the process up" (static); /ready answers "can this
    instance actually serve". DB is mandatory; chroma is only checked when a
    client already exists (skipped otherwise) so readiness polling never
    triggers an embedding-model load.
    """
    checks: dict = {}
    db_ok = True
    try:
        await _check_database()
        checks["db"] = "ok"
    except Exception as e:
        db_ok = False
        checks["db"] = f"fail: {e}"
    checks["chroma"] = _probe_chroma()

    if not db_ok or checks["chroma"].startswith("fail:"):
        return JSONResponse(status_code=503, content={"status": "unavailable", "checks": checks})
    return {"status": "ready", "checks": checks}


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(
        "app.main:app",
        host="0.0.0.0",
        port=8000,
        reload=True
    )
