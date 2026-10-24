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
from app.models.execution import RunStatus, WorkflowRun, NodeStatus
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


@pytest.fixture
async def approval_workflow(db_session: AsyncSession, sample_user: User):
    """Create an APPROVED workflow with AI step + Approval step + Rule step."""
    workflow = WorkflowDefinition(
        id=uuid4(),
        name="Test Approval Workflow",
        description="Workflow with AI analysis, human approval, and domain rule",
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
                "prompt_template": "Analyze this transaction for fraud risk"
            }
        ),
        WorkflowStep(
            id=uuid4(),
            workflow_id=workflow.id,
            order_index=1,
            step_type=StepType.HUMAN_APPROVAL,
            name="Fraud Manager Approval",
            description="Human reviewer must approve transaction before decision rules execute",
            config_json={}
        ),
        WorkflowStep(
            id=uuid4(),
            workflow_id=workflow.id,
            order_index=2,
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


def test_approval_flow_pause_resume_audit_and_replay(client, approval_workflow, high_risk_rule, mock_llm):
    """
    Workflow with AI step + approval step + rule step:
    - posted to POST /runs, returns status PAUSED
    - approving it via POST /runs/{id}/approve drives it to COMPLETED
    - rule step executes and is audited in GET /runs/{id}/executions
    - replay via POST /runs/{id}/replay works end-to-end on resumed run
    """
    email = f"approver_{uuid4()}@example.com"
    client.post("/api/v1/auth/register", json={
        "email": email, "password": "testpass123", "full_name": "Approver User"
    })
    login = client.post("/api/v1/auth/login", data={
        "username": email, "password": "testpass123"
    })
    headers = {"Authorization": f"Bearer {login.json()['access_token']}"}

    input_data = {"transaction_id": "TX_APPROVAL_1", "amount": 60000, "risk_score": 0.88}

    # 1. Post run -> reaches HUMAN_APPROVAL step and pauses
    created = client.post("/api/v1/runs/", headers=headers, json={
        "workflow_id": str(approval_workflow.id),
        "account_id": "ACC_APP_1",
        "input_data": input_data,
        "trigger_reason": "High fraud score requires human approval"
    })
    assert created.status_code == 201, created.text
    run = created.json()
    run_id = run["id"]
    assert run["status"] == RunStatus.PAUSED.value
    assert run["current_step_index"] == 1

    # 2. Check executions while paused: AI step COMPLETED, Approval step PENDING, Rule step not run
    executions_paused = client.get(f"/api/v1/runs/{run_id}/executions").json()
    assert len(executions_paused) == 2
    assert executions_paused[0]["node_type"] == StepType.AI_ANALYSIS.value
    assert executions_paused[0]["status"] == NodeStatus.COMPLETED.value
    assert executions_paused[1]["node_type"] == StepType.HUMAN_APPROVAL.value
    assert executions_paused[1]["status"] == NodeStatus.PENDING.value

    # 3. Approve via POST /runs/{run_id}/approve
    approve_resp = client.post(
        f"/api/v1/runs/{run_id}/approve",
        headers=headers,
        json={"notes": "Manager verified via phone call"}
    )
    assert approve_resp.status_code == 200, approve_resp.text
    approved_run = approve_resp.json()
    assert approved_run["status"] == RunStatus.COMPLETED.value

    # 4. Check executions after approval: all 3 steps COMPLETED, rule step evaluated
    executions_completed = client.get(f"/api/v1/runs/{run_id}/executions").json()
    assert len(executions_completed) == 3
    assert executions_completed[0]["status"] == NodeStatus.COMPLETED.value
    assert executions_completed[1]["status"] == NodeStatus.COMPLETED.value
    assert executions_completed[1]["output_snapshot"]["approved"] is True
    assert executions_completed[1]["output_snapshot"]["notes"] == "Manager verified via phone call"
    assert executions_completed[2]["node_type"] == StepType.DOMAIN_RULE.value
    assert executions_completed[2]["status"] == NodeStatus.COMPLETED.value
    assert executions_completed[2]["output_snapshot"] == "ESCALATE"

    # 5. Replay must work end-to-end on the resumed run
    replay_resp = client.post(f"/api/v1/runs/{run_id}/replay", headers=headers)
    assert replay_resp.status_code == 201, replay_resp.text
    replayed = replay_resp.json()
    assert replayed["status"] == RunStatus.COMPLETED.value
    assert replayed["id"] != run_id

    # Verify replay node executions are also all completed
    replay_execs = client.get(f"/api/v1/runs/{replayed['id']}/executions").json()
    assert len(replay_execs) == 3
    assert all(e["status"] == NodeStatus.COMPLETED.value for e in replay_execs)


def test_approval_flow_rejection(client, approval_workflow, high_risk_rule, mock_llm):
    """
    A rejected run ends FAILED with the rejection reason recorded:
    - approval step marked FAILED with reason
    - following steps not executed
    """
    email = f"rejector_{uuid4()}@example.com"
    client.post("/api/v1/auth/register", json={
        "email": email, "password": "testpass123", "full_name": "Rejector User"
    })
    login = client.post("/api/v1/auth/login", data={
        "username": email, "password": "testpass123"
    })
    headers = {"Authorization": f"Bearer {login.json()['access_token']}"}

    created = client.post("/api/v1/runs/", headers=headers, json={
        "workflow_id": str(approval_workflow.id),
        "account_id": "ACC_REJ_1",
        "input_data": {"risk_score": 0.99},
    })
    assert created.status_code == 201, created.text
    run_id = created.json()["id"]
    assert created.json()["status"] == RunStatus.PAUSED.value

    # Reject run
    rejection_reason = "Transaction flagged as confirmed identity fraud"
    reject_resp = client.post(
        f"/api/v1/runs/{run_id}/reject",
        headers=headers,
        json={"reason": rejection_reason}
    )
    assert reject_resp.status_code == 200, reject_resp.text
    rejected_run = reject_resp.json()
    assert rejected_run["status"] == RunStatus.FAILED.value
    assert rejected_run["error_message"] == rejection_reason

    # Audit check: approval node execution is FAILED with rejection reason
    executions = client.get(f"/api/v1/runs/{run_id}/executions").json()
    assert len(executions) == 2
    assert executions[0]["status"] == NodeStatus.COMPLETED.value
    assert executions[1]["status"] == NodeStatus.FAILED.value
    assert executions[1]["error_message"] == rejection_reason
    assert executions[1]["output_snapshot"]["approved"] is False

    # GET /runs/{run_id} also shows FAILED and error_message
    get_run_resp = client.get(f"/api/v1/runs/{run_id}").json()
    assert get_run_resp["status"] == RunStatus.FAILED.value
    assert get_run_resp["error_message"] == rejection_reason


def test_approval_endpoints_non_paused_error(client, approval_workflow, sample_workflow, high_risk_rule, mock_llm):
    """
    Reject on non-PAUSED runs -> 400.
    Approving twice -> 400.
    """
    email = f"error_tester_{uuid4()}@example.com"
    client.post("/api/v1/auth/register", json={
        "email": email, "password": "testpass123", "full_name": "Tester"
    })
    login = client.post("/api/v1/auth/login", data={
        "username": email, "password": "testpass123"
    })
    headers = {"Authorization": f"Bearer {login.json()['access_token']}"}

    # 1. Normal workflow that completes without pausing
    norm_run = client.post("/api/v1/runs/", headers=headers, json={
        "workflow_id": str(sample_workflow.id),
        "account_id": "ACC_NORM",
        "input_data": {"risk_score": 0.85},
    }).json()
    assert norm_run["status"] == RunStatus.COMPLETED.value

    # Approve on non-paused run -> 400
    res = client.post(f"/api/v1/runs/{norm_run['id']}/approve", headers=headers)
    assert res.status_code == 400, res.text

    # Reject on non-paused run -> 400
    res = client.post(f"/api/v1/runs/{norm_run['id']}/reject", headers=headers)
    assert res.status_code == 400, res.text

    # 2. Approval workflow: pause then approve
    paused_run = client.post("/api/v1/runs/", headers=headers, json={
        "workflow_id": str(approval_workflow.id),
        "account_id": "ACC_PAUSED_ERR",
        "input_data": {"risk_score": 0.85},
    }).json()
    assert paused_run["status"] == RunStatus.PAUSED.value

    # First approve succeeds -> COMPLETED
    res = client.post(f"/api/v1/runs/{paused_run['id']}/approve", headers=headers)
    assert res.status_code == 200

    # Approving twice -> 400
    res = client.post(f"/api/v1/runs/{paused_run['id']}/approve", headers=headers)
    assert res.status_code == 400, res.text

    # Rejecting now that it is COMPLETED -> 400
    res = client.post(f"/api/v1/runs/{paused_run['id']}/reject", headers=headers)
    assert res.status_code == 400, res.text

    # 3. Nonexistent run -> 404
    random_id = str(uuid4())
    assert client.post(f"/api/v1/runs/{random_id}/approve", headers=headers).status_code == 404
    assert client.post(f"/api/v1/runs/{random_id}/reject", headers=headers).status_code == 404

    # 4. Unauthenticated calls -> 401
    assert client.post(f"/api/v1/runs/{paused_run['id']}/approve").status_code == 401
    assert client.post(f"/api/v1/runs/{paused_run['id']}/reject").status_code == 401


def test_approval_workflow_missing_rule_fails(client, approval_workflow, mock_llm):
    """The executor rule-fix semantics (a missing rule => run FAILED, row persisted) are preserved on approval workflows."""
    email = f"rule_fail_runner_{uuid4()}@example.com"
    client.post("/api/v1/auth/register", json={
        "email": email, "password": "testpass123", "full_name": "Rule Fail User"
    })
    login = client.post("/api/v1/auth/login", data={
        "username": email, "password": "testpass123"
    })
    headers = {"Authorization": f"Bearer {login.json()['access_token']}"}

    # approval_workflow references "high_risk_threshold", which is not seeded in this test
    created = client.post("/api/v1/runs/", headers=headers, json={
        "workflow_id": str(approval_workflow.id),
        "account_id": "ACC_MISSING_RULE",
        "input_data": {"risk_score": 0.5},
    })
    assert created.status_code == 201, created.text
    run_body = created.json()
    assert run_body["status"] == RunStatus.FAILED.value
    assert "high_risk_threshold" in run_body["error_message"]
