"""Main FastAPI application."""
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from app.core.config import get_settings
from app.core.database import init_db
from app.api.v1.api import api_router

settings = get_settings()

# Create FastAPI app
app = FastAPI(
    title=settings.PROJECT_NAME,
    version=settings.VERSION,
    description="AI-Governed Workflow Orchestration System for Regulated Banking Operations"
)

# CORS Middleware
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],  # Configure appropriately for production
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Include API routers
app.include_router(api_router, prefix=settings.API_V1_STR)


@app.on_event("startup")
async def startup_event():
    """Initialize database on startup."""
    try:
        await init_db()
        print(f"✓ {settings.PROJECT_NAME} started successfully")
        print(f"✓ API docs available at http://localhost:8000{settings.API_V1_STR}/docs")
    except Exception as e:
        print(f"Note: Database initialization skipped or tables already exist: {e}")
        print(f"✓ {settings.PROJECT_NAME} started successfully")
        print(f"✓ API docs available at http://localhost:8000{settings.API_V1_STR}/docs")


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
