import uuid
from datetime import datetime
from sqlalchemy import Column, String, Integer, Boolean, DateTime, JSON, Text, Enum as SQLEnum, ForeignKey
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import relationship
import enum
from app.db.base import Base


class WorkflowStatus(str, enum.Enum):
    """Workflow definition status."""
    DRAFT = "DRAFT"
    APPROVED = "APPROVED"
    ARCHIVED = "ARCHIVED"


class StepType(str, enum.Enum):
    """Type of workflow step."""
    AI_ANALYSIS = "AI_ANALYSIS"
    DOMAIN_RULE = "DOMAIN_RULE"
    HUMAN_APPROVAL = "HUMAN_APPROVAL"
    CONDITIONAL = "CONDITIONAL"


class WorkflowDefinition(Base):
    """Workflow definition model (Immutable after approval)."""
    __tablename__ = "workflow_definitions"
    
    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    name = Column(String, nullable=False)
    description = Column(Text, nullable=True)
    domain = Column(String, nullable=False)  # e.g., "fraud", "kyc", "loan_origination"
    version = Column(Integer, default=1, nullable=False)
    status = Column(SQLEnum(WorkflowStatus), default=WorkflowStatus.DRAFT)
    is_active = Column(Boolean, default=True)
    
    # Audit fields
    created_by = Column(UUID(as_uuid=True), nullable=False)
    approved_by = Column(UUID(as_uuid=True), nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow)
    approved_at = Column(DateTime, nullable=True)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)
    
    # Relationships
    steps = relationship("WorkflowStep", back_populates="workflow", cascade="all, delete-orphan", order_by="WorkflowStep.order_index")
    runs = relationship("WorkflowRun", back_populates="workflow")
    
    def __repr__(self):
        return f"<WorkflowDefinition(name='{self.name}', version={self.version}, status='{self.status}')>"


class WorkflowStep(Base):
    """Individual step in a workflow."""
    __tablename__ = "workflow_steps"
    
    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    workflow_id = Column(UUID(as_uuid=True), ForeignKey("workflow_definitions.id"), nullable=False)
    order_index = Column(Integer, nullable=False)
    step_type = Column(SQLEnum(StepType), nullable=False)
    name = Column(String, nullable=False)
    description = Column(Text, nullable=True)
    
    # Configuration (JSON) - stores prompt_template_id, rule_id, rag_query, etc.
    config_json = Column(JSON, nullable=False, default=dict)
    
    # Relationships
    workflow = relationship("WorkflowDefinition", back_populates="steps")
    executions = relationship("NodeExecution", back_populates="step")
    
    def __repr__(self):
        return f"<WorkflowStep(name='{self.name}', type='{self.step_type}', order={self.order_index})>"


class PromptTemplate(Base):
    """Versioned prompt templates for AI nodes."""
    __tablename__ = "prompt_templates"
    
    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    name = Column(String, nullable=False)
    version = Column(Integer, default=1, nullable=False)
    template_text = Column(Text, nullable=False)
    input_schema = Column(JSON, nullable=True)  # JSON schema for expected inputs
    output_schema = Column(JSON, nullable=True)  # Expected output structure
    is_active = Column(Boolean, default=True)
    created_at = Column(DateTime, default=datetime.utcnow)
    
    def __repr__(self):
        return f"<PromptTemplate(name='{self.name}', version={self.version})>"
