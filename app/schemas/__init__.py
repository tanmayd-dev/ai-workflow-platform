"""Pydantic schemas for API request/response validation."""
from pydantic import BaseModel, EmailStr, Field, model_validator
from datetime import datetime
from typing import Optional, List, Dict, Any
from uuid import UUID
from app.models.workflow import WorkflowStatus, StepType
from app.models.execution import RunStatus, NodeStatus


# ============= User Schemas =============
class UserBase(BaseModel):
    email: EmailStr
    full_name: Optional[str] = None


class UserCreate(UserBase):
    password: str


class UserResponse(UserBase):
    id: UUID
    is_active: bool
    created_at: datetime
    
    class Config:
        from_attributes = True


class Token(BaseModel):
    access_token: str
    token_type: str = "bearer"


class TokenData(BaseModel):
    email: Optional[str] = None


# ============= Document Schemas =============
class DocumentCreate(BaseModel):
    domain: str = Field(..., description="Domain classification (fraud, kyc, etc.)")
    description: Optional[str] = None


class DocumentResponse(BaseModel):
    id: UUID
    filename: str
    domain: str
    description: Optional[str]
    file_size: int
    chunk_count: int
    uploaded_by: Optional[UUID] = None
    uploaded_at: datetime
    
    class Config:
        from_attributes = True


# ============= Workflow Schemas =============
class WorkflowStepCreate(BaseModel):
    order_index: int
    step_type: StepType
    name: str
    description: Optional[str] = None
    config_json: Dict[str, Any] = Field(default_factory=dict)


class WorkflowStepResponse(BaseModel):
    id: UUID
    order_index: int
    step_type: StepType
    name: str
    description: Optional[str]
    config_json: Dict[str, Any]
    
    class Config:
        from_attributes = True


class WorkflowCreate(BaseModel):
    name: str
    description: Optional[str] = None
    domain: str
    steps: List[WorkflowStepCreate] = []


class WorkflowResponse(BaseModel):
    id: UUID
    name: str
    description: Optional[str]
    domain: str
    version: int
    status: WorkflowStatus
    is_active: bool
    created_by: Optional[UUID] = None
    approved_by: Optional[UUID] = None
    created_at: datetime
    approved_at: Optional[datetime]
    steps: List[WorkflowStepResponse] = []
    
    class Config:
        from_attributes = True


# ============= Intent/Drafting Schemas =============
class IntentRequest(BaseModel):
    intent: str = Field(..., description="Natural language intent")
    domain: str = Field(..., description="Domain (fraud, kyc, etc.)")


class IntentResponse(BaseModel):
    workflow_draft: Dict[str, Any]
    retrieved_context: str


class WorkflowDraft(BaseModel):
    """Schema the LLM's workflow draft must satisfy before we return it.

    Mirrors ``WorkflowStepCreate`` but deliberately omits ``domain`` (the caller
    supplies that) and requires at least one step.
    """

    name: str = Field(..., min_length=1)
    description: Optional[str] = None
    steps: List[WorkflowStepCreate] = Field(..., min_length=1)


# ============= Rule Schemas =============
class RuleVersionCreate(BaseModel):
    logic_json: Dict[str, Any]


class RuleVersionResponse(BaseModel):
    id: UUID
    rule_id: UUID
    version_number: int
    logic_json: Dict[str, Any]
    is_active: bool
    created_at: datetime
    created_by: UUID

    class Config:
        from_attributes = True


class RuleCreate(BaseModel):
    name: str
    description: Optional[str] = None
    domain: str
    initial_logic: Optional[Dict[str, Any]] = None
    logic_json: Optional[Dict[str, Any]] = None

    @model_validator(mode="after")
    def validate_logic(self):
        if self.logic_json is None and self.initial_logic is None:
            raise ValueError("Rule logic must be provided via logic_json or initial_logic")
        if self.logic_json is None:
            self.logic_json = self.initial_logic
        return self


class RuleResponse(BaseModel):
    id: UUID
    name: str
    domain: str
    description: Optional[str] = None
    created_at: datetime
    versions: List[RuleVersionResponse] = []
    
    class Config:
        from_attributes = True


# ============= Execution Schemas =============
class RunCreate(BaseModel):
    workflow_id: UUID
    account_id: str
    input_data: Dict[str, Any] = Field(default_factory=dict, description="Input data evaluated by workflow steps")
    trigger_reason: Optional[str] = None


class RunApprovalRequest(BaseModel):
    notes: Optional[str] = Field(default=None, description="Optional notes from human approver")


class RunRejectRequest(BaseModel):
    reason: Optional[str] = Field(default=None, description="Reason for rejection")
    rejection_reason: Optional[str] = Field(default=None, description="Alias for reason")

    def get_reason(self) -> str:
        return self.reason or self.rejection_reason or "Rejected by user"


class RunResponse(BaseModel):
    id: UUID
    workflow_id: UUID
    account_id: str
    status: RunStatus
    current_step_index: int
    started_at: datetime
    completed_at: Optional[datetime]
    error_message: Optional[str] = None
    # Final decision output for terminal runs: the graph's current_output, which
    # may be a dict (AI output, approval verdict) or a scalar (rule result).
    # Validate as any JSON, not dict-only.
    output_data: Optional[Any] = None

    class Config:
        from_attributes = True


class NodeExecutionResponse(BaseModel):
    id: UUID
    node_type: str
    status: NodeStatus
    # Snapshots are JSONB columns: AI output dicts, rule scalars, or nested
    # documents — validate as any JSON, not dict-only.
    input_snapshot: Optional[Any]
    output_snapshot: Optional[Any]

    # Lineage: exactly which prompt / model / rule version produced this node.
    prompt_template_id: Optional[UUID] = None
    prompt_snapshot: Optional[str] = None
    model_name: Optional[str] = None
    rule_version_id: Optional[UUID] = None
    rule_evaluation_result: Optional[Any] = None
    retrieved_context: Optional[Any] = None

    started_at: datetime
    completed_at: Optional[datetime]
    error_message: Optional[str] = None
    
    class Config:
        from_attributes = True
