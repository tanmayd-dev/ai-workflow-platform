"""
Cancel + idempotency + DB-enforced active-run guard tests (T2.2).

- POST /api/v1/runs/{id}/cancel: triggerer-or-admin authorization, CAS status
  transition for RUNNING/PAUSED, 409 on terminal status, 404 unknown.
- POST /api/v1/runs honors an optional Idempotency-Key header with a 200 replay.
- The one-active-run-per-account rule is enforced by a partial unique index:
  a sequential duplicate fails at commit (IntegrityError) and surfaces as 409.
"""
import time
from datetime import datetime, timedelta
from uuid import UUID, uuid4

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from app.core.database import get_session_maker
from app.main import app
from app.models.execution import AuditLog, RunStatus, WorkflowRun
from app.services.executor import try_claim_run_lease

from conftest import grant_role
from test_background_runs import _poll_until_terminal


@pytest.fixture
def client():
    """In-process HTTP test client (startup initializes the test database)."""
    with TestClient(app) as test_client:
        yield test_client


def _register_and_login(client: TestClient, prefix: str = "cancel_user", role: str = "admin") -> dict:
    email = f"{prefix}_{uuid4().hex[:8]}@example.com"
    register = client.post("/api/v1/auth/register", json={
        "email": email,
        "password": "testpass123",
        "full_name": "Cancel Test User",
    })
    assert register.status_code == 201, register.text
    grant_role(email, role)

    login = client.post("/api/v1/auth/login", data={
        "username": email,
        "password": "testpass123",
    })
    assert login.status_code == 200, login.text
    return {"Authorization": f"Bearer {login.json()['access_token']}"}


def _create_approved_workflow(client: TestClient, headers: dict, *, name: str) -> str:
    """AI step + human approval step: the run pauses and can be cancelled."""
    created = client.post("/api/v1/workflows/draft", headers=headers, json={
        "name": f"{name} {uuid4().hex[:8]}",
        "description": "Cancel/idempotency test workflow",
        "domain": "fraud",
        "steps": [
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
    })
    assert created.status_code == 201, created.text
    workflow_id = created.json()["id"]

    approved = client.post(f"/api/v1/workflows/{workflow_id}/approve", headers=headers)
    assert approved.status_code == 200, approved.text
    return workflow_id


async def _insert_run(status: RunStatus, *, triggered_by=None, lease_token=None, **overrides) -> UUID:
    """Insert a bare WorkflowRun row directly (SQLite does not enforce FKs)."""
    session_maker = get_session_maker()
    async with session_maker() as session:
        run = WorkflowRun(
            id=uuid4(),
            workflow_id=uuid4(),
            account_id=f"ACC_CANCEL_{uuid4().hex[:8]}",
            status=status,
            current_step_index=0,
            triggered_by=triggered_by or uuid4(),
            started_at=datetime.utcnow(),
            lease_token=lease_token,
            **overrides,
        )
        session.add(run)
        await session.commit()
    return run.id


# ---------------------------------------------------------------- cancel


async def test_cancel_paused_run_transitions_to_cancelled_and_is_terminal(client, mock_llm):
    """A paused run is cancelled immediately; cancelling again is a 409."""
    admin = _register_and_login(client, prefix="cancel_admin")
    workflow_id = _create_approved_workflow(client, admin, name="Cancel Paused Workflow")

    created = client.post("/api/v1/runs/", headers=admin, json={
        "workflow_id": workflow_id,
        "account_id": f"ACC_CANCEL_PAUSE_{uuid4().hex[:8]}",
        "input_data": {"risk_score": 0.9},
    })
    assert created.status_code == 202, created.text
    run_id = created.json()["id"]
    final = _poll_until_terminal(client, run_id, admin)
    assert final["status"] == RunStatus.PAUSED.value

    first = client.post(f"/api/v1/runs/{run_id}/cancel", headers=admin)
    assert first.status_code == 200, first.text
    assert first.json()["status"] == RunStatus.CANCELLED.value
    assert first.json()["error_message"], "cancelled runs carry a durable reason"

    again = client.post(f"/api/v1/runs/{run_id}/cancel", headers=admin)
    assert again.status_code == 409, again.text

    # The status is durable through an independent read.
    polled = client.get(f"/api/v1/runs/{run_id}", headers=admin)
    assert polled.json()["status"] == RunStatus.CANCELLED.value

    # Audit trail: exactly one cancelled audit row with an actor.
    async with get_session_maker()() as session:
        rows = (
            await session.execute(
                select(AuditLog).where(AuditLog.action == "cancelled", AuditLog.entity_id == UUID(run_id))
            )
        ).scalars().all()
    assert len(rows) == 1
    assert rows[0].actor_id is not None


def test_cancel_unknown_run_is_404(client):
    admin = _register_and_login(client, prefix="cancel_admin2")
    unknown = client.post(f"/api/v1/runs/{uuid4()}/cancel", headers=admin)
    assert unknown.status_code == 404, unknown.text


def test_cancel_terminal_status_is_409(client, mock_llm):
    headers = _register_and_login(client, prefix="cancel_terminal")
    workflow_id = _create_approved_workflow(client, headers, name="Cancel Terminal Workflow")

    created = client.post("/api/v1/runs/", headers=headers, json={
        "workflow_id": workflow_id,
        "account_id": f"ACC_CANCEL_DONE_{uuid4().hex[:8]}",
        "input_data": {"risk_score": 0.4},  # no approval step past AI? two-step flow pauses...
    })
    # This workflow pauses at step 1, so poll and reject it to reach a terminal status.
    assert created.status_code == 202, created.text
    run_id = created.json()["id"]
    final = _poll_until_terminal(client, run_id, headers)
    if final["status"] == RunStatus.PAUSED.value:
        rejected = client.post(
            f"/api/v1/runs/{run_id}/reject", headers=headers,
            json={"reason": "rejected to reach terminal status"},
        )
        assert rejected.status_code == 200, rejected.text

    cancelled_attempt = client.post(f"/api/v1/runs/{run_id}/cancel", headers=headers)
    assert cancelled_attempt.status_code == 409, cancelled_attempt.text


async def test_cancel_running_run_does_not_touch_the_lease(client):
    """Cancelling a RUNNING run flips the status but never the lease columns."""
    now = datetime.utcnow()
    run_id = await _insert_run(
        RunStatus.RUNNING,
        lease_token="live-worker",
        lease_issued_at=now,
        lease_expires_at=now + timedelta(seconds=300),
    )

    admin = _register_and_login(client, prefix="cancel_running")
    response = client.post(f"/api/v1/runs/{run_id}/cancel", headers=admin)
    assert response.status_code == 200, response.text
    assert response.json()["status"] == RunStatus.CANCELLED.value

    async with get_session_maker()() as session:
        row = await session.get(WorkflowRun, run_id)
        assert row.status == RunStatus.CANCELLED
        assert row.lease_token == "live-worker", "cancel must not yank the lease"


async def test_cancel_authorization_triggerer_or_admin_else_403(client, mock_llm):
    """Only the triggering user or an admin may cancel; others get 403."""
    workflow_admin = _register_and_login(client, prefix="cancel_wf_author")
    workflow_id = _create_approved_workflow(
        client, workflow_admin, name="Cancel Authorization Workflow"
    )

    triggerer_headers = _register_and_login(client, prefix="cancel_triggerer", role="executor")
    created = client.post("/api/v1/runs/", headers=triggerer_headers, json={
        "workflow_id": workflow_id,
        "account_id": f"ACC_CANCEL_AUTH_{uuid4().hex[:8]}",
        "input_data": {"risk_score": 0.9},
    })
    assert created.status_code == 202, created.text
    run_id = created.json()["id"]
    assert _poll_until_terminal(client, run_id, triggerer_headers)["status"] == RunStatus.PAUSED.value

    # Another non-admin executor: 403 (regardless of the run state).
    outsider = _register_and_login(client, prefix="cancel_outsider", role="executor")
    denied = client.post(f"/api/v1/runs/{run_id}/cancel", headers=outsider)
    assert denied.status_code == 403, denied.text

    async with get_session_maker()() as session:
        row = await session.get(WorkflowRun, UUID(run_id))
        assert row.status == RunStatus.PAUSED, "a denied cancel must not change state"

    # Admin bypass: a fresh admin cancels someone else's run.
    admin_headers = _register_and_login(client, prefix="cancel_admin3")
    allowed = client.post(f"/api/v1/runs/{run_id}/cancel", headers=admin_headers)
    assert allowed.status_code == 200, allowed.text
    assert allowed.json()["status"] == RunStatus.CANCELLED.value

    # The triggerer may cancel their own run: cover via a second run.
    created2 = client.post("/api/v1/runs/", headers=triggerer_headers, json={
        "workflow_id": workflow_id,
        "account_id": f"ACC_CANCEL_AUTH2_{uuid4().hex[:8]}",
        "input_data": {"risk_score": 0.9},
    })
    assert created2.status_code == 202, created2.text
    run_id2 = created2.json()["id"]
    assert _poll_until_terminal(client, run_id2, triggerer_headers)["status"] == RunStatus.PAUSED.value
    own = client.post(f"/api/v1/runs/{run_id2}/cancel", headers=triggerer_headers)
    assert own.status_code == 200, own.text
    assert own.json()["status"] == RunStatus.CANCELLED.value


# ---------------------------------------------------------------- executor CAS


async def test_terminal_cas_refuses_to_overwrite_a_cancellation():
    """The executor's terminal write only applies while the row is still RUNNING."""
    run_id = await _insert_run(RunStatus.RUNNING)
    from app.services.executor import WorkflowExecutor

    executor = WorkflowExecutor()
    session_maker = get_session_maker()
    async with session_maker() as db:
        assert await executor._terminal_cas_claim(
            db, run_id, status=RunStatus.COMPLETED
        ) is True

    # Simulate the cancel landing after the executor's write: CANCELLED rows
    # must never be flipped by the executor's terminal CAS again.
    async with session_maker() as db:
        run = await db.get(WorkflowRun, run_id)
        run.status = RunStatus.CANCELLED
        await db.commit()

    async with session_maker() as db:
        assert await executor._terminal_cas_claim(
            db, run_id, status=RunStatus.COMPLETED
        ) is False


async def test_background_claim_aborts_for_a_cancelled_run():
    """A run cancelled before the background task starts is never claimed."""
    run_id = await _insert_run(RunStatus.CANCELLED)
    async with get_session_maker()() as db:
        assert await try_claim_run_lease(db, run_id, token="tok") is None


# ---------------------------------------------------------------- idempotency


def test_idempotency_key_replays_the_same_run(client, mock_llm):
    """The same Idempotency-Key returns the original run with a replay header."""
    headers = _register_and_login(client, prefix="idem_user")
    workflow_id = _create_approved_workflow(client, headers, name="Idempotent Workflow")
    account_id = f"ACC_IDEM_{uuid4().hex[:8]}"

    key = f"key-{uuid4().hex[:12]}"
    first = client.post(
        "/api/v1/runs/", headers={**headers, "Idempotency-Key": key},
        json={"workflow_id": workflow_id, "account_id": account_id,
              "input_data": {"risk_score": 0.4}},
    )
    assert first.status_code == 202, first.text
    first_id = first.json()["id"]

    second = client.post(
        "/api/v1/runs/", headers={**headers, "Idempotency-Key": key},
        json={"workflow_id": workflow_id, "account_id": account_id,
              "input_data": {"risk_score": 0.4}},
    )
    assert second.status_code == 200, second.text
    assert second.headers.get("Idempotency-Key-Replayed") == "true"
    assert second.json()["id"] == first_id

    # The key applies across accounts/bodies: the original run is returned.
    other_account = client.post(
        "/api/v1/runs/", headers={**headers, "Idempotency-Key": key},
        json={"workflow_id": workflow_id, "account_id": f"ACC_IDEM_OTHER_{uuid4().hex[:8]}",
              "input_data": {"risk_score": 0.4}},
    )
    assert other_account.status_code == 200, other_account.text
    assert other_account.json()["id"] == first_id

    # A different key creates a different run (no key reuse collision).
    third = client.post(
        "/api/v1/runs/", headers={**headers, "Idempotency-Key": f"key-{uuid4().hex[:12]}"},
        json={"workflow_id": workflow_id, "account_id": f"ACC_IDEM_NEXT_{uuid4().hex[:8]}",
              "input_data": {"risk_score": 0.4}},
    )
    assert third.status_code == 202, third.text
    assert third.json()["id"] != first_id


def test_idempotency_key_over_200_chars_is_rejected(client):
    headers = _register_and_login(client, prefix="idem_too_long")
    workflow_id = _create_approved_workflow(client, headers, name="Idempotent Too Long")
    response = client.post(
        "/api/v1/runs/", headers={**headers, "Idempotency-Key": "x" * 201},
        json={"workflow_id": workflow_id, "account_id": f"ACC_IDEM_LONG_{uuid4().hex[:8]}"},
    )
    assert response.status_code == 400, response.text


async def test_cancel_frees_the_account_active_slot(client, mock_llm):
    """After a cancel (or any terminal status), the account can run again."""
    headers = _register_and_login(client, prefix="cancel_free")
    workflow_id = _create_approved_workflow(client, headers, name="Cancel Slot Free Workflow")
    account_id = f"ACC_CANCEL_FREE_{uuid4().hex[:8]}"

    created = client.post("/api/v1/runs/", headers=headers, json={
        "workflow_id": workflow_id,
        "account_id": account_id,
        "input_data": {"risk_score": 0.9},
    })
    assert created.status_code == 202, created.text
    run_id = created.json()["id"]
    assert _poll_until_terminal(client, run_id, headers)["status"] == RunStatus.PAUSED.value

    # While paused, the account is blocked (via the DB index → 409).
    blocked = client.post("/api/v1/runs/", headers=headers, json={
        "workflow_id": workflow_id,
        "account_id": account_id,
        "input_data": {"risk_score": 0.9},
    })
    assert blocked.status_code == 409, blocked.text
    assert account_id in blocked.json()["detail"]
    assert run_id in blocked.json()["detail"]

    cancelled = client.post(f"/api/v1/runs/{run_id}/cancel", headers=headers)
    assert cancelled.status_code == 200, cancelled.text

    retry = client.post("/api/v1/runs/", headers=headers, json={
        "workflow_id": workflow_id,
        "account_id": account_id,
        "input_data": {"risk_score": 0.9},
    })
    assert retry.status_code == 202, retry.text
    assert retry.json()["id"] != run_id
