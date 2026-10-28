"""
Contract tests for background run execution (issue #9).

``POST /api/v1/runs`` now returns 202 Accepted with the run id while the
executor runs in a FastAPI background task. Starlette's ``TestClient`` awaits
background tasks before handing control back to the test, so these tests assert
the *contract* (202 + run id + pollable terminal status) rather than wall-clock
timing.
"""
import time
from uuid import UUID, uuid4

import pytest
from fastapi.testclient import TestClient

from app.core.database import get_session_maker
from app.main import app
from app.models.execution import RunStatus, WorkflowRun

from conftest import grant_role

# A run in one of these states will not progress on its own.
TERMINAL_STATUSES = {
    RunStatus.COMPLETED.value,
    RunStatus.FAILED.value,
    RunStatus.PAUSED.value,
}
ALL_STATUSES = {status.value for status in RunStatus}


@pytest.fixture
def client():
    """In-process HTTP test client (startup initializes the test database)."""
    with TestClient(app) as test_client:
        yield test_client


def _register_and_login(client: TestClient) -> dict:
    email = f"bg_runner_{uuid4().hex}@example.com"
    register = client.post("/api/v1/auth/register", json={
        "email": email,
        "password": "testpass123",
        "full_name": "Background Run User",
    })
    assert register.status_code == 201, register.text
    grant_role(email, "admin")

    login = client.post("/api/v1/auth/login", data={
        "username": email,
        "password": "testpass123",
    })
    assert login.status_code == 200, login.text
    return {"Authorization": f"Bearer {login.json()['access_token']}"}


def _create_approved_workflow(
    client: TestClient,
    headers: dict,
    *,
    name: str,
    steps: list,
) -> str:
    created = client.post("/api/v1/workflows/draft", headers=headers, json={
        "name": f"{name} {uuid4().hex[:8]}",
        "description": "Background execution test workflow",
        "domain": "fraud",
        "steps": steps,
    })
    assert created.status_code == 201, created.text
    workflow_id = created.json()["id"]

    approved = client.post(f"/api/v1/workflows/{workflow_id}/approve", headers=headers)
    assert approved.status_code == 200, approved.text
    return workflow_id


def _poll_until_terminal(
    client: TestClient,
    run_id: str,
    headers: dict,
    *,
    timeout_seconds: float = 5.0,
) -> dict:
    """Poll GET /runs/{id} until it reaches COMPLETED, FAILED, or PAUSED."""
    deadline = time.monotonic() + timeout_seconds
    last_body = None
    while time.monotonic() < deadline:
        response = client.get(f"/api/v1/runs/{run_id}", headers=headers)
        assert response.status_code == 200, response.text
        last_body = response.json()
        if last_body["status"] in TERMINAL_STATUSES:
            return last_body
        time.sleep(0.05)
    raise AssertionError(
        f"Run {run_id} did not reach a terminal status within "
        f"{timeout_seconds}s; last seen: {last_body}"
    )


async def test_post_run_returns_202_and_is_pollable_to_completion(client, mock_llm):
    """202 + run id on POST; GET /runs/{id} then reaches COMPLETED."""
    headers = _register_and_login(client)
    workflow_id = _create_approved_workflow(
        client,
        headers,
        name="Background Completion Workflow",
        steps=[{
            "order_index": 0,
            "step_type": "AI_ANALYSIS",
            "name": "Risk Analysis",
            "description": "AI analyzes transaction risk",
            "config_json": {"prompt_template": "Analyze this transaction"},
        }],
    )
    input_data = {"transaction_id": "TX_BG_1", "risk_score": 0.4}

    created = client.post("/api/v1/runs/", headers=headers, json={
        "workflow_id": workflow_id,
        "account_id": f"ACC_BG_{uuid4().hex[:8]}",
        "input_data": input_data,
        "trigger_reason": "Background execution test",
    })

    # 202 Accepted, not 201 with a final result.
    assert created.status_code == 202, created.text
    body = created.json()
    UUID(body["id"])  # run id is present and well-formed
    assert body["workflow_id"] == workflow_id
    # The response body is a snapshot taken before the background task runs:
    # a synchronous endpoint would already report COMPLETED here.
    assert body["status"] == RunStatus.RUNNING.value, body

    # Polling the run resource reaches a terminal state.
    final = _poll_until_terminal(client, body["id"], headers)
    assert final["id"] == body["id"]
    assert final["status"] == RunStatus.COMPLETED.value, final
    assert final["error_message"] is None

    # Inputs and trigger ownership were committed with the row before the
    # background task ran (read through an independent session).
    async with get_session_maker()() as session:
        persisted = await session.get(WorkflowRun, UUID(body["id"]))
        assert persisted is not None
        assert persisted.input_data == input_data
        assert persisted.triggered_by is not None

    # The deferred execution still produced the audit trail.
    executions = client.get(f"/api/v1/runs/{body['id']}/executions", headers=headers)
    assert executions.status_code == 200, executions.text
    assert len(executions.json()) == 1


def test_post_run_polls_to_paused_at_human_approval(client, mock_llm):
    """PAUSED is a terminal polling state: approval gates are not auto-run."""
    headers = _register_and_login(client)
    workflow_id = _create_approved_workflow(
        client,
        headers,
        name="Background Approval Workflow",
        steps=[
            {
                "order_index": 0,
                "step_type": "AI_ANALYSIS",
                "name": "Risk Analysis",
                "description": "AI analyzes transaction risk",
                "config_json": {"prompt_template": "Analyze this transaction"},
            },
            {
                "order_index": 1,
                "step_type": "HUMAN_APPROVAL",
                "name": "Manager Approval",
                "description": "Human review before proceeding",
                "config_json": {},
            },
        ],
    )

    created = client.post("/api/v1/runs/", headers=headers, json={
        "workflow_id": workflow_id,
        "account_id": f"ACC_BG_PAUSE_{uuid4().hex[:8]}",
        "input_data": {"risk_score": 0.9},
    })
    assert created.status_code == 202, created.text
    assert created.json()["status"] in ALL_STATUSES

    final = _poll_until_terminal(client, created.json()["id"], headers)
    assert final["status"] == RunStatus.PAUSED.value, final
    assert final["current_step_index"] == 1


def test_duplicate_active_run_still_returns_409(client, mock_llm):
    """The one-active-run-per-account guard still fires while a run is active."""
    headers = _register_and_login(client)
    workflow_id = _create_approved_workflow(
        client,
        headers,
        name="Background Duplicate Guard Workflow",
        steps=[
            {
                "order_index": 0,
                "step_type": "AI_ANALYSIS",
                "name": "Risk Analysis",
                "description": "AI analyzes transaction risk",
                "config_json": {"prompt_template": "Analyze this transaction"},
            },
            {
                "order_index": 1,
                "step_type": "HUMAN_APPROVAL",
                "name": "Manager Approval",
                "description": "Human review before proceeding",
                "config_json": {},
            },
        ],
    )
    account_id = f"ACC_BG_DUP_{uuid4().hex[:8]}"

    first = client.post("/api/v1/runs/", headers=headers, json={
        "workflow_id": workflow_id,
        "account_id": account_id,
        "input_data": {"risk_score": 0.9},
    })
    assert first.status_code == 202, first.text
    first_run_id = first.json()["id"]
    assert _poll_until_terminal(client, first_run_id, headers)["status"] == RunStatus.PAUSED.value

    # The paused run is still active for this account -> 409, as before.
    second = client.post("/api/v1/runs/", headers=headers, json={
        "workflow_id": workflow_id,
        "account_id": account_id,
        "input_data": {"risk_score": 0.9},
    })
    assert second.status_code == 409, second.text
    assert second.json()["detail"] == (
        f"Account {account_id} already has an active run: {first_run_id}"
    )


def test_active_run_guard_is_scoped_per_account(client, mock_llm):
    """A paused run blocks only its own account; another account may still start."""
    headers = _register_and_login(client)
    workflow_id = _create_approved_workflow(
        client,
        headers,
        name="Background Per-Account Guard Workflow",
        steps=[
            {
                "order_index": 0,
                "step_type": "AI_ANALYSIS",
                "name": "Risk Analysis",
                "description": "AI analyzes transaction risk",
                "config_json": {"prompt_template": "Analyze this transaction"},
            },
            {
                "order_index": 1,
                "step_type": "HUMAN_APPROVAL",
                "name": "Manager Approval",
                "description": "Human review before proceeding",
                "config_json": {},
            },
        ],
    )
    blocked_account = f"ACC_BG_SCOPE_A_{uuid4().hex[:8]}"
    other_account = f"ACC_BG_SCOPE_B_{uuid4().hex[:8]}"

    first = client.post("/api/v1/runs/", headers=headers, json={
        "workflow_id": workflow_id,
        "account_id": blocked_account,
        "input_data": {"risk_score": 0.9},
    })
    assert first.status_code == 202, first.text
    assert _poll_until_terminal(
        client, first.json()["id"], headers
    )["status"] == RunStatus.PAUSED.value

    # Same workflow, different account: the guard is per account, not global.
    other = client.post("/api/v1/runs/", headers=headers, json={
        "workflow_id": workflow_id,
        "account_id": other_account,
        "input_data": {"risk_score": 0.9},
    })
    assert other.status_code == 202, other.text
    assert other.json()["account_id"] == other_account
    other_final = _poll_until_terminal(client, other.json()["id"], headers)
    assert other_final["status"] == RunStatus.PAUSED.value
    assert other_final["account_id"] == other_account


def test_terminal_runs_do_not_block_new_runs(client, mock_llm):
    """COMPLETED and FAILED are terminal: a new run for the same account is accepted."""
    headers = _register_and_login(client)

    completed_workflow_id = _create_approved_workflow(
        client,
        headers,
        name="Background Terminal Completion Workflow",
        steps=[{
            "order_index": 0,
            "step_type": "AI_ANALYSIS",
            "name": "Risk Analysis",
            "description": "AI analyzes transaction risk",
            "config_json": {"prompt_template": "Analyze this transaction"},
        }],
    )
    completed_account = f"ACC_BG_DONE_{uuid4().hex[:8]}"
    first = client.post("/api/v1/runs/", headers=headers, json={
        "workflow_id": completed_workflow_id,
        "account_id": completed_account,
        "input_data": {"risk_score": 0.4},
    })
    assert first.status_code == 202, first.text
    assert _poll_until_terminal(
        client, first.json()["id"], headers
    )["status"] == RunStatus.COMPLETED.value

    retry = client.post("/api/v1/runs/", headers=headers, json={
        "workflow_id": completed_workflow_id,
        "account_id": completed_account,
        "input_data": {"risk_score": 0.4},
    })
    assert retry.status_code == 202, retry.text
    assert retry.json()["id"] != first.json()["id"]
    assert _poll_until_terminal(
        client, retry.json()["id"], headers
    )["status"] == RunStatus.COMPLETED.value

    # A FAILED run is terminal too: the guard must not count it as active.
    failing_workflow_id = _create_approved_workflow(
        client,
        headers,
        name="Background Terminal Failure Workflow",
        steps=[{
            "order_index": 0,
            "step_type": "DOMAIN_RULE",
            "name": "Missing Rule Check",
            "description": "References a rule that does not exist",
            "config_json": {"rule_id": f"missing_rule_{uuid4().hex[:8]}"},
        }],
    )
    failed_account = f"ACC_BG_FAILED_{uuid4().hex[:8]}"
    failed = client.post("/api/v1/runs/", headers=headers, json={
        "workflow_id": failing_workflow_id,
        "account_id": failed_account,
        "input_data": {"risk_score": 0.5},
    })
    assert failed.status_code == 202, failed.text
    assert _poll_until_terminal(
        client, failed.json()["id"], headers
    )["status"] == RunStatus.FAILED.value

    failed_retry = client.post("/api/v1/runs/", headers=headers, json={
        "workflow_id": failing_workflow_id,
        "account_id": failed_account,
        "input_data": {"risk_score": 0.5},
    })
    assert failed_retry.status_code == 202, failed_retry.text
    assert failed_retry.json()["id"] != failed.json()["id"]


def test_background_failure_persists_failed_run_with_error_message(client, mock_llm):
    """An executor failure is durable: polling shows FAILED plus the reason."""
    headers = _register_and_login(client)
    missing_rule = f"missing_rule_{uuid4().hex[:8]}"
    workflow_id = _create_approved_workflow(
        client,
        headers,
        name="Background Failure Workflow",
        steps=[
            {
                "order_index": 0,
                "step_type": "AI_ANALYSIS",
                "name": "Risk Analysis",
                "description": "AI analyzes transaction risk",
                "config_json": {"prompt_template": "Analyze this transaction"},
            },
            {
                "order_index": 1,
                "step_type": "DOMAIN_RULE",
                "name": "Missing Rule Check",
                "description": "References a rule that does not exist",
                "config_json": {"rule_id": missing_rule},
            },
        ],
    )

    created = client.post("/api/v1/runs/", headers=headers, json={
        "workflow_id": workflow_id,
        "account_id": f"ACC_BG_FAIL_{uuid4().hex[:8]}",
        "input_data": {"risk_score": 0.5},
    })
    assert created.status_code == 202, created.text

    final = _poll_until_terminal(client, created.json()["id"], headers)
    assert final["status"] == RunStatus.FAILED.value, final
    assert final["error_message"], "FAILED run must carry a durable error_message"
    assert missing_rule in final["error_message"]


async def test_completed_run_output_data_matches_final_state_output(client, mock_llm):
    """COMPLETED runs persist and expose the final step's output via GET /runs/{id}."""
    headers = _register_and_login(client)
    workflow_id = _create_approved_workflow(
        client,
        headers,
        name="Background Output Data Workflow",
        steps=[{
            "order_index": 0,
            "step_type": "AI_ANALYSIS",
            "name": "Risk Analysis",
            "description": "AI analyzes transaction risk",
            "config_json": {"prompt_template": "Analyze this transaction"},
        }],
    )

    created = client.post("/api/v1/runs/", headers=headers, json={
        "workflow_id": workflow_id,
        "account_id": f"ACC_BG_OUTPUT_{uuid4().hex[:8]}",
        "input_data": {"risk_score": 0.4},
    })
    assert created.status_code == 202, created.text

    final = _poll_until_terminal(client, created.json()["id"], headers)
    assert final["status"] == RunStatus.COMPLETED.value, final

    # The final state's current_output for an AI step is {"content": ...}.
    expected_output = {"content": "MOCK-LLM ANALYSIS: transaction risk assessed"}
    assert final["output_data"] == expected_output, final

    # It is durable (read through an independent session), not transient.
    async with get_session_maker()() as session:
        persisted = await session.get(WorkflowRun, UUID(created.json()["id"]))
        assert persisted is not None
        assert persisted.output_data == expected_output


def test_failed_run_output_data_is_none(client, mock_llm):
    """FAILED runs carry no output_data: no final decision output was produced."""
    headers = _register_and_login(client)
    workflow_id = _create_approved_workflow(
        client,
        headers,
        name="Background Output Failure Workflow",
        steps=[{
            "order_index": 0,
            "step_type": "DOMAIN_RULE",
            "name": "Missing Rule Check",
            "description": "References a rule that does not exist",
            "config_json": {"rule_id": f"missing_rule_{uuid4().hex[:8]}"},
        }],
    )

    created = client.post("/api/v1/runs/", headers=headers, json={
        "workflow_id": workflow_id,
        "account_id": f"ACC_BG_OUTPUT_FAIL_{uuid4().hex[:8]}",
        "input_data": {"risk_score": 0.5},
    })
    assert created.status_code == 202, created.text

    final = _poll_until_terminal(client, created.json()["id"], headers)
    assert final["status"] == RunStatus.FAILED.value, final
    assert final["output_data"] is None, final
