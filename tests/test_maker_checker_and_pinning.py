"""Maker-checker (SoD) approvals + rule-version pinning tests.

Ends-to-end checks for:

- ``POST /runs/{id}/approve`` / ``POST /runs/{id}/reject``: the user who
  triggered a run cannot approve or reject it (409) — a second, different
  user must act. The ``admin`` superuser role bypasses the check.
- ``POST /workflows/{id}/approve``: the workflow's creator cannot approve
  their own draft (409); a different approver or an admin can.
- Rule-version pinning: the executor records ``{rule_name: version_number}``
  on the run at start; ``resume`` and ``replay`` re-resolve exactly those
  versions instead of the latest-active ones, so bumping a rule afterwards
  cannot alter a past or resumed leg (the node execution must still record
  version 1's ``rule_version_id``). A pinned version that no longer exists
  fails the leg loudly (durable FAILED, HTTP 500) instead of silently
  substituting a newer version.
"""
import time
from uuid import UUID, uuid4

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from app.core.database import get_session_maker
from app.main import app
from app.models.execution import AuditLog, NodeExecution, RunStatus, WorkflowRun
from app.models.rule import Rule, RuleVersion
from app.models.workflow import WorkflowStep

from conftest import grant_role

AI_STEP = {
    "order_index": 0,
    "step_type": "AI_ANALYSIS",
    "name": "Risk Analysis",
    "description": "AI analyzes transaction risk",
    "config_json": {"prompt_template": "Analyze this transaction"},
}

APPROVAL_STEP = {
    "order_index": 1,
    "step_type": "HUMAN_APPROVAL",
    "name": "Manager Approval",
    "description": "Human review before proceeding",
    "config_json": {},
}

TERMINAL_STATUSES = {"COMPLETED", "FAILED", "PAUSED"}


@pytest.fixture
def client():
    """In-process HTTP test client (startup initializes the test database)."""
    with TestClient(app) as test_client:
        yield test_client


def _register(client: TestClient, roles=("admin",)) -> dict:
    """Register a user (no auto-roles), grant roles, log in."""
    email = f"sod_{uuid4().hex[:8]}@example.com"
    register = client.post("/api/v1/auth/register", json={
        "email": email,
        "password": "testpass123",
        "full_name": "Maker-Checker Test User",
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


def _draft_workflow(client: TestClient, headers: dict, *, name: str, steps: list) -> str:
    """Create a DRAFT workflow (caller must approve it — that is the point
    of the maker-checker tests, so no auto-approval hides the guard)."""
    created = client.post("/api/v1/workflows/draft", headers=headers, json={
        "name": f"{name} {uuid4().hex[:8]}",
        "description": "Maker-checker/pinning test workflow",
        "domain": "fraud",
        "steps": steps,
    })
    assert created.status_code == 201, created.text
    return created.json()["id"]


def _approve_workflow(client: TestClient, headers: dict, workflow_id: str) -> None:
    approved = client.post(f"/api/v1/workflows/{workflow_id}/approve", headers=headers)
    assert approved.status_code == 200, approved.text


def _create_rule(client: TestClient, headers: dict, *, logic_json: dict) -> tuple[str, str]:
    """Create the pinning test rule (v1); returns (rule_id, rule_name)."""
    rule_name = f"PinCheckRiskGate_{uuid4().hex[:8]}"
    created = client.post("/api/v1/rules/", headers=headers, json={
        "name": rule_name,
        "description": "Rule-version pinning test rule",
        "domain": "fraud",
        "logic_json": logic_json,
    })
    assert created.status_code == 201, created.text
    return created.json()["id"], rule_name


def _bump_rule_version(client: TestClient, headers: dict, rule_id: str, logic_json: dict) -> dict:
    """Add a new version (becomes the single active one) and return it."""
    bumped = client.post(
        f"/api/v1/rules/{rule_id}/versions", headers=headers, json={"logic_json": logic_json}
    )
    assert bumped.status_code == 201, bumped.text
    return bumped.json()


def _rule_step(order_index: int, rule_name: str) -> dict:
    return {
        "order_index": order_index,
        "step_type": "DOMAIN_RULE",
        "name": f"Rule Check {order_index}",
        "description": "Deterministic rule check",
        "config_json": {"rule_id": rule_name},
    }


def _start_run(client: TestClient, headers: dict, workflow_id: str, account_id: str) -> str:
    started = client.post("/api/v1/runs/", headers=headers, json={
        "workflow_id": workflow_id,
        "account_id": account_id,
        "input_data": {"risk_score": 0.9},
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


async def _fetch_run(run_id: str) -> WorkflowRun:
    session_maker = get_session_maker()
    async with session_maker() as session:
        return await session.get(WorkflowRun, UUID(run_id))


async def _rule_version_id(rule_id: str, version_number: int) -> UUID:
    session_maker = get_session_maker()
    async with session_maker() as session:
        version = (
            await session.execute(
                select(RuleVersion).where(
                    RuleVersion.rule_id == UUID(rule_id),
                    RuleVersion.version_number == version_number,
                )
            )
        ).scalar_one()
        return version.id


async def _node_executions_for_step(run_id: str, order_index: int) -> list:
    """Node executions of the relevant run's workflow step at ``order_index``."""
    run = await _fetch_run(run_id)
    session_maker = get_session_maker()
    async with session_maker() as session:
        step = (
            await session.execute(
                select(WorkflowStep).where(
                    WorkflowStep.workflow_id == run.workflow_id,
                    WorkflowStep.order_index == order_index,
                )
            )
        ).scalar_one()
        return list(
            reversed(
                (
                    await session.execute(
                        select(NodeExecution)
                        .where(
                            NodeExecution.run_id == UUID(run_id),
                            NodeExecution.step_id == step.id,
                        )
                        .order_by(NodeExecution.started_at)
                    )
                )
                .scalars()
                .all()
            )
        )


async def _latest_run_audit(run_id: UUID, action: str):
    session_maker = get_session_maker()
    async with session_maker() as session:
        return (
            await session.execute(
                select(AuditLog)
                .where(
                    AuditLog.entity_type == "run",
                    AuditLog.entity_id == run_id,
                    AuditLog.action == action,
                )
                .order_by(AuditLog.timestamp.desc(), AuditLog.id.desc())
            )
        ).scalars().first()


# ============= Maker-checker: run approval =============

def test_run_approve_triggerer_denied_then_checker_allowed(client, mock_llm):
    """The run triggerer cannot approve their own run (409); a different
    checker (here: an admin) can."""
    maker = _register(client, roles=("author", "executor", "approver"))
    checker = _register(client, roles=("admin",))

    workflow_id = _draft_workflow(
        client, checker["headers"], name="SoD Approve Workflow",
        steps=[AI_STEP, APPROVAL_STEP],
    )
    _approve_workflow(client, checker["headers"], workflow_id)

    run_id = _start_run(
        client, maker["headers"], workflow_id, f"ACC_SOD_APPROVE_{uuid4().hex[:8]}"
    )
    paused = _poll_until_terminal(client, run_id, maker["headers"])
    assert paused["status"] == "PAUSED", paused

    denied = client.post(
        f"/api/v1/runs/{run_id}/approve", headers=maker["headers"], json={"notes": "self ok?"}
    )
    assert denied.status_code == 409, denied.text
    assert "maker-checker" in denied.json()["detail"].lower()

    # Nothing changed by the denied attempt: still PAUSED for a real checker.
    assert client.get(f"/api/v1/runs/{run_id}", headers=maker["headers"]).json()[
        "status"
    ] == "PAUSED"

    approved = client.post(
        f"/api/v1/runs/{run_id}/approve", headers=checker["headers"], json={"notes": "checker"}
    )
    assert approved.status_code == 200, approved.text
    assert approved.json()["status"] == "COMPLETED"


def test_run_approve_admin_triggerer_bypasses(client, mock_llm):
    """Admin superusers are exempt: an admin who triggered the run may approve it."""
    admin = _register(client, roles=("admin",))

    workflow_id = _draft_workflow(
        client, admin["headers"], name="SoD Admin Approve Workflow",
        steps=[AI_STEP, APPROVAL_STEP],
    )
    _approve_workflow(client, admin["headers"], workflow_id)

    run_id = _start_run(
        client, admin["headers"], workflow_id, f"ACC_SOD_ADMIN_{uuid4().hex[:8]}"
    )
    paused = _poll_until_terminal(client, run_id, admin["headers"])
    assert paused["status"] == "PAUSED", paused

    approved = client.post(f"/api/v1/runs/{run_id}/approve", headers=admin["headers"])
    assert approved.status_code == 200, approved.text
    assert approved.json()["status"] == "COMPLETED"


def test_run_reject_triggerer_denied_then_checker_allowed(client, mock_llm):
    """The run triggerer cannot reject their own run (409); a different checker can."""
    maker = _register(client, roles=("author", "executor", "approver"))
    checker = _register(client, roles=("admin",))

    workflow_id = _draft_workflow(
        client, checker["headers"], name="SoD Reject Workflow",
        steps=[AI_STEP, APPROVAL_STEP],
    )
    _approve_workflow(client, checker["headers"], workflow_id)

    run_id = _start_run(
        client, maker["headers"], workflow_id, f"ACC_SOD_REJECT_{uuid4().hex[:8]}"
    )
    paused = _poll_until_terminal(client, run_id, maker["headers"])
    assert paused["status"] == "PAUSED", paused

    denied = client.post(
        f"/api/v1/runs/{run_id}/reject",
        headers=maker["headers"],
        json={"reason": "self rejection"},
    )
    assert denied.status_code == 409, denied.text

    rejected = client.post(
        f"/api/v1/runs/{run_id}/reject",
        headers=checker["headers"],
        json={"reason": "checker rejection"},
    )
    assert rejected.status_code == 200, rejected.text
    assert rejected.json()["status"] == "FAILED"


# ============= Maker-checker: workflow approval =============

def test_workflow_approve_creator_denied_then_checker_allowed(client):
    """The workflow creator cannot approve their own draft (409): it stays
    DRAFT with no approved_by, and a different admin checker approves after."""
    maker = _register(client, roles=("author", "approver"))
    checker = _register(client, roles=("admin",))

    workflow_id = _draft_workflow(
        client, maker["headers"], name="SoD Workflow Creator", steps=[AI_STEP]
    )

    denied = client.post(f"/api/v1/workflows/{workflow_id}/approve", headers=maker["headers"])
    assert denied.status_code == 409, denied.text
    assert "maker-checker" in denied.json()["detail"].lower()

    # The failed attempt must not have flipped the status or set approved_by.
    fetched = client.get(f"/api/v1/workflows/{workflow_id}", headers=maker["headers"])
    assert fetched.status_code == 200, fetched.text
    assert fetched.json()["status"] == "DRAFT"
    assert fetched.json()["approved_by"] is None

    approved = client.post(f"/api/v1/workflows/{workflow_id}/approve", headers=checker["headers"])
    assert approved.status_code == 200, approved.text
    assert approved.json()["status"] == "APPROVED"
    assert approved.json()["approved_by"] == str(checker["id"])


def test_workflow_approve_admin_creator_bypasses(client):
    """Admin superusers are exempt: an admin may approve their own draft."""
    admin = _register(client, roles=("admin",))

    workflow_id = _draft_workflow(
        client, admin["headers"], name="SoD Admin Workflow Creator", steps=[AI_STEP]
    )
    approved = client.post(f"/api/v1/workflows/{workflow_id}/approve", headers=admin["headers"])
    assert approved.status_code == 200, approved.text
    assert approved.json()["approved_by"] == str(admin["id"])


# ============= Rule-version pinning =============

async def test_replay_reuses_pinned_rule_version_not_latest_active(client):
    """After the original run completed, bumping the rule to v2 must not
    affect the replay: the replayed leg records v1's rule_version_id and
    v1's output."""
    admin = _register(client, roles=("admin",))

    rule_id, rule_name = _create_rule(
        client, admin["headers"], logic_json={"result": "PIN_RESULT_V1"}
    )
    v1_id = await _rule_version_id(rule_id, 1)

    workflow_id = _draft_workflow(
        client, admin["headers"], name="Pinning Replay Workflow",
        steps=[_rule_step(0, rule_name)],
    )
    _approve_workflow(client, admin["headers"], workflow_id)

    run_id = _start_run(
        client, admin["headers"], workflow_id, f"ACC_PIN_REPLAY_{uuid4().hex[:8]}"
    )
    completed = _poll_until_terminal(client, run_id, admin["headers"])
    assert completed["status"] == "COMPLETED", completed

    # Sanity: the original leg executed v1, and v1 was pinned on the run.
    original_nodes = await _node_executions_for_step(run_id, 0)
    assert len(original_nodes) == 1
    assert original_nodes[0].rule_version_id == v1_id
    assert original_nodes[0].output_snapshot == "PIN_RESULT_V1"
    assert (await _fetch_run(run_id)).rule_versions_pinned == {rule_name: 1}

    # Bump to v2: v2 becomes the single active version, v1 is deactivated.
    bumped = _bump_rule_version(
        client, admin["headers"], rule_id, {"result": "PIN_RESULT_V2"}
    )
    assert bumped["version_number"] == 2

    replay = client.post(f"/api/v1/runs/{run_id}/replay", headers=admin["headers"])
    assert replay.status_code == 201, replay.text
    replay_run = replay.json()
    assert replay_run["status"] == "COMPLETED", replay_run
    assert replay_run["id"] != run_id
    # The replay inherited the original run's pins.
    assert replay_run["rule_versions_pinned"] == {rule_name: 1}

    replay_nodes = await _node_executions_for_step(replay_run["id"], 0)
    assert len(replay_nodes) == 1
    # v2 is now the only active version, but the replay leg must have
    # re-evaluated exactly the pinned v1.
    assert replay_nodes[0].rule_version_id == v1_id, (
        "the replay leg must record the pinned v1 rule_version_id, not v2"
    )
    assert replay_nodes[0].output_snapshot == "PIN_RESULT_V1", replay_nodes[0].output_snapshot


async def test_resume_reuses_pinned_rule_version_not_latest_active(client):
    """A run paused at its approval step, then resumed after the rule was
    bumped to v2, must execute the post-approval rule step with v1."""
    admin = _register(client, roles=("admin",))

    rule_id, rule_name = _create_rule(
        client, admin["headers"], logic_json={"result": "RESUME_RESULT_V1"}
    )
    v1_id = await _rule_version_id(rule_id, 1)

    workflow_id = _draft_workflow(
        client, admin["headers"], name="Pinning Resume Workflow",
        steps=[_rule_step(0, rule_name), APPROVAL_STEP, _rule_step(2, rule_name)],
    )
    _approve_workflow(client, admin["headers"], workflow_id)

    run_id = _start_run(
        client, admin["headers"], workflow_id, f"ACC_PIN_RESUME_{uuid4().hex[:8]}"
    )
    paused = _poll_until_terminal(client, run_id, admin["headers"])
    assert paused["status"] == "PAUSED", paused
    assert paused["current_step_index"] == 1

    # Pin record persisted durably at start; the started audit carries it too.
    assert (await _fetch_run(run_id)).rule_versions_pinned == {rule_name: 1}
    started_audit = await _latest_run_audit(UUID(run_id), "started")
    assert started_audit.details is not None
    assert started_audit.details.get("rule_versions") == {rule_name: 1}

    # Bump the rule to v2 while the run sits paused.
    bumped = _bump_rule_version(
        client, admin["headers"], rule_id, {"result": "RESUME_RESULT_V2"}
    )
    assert bumped["version_number"] == 2

    approved = client.post(
        f"/api/v1/runs/{run_id}/approve", headers=admin["headers"], json={"notes": "resume"}
    )
    assert approved.status_code == 200, approved.text
    assert approved.json()["status"] == "COMPLETED", approved.json()

    # Step 2 executed AFTER the bump: pinning must have forced v1 again.
    resumed_nodes = await _node_executions_for_step(run_id, 2)
    assert len(resumed_nodes) == 1
    assert resumed_nodes[0].rule_version_id == v1_id, (
        "the resumed leg must record the pinned v1 rule_version_id, not v2"
    )
    assert resumed_nodes[0].output_snapshot == "RESUME_RESULT_V1"


async def test_missing_pinned_version_fails_loudly(client):
    """A pinned (rule, version) that no longer exists must fail the leg
    loudly: the resume 500s and the run lands durably FAILED — never a
    silent upgrade to a newer version."""
    admin = _register(client, roles=("admin",))

    rule_id, rule_name = _create_rule(
        client, admin["headers"], logic_json={"result": "MISSING_RESULT_V1"}
    )

    workflow_id = _draft_workflow(
        client, admin["headers"], name="Pinning Missing Workflow",
        steps=[_rule_step(0, rule_name), APPROVAL_STEP],
    )
    _approve_workflow(client, admin["headers"], workflow_id)

    run_id = _start_run(
        client, admin["headers"], workflow_id, f"ACC_PIN_MISSING_{uuid4().hex[:8]}"
    )
    paused = _poll_until_terminal(client, run_id, admin["headers"])
    assert paused["status"] == "PAUSED", paused

    # Simulate the pinned version vanishing: point the pin at a version
    # number that never existed (v999) while the rule row itself remains.
    session_maker = get_session_maker()
    async with session_maker() as session:
        run = await session.get(WorkflowRun, UUID(run_id))
        run.rule_versions_pinned = {rule_name: 999}
        await session.commit()

    approved = client.post(f"/api/v1/runs/{run_id}/approve", headers=admin["headers"])
    # A wiped pinned version is a conflict: the run is PAUSED but cannot be
    # resumed against the exact version the approver saw. It must fail loudly
    # (non-500-and-non-200), never silently substitute a newer version.
    assert approved.status_code == 409, approved.text
    assert "no longer exist" in approved.text
    assert "refusing" in approved.json()["detail"].lower()

    final_run = await _fetch_run(run_id)
    assert final_run.status == RunStatus.FAILED
    assert "999" in (final_run.error_message or ""), final_run.error_message
    assert final_run.rule_versions_pinned == {rule_name: 999}, (
        "the pin record must stand as the audit trail of what the leg wanted"
    )
