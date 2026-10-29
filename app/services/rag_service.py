"""
RAG (Retrieval Augmenting Generation) Service
Handles document ingestion, chunking, embedding, and retrieval using ChromaDB.

Construction is deliberately lazy: instantiating ``RAGService`` is cheap and
touch-free. The Chroma client and the HuggingFace embedding model are only
built on first actual use (via the ``client``/``embeddings`` properties), so a
module import or ``ENABLE_RAG=false`` deployment never triggers an ~88 MB model
download. Use :func:`get_rag_service` to share one instance process-wide.
"""
import os
import hashlib
import asyncio
from typing import List, Dict, Optional
from pathlib import Path
import chromadb
from chromadb.config import Settings as ChromaSettings
from langchain_text_splitters import RecursiveCharacterTextSplitter
from langchain_community.embeddings import HuggingFaceEmbeddings
from app.core.config import get_settings

settings = get_settings()


class RAGService:
    """Service for managing knowledge base documents via RAG."""

    def __init__(self):
        """Initialize only cheap state; heavy clients are lazy (see above)."""
        self._client: Optional["chromadb.api.ClientAPI"] = None
        self._embeddings: Optional[HuggingFaceEmbeddings] = None

        # Initialize text splitter (cheap, pure CPU, safe to build eagerly)
        self.text_splitter = RecursiveCharacterTextSplitter(
            chunk_size=1000,
            chunk_overlap=200,
            length_function=len
        )

    @property
    def client(self):
        """ChromaDB client, created on first use.

        PersistentClient keeps the index on disk at CHROMA_DB_DIR;
        chromadb.Client(Settings(persist_directory=...)) is ephemeral in
        chromadb 0.5.x and silently loses ingested documents on restart
        (which would defeat the container volume).
        """
        if self._client is None:
            self._client = chromadb.PersistentClient(
                path=str(settings.CHROMA_DB_DIR),
                settings=ChromaSettings(anonymized_telemetry=False),
            )
        return self._client

    @property
    def embeddings(self) -> HuggingFaceEmbeddings:
        """Embedding model (local for cost savings), loaded on first use."""
        if self._embeddings is None:
            self._embeddings = HuggingFaceEmbeddings(
                model_name=settings.EMBEDDING_MODEL
            )
        return self._embeddings

    def _get_or_create_collection(self, domain: str):
        """Get or create a ChromaDB collection for a domain."""
        collection_name = f"domain_{domain}"
        try:
            collection = self.client.get_collection(name=collection_name)
        except Exception:
            collection = self.client.create_collection(
                name=collection_name,
                metadata={"domain": domain}
            )
        return collection

    def _ingest_document_sync(
        self,
        content: str,
        document_id: str,
        filename: str,
        domain: str
    ) -> int:
        """Synchronous implementation of ingest_document (CPU/embedding bound).

        Chunks carry a ``document_id`` metadata field so that RAG entries can
        be cleaned up again (see delete_document) without scanning the whole
        collection.
        """
        # Chunk the document
        chunks = self.text_splitter.split_text(content)

        # Get collection for this domain
        collection = self._get_or_create_collection(domain)

        # Prepare metadata and IDs
        chunk_ids = [f"{document_id}_chunk_{i}" for i in range(len(chunks))]
        metadatas = [
            {
                "document_id": str(document_id),
                "filename": filename,
                "chunk_index": i,
                "domain": domain
            }
            for i in range(len(chunks))
        ]

        # Embed and store
        embeddings_list = self.embeddings.embed_documents(chunks)

        collection.add(
            ids=chunk_ids,
            documents=chunks,
            embeddings=embeddings_list,
            metadatas=metadatas
        )

        return len(chunks)

    async def ingest_document(
        self,
        content: str,
        document_id: str,
        filename: str,
        domain: str
    ) -> int:
        """
        Ingest a document: chunk it, embed it, and store in ChromaDB.

        Args:
            content: The document text content
            document_id: UUID of the document in database
            filename: Original filename
            domain: Domain classification (e.g., 'fraud', 'kyc')

        Returns:
            Number of chunks created
        """
        # The embedding + add work is CPU-bound; run it on a worker thread so
        # it cannot block the event loop when called from async endpoints.
        return await asyncio.to_thread(
            self._ingest_document_sync, content, document_id, filename, domain
        )

    def _query_similar_sync(
        self,
        query_text: str,
        domain: str,
        top_k: int
    ) -> List[Dict]:
        """Synchronous implementation of query_similar (embedding bound)."""
        collection = self._get_or_create_collection(domain)

        # Embed query
        query_embedding = self.embeddings.embed_query(query_text)

        # Query ChromaDB
        results = collection.query(
            query_embeddings=[query_embedding],
            n_results=top_k
        )

        # Format results
        formatted = []
        if results['documents']:
            for i, doc in enumerate(results['documents'][0]):
                formatted.append({
                    "text": doc,
                    "metadata": results['metadatas'][0][i] if results['metadatas'] else {},
                    "distance": results['distances'][0][i] if results['distances'] else None
                })

        return formatted

    async def query_similar(
        self,
        query_text: str,
        domain: str,
        top_k: int = 3
    ) -> List[Dict]:
        """
        Query for similar documents.

        Args:
            query_text: The query string
            domain: Domain to search within
            top_k: Number of results to return

        Returns:
            List of {text, metadata, score} dicts
        """
        return await asyncio.to_thread(
            self._query_similar_sync, query_text, domain, top_k
        )

    async def delete_document(self, domain: str, document_id: str) -> int:
        """
        Remove every chunk belonging to a document from its domain collection.

        Chunks are filtered by the ``document_id`` metadata written at ingest
        time, so only this document's vectors are deleted; other documents in
        the domain are untouched.

        Args:
            domain: Domain the document was ingested into
            document_id: UUID (as string) of the document in the database

        Returns:
            Number of chunks deleted (0 when the collection or document is
            already absent).
        """

        def _delete() -> int:
            try:
                collection = self.client.get_collection(
                    name=f"domain_{domain}"
                )
            except Exception:
                # No such domain collection yet: nothing to delete.
                return 0

            existing = collection.get(where={"document_id": str(document_id)})
            ids = existing.get("ids") or []
            if not ids:
                return 0
            collection.delete(ids=ids)
            return len(ids)

        return await asyncio.to_thread(_delete)

    def compute_content_hash(self, content: str) -> str:
        """Compute SHA256 hash of content for integrity."""
        return hashlib.sha256(content.encode()).hexdigest()


# Process-wide lazily-created singleton. The instance itself is cheap to make
# (no model load, no client connect), so creating it in a dependency/accessor
# never triggers a download; the heavy work happens on first real use.
_rag_service_singleton: Optional[RAGService] = None


def get_rag_service() -> RAGService:
    """Return the shared process-wide RAGService instance (lazily created)."""
    global _rag_service_singleton
    if _rag_service_singleton is None:
        _rag_service_singleton = RAGService()
    return _rag_service_singleton
