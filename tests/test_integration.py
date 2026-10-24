"""
Integration tests for the AI-governed workflow system.

These tests run entirely in-process:
- API tests go through `fastapi.testclient.TestClient` (no live server needed)
- executor tests call `WorkflowExecutor` directly
- LLM calls are mocked by the `mock_llm` fixture in conftest.py
"""
import asyncio
from datetime import datetime
from uuid import UUID, uuid4

import pytest
import pytest_asyncio
from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.database import get_session_maker
from app.core.security import get_password_hash
from app.main import app
from app.models.user import User
from app.models.rule import Rule, RuleVersion
from app.models.workflow import WorkflowDefinition, WorkflowStep, StepType, WorkflowStatus
from app.models.execution import RunStatus, WorkflowRun, NodeStatus, NodeExecution
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


def test_rules_api_auth_and_duplicate_validation(client):
    """
    Test auth constraints and duplicate validations on Rules API:
    - Mutation endpoints reject unauthenticated requests with 401
    - Duplicate rule name returns 409 Conflict
    """
    rule_name = f"rule_{uuid4().hex[:8]}"

    # 1. Unauthenticated mutations -> 401
    unauth_create = client.post("/api/v1/rules", json={
        "name": rule_name,
        "domain": "fraud",
        "logic_json": {"condition": {"field": "amount", "op": ">", "value": 100}, "result": "FLAG"}
    })
    assert unauth_create.status_code == 401, unauth_create.text

    unauth_version = client.post(f"/api/v1/rules/{uuid4()}/versions", json={
        "logic_json": {"result": "FLAG_V2"}
    })
    assert unauth_version.status_code == 401, unauth_version.text

    unauth_delete = client.delete(f"/api/v1/rules/{uuid4()}")
    assert unauth_delete.status_code == 401, unauth_delete.text

    # 2. Register & login
    email = f"rule_auth_{uuid4()}@example.com"
    client.post("/api/v1/auth/register", json={
        "email": email, "password": "testpass123", "full_name": "Auth Tester"
    })
    login = client.post("/api/v1/auth/login", data={
        "username": email, "password": "testpass123"
    })
    headers = {"Authorization": f"Bearer {login.json()['access_token']}"}

    # 3. Create initial rule -> 201
    created = client.post("/api/v1/rules", headers=headers, json={
        "name": rule_name,
        "description": "Initial rule for validation test",
        "domain": "fraud",
        "logic_json": {"condition": {"field": "amount", "op": ">", "value": 100}, "result": "FLAG"}
    })
    assert created.status_code == 201, created.text

    # 4. Duplicate name in same domain -> 409
    dup = client.post("/api/v1/rules", headers=headers, json={
        "name": rule_name,
        "description": "Duplicate name attempt",
        "domain": "fraud",
        "logic_json": {"result": "DUP"}
    })
    assert dup.status_code == 409, dup.text


def test_rules_api_crud_workflow_execution_and_versioning(client, mock_llm):
    """
    End-to-end behavioral test:
    - Register + login
    - Create a rule via POST /rules (v1, active)
    - Query rule via GET /rules and GET /rules/{rule_id} (includes versions)
    - Build workflow whose DOMAIN_RULE step's config_json.rule_id references the rule NAME
    - Approve the workflow
    - Run via POST /runs with matching input -> run uses API-created rule's logic and COMPLETES
    - Add a new version v2 via POST /rules/{rule_id}/versions -> v1 flips to inactive, v2 is active
    - Run workflow again -> run uses v2 logic and COMPLETES with new result
    - Delete rule via DELETE /rules/{rule_id} -> returns 204, rule gone (404)
    """
    email = f"rule_exec_{uuid4()}@example.com"
    reg_resp = client.post("/api/v1/auth/register", json={
        "email": email, "password": "testpass123", "full_name": "Rule Exec User"
    })
    user_id = reg_resp.json()["id"]

    login = client.post("/api/v1/auth/login", data={
        "username": email, "password": "testpass123"
    })
    headers = {"Authorization": f"Bearer {login.json()['access_token']}"}

    # 1. Create rule via API
    rule_name = f"fraud_tx_threshold_{uuid4().hex[:8]}"
    rule_data = {
        "name": rule_name,
        "description": "Flag transactions exceeding $500",
        "domain": "fraud",
        "logic_json": {
            "condition": {"field": "amount", "op": ">", "value": 500},
            "result": "FLAG_OVER_500"
        }
    }
    create_resp = client.post("/api/v1/rules", headers=headers, json=rule_data)
    assert create_resp.status_code == 201, create_resp.text
    rule_body = create_resp.json()
    rule_id = rule_body["id"]

    assert rule_body["name"] == rule_name
    assert rule_body["domain"] == "fraud"
    assert len(rule_body["versions"]) == 1
    v1 = rule_body["versions"][0]
    assert v1["version_number"] == 1
    assert v1["is_active"] is True
    assert v1["created_by"] == user_id
    assert v1["logic_json"] == rule_data["logic_json"]

    # Also verify rule creation works when providing initial_logic
    alt_rule_name = f"alt_rule_{uuid4().hex[:8]}"
    alt_create = client.post("/api/v1/rules", headers=headers, json={
        "name": alt_rule_name,
        "domain": "fraud",
        "initial_logic": {"condition": {"field": "amount", "op": ">", "value": 100}, "result": "OK"}
    })
    assert alt_create.status_code == 201, alt_create.text
    assert len(alt_create.json()["versions"]) == 1

    # 2. GET /rules and GET /rules/{rule_id} include versions
    get_resp = client.get(f"/api/v1/rules/{rule_id}")
    assert get_resp.status_code == 200, get_resp.text
    assert get_resp.json()["id"] == rule_id
    assert len(get_resp.json()["versions"]) == 1

    list_resp = client.get("/api/v1/rules?domain=fraud")
    assert list_resp.status_code == 200, list_resp.text
    matching_rules = [r for r in list_resp.json() if r["id"] == rule_id]
    assert len(matching_rules) == 1
    assert len(matching_rules[0]["versions"]) == 1

    # 3. Build a workflow whose DOMAIN_RULE step's config_json.rule_id references rule NAME
    wf_create = client.post("/api/v1/workflows/draft", headers=headers, json={
        "name": f"Workflow with Rule {rule_name}",
        "description": "Evaluates custom rule created via API",
        "domain": "fraud",
        "steps": [
            {
                "order_index": 0,
                "step_type": "AI_ANALYSIS",
                "name": "Risk Analysis",
                "description": "AI analyzes transaction",
                "config_json": {
                    "prompt_template": "Analyze transaction risk"
                }
            },
            {
                "order_index": 1,
                "step_type": "DOMAIN_RULE",
                "name": "Threshold Check",
                "description": "Apply custom amount threshold rule",
                "config_json": {
                    "rule_id": rule_name
                }
            }
        ]
    })
    assert wf_create.status_code == 201, wf_create.text
    wf_id = wf_create.json()["id"]

    # Approve workflow
    approve_resp = client.post(f"/api/v1/workflows/{wf_id}/approve", headers=headers)
    assert approve_resp.status_code == 200, approve_resp.text

    # 4. Run via POST /runs with matching input — must use API-created rule's logic and COMPLETE
    run_resp = client.post("/api/v1/runs/", headers=headers, json={
        "workflow_id": wf_id,
        "account_id": "ACC_TEST_RULE_1",
        "input_data": {"amount": 750},
        "trigger_reason": "Testing API-created rule"
    })
    assert run_resp.status_code == 201, run_resp.text
    run_body = run_resp.json()
    assert run_body["status"] == RunStatus.COMPLETED.value

    # Verify node execution recorded rule output
    execs_resp = client.get(f"/api/v1/runs/{run_body['id']}/executions")
    assert execs_resp.status_code == 200, execs_resp.text
    execs = execs_resp.json()
    assert len(execs) == 2
    assert execs[0]["node_type"] == StepType.AI_ANALYSIS.value
    assert execs[0]["status"] == NodeStatus.COMPLETED.value
    assert execs[1]["node_type"] == StepType.DOMAIN_RULE.value
    assert execs[1]["status"] == NodeStatus.COMPLETED.value
    assert execs[1]["output_snapshot"] == "FLAG_OVER_500"

    # 5. Add a new version via POST /rules/{rule_id}/versions
    v2_logic = {
        "condition": {"field": "amount", "op": ">", "value": 500},
        "result": "FLAG_OVER_500_V2"
    }
    v2_resp = client.post(
        f"/api/v1/rules/{rule_id}/versions",
        headers=headers,
        json={"logic_json": v2_logic}
    )
    assert v2_resp.status_code == 201, v2_resp.text
    v2_body = v2_resp.json()
    assert v2_body["version_number"] == 2
    assert v2_body["is_active"] is True
    assert v2_body["rule_id"] == rule_id
    assert v2_body["created_by"] == user_id

    # Verify older version is flipped to is_active=False
    updated_rule = client.get(f"/api/v1/rules/{rule_id}").json()
    assert len(updated_rule["versions"]) == 2
    v2_item = next(v for v in updated_rule["versions"] if v["version_number"] == 2)
    v1_item = next(v for v in updated_rule["versions"] if v["version_number"] == 1)
    assert v2_item["is_active"] is True
    assert v1_item["is_active"] is False

    # 6. Run workflow again — executor resolves latest active version (v2) and COMPLETES
    run_v2_resp = client.post("/api/v1/runs/", headers=headers, json={
        "workflow_id": wf_id,
        "account_id": "ACC_TEST_RULE_2",
        "input_data": {"amount": 750},
        "trigger_reason": "Testing v2 rule resolution"
    })
    assert run_v2_resp.status_code == 201, run_v2_resp.text
    run_v2_body = run_v2_resp.json()
    assert run_v2_body["status"] == RunStatus.COMPLETED.value

    execs_v2 = client.get(f"/api/v1/runs/{run_v2_body['id']}/executions").json()
    assert execs_v2[1]["output_snapshot"] == "FLAG_OVER_500_V2"

    # 7. Delete rule via DELETE /rules/{rule_id} — still referenced by the
    # workflow above (left in place deliberately), so an unforced delete must
    # be refused with 409; the forced delete proceeds and the rule is gone.
    assert client.delete(f"/api/v1/rules/{rule_id}", headers=headers).status_code == 409
    del_resp = client.delete(f"/api/v1/rules/{rule_id}?force=true", headers=headers)
    assert del_resp.status_code == 204

    # Rule is no longer accessible
    assert client.get(f"/api/v1/rules/{rule_id}").status_code == 404
def test_rejected_run_error_message_persists_in_database(client, approval_workflow, high_risk_rule, mock_llm):
    """
    The rejection reason must live in the committed workflow_runs row
    (read through an independent session), not merely in request-scoped
    response state — the durable-audit requirement for failure reasons.
    """
    email = f"persist_check_{uuid4()}@example.com"
    client.post("/api/v1/auth/register", json={
        "email": email, "password": "testpass123", "full_name": "Persist Checker"
    })
    login = client.post("/api/v1/auth/login", data={
        "username": email, "password": "testpass123"
    })
    headers = {"Authorization": f"Bearer {login.json()['access_token']}"}

    created = client.post("/api/v1/runs/", headers=headers, json={
        "workflow_id": str(approval_workflow.id),
        "account_id": "ACC_PERSIST",
        "input_data": {"risk_score": 0.9},
    })
    assert created.status_code == 201, created.text
    run_id = created.json()["id"]
    assert created.json()["status"] == RunStatus.PAUSED.value

    rejection_reason = "Rejected: reason must survive request scope"
    reject_resp = client.post(
        f"/api/v1/runs/{run_id}/reject",
        headers=headers,
        json={"reason": rejection_reason}
    )
    assert reject_resp.status_code == 200, reject_resp.text

    async def _read_committed_row():
        session_maker = get_session_maker()
        async with session_maker() as session:
            return (await session.execute(
                select(WorkflowRun).where(WorkflowRun.id == UUID(run_id))
            )).scalar_one()

    row = asyncio.run(_read_committed_row())
    assert row.status == RunStatus.FAILED
    assert row.error_message == rejection_reason


async def test_concurrent_approves_exactly_one_wins(db_session, approval_workflow, high_risk_rule, mock_llm):
    """
    Two simultaneous approve attempts on one PAUSED run: the compare-and-swap
    claim must let exactly one proceed, fail the other closed ('not in
    PAUSED'), and never execute a step twice (no duplicated audit rows).
    """
    executor = WorkflowExecutor()
    run = await executor.execute(
        workflow_id=approval_workflow.id,
        input_data={"risk_score": 0.9},
        account_id="ACC_RACE",
        db=db_session
    )
    assert run.status == RunStatus.PAUSED

    session_maker = get_session_maker()

    async def attempt():
        async with session_maker() as session:
            return await WorkflowExecutor().resume(
                run.id,
                {
                    "approved": True,
                    "approver": "racer@example.com",
                    "notes": "race claim",
                    "timestamp": datetime.utcnow().isoformat()
                },
                session,
            )

    results = await asyncio.gather(attempt(), attempt(), return_exceptions=True)
    succeeded = [r for r in results if not isinstance(r, BaseException)]
    failed = [r for r in results if isinstance(r, BaseException)]

    assert len(succeeded) == 1, results
    assert len(failed) == 1, results
    assert "not in PAUSED" in str(failed[0])

    # No double execution: the winner leaves exactly 3 COMPLETED node rows.
    async with session_maker() as audit_session:
        fresh = await audit_session.get(WorkflowRun, run.id)
        assert fresh.status == RunStatus.COMPLETED
        execs = (await audit_session.execute(
            select(NodeExecution).where(NodeExecution.run_id == run.id)
        )).scalars().all()
        assert len(execs) == 3
        assert all(e.status == NodeStatus.COMPLETED for e in execs)


def test_delete_referenced_rule_guarded_by_409_and_force(client, mock_llm):
    """
    A rule referenced by an approved workflow cannot be silently deleted:
    - DELETE /rules/{id}            -> 409, detail lists the referencing workflow
    - DELETE still possible with ?force=true -> 204, rule 404 afterwards, and a
      run of the referencing workflow then fails with a resolution error
      (the documented blast radius).
    """
    email = f"guard_{uuid4()}@example.com"
    client.post("/api/v1/auth/register", json={
        "email": email, "password": "testpass123", "full_name": "Guard Tester"
    })
    login = client.post("/api/v1/auth/login", data={
        "username": email, "password": "testpass123"
    })
    headers = {"Authorization": f"Bearer {login.json()['access_token']}"}

    rule_name = f"guarded_rule_{uuid4().hex[:8]}"
    create = client.post("/api/v1/rules", headers=headers, json={
        "name": rule_name,
        "domain": "fraud",
        "logic_json": {"condition": {"field": "amount", "op": ">", "value": 100}, "result": "OK"}
    })
    assert create.status_code == 201, create.text
    rule_id = create.json()["id"]

    wf = client.post("/api/v1/workflows/draft", headers=headers, json={
        "name": f"Guard WF {rule_name}",
        "description": "references the guarded rule",
        "domain": "fraud",
        "steps": [{
            "order_index": 0,
            "step_type": "DOMAIN_RULE",
            "name": "Check",
            "description": "uses guarded rule",
            "config_json": {"rule_id": rule_name}
        }]
    })
    assert wf.status_code == 201, wf.text
    wf_id = wf.json()["id"]
    assert client.post(f"/api/v1/workflows/{wf_id}/approve", headers=headers).status_code == 200

    # Unguarded delete -> 409 naming the referencing workflow; rule survives
    guarded = client.delete(f"/api/v1/rules/{rule_id}", headers=headers)
    assert guarded.status_code == 409, guarded.text
    assert str(wf_id) in guarded.text
    assert client.get(f"/api/v1/rules/{rule_id}").status_code == 200

    # Forced delete -> 204, rule gone, referencing workflow's next run fails
    forced = client.delete(f"/api/v1/rules/{rule_id}?force=true", headers=headers)
    assert forced.status_code == 204, forced.text
    assert client.get(f"/api/v1/rules/{rule_id}").status_code == 404

    run = client.post("/api/v1/runs/", headers=headers, json={
        "workflow_id": wf_id,
        "account_id": "ACC_FORCE_DEL",
        "input_data": {"amount": 500}
    })
    assert run.status_code == 201, run.text
    body = run.json()
    assert body["status"] == RunStatus.FAILED.value
    assert rule_name in body["error_message"]


async def test_rule_version_constraints_enforced(db_session, sample_user):
    """
    DB-level integrity from the model constraints (mirrored by migration
    d7e2b4f9c1a6): duplicate (rule_id, version_number) rows and a second
    ACTIVE version for the same rule are both rejected by the database.
    """
    from sqlalchemy.exc import IntegrityError as IE

    rule = Rule(
        id=uuid4(),
        name=f"constraint_rule_{uuid4().hex[:8]}",
        description=None,
        domain="fraud"
    )
    db_session.add(rule)
    await db_session.flush()

    v1 = RuleVersion(
        id=uuid4(),
        rule_id=rule.id,
        version_number=1,
        logic_json={"result": "V1"},
        is_active=True,
        created_by=sample_user.id
    )
    db_session.add(v1)
    await db_session.commit()

    session_maker = get_session_maker()

    # 1. Same version_number twice -> violates uq_rule_version_number
    async with session_maker() as s:
        dup = RuleVersion(
            id=uuid4(),
            rule_id=rule.id,
            version_number=1,
            logic_json={"result": "dup"},
            is_active=True,
            created_by=sample_user.id
        )
        s.add(dup)
        with pytest.raises(IE):
            await s.commit()
        await s.rollback()

    # 2. Second ACTIVE version for the same rule -> violates uq_rule_active_version
    async with session_maker() as s:
        second_active = RuleVersion(
            id=uuid4(),
            rule_id=rule.id,
            version_number=2,
            logic_json={"result": "two actives"},
            is_active=True,
            created_by=sample_user.id
        )
        s.add(second_active)
        with pytest.raises(IE):
            await s.commit()
        await s.rollback()
