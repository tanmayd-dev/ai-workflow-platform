import uuid
from datetime import datetime
from sqlalchemy import Column, String, Integer, DateTime, JSON, Text, ForeignKey, Boolean
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import relationship
from app.db.base import Base


class Rule(Base):
    """Global rule definition."""
    __tablename__ = "rules"
    
    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    name = Column(String, unique=True, nullable=False)
    description = Column(Text, nullable=True)
    domain = Column(String, nullable=False)  # e.g., "fraud", "kyc"
    created_at = Column(DateTime, default=datetime.utcnow)
    
    # Relationships
    versions = relationship("RuleVersion", back_populates="rule", cascade="all, delete-orphan", order_by="RuleVersion.version_number.desc()")
    
    def __repr__(self):
        return f"<Rule(name='{self.name}', domain='{self.domain}')>"


class RuleVersion(Base):
    """Versioned rule logic using JSON DSL."""
    __tablename__ = "rule_versions"
    
    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    rule_id = Column(UUID(as_uuid=True), ForeignKey("rules.id"), nullable=False)
    version_number = Column(Integer, nullable=False)
    
    # JSON DSL Logic
    # Example: {"condition": {"field": "risk_score", "op": ">", "value": 0.8}, "result": "REJECT"}
    logic_json = Column(JSON, nullable=False)
    
    is_active = Column(Boolean, default=True)
    created_at = Column(DateTime, default=datetime.utcnow)
    created_by = Column(UUID(as_uuid=True), nullable=False)
    
    # Relationships
    rule = relationship("Rule", back_populates="versions")
    
    def __repr__(self):
        return f"<RuleVersion(rule_id='{self.rule_id}', version={self.version_number})>"
