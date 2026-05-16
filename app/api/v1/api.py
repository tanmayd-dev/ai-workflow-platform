"""API v1 router aggregation."""
from fastapi import APIRouter
from app.api.v1.endpoints import documents, workflows, intents, runs, auth

api_router = APIRouter()

# Authentication
api_router.include_router(auth.router, prefix="/auth", tags=["authentication"])

# Core functionality
api_router.include_router(documents.router, prefix="/documents", tags=["documents"])
api_router.include_router(workflows.router, prefix="/workflows", tags=["workflows"])
api_router.include_router(intents.router, prefix="/intents", tags=["intents"])
api_router.include_router(runs.router, prefix="/runs", tags=["runs"])
