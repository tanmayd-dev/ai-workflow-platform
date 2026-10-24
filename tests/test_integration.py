"""
Integration tests for the AI-governed workflow system.

These tests run entirely in-process:
- API tests go through `fastapi.testclient.TestClient` (no live server needed)
- executor tests call `WorkflowExecutor` directly
- LLM calls are mocked by the `mock_llm` fixture in conftest.py
"""
from uuid import uuid4

import pytest
import pytest_asyncio
from fastapi.testclient import TestClient
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.database import get_session_maker
from app.core.security import get_password_hash
from app.main import app
from app.models.user import User
from app.models.rule import Rule, RuleVersion
from app.models.workflow import WorkflowDefinition, WorkflowStep, StepType, WorkflowStatus
from app.models.execution import RunStatus, WorkflowRun
from app.services.executor import WorkflowExecutor


@pytest_asyncio.fixture
async def db_session():
    """Provide a database session for tests."""
    async_session_maker = get_session_maker()
    async with async_session_maker() as session:
        yield session


@pytest.fixture
def client():
    """In-process HTTP test client (startup event initializes the DB)."""
    with TestClient(app) as test_client:
        yield test_client


@pytest.fixture
async def sample_user(db_session: AsyncSession):
    """Create a sample user."""
    user = User(
        id=uuid4(),
        email="test@example.com",
        hashed_password=get_password_hash("testpass123"),
        full_name="Test User",
        is_active=True
    )
    db_session.add(user)
    await db_session.commit()
    await db_session.refresh(user)
    return user


@pytest.fixture
async def high_risk_rule(db_session: AsyncSession, sample_user: User):
    """Seed the rule referenced by sample_workflow steps."""
    rule = Rule(
        id=uuid4(),
        name="high_risk_threshold",
        description="Escalate high-risk transactions",
        domain="fraud"
    )
    db_session.add(rule)
    await db_session.flush()

    version = RuleVersion(
        id=uuid4(),
        rule_id=rule.id,
        version_number=1,
        logic_json={
            "condition": {"field": "risk_score", "op": ">", "value": 0.8},
            "result": "ESCALATE"
        },
        is_active=True,
        created_by=sample_user.id
    )
    db_session.add(version)
    await db_session.commit()
    return (rule, version)


@pytest.fixture
async def sample_workflow(db_session: AsyncSession, sample_user: User):
    """Create an APPROVED workflow definition with an AI + rule step pair."""
    workflow = WorkflowDefinition(
        id=uuid4(),
        name="Test Fraud Workflow",
        description="Test workflow for integration testing",
        domain="fraud",
        version=1,
        status=WorkflowStatus.APPROVED,
        is_active=True,
        created_by=sample_user.id,
        approved_by=sample_user.id
    )
    db_session.add(workflow)
    await db_session.flush()

    steps = [
        WorkflowStep(
            id=uuid4(),
            workflow_id=workflow.id,
            order_index=0,
            step_type=StepType.AI_ANALYSIS,
            name="Risk Analysis",
            description="AI analyzes transaction risk",
            config_json={
                "rag_query": "fraud detection",
                "prompt_template": "Analyze this transaction for fraud risk"
            }
        ),
        WorkflowStep(
            id=uuid4(),
            workflow_id=workflow.id,
            order_index=1,
            step_type=StepType.DOMAIN_RULE,
            name="Threshold Check",
            description="Apply risk threshold rules",
            config_json={
                "rule_id": "high_risk_threshold"
            }
        )
    ]

    for step in steps:
        db_session.add(step)

    await db_session.commit()
    await db_session.refresh(workflow)
    return workflow


def test_authentication_flow(client):
    """Register, login, and access a protected endpoint — all in-process."""
    register_data = {
        "email": f"test_{uuid4()}@example.com",
        "password": "testpass123",
        "full_name": "Integration Test User"
    }

    response = client.post("/api/v1/auth/register", json=register_data)
    assert response.status_code == 201, response.text
    assert response.json()["email"] == register_data["email"]

    login = client.post("/api/v1/auth/login", data={
        "username": register_data["email"],
        "password": register_data["password"],
    })
    assert login.status_code == 200, login.text
    assert "access_token" in login.json()

    me = client.get("/api/v1/auth/me", headers={
        "Authorization": f"Bearer {login.json()['access_token']}"
    })
    assert me.status_code == 200, me.text
    assert me.json()["email"] == register_data["email"]


@pytest.mark.asyncio
async def test_workflow_execution(db_session: AsyncSession, sample_workflow: WorkflowDefinition, high_risk_rule, mock_llm):
    """Direct executor test: run completes and persists node executions."""
    executor = WorkflowExecutor()

    input_data = {
        "transaction_id": "TX123",
        "amount": 50000,
        "risk_score": 0.85
    }

    run = await executor.execute(
        workflow_id=sample_workflow.id,
        input_data=input_data,
        account_id="ACC123",
        db=db_session
    )

    assert run.id is not None
    assert run.workflow_id == sample_workflow.id
    assert run.account_id == "ACC123"
    assert run.status == RunStatus.COMPLETED, f"run failed: {run.error_message}"
    assert run.input_data == input_data


@pytest.mark.asyncio
async def test_workflow_replay(db_session: AsyncSession, sample_workflow: WorkflowDefinition, high_risk_rule, mock_llm):
    """Direct executor test: replay re-executes with the stored inputs."""
    executor = WorkflowExecutor()

    input_data = {
        "transaction_id": "TX456",
        "amount": 75000,
        "risk_score": 0.92
    }

    original_run = await executor.execute(
        workflow_id=sample_workflow.id,
        input_data=input_data,
        account_id="ACC456",
        db=db_session
    )
    assert original_run.status == RunStatus.COMPLETED, original_run.error_message

    replay_run = await executor.replay(
        original_run_id=original_run.id,
        db=db_session
    )

    assert replay_run.id != original_run.id
    assert replay_run.input_data == original_run.input_data
    assert replay_run.status == RunStatus.COMPLETED, replay_run.error_message

    # The replay linkage must be persisted, not just set on the in-memory object
    from sqlalchemy import select
    result = await db_session.execute(
        select(WorkflowRun).where(WorkflowRun.id == replay_run.id)
    )
    persisted = result.scalar_one()
    assert persisted.replayed_from_run_id == original_run.id


def test_run_endpoint_executes_workflow(client, sample_workflow, high_risk_rule, mock_llm):
    """POST /api/v1/runs drives the executor end-to-end (sync execution)."""
    email = f"runner_{uuid4()}@example.com"

    register = client.post("/api/v1/auth/register", json={
        "email": email, "password": "testpass123", "full_name": "Run User"
    })
    assert register.status_code == 201, register.text

    login = client.post("/api/v1/auth/login", data={
        "username": email, "password": "testpass123"
    })
    assert login.status_code == 200, login.text
    headers = {"Authorization": f"Bearer {login.json()['access_token']}"}

    input_data = {"transaction_id": "TX789", "amount": 90000, "risk_score": 0.91}

    created = client.post("/api/v1/runs/", headers=headers, json={
        "workflow_id": str(sample_workflow.id),
        "account_id": "ACC789",
        "input_data": input_data,
        "trigger_reason": "High-value transaction detected"
    })
    assert created.status_code == 201, created.text
    run = created.json()
    assert run["status"] == RunStatus.COMPLETED.value

    # Unauthenticated runs are rejected
    denied = client.post("/api/v1/runs/", json={
        "workflow_id": str(sample_workflow.id),
        "account_id": "ACC789",
    })
    assert denied.status_code == 401

    # Audit trail: one node execution per step, inputs recorded
    executions = client.get(f"/api/v1/runs/{run['id']}/executions")
    assert executions.status_code == 200, executions.text
    assert len(executions.json()) == 2

    # Replay endpoint re-executes with the stored inputs
    replay = client.post(f"/api/v1/runs/{run['id']}/replay", headers=headers)
    assert replay.status_code == 201, replay.text
    assert replay.json()["status"] == RunStatus.COMPLETED.value


def test_workflow_endpoints_require_auth_and_record_user(client):
    """Draft/approve record the JWT caller (not a placeholder UUID); 401 without a token."""
    email = f"author_{uuid4()}@example.com"
    client.post("/api/v1/auth/register", json={
        "email": email, "password": "testpass123", "full_name": "Author"
    })
    login = client.post("/api/v1/auth/login", data={"username": email, "password": "testpass123"})
    assert login.status_code == 200, login.text
    headers = {"Authorization": f"Bearer {login.json()['access_token']}"}
    me = client.get("/api/v1/auth/me", headers=headers).json()

    created = client.post("/api/v1/workflows/draft", headers=headers, json={
        "name": "Auth Audit Workflow",
        "domain": "fraud",
        "steps": [
            {"order_index": 0, "step_type": "AI_ANALYSIS", "name": "Analyze"},
        ],
    })
    assert created.status_code == 201, created.text
    body = created.json()
    # created_by must be the authenticated user, not a random uuid4()
    assert body["created_by"] == me["id"], body

    approved = client.post(f"/api/v1/workflows/{body['id']}/approve", headers=headers)
    assert approved.status_code == 200, approved.text
    # approved_by must be the authenticated user as well
    assert approved.json()["approved_by"] == me["id"]

    # Unauthenticated mutations are now rejected
    assert client.post("/api/v1/workflows/draft", json={
        "name": "no token", "domain": "fraud", "steps": []
    }).status_code == 401
    assert client.post("/api/v1/documents/upload").status_code == 401


def test_run_endpoint_fails_without_rule(client, sample_workflow, mock_llm):
    """Rule references that do not exist in the DB fail loudly, not silently."""
    email = f"runner_{uuid4()}@example.com"
    client.post("/api/v1/auth/register", json={
        "email": email, "password": "testpass123", "full_name": "Run User"
    })
    login = client.post("/api/v1/auth/login", data={
        "username": email, "password": "testpass123"
    })
    headers = {"Authorization": f"Bearer {login.json()['access_token']}"}

    created = client.post("/api/v1/runs/", headers=headers, json={
        "workflow_id": str(sample_workflow.id),
        "account_id": "ACC999",
        "input_data": {"risk_score": 0.5},
    })
    assert created.status_code == 201, created.text
    assert created.json()["status"] == RunStatus.FAILED.value
