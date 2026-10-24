"""
RAG (Retrieval Augmented Generation) Service
Handles document ingestion, chunking, embedding, and retrieval using ChromaDB.
"""
import os
import hashlib
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
        """Initialize ChromaDB and embedding model."""
        # Create ChromaDB client
        self.client = chromadb.Client(
            ChromaSettings(
                persist_directory=settings.CHROMA_DB_DIR,
                anonymized_telemetry=False
            )
        )
        
        # Initialize text splitter
        self.text_splitter = RecursiveCharacterTextSplitter(
            chunk_size=1000,
            chunk_overlap=200,
            length_function=len
        )
        
        # Initialize embeddings (local for cost savings)
        self.embeddings = HuggingFaceEmbeddings(
            model_name=settings.EMBEDDING_MODEL
        )
        
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
    
    def compute_content_hash(self, content: str) -> str:
        """Compute SHA256 hash of content for integrity."""
        return hashlib.sha256(content.encode()).hexdigest()
