import uuid
from datetime import datetime
from sqlalchemy import Column, String, Integer, DateTime, JSON, Text, Enum as SQLEnum, ForeignKey, Boolean, Index, text
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import relationship
import enum
from app.db.base import Base


class RunStatus(str, enum.Enum):
    """Workflow run status."""
    RUNNING = "RUNNING"
    PAUSED = "PAUSED"
    COMPLETED = "COMPLETED"
    FAILED = "FAILED"
    CANCELLED = "CANCELLED"


class NodeStatus(str, enum.Enum):
    """Node execution status."""
    PENDING = "PENDING"
    RUNNING = "RUNNING"
    COMPLETED = "COMPLETED"
    FAILED = "FAILED"
    SKIPPED = "SKIPPED"


class WorkflowRun(Base):
    """Workflow execution run (one active per account)."""
    __tablename__ = "workflow_runs"
    __table_args__ = (
        # DB-enforced replacement for the old check-then-insert active-run
        # guard: a second RUNNING/PAUSED run for one account violates this
        # index at commit time. (Defined here as well as in the migration so
        # create_all-based test schemas get the same enforcement.)
        Index(
            "uq_workflow_runs_active_per_account",
            "account_id",
            unique=True,
            sqlite_where=text("status IN ('RUNNING', 'PAUSED')"),
            postgresql_where=text("status IN ('RUNNING', 'PAUSED')"),
        ),
        # Idempotency: one run per Idempotency-Key (NULL keys are exempt).
        Index(
            "uq_workflow_runs_idempotency_key",
            "idempotency_key",
            unique=True,
            sqlite_where=text("idempotency_key IS NOT NULL"),
            postgresql_where=text("idempotency_key IS NOT NULL"),
        ),
    )

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    workflow_id = Column(UUID(as_uuid=True), ForeignKey("workflow_definitions.id"), nullable=False)
    account_id = Column(String, nullable=False, index=True)  # Account being assessed
    status = Column(SQLEnum(RunStatus), default=RunStatus.RUNNING)
    current_step_index = Column(Integer, default=0)
    
    # Workflow state data
    input_data = Column(JSON, nullable=True)  # Declared inputs, saved for replay
    state_snapshot = Column(JSON, nullable=True)  # Current graph state
    output_data = Column(JSON, nullable=True)  # Final decision output (COMPLETED approval/replay paths)
    replayed_from_run_id = Column(UUID(as_uuid=True), nullable=True)  # Set when this run is a replay
    
    # Execution lease (durable claim against concurrent/duplicated execution).
    # NULL lease_token = nobody holds the run; the background executor CAS-claims
    # it before executing and extends (heartbeats) the expiry while it works.
    lease_token = Column(String, nullable=True)
    lease_issued_at = Column(DateTime, nullable=True)
    lease_expires_at = Column(DateTime, nullable=True)

    # Idempotency: retrying POST /runs with the same key returns the original
    # run instead of creating a duplicate decision (unique partial index).
    idempotency_key = Column(String(200), nullable=True)

    # Rule-version pinning: {rule_name: version_number} recorded at execution
    # start; resume/replay re-resolve exactly these versions (not the latest
    # active ones) so later version bumps cannot alter a past or resumed leg.
    rule_versions_pinned = Column(JSON, nullable=True)

    # Audit
    triggered_by = Column(UUID(as_uuid=True), nullable=False)
    trigger_reason = Column(Text, nullable=True)
    started_at = Column(DateTime, default=datetime.utcnow)
    completed_at = Column(DateTime, nullable=True)
    error_message = Column(Text, nullable=True)  # Durable failure/rejection reason
    
    # Relationships
    workflow = relationship("WorkflowDefinition", back_populates="runs")
    node_executions = relationship("NodeExecution", back_populates="run", cascade="all, delete-orphan")
    
    def __repr__(self):
        return f"<WorkflowRun(id='{self.id}', account='{self.account_id}', status='{self.status}')>"


class NodeExecution(Base):
    """Individual node execution record (audit trail)."""
    __tablename__ = "node_executions"
    
    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    run_id = Column(UUID(as_uuid=True), ForeignKey("workflow_runs.id"), nullable=False)
    step_id = Column(UUID(as_uuid=True), ForeignKey("workflow_steps.id"), nullable=False)
    node_type = Column(String, nullable=False)  # Maps to StepType
    status = Column(SQLEnum(NodeStatus), default=NodeStatus.PENDING)
    
    # Input/Output snapshots
    input_snapshot = Column(JSON, nullable=True)
    output_snapshot = Column(JSON, nullable=True)
    
    # For AI nodes
    prompt_template_id = Column(UUID(as_uuid=True), nullable=True)
    prompt_snapshot = Column(Text, nullable=True)  # Actual prompt sent
    model_name = Column(String, nullable=True)
    
    # For Rule nodes
    rule_version_id = Column(UUID(as_uuid=True), nullable=True)
    rule_evaluation_result = Column(JSON, nullable=True)
    
    # Retrieved RAG context (for replayability)
    retrieved_context = Column(JSON, nullable=True)
    
    # Execution metadata
    started_at = Column(DateTime, default=datetime.utcnow)
    completed_at = Column(DateTime, nullable=True)
    error_message = Column(Text, nullable=True)
    
    # Relationships
    run = relationship("WorkflowRun", back_populates="node_executions")
    step = relationship("WorkflowStep", back_populates="executions")
    
    def __repr__(self):
        return f"<NodeExecution(run_id='{self.run_id}', type='{self.node_type}', status='{self.status}')>"


class AuditLog(Base):
    """Comprehensive audit log for all system actions."""
    __tablename__ = "audit_logs"
    
    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    entity_type = Column(String, nullable=False)  # e.g., "workflow", "run", "node"
    entity_id = Column(UUID(as_uuid=True), nullable=False)
    action = Column(String, nullable=False)  # e.g., "created", "approved", "executed"
    actor_id = Column(UUID(as_uuid=True), nullable=False)
    actor_role = Column(String, nullable=True)
    details = Column(JSON, nullable=True)
    timestamp = Column(DateTime, default=datetime.utcnow, index=True)
    
    def __repr__(self):
        return f"<AuditLog(action='{self.action}', entity='{self.entity_type}')>"
