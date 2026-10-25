import uuid
from datetime import datetime
from sqlalchemy import (
    Column, String, Integer, DateTime, JSON, Text, ForeignKey, Boolean,
    UniqueConstraint, Index, text,
)
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
    __table_args__ = (
        # Race-safe version numbering: at most one row per (rule, version).
        UniqueConstraint("rule_id", "version_number", name="uq_rule_version_number"),
        # One ACTIVE version per rule (partial unique index; enforced by
        # create_rule_version semantics + guards against accidental double-actives).
        Index(
            "uq_rule_active_version",
            "rule_id",
            unique=True,
            sqlite_where=text("is_active"),
            postgresql_where=text("is_active"),
        ),
    )
    
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
