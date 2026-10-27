"""Document upload and management endpoints."""
from fastapi import APIRouter, UploadFile, File, Form, Depends, HTTPException
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select
from typing import List
from uuid import uuid4
import os
from pathlib import Path

from app.core.database import get_db
from app.core.config import get_settings
from app.core.rbac import require_roles
from app.api.v1.endpoints.auth import get_current_active_user
from app.models.user import User
from app.models.document import Document
from app.schemas import DocumentCreate, DocumentResponse
from app.services.rag_service import RAGService

router = APIRouter()
settings = get_settings()
rag_service = RAGService()


@router.post("/upload", response_model=DocumentResponse, status_code=201)
async def upload_document(
    file: UploadFile = File(...),
    domain: str = Form("general"),
    description: str = Form(None),
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(require_roles("author"))
):
    """
    Upload a document to the knowledge base.

    Requires the author role (admin bypasses).

    - Saves file to disk
    - Chunks and embeds content
    - Stores metadata in database
    """
    # Create documents directory if not exists
    docs_dir = Path(settings.DOCUMENTS_DIR)
    docs_dir.mkdir(exist_ok=True)
    
    # Generate unique filename
    doc_id = uuid4()
    file_extension = Path(file.filename).suffix
    safe_filename = f"{doc_id}{file_extension}"
    filepath = docs_dir / safe_filename
    
    # Save file
    content = await file.read()
    with open(filepath, "wb") as f:
        f.write(content)
    
    # Read text content (simplified - in production would handle PDFs, etc.)
    try:
        text_content = content.decode('utf-8')
    except UnicodeDecodeError:
        raise HTTPException(status_code=400, detail="File must be UTF-8 text")
    
    # Ingest into vector store
    chunk_count = await rag_service.ingest_document(
        content=text_content,
        document_id=str(doc_id),
        filename=file.filename,
        domain=domain
    )
    
    # Compute content hash
    content_hash = rag_service.compute_content_hash(text_content)
    
    # Create database record
    user_id = current_user.id
    
    doc = Document(
        id=doc_id,
        filename=file.filename,
        filepath=str(filepath),
        domain=domain,
        description=description,
        content_hash=content_hash,
        file_size=len(content),
        chunk_count=chunk_count,
        uploaded_by=user_id
    )
    
    db.add(doc)
    await db.commit()
    await db.refresh(doc)
    
    return doc


@router.get("/", response_model=List[DocumentResponse])
async def list_documents(
    domain: str = None,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_active_user)
):
    """List all documents, optionally filtered by domain (any authenticated user)."""
    query = select(Document)
    if domain:
        query = query.where(Document.domain == domain)
    
    result = await db.execute(query)
    documents = result.scalars().all()
    return documents


@router.get("/{document_id}", response_model=DocumentResponse)
async def get_document(
    document_id: str,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_active_user)
):
    """Get document metadata by ID (any authenticated user)."""
    result = await db.execute(
        select(Document).where(Document.id == document_id)
    )
    doc = result.scalar_one_or_none()
    
    if not doc:
        raise HTTPException(status_code=404, detail="Document not found")
    
    return doc
