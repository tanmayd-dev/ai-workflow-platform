"""Pydantic schemas for API request/response validation."""
from pydantic import BaseModel, EmailStr, Field
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


# ============= Rule Schemas =============
class RuleVersionCreate(BaseModel):
    logic_json: Dict[str, Any]


class RuleCreate(BaseModel):
    name: str
    description: Optional[str] = None
    domain: str
    initial_logic: Dict[str, Any]


class RuleResponse(BaseModel):
    id: UUID
    name: str
    domain: str
    description: Optional[str]
    created_at: datetime
    
    class Config:
        from_attributes = True


# ============= Execution Schemas =============
class RunCreate(BaseModel):
    workflow_id: UUID
    account_id: str
    input_data: Dict[str, Any] = Field(default_factory=dict, description="Input data evaluated by workflow steps")
    trigger_reason: Optional[str] = None


class RunResponse(BaseModel):
    id: UUID
    workflow_id: UUID
    account_id: str
    status: RunStatus
    current_step_index: int
    started_at: datetime
    completed_at: Optional[datetime]
    
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
    started_at: datetime
    completed_at: Optional[datetime]
    
    class Config:
        from_attributes = True
