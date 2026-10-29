"""Hardening tests for document upload/ingest and the lazily-wired RAG layer.

Covers the phase-0 correctness findings for documents:

- giant uploads are rejected with 413 BEFORE anything is written to disk
- non-UTF-8 uploads are rejected with 400 with NO orphan file
- duplicate content in the same domain returns 409 with the existing id and
  does not re-run ingestion
- DELETE /documents/{id} (admin only) removes the DB row, disk file, and RAG
  chunks; unknown ids 404; non-admin 403
- an ingest failure after a successful disk write rolls back to NO DB row and
  NO orphan file

Fast tests monkeypatch ``RAGService.ingest_document`` so the real embedding
pipeline is not loaded; exactly one test exercises the real pipeline (limit).
"""
import asyncio
from pathlib import Path
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient

from app.main import app
from app.core.config import get_settings
from app.services import rag_service as rag_service_module
from app.services.rag_service import RAGService, get_rag_service
from conftest import grant_role


@pytest.fixture(autouse=True)
def _enable_rag_for_real_pipeline(monkeypatch):
    """The one real-pipeline test exercises the actual embedding path, so the
    global ENABLE_RAG=false default (conftest) must be lifted here."""
    monkeypatch.setattr(rag_service_module.settings, "ENABLE_RAG", True)


@pytest.fixture
def client():
    """In-process HTTP test client (startup event initializes the DB)."""
    with TestClient(app) as test_client:
        yield test_client


def _register(client: TestClient, roles=("admin",)) -> dict:
    """Register a user directly (no auto-role assignment), then grant roles."""
    email = f"docsharden_{uuid4().hex[:8]}@example.com"
    register = client.post("/api/v1/auth/register", json={
        "email": email,
        "password": "testpass123",
        "full_name": "Documents Hardening Test User",
    })
    assert register.status_code == 201, register.text

    for role in roles:
        grant_role(email, role)

    login = client.post("/api/v1/auth/login", data={
        "username": email,
        "password": "testpass123",
    })
    assert login.status_code == 200, login.text

    return {
        "email": email,
        "id": register.json()["id"],
        "headers": {"Authorization": f"Bearer {login.json()['access_token']}"},
    }


def _upload(client: TestClient, headers: dict, *, content: bytes = b"policy text",
            filename: str = "policy.txt", domain: str = "fraud"):
    return client.post(
        "/api/v1/documents/upload",
        headers=headers,
        files={"file": (filename, content, "text/plain")},
        data={"domain": domain},
    )


def _docs_dir_files():
    """Names of files currently stored in the configured documents dir."""
    docs_dir = Path(get_settings().DOCUMENTS_DIR)
    if not docs_dir.exists():
        return []
    return sorted(p.name for p in docs_dir.iterdir())


def _fetch_document(doc_id: str):
    """Fetch one Document row on a short-lived loop (async engine, sync test)."""
    from uuid import UUID as _UUID
    from sqlalchemy import select
    from app.core.database import get_session_maker
    from app.models.document import Document

    async def _inner():
        session_maker = get_session_maker()
        async with session_maker() as session:
            return (
                await session.execute(
                    select(Document).where(Document.id == _UUID(doc_id))
                )
            ).scalar_one_or_none()

    return asyncio.run(_inner())


def _count_documents() -> int:
    from sqlalchemy import select, func
    from app.core.database import get_session_maker
    from app.models.document import Document

    async def _inner():
        session_maker = get_session_maker()
        async with session_maker() as session:
            return (
                await session.execute(
                    select(func.count()).select_from(Document)
                )
            ).scalar_one()

    return asyncio.run(_inner())


# ---------------------------------------------------------------------------
# config / lazy wiring
# ---------------------------------------------------------------------------

def test_document_max_bytes_default_is_five_megabytes():
    """The upload size cap exists and defaults to 5 MB."""
    assert get_settings().DOCUMENT_MAX_BYTES == 5 * 1024 * 1024


def test_rag_service_constructor_is_cheap_and_lazy():
    """Building RAGService must not connect to Chroma or load the model."""
    service = RAGService()
    # Heavy clients are deferred: neither exists after plain construction.
    assert service._client is None
    assert service._embeddings is None
    # Cheap helpers still work without any client/model.
    digest = service.compute_content_hash("hello")
    assert len(digest) == 64


def test_get_rag_service_returns_cached_singleton():
    assert get_rag_service() is get_rag_service()
    assert isinstance(get_rag_service(), RAGService)


# ---------------------------------------------------------------------------
# upload hardening
# ---------------------------------------------------------------------------

def test_oversized_upload_rejected_before_disk_write(client, monkeypatch):
    """
    A file over DOCUMENT_MAX_BYTES gets 413 and leaves NO file on disk.

    The limit is lowered via the (cached) settings object the endpoint reads,
    so the on-disk docs dir configured by conftest proves the no-write claim.
    """
    import app.api.v1.endpoints.documents as documents_module

    monkeypatch.setattr(
        documents_module.settings, "DOCUMENT_MAX_BYTES", 16
    )

    author = _register(client, ["author"])
    files_before = _docs_dir_files()

    response = _upload(client, author["headers"], content=b"x" * 20)
    assert response.status_code == 413, response.text
    assert "16 bytes" in response.json()["detail"]
    assert _docs_dir_files() == files_before, "oversized upload wrote a file"


def test_non_utf8_upload_rejected_without_orphan_file(client, monkeypatch):
    """Invalid UTF-8 bytes get 400 and leave NO orphan file on disk."""
    author = _register(client, ["author"])
    files_before = _docs_dir_files()

    response = _upload(client, author["headers"], content=b"\xff\xfe\xfa")
    assert response.status_code == 400, response.text
    assert response.json()["detail"] == "File must be UTF-8 text"
    assert _docs_dir_files() == files_before, "invalid upload left an orphan"


def test_duplicate_content_returns_409_and_skips_reingest(client, monkeypatch):
    """Same content_hash in the same domain -> 409 + existing id, no re-ingest."""
    calls: list = []

    async def fake_ingest_document(self, content, document_id, filename, domain):
        calls.append((domain, document_id))
        return 1

    monkeypatch.setattr(
        "app.services.rag_service.RAGService.ingest_document",
        fake_ingest_document,
    )

    author = _register(client, ["author"])
    first = _upload(client, author["headers"], content=b"same content")
    assert first.status_code == 201, first.text
    first_id = first.json()["id"]

    second = _upload(client, author["headers"], content=b"same content")
    assert second.status_code == 409, second.text
    detail = second.json()["detail"]
    assert "already ingested" in detail["message"]
    assert detail["existing_document_id"] == first_id

    # Ingestion ran exactly once: the duplicate skipped the RAG write.
    assert calls == [("fraud", first_id)]

    # Same content in a DIFFERENT domain is not a duplicate.
    other = _upload(client, author["headers"], content=b"same content",
                    domain="kyc")
    assert other.status_code == 201, other.text
    assert len(calls) == 2


def test_ingest_failure_after_write_rolls_back_everything(client, monkeypatch):
    """
    An ingest failure after the successful disk write leaves NO DB row and NO
    orphan file behind (and attempts the Chroma-chunk cleanup).
    """
    async def failing_ingest(self, content, document_id, filename, domain):
        raise RuntimeError("embedding backend exploded")

    cleanup_calls: list = []

    async def fake_delete_document(self, domain, document_id):
        cleanup_calls.append((domain, document_id))
        return 1

    monkeypatch.setattr(
        "app.services.rag_service.RAGService.ingest_document",
        failing_ingest,
    )
    monkeypatch.setattr(
        "app.services.rag_service.RAGService.delete_document",
        fake_delete_document,
    )

    author = _register(client, ["author"])
    files_before = _docs_dir_files()

    response = _upload(client, author["headers"])
    assert response.status_code == 500, response.text
    assert "rolled back" in response.json()["detail"]

    assert _count_documents() == 0
    assert _docs_dir_files() == files_before, "failed upload left an orphan"
    # Best-effort cleanup was attempted for the chunks that may have been added.
    assert cleanup_calls, "Chroma chunk cleanup was never attempted"


def test_delete_document_requires_admin(client, monkeypatch):
    """Non-admin gets 403 even though they can upload as author."""
    async def fake_ingest_document(self, content, document_id, filename, domain):
        return 1

    monkeypatch.setattr(
        "app.services.rag_service.RAGService.ingest_document",
        fake_ingest_document,
    )

    author = _register(client, ["author"])
    upload = _upload(client, author["headers"])
    assert upload.status_code == 201, upload.text
    doc_id = upload.json()["id"]

    denied = client.delete(f"/api/v1/documents/{doc_id}",
                           headers=author["headers"])
    assert denied.status_code == 403, denied.text


def test_delete_document_removes_row_file_and_chunks(client, monkeypatch):
    """
    Admin DELETE removes the DB row, the disk file, and (best-effort) the RAG
    chunks; GET afterwards 404s; unknown ids 404.
    """
    async def fake_ingest_document(self, content, document_id, filename, domain):
        return 1

    monkeypatch.setattr(
        "app.services.rag_service.RAGService.ingest_document",
        fake_ingest_document,
    )

    author = _register(client, ["author"])
    admin = _register(client, ["admin"])

    upload = _upload(client, author["headers"])
    assert upload.status_code == 201, upload.text
    doc_id = upload.json()["id"]
    stored = _fetch_document(doc_id)
    assert stored is not None
    filepath = Path(stored.filepath)
    assert filepath.exists()
    assert _count_documents() == 1

    unknown = client.delete(f"/api/v1/documents/{uuid4()}",
                            headers=admin["headers"])
    assert unknown.status_code == 404, unknown.text

    deleted = client.delete(f"/api/v1/documents/{doc_id}",
                            headers=admin["headers"])
    assert deleted.status_code == 204, deleted.text

    # Disk file gone, DB row gone, GET 404s for any authenticated user.
    assert not filepath.exists()
    assert _fetch_document(doc_id) is None
    assert _count_documents() == 0
    reader = _register(client, roles=())
    got = client.get(f"/api/v1/documents/{doc_id}", headers=reader["headers"])
    assert got.status_code == 404, got.text


# ---------------------------------------------------------------------------
# exactly one test touches the real embedding pipeline
# ---------------------------------------------------------------------------

async def test_delete_document_removes_chunks_from_real_vector_store():
    """Real pipeline: delete_document() drops only this document's chunks."""
    service = RAGService()
    domain = f"hardening_{uuid4().hex[:10]}"
    document_id = str(uuid4())
    content = " ".join(
        f"Hardening clause {i}: suspicious transactions need review."
        for i in range(40)
    )

    chunk_count = await service.ingest_document(
        content=content, document_id=document_id,
        filename="hardening.txt", domain=domain,
    )
    assert chunk_count >= 1

    deleted = await service.delete_document(domain, document_id)
    assert deleted == chunk_count

    # The document's text is gone from its domain collection...
    results = await service.query_similar(
        query_text="suspicious transactions need review",
        domain=domain, top_k=chunk_count,
    )
    assert results == []

    # ...and deleting again is a no-op, not an error.
    assert await service.delete_document(domain, document_id) == 0
