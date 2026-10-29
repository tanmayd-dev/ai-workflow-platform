"""Document upload and management endpoints."""
import asyncio
import logging
from fastapi import (APIRouter, UploadFile, File, Form, Depends, HTTPException,
                     Response)
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select
from typing import List
from uuid import UUID, uuid4
from pathlib import Path
from app.core.database import get_db
from app.core.config import get_settings
from app.core.rbac import require_roles
from app.api.v1.endpoints.auth import get_current_active_user
from app.models.user import User
from app.models.document import Document
from app.schemas import DocumentCreate, DocumentResponse
from app.services.rag_service import get_rag_service

logger = logging.getLogger(__name__)

router = APIRouter()
settings = get_settings()

# Uploaded files are read from the request in bounded chunks so oversized
# uploads are rejected (413) before anything is written to disk.
_UPLOAD_READ_CHUNK = 1024 * 1024


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

    - Rejects files over DOCUMENT_MAX_BYTES before touching disk (413)
    - Validates UTF-8 before writing the file to disk (400)
    - Rejects duplicate content in the same domain (409 with the existing id)
    - Chunks and embeds content without blocking the event loop
    - Rolls back the disk/Chroma writes if ingestion or the DB commit fails
    """
    # Read the upload in bounded chunks and enforce the size limit BEFORE any
    # filesystem work, so an oversized upload never creates state on disk.
    max_bytes = settings.DOCUMENT_MAX_BYTES
    chunks: list = []
    total = 0
    while True:
        chunk = await file.read(_UPLOAD_READ_CHUNK)
        if not chunk:
            break
        total += len(chunk)
        if total > max_bytes:
            raise HTTPException(
                status_code=413,
                detail=(
                    f"File is too large ({total} bytes); the maximum allowed "
                    f"size is {max_bytes} bytes"
                ),
            )
        chunks.append(chunk)
    content = b"".join(chunks)

    # Validate UTF-8 BEFORE writing anything to disk so an invalid upload
    # cannot leave an orphan file behind.
    try:
        text_content = content.decode('utf-8')
    except UnicodeDecodeError:
        raise HTTPException(status_code=400, detail="File must be UTF-8 text")

    # Create documents directory if not exists
    docs_dir = Path(settings.DOCUMENTS_DIR)
    docs_dir.mkdir(parents=True, exist_ok=True)

    # Compute content hash and reject duplicates within the same domain.
    doc_id = uuid4()
    content_hash = get_rag_service().compute_content_hash(text_content)
    duplicate_result = await db.execute(
        select(Document).where(
            Document.domain == domain,
            Document.content_hash == content_hash,
        )
    )
    existing = duplicate_result.scalar_one_or_none()
    if existing:
        raise HTTPException(
            status_code=409,
            detail={
                "message": (
                    "Document content already ingested in domain "
                    f"'{domain}'"
                ),
                "existing_document_id": str(existing.id),
            },
        )

    # Generate unique filename
    file_extension = Path(file.filename).suffix
    safe_filename = f"{doc_id}{file_extension}"
    filepath = docs_dir / safe_filename

    with open(filepath, "wb") as f:
        f.write(content)

    try:
        # Ingest into vector store (the service runs its CPU-bound embedding
        # work on a worker thread, so this awaits without blocking the loop).
        chunk_count = await get_rag_service().ingest_document(
            content=text_content,
            document_id=str(doc_id),
            filename=file.filename,
            domain=domain
        )

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
    except HTTPException:
        raise
    except Exception as e:
        logger.warning(
            "Document ingestion failed for %s in domain '%s': %s",
            doc_id, domain, e
        )
        # Best-effort cross-store cleanup: no DB row (rolled back), no orphan
        # file, and no Chroma chunks for this document.
        await db.rollback()
        filepath.unlink(missing_ok=True)
        try:
            await get_rag_service().delete_document(domain, str(doc_id))
        except Exception as cleanup_error:
            logger.warning(
                "Chroma cleanup failed for document %s: %s; the collection "
                "may need manual cleanup",
                doc_id, cleanup_error
            )
        raise HTTPException(
            status_code=500,
            detail="File stored but ingestion failed; upload was rolled back",
        )

    return doc


@router.delete("/{document_id}", status_code=204)
async def delete_document(
    document_id: UUID,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(require_roles("admin"))
):
    """
    Delete a document and everything it owns.

    Admin only (same policy as rule deletion): hard-deletes the DB row, the
    file on disk, and the document's chunks from the Chroma collection
    (filtered by the document_id chunk metadata written at ingest time).

    Chroma/file cleanup is best-effort: failures are logged but the DB row is
    still removed so repeated admin attempts cannot linger as 500s.
    """
    result = await db.execute(
        select(Document).where(Document.id == document_id)
    )
    doc = result.scalar_one_or_none()

    if not doc:
        raise HTTPException(status_code=404, detail="Document not found")

    await db.delete(doc)
    await db.commit()

    filepath = Path(doc.filepath)
    filepath.unlink(missing_ok=True)
    logger.info(
        "Deleted document %s from domain '%s' (file %s)",
        doc.id, doc.domain, filepath,
    )

    try:
        removed = await get_rag_service().delete_document(
            domain=doc.domain, document_id=str(doc.id)
        )
        logger.info(
            "Removed %d RAG chunks for deleted document %s", removed, doc.id
        )
    except Exception as cleanup_error:
        logger.warning(
            "Chroma cleanup failed for deleted document %s: %s; the "
            "collection may need manual cleanup",
            doc.id, cleanup_error
        )

    return Response(status_code=204)


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
    document_id: UUID,
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
