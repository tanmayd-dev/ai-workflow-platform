"""Run discovery + audit log API contract tests.

Ends-to-end checks for:

- ``GET /api/v1/runs``: authenticated list, newest-first, filterable by
  ``status`` / ``workflow_id`` / ``account_id`` with limit/offset paging.
- ``GET /api/v1/audit-logs``: auditor/admin-guarded view into the executor's
  AuditLog rows.
- Approver attribution: after ``POST /runs/{id}/approve``, the "resumed" and
  "approved" audit rows record the approving user, not ``run.triggered_by``.
"""
import time
from uuid import UUID, uuid4

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from app.core.database import get_session_maker
from app.main import app
from app.models.execution import AuditLog, RunStatus

from conftest import grant_role

# A run in one of these states will not progress on its own.
TERMINAL_STATUSES = {"COMPLETED", "FAILED", "PAUSED"}

AI_STEP = {
    "order_index": 0,
    "step_type": "AI_ANALYSIS",
    "name": "Risk Analysis",
    "description": "AI analyzes transaction risk",
    "config_json": {"prompt_template": "Analyze this transaction"},
}

APPROVAL_STEPS = [
    AI_STEP,
    {
        "order_index": 1,
        "step_type": "HUMAN_APPROVAL",
        "name": "Manager Approval",
        "description": "Human review before proceeding",
        "config_json": {},
    },
]


@pytest.fixture
def client():
    """In-process HTTP test client (startup initializes the test database)."""
    with TestClient(app) as test_client:
        yield test_client


def _register(client: TestClient, roles=("admin",)) -> dict:
    """Register a user (no auto-roles), grant roles, log in."""
    email = f"discovery_{uuid4().hex[:8]}@example.com"
    register = client.post("/api/v1/auth/register", json={
        "email": email,
        "password": "testpass123",
        "full_name": "Discovery Test User",
    })
    assert register.status_code == 201, register.text

    for role in roles:
        grant_role(email, role)

    login = client.post("/api/v1/auth/login", data={
        "username": email,
        "password": "testpass123",
    })
    assert login.status_code == 200, login.text

    return {
        "email": email,
        "id": UUID(register.json()["id"]),
        "headers": {"Authorization": f"Bearer {login.json()['access_token']}"},
    }


def _create_approved_workflow(
    client: TestClient,
    headers: dict,
    *,
    name: str,
    steps: list,
) -> str:
    created = client.post("/api/v1/workflows/draft", headers=headers, json={
        "name": f"{name} {uuid4().hex[:8]}",
        "description": "Discovery test workflow",
        "domain": "fraud",
        "steps": steps,
    })
    assert created.status_code == 201, created.text
    workflow_id = created.json()["id"]

    approved = client.post(f"/api/v1/workflows/{workflow_id}/approve", headers=headers)
    assert approved.status_code == 200, approved.text
    return workflow_id


def _start_run(client: TestClient, headers: dict, workflow_id: str, account_id: str) -> str:
    started = client.post("/api/v1/runs/", headers=headers, json={
        "workflow_id": workflow_id,
        "account_id": account_id,
        "input_data": {"risk_score": 0.4},
    })
    assert started.status_code == 202, started.text
    return started.json()["id"]


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


def _list_runs(client: TestClient, headers: dict, **params) -> dict:
    response = client.get("/api/v1/runs", headers=headers, params=params)
    assert response.status_code == 200, response.text
    return response.json()


# ============= GET /api/v1/runs =============

def test_list_runs_filters_by_status_paused_excludes_completed(client, mock_llm):
    """An approver looking for paused runs sees only the paused run."""
    headers = _register(client)["headers"]

    ai_workflow_id = _create_approved_workflow(
        client, headers, name="Discovery Filter AI Workflow", steps=[AI_STEP]
    )
    approval_workflow_id = _create_approved_workflow(
        client, headers, name="Discovery Filter Approval Workflow", steps=APPROVAL_STEPS
    )

    completed_run_id = _start_run(
        client, headers, ai_workflow_id, f"ACC_DISC_DONE_{uuid4().hex[:8]}"
    )
    paused_run_id = _start_run(
        client, headers, approval_workflow_id, f"ACC_DISC_PAUSE_{uuid4().hex[:8]}"
    )
    assert _poll_until_terminal(
        client, completed_run_id, headers
    )["status"] == "COMPLETED"
    assert _poll_until_terminal(client, paused_run_id, headers)["status"] == "PAUSED"

    paused_list = _list_runs(client, headers, status="PAUSED")
    listed_ids = {run["id"] for run in paused_list}
    assert paused_run_id in listed_ids
    assert completed_run_id not in listed_ids
    assert all(run["status"] == "PAUSED" for run in paused_list)

    # Complement: the completed filter behaves symmetrically.
    completed_list = _list_runs(client, headers, status="COMPLETED")
    completed_ids = {run["id"] for run in completed_list}
    assert completed_run_id in completed_ids
    assert paused_run_id not in completed_ids


def test_list_runs_filters_by_status_combination_workflow_and_account(client, mock_llm):
    """Comma-separated statuses, workflow_id and account_id all filter correctly."""
    headers = _register(client)["headers"]
    ai_workflow_id = _create_approved_workflow(
        client, headers, name="Discovery Multi AI Workflow", steps=[AI_STEP]
    )
    approval_workflow_id = _create_approved_workflow(
        client, headers, name="Discovery Multi Approval Workflow", steps=APPROVAL_STEPS
    )

    account_a = f"ACC_DISC_MULTI_A_{uuid4().hex[:8]}"
    account_b = f"ACC_DISC_MULTI_B_{uuid4().hex[:8]}"

    completed_run_id = _start_run(client, headers, ai_workflow_id, account_a)
    paused_run_id = _start_run(client, headers, approval_workflow_id, account_b)
    assert _poll_until_terminal(
        client, completed_run_id, headers
    )["status"] == "COMPLETED"
    assert _poll_until_terminal(client, paused_run_id, headers)["status"] == "PAUSED"

    combined = _list_runs(client, headers, status="RUNNING,PAUSED")
    combined_ids = {run["id"] for run in combined}
    assert paused_run_id in combined_ids
    assert completed_run_id not in combined_ids
    assert all(run["status"] in ("RUNNING", "PAUSED") for run in combined)

    by_workflow = _list_runs(client, headers, workflow_id=ai_workflow_id)
    by_workflow_ids = {run["id"] for run in by_workflow}
    assert completed_run_id in by_workflow_ids
    assert paused_run_id not in by_workflow_ids

    by_account = _list_runs(client, headers, account_id=account_b)
    by_account_ids = {run["id"] for run in by_account}
    assert paused_run_id in by_account_ids
    assert completed_run_id not in by_account_ids

    # Fixture pollution aside, the unknown token is rejected loudly.
    bad_status = client.get(
        "/api/v1/runs", headers=headers, params={"status": "NOT_A_STATUS"}
    )
    assert bad_status.status_code == 400, bad_status.text


def test_list_runs_is_newest_first_and_respects_limit_offset(client, mock_llm):
    """Ordering is started_at desc with id tiebreak; limit/offset page into it."""
    headers = _register(client)["headers"]
    workflow_id = _create_approved_workflow(
        client, headers, name="Discovery Paging Workflow", steps=[AI_STEP]
    )

    run_ids = [
        _start_run(
            client, headers, workflow_id, f"ACC_DISC_PAGE_{index}_{uuid4().hex[:6]}"
        )
        for index in range(3)
    ]
    for run_id in run_ids:
        assert _poll_until_terminal(
            client, run_id, headers
        )["status"] == "COMPLETED"

    oldest, middle, newest = run_ids  # created in order: run_ids[0] started first

    page = _list_runs(
        client, headers, workflow_id=workflow_id, limit=2, offset=0
    )
    assert len(page) == 2
    assert [run["id"] for run in page] == [newest, middle]

    offset_page = _list_runs(
        client, headers, workflow_id=workflow_id, limit=2, offset=1
    )
    assert [run["id"] for run in offset_page] == [middle, oldest]

    beyond = _list_runs(
        client, headers, workflow_id=workflow_id, limit=200, offset=3
    )
    assert beyond == []

    # Default limit is 50 and max 200; out-of-range limits are rejected.
    assert client.get(
        "/api/v1/runs",
        headers=headers,
        params={"workflow_id": workflow_id, "limit": 201},
    ).status_code == 422
    assert client.get(
        "/api/v1/runs",
        headers=headers,
        params={"workflow_id": workflow_id, "limit": 0},
    ).status_code == 422


def test_list_runs_requires_authentication(client):
    """Anonymous callers are 401 on both discovery endpoints."""
    assert client.get("/api/v1/runs").status_code == 401
    assert client.get("/api/v1/audit-logs").status_code == 401


# ============= GET /api/v1/audit-logs =============

def test_audit_logs_role_guard_and_filters(client, mock_llm):
    """403 for a roleless user, 200 for admin, with real audit rows to read."""
    admin = _register(client, roles=("admin",))
    plain = _register(client, roles=())

    workflow_id = _create_approved_workflow(
        client, admin["headers"], name="Discovery Audit Workflow", steps=[AI_STEP]
    )
    run_id = _start_run(client, admin["headers"], workflow_id, f"ACC_DISC_AUDIT_{uuid4().hex[:8]}")
    assert _poll_until_terminal(
        client, run_id, admin["headers"]
    )["status"] == "COMPLETED"

    forbidden = client.get("/api/v1/audit-logs", headers=plain["headers"])
    assert forbidden.status_code == 403, forbidden.text

    listed = client.get("/api/v1/audit-logs", headers=admin["headers"])
    assert listed.status_code == 200, listed.text
    rows = listed.json()
    assert rows, "at least one audit row must exist after a run executes"

    # Newest-first ordering.
    timestamps = [row["timestamp"] for row in rows]
    assert timestamps == sorted(timestamps, reverse=True)
    for row in rows:
        assert row["entity_type"] and row["action"] and row["actor_id"]

    filtered = client.get(
        "/api/v1/audit-logs",
        headers=admin["headers"],
        params={"entity_type": "run", "entity_id": run_id},
    )
    assert filtered.status_code == 200, filtered.text
    run_rows = filtered.json()
    assert run_rows, "rows filtered by entity_id must include the executed run"
    assert all(
        row["entity_id"] == run_id and row["entity_type"] == "run"
        for row in run_rows
    )
    # The executor wrote the run lifecycle rows for this completed run.
    assert {"started", "completed"} <= {row["action"] for row in run_rows}


async def test_approve_attribution_records_approver_not_triggered_by(client, mock_llm):
    """POST /runs/{id}/approve as an approver: 'resumed'/'approved' audit rows
    carry the approver's identity, not the run triggerer's."""
    admin = _register(client, roles=("admin",))  # creator + triggerer
    approver = _register(client, roles=("approver",))

    workflow_id = _create_approved_workflow(
        client, admin["headers"], name="Discovery Attribution Workflow", steps=APPROVAL_STEPS
    )
    run_id = _start_run(
        client, admin["headers"], workflow_id, f"ACC_DISC_ATTR_{uuid4().hex[:8]}"
    )
    paused = _poll_until_terminal(client, run_id, admin["headers"])
    assert paused["status"] == "PAUSED", paused

    approved = client.post(
        f"/api/v1/runs/{run_id}/approve",
        headers=approver["headers"],
        json={"notes": "Discovery attribution check"},
    )
    assert approved.status_code == 200, approved.text
    assert approved.json()["status"] == "COMPLETED", approved.json()

    session_maker = get_session_maker()
    async with session_maker() as session:
        result = await session.execute(
            select(AuditLog)
            .where(
                AuditLog.entity_type == "run",
                AuditLog.entity_id == UUID(run_id),
                AuditLog.action == "resumed",
            )
            .order_by(AuditLog.timestamp.desc(), AuditLog.id.desc())
        )
        resumed_rows = result.scalars().all()
        assert resumed_rows, "approving a paused run must write a 'resumed' audit row"

        latest = resumed_rows[0]
        assert latest.actor_id == approver["id"], (
            f"expected approver {approver['id']} as actor, got {latest.actor_id}"
        )
        assert latest.actor_id != admin["id"], (
            "'resumed' audit must not be attributed to run.triggered_by"
        )
        assert latest.actor_role == "approver"

        approve_result = await session.execute(
            select(AuditLog)
            .where(
                AuditLog.entity_type == "run",
                AuditLog.entity_id == UUID(run_id),
                AuditLog.action == "approved",
            )
            .order_by(AuditLog.timestamp.desc(), AuditLog.id.desc())
        )
        approved_rows = approve_result.scalars().all()
        assert approved_rows
        assert approved_rows[0].actor_id == approver["id"]
