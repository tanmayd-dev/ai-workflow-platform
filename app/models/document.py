import uuid
from datetime import datetime
from sqlalchemy import Column, String, DateTime, Text, Integer
from sqlalchemy.dialects.postgresql import UUID
from app.db.base import Base


class Document(Base):
    """Knowledge base document metadata."""
    __tablename__ = "documents"
    
    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    filename = Column(String, nullable=False)
    filepath = Column(String, nullable=False, unique=True)
    domain = Column(String, nullable=False)  # e.g., "fraud_regulations", "kyc_policies"
    description = Column(Text, nullable=True)
    content_hash = Column(String, nullable=True)  # SHA256 hash for integrity
    file_size = Column(Integer, nullable=True)
    
    # Chunking metadata
    chunk_count = Column(Integer, default=0)
    
    # Upload metadata
    uploaded_by = Column(UUID(as_uuid=True), nullable=False)
    uploaded_at = Column(DateTime, default=datetime.utcnow)
    
    def __repr__(self):
        return f"<Document(filename='{self.filename}', domain='{self.domain}')>"
