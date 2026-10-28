"""RBAC enforcement tests for issue #13.

Roles are checked by ``require_roles`` (app/core/rbac.py):

- author   -> draft workflows, author rules, upload documents
- approver -> approve workflows, approve/reject paused runs
- executor -> start/replay runs
- auditor  -> authenticated reads only
- admin    -> superuser, bypasses every check

Reads need authentication but no role; a user without any role is denied every
guarded (role-checked) endpoint with 403.
"""
import time
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient

from app.main import app
from conftest import grant_role

TERMINAL_STATUSES = {"COMPLETED", "FAILED", "PAUSED"}


@pytest.fixture
def client():
    """In-process HTTP test client (startup event initializes the DB)."""
    with TestClient(app) as test_client:
        yield test_client


def _register(client: TestClient, roles=("admin",)) -> dict:
    """Register a user directly (no auto-role assignment), then grant roles."""
    email = f"rbac_{uuid4().hex[:8]}@example.com"
    register = client.post("/api/v1/auth/register", json={
        "email": email,
        "password": "testpass123",
        "full_name": "RBAC Test User",
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
        "id": register.json()["id"],
        "headers": {"Authorization": f"Bearer {login.json()['access_token']}"},
    }


def _draft_workflow(client: TestClient, headers: dict, steps=None):
    return client.post("/api/v1/workflows/draft", headers=headers, json={
        "name": f"RBAC Workflow {uuid4().hex[:8]}",
        "description": "RBAC test workflow",
        "domain": "fraud",
        "steps": steps or [
            {
                "order_index": 0,
                "step_type": "AI_ANALYSIS",
                "name": "Analyze",
                "description": "AI analysis",
                "config_json": {"prompt_template": "Analyze this transaction"},
            },
        ],
    })


def _approve_workflow(client: TestClient, headers: dict, workflow_id: str):
    return client.post(f"/api/v1/workflows/{workflow_id}/approve", headers=headers)


def _create_rule(client: TestClient, headers: dict):
    return client.post("/api/v1/rules", headers=headers, json={
        "name": f"rbac_rule_{uuid4().hex[:8]}",
        "domain": "fraud",
        "logic_json": {
            "condition": {"field": "amount", "op": ">", "value": 100},
            "result": "FLAG",
        },
    })


def _upload_document(client: TestClient, headers: dict):
    return client.post(
        "/api/v1/documents/upload",
        headers=headers,
        files={"file": ("policy.txt", b"policy text", "text/plain")},
        data={"domain": "fraud"},
    )


def _start_run(client: TestClient, headers: dict, workflow_id: str):
    return client.post("/api/v1/runs/", headers=headers, json={
        "workflow_id": workflow_id,
        "account_id": f"ACC_RBAC_{uuid4().hex[:8]}",
        "input_data": {"risk_score": 0.85},
    })


def _wait_for_run(client: TestClient, headers: dict, run_id: str, timeout: float = 5.0) -> dict:
    deadline = time.monotonic() + timeout
    last_body = None
    while time.monotonic() < deadline:
        response = client.get(f"/api/v1/runs/{run_id}", headers=headers)
        assert response.status_code == 200, response.text
        last_body = response.json()
        if last_body["status"] in TERMINAL_STATUSES:
            return last_body
        time.sleep(0.05)
    raise AssertionError(f"Run {run_id} did not finish; last seen: {last_body}")


def test_admin_bypasses_every_role_check(client, mock_llm):
    """admin can perform author, approver, executor, and admin-only actions."""
    admin = _register(client, ["admin"])
    headers = admin["headers"]

    rule = _create_rule(client, headers)                 # author action
    assert rule.status_code == 201, rule.text

    workflow = _draft_workflow(client, headers)          # author action
    assert workflow.status_code == 201, workflow.text
    workflow_id = workflow.json()["id"]

    approved = _approve_workflow(client, headers, workflow_id)  # approver action
    assert approved.status_code == 200, approved.text

    run = _start_run(client, headers, workflow_id)       # executor action
    assert run.status_code == 202, run.text
    assert _wait_for_run(client, headers, run.json()["id"])["status"] == "COMPLETED"

    # admin-only action
    deleted = client.delete(f"/api/v1/rules/{rule.json()['id']}", headers=headers)
    assert deleted.status_code == 204, deleted.text


def test_author_can_author_but_not_approve_or_delete(client):
    """author may draft workflows, create rules, and add versions."""
    author = _register(client, ["author"])
    headers = author["headers"]

    rule = _create_rule(client, headers)
    assert rule.status_code == 201, rule.text

    version = client.post(
        f"/api/v1/rules/{rule.json()['id']}/versions",
        headers=headers,
        json={"logic_json": {"result": "FLAG_V2"}},
    )
    assert version.status_code == 201, version.text

    workflow = _draft_workflow(client, headers)
    assert workflow.status_code == 201, workflow.text
    workflow_id = workflow.json()["id"]

    # approver action is denied
    denied = _approve_workflow(client, headers, workflow_id)
    assert denied.status_code == 403, denied.text

    # admin-only delete is denied
    assert client.delete(f"/api/v1/rules/{rule.json()['id']}", headers=headers).status_code == 403


def test_approver_can_approve_workflows_and_paused_runs(client, mock_llm):
    """approver handles workflow approval and human gates, and cannot author."""
    author = _register(client, ["author"])
    approver = _register(client, ["approver"])
    executor = _register(client, ["executor"])

    workflow = _draft_workflow(client, author["headers"])
    assert workflow.status_code == 201, workflow.text
    workflow_id = workflow.json()["id"]

    approved = _approve_workflow(client, approver["headers"], workflow_id)
    assert approved.status_code == 200, approved.text

    # approver cannot author
    assert _draft_workflow(client, approver["headers"]).status_code == 403
    assert _create_rule(client, approver["headers"]).status_code == 403

    # A run that pauses at a HUMAN_APPROVAL gate can only be approved by an approver.
    gated = _draft_workflow(
        client,
        author["headers"],
        steps=[
            {
                "order_index": 0,
                "step_type": "AI_ANALYSIS",
                "name": "Analyze",
                "config_json": {"prompt_template": "Analyze this transaction"},
            },
            {
                "order_index": 1,
                "step_type": "HUMAN_APPROVAL",
                "name": "Manager Gate",
                "config_json": {},
            },
        ],
    )
    assert gated.status_code == 201, gated.text
    gated_id = gated.json()["id"]
    assert _approve_workflow(client, approver["headers"], gated_id).status_code == 200

    run = _start_run(client, executor["headers"], gated_id)
    assert run.status_code == 202, run.text
    run_id = run.json()["id"]
    assert _wait_for_run(client, executor["headers"], run_id)["status"] == "PAUSED"

    # executor cannot decide the gate; approver can
    assert client.post(f"/api/v1/runs/{run_id}/approve", headers=executor["headers"]).status_code == 403
    assert client.post(f"/api/v1/runs/{run_id}/reject", headers=executor["headers"]).status_code == 403
    resumed = client.post(f"/api/v1/runs/{run_id}/approve", headers=approver["headers"])
    assert resumed.status_code == 200, resumed.text
    assert resumed.json()["status"] == "COMPLETED"


def test_executor_can_start_and_replay_runs(client, mock_llm):
    """executor may start and replay runs, but cannot author."""
    author = _register(client, ["author"])
    approver = _register(client, ["approver"])
    executor = _register(client, ["executor"])

    workflow = _draft_workflow(client, author["headers"])
    assert workflow.status_code == 201, workflow.text
    workflow_id = workflow.json()["id"]
    assert _approve_workflow(client, approver["headers"], workflow_id).status_code == 200

    created = _start_run(client, executor["headers"], workflow_id)
    assert created.status_code == 202, created.text
    run = _wait_for_run(client, executor["headers"], created.json()["id"])
    assert run["status"] == "COMPLETED"

    replay = client.post(f"/api/v1/runs/{run['id']}/replay", headers=executor["headers"])
    assert replay.status_code == 201, replay.text
    assert replay.json()["status"] == "COMPLETED"

    # executor cannot author definitions or approve workflows
    assert _create_rule(client, executor["headers"]).status_code == 403
    assert _draft_workflow(client, executor["headers"]).status_code == 403
    assert _approve_workflow(client, executor["headers"], workflow_id).status_code == 403


def test_auditor_is_read_only(client):
    """auditor can read every resource but mutate nothing."""
    auditor = _register(client, ["auditor"])
    headers = auditor["headers"]

    assert client.get("/api/v1/workflows/", headers=headers).status_code == 200
    assert client.get("/api/v1/rules", headers=headers).status_code == 200
    assert client.get("/api/v1/documents/", headers=headers).status_code == 200

    assert _draft_workflow(client, headers).status_code == 403
    assert _create_rule(client, headers).status_code == 403
    upload = client.post(
        "/api/v1/documents/upload",
        headers=headers,
        files={"file": ("policy.txt", b"policy text", "text/plain")},
        data={"domain": "fraud"},
    )
    assert upload.status_code == 403, upload.text


def test_user_without_roles_is_denied_but_can_read(client):
    """Strict mapping: no role means no guarded action (403), reads still work."""
    user = _register(client, roles=())
    headers = user["headers"]

    denied = _draft_workflow(client, headers)
    assert denied.status_code == 403, denied.text
    assert "permission" in denied.json()["detail"].lower()
    assert _create_rule(client, headers).status_code == 403

    # authenticated reads require no role
    assert client.get("/api/v1/workflows/", headers=headers).status_code == 200
    assert client.get("/api/v1/rules", headers=headers).status_code == 200
    assert client.get("/api/v1/documents/", headers=headers).status_code == 200


def test_unauthenticated_requests_get_401(client):
    """No token (or a bad token) is 401, not 403."""
    assert _draft_workflow(client, {}).status_code == 401
    assert _create_rule(client, {}).status_code == 401
    assert client.get("/api/v1/workflows/").status_code == 401
    assert client.get("/api/v1/rules").status_code == 401
    assert _draft_workflow(
        client, {"Authorization": "Bearer not-a-valid-token"}
    ).status_code == 401


def test_intents_draft_requires_author(client):
    """POST /intents/draft is an authoring endpoint: no role -> 403, no token -> 401.

    The dependency rejects the caller before the RAG/LLM services run, so this
    asserts the wiring without making a provider call.
    """
    approver = _register(client, ["approver"])
    payload = {"intent": "draft a fraud review workflow", "domain": "fraud"}

    assert client.post(
        "/api/v1/intents/draft", headers=approver["headers"], json=payload
    ).status_code == 403
    assert client.post("/api/v1/intents/draft", json=payload).status_code == 401


def test_role_names_are_case_insensitive(client):
    """A role stored as 'AUTHOR' satisfies require_roles('author')."""
    user = _register(client, ["AUTHOR"])
    assert _draft_workflow(client, user["headers"]).status_code == 201


def test_get_document_by_id_returns_metadata_for_any_authenticated_user(client, monkeypatch):
    """Upload as author, then GET /documents/{id} returns 200 with correct fields.

    The path parameter is a UUID (Document.id is a PG UUID column), so it must
    round-trip through the response without a 500.
    """
    # Document upload ingestion runs the real embedding pipeline; mock it the
    # way test_intent_drafting mocks the RAG layer (chunk_count is asserted).
    async def fake_ingest_document(self, content, document_id, filename, domain):
        return 1

    monkeypatch.setattr(
        "app.services.rag_service.RAGService.ingest_document",
        fake_ingest_document,
    )

    author = _register(client, ["author"])
    upload = _upload_document(client, author["headers"])
    assert upload.status_code == 201, upload.text
    doc = upload.json()

    # any authenticated user (no role required) can read it
    reader = _register(client, roles=())
    got = client.get(f"/api/v1/documents/{doc['id']}", headers=reader["headers"])
    assert got.status_code == 200, got.text
    body = got.json()
    assert body["id"] == doc["id"]
    assert body["filename"] == "policy.txt"
    assert body["domain"] == "fraud"
    assert body["file_size"] == len(b"policy text")
    assert body["chunk_count"] == 1


def test_get_document_malformed_uuid_returns_422(client):
    """A malformed {id} fails UUID parsing with FastAPI's 422, not 500."""
    user = _register(client, roles=())
    response = client.get(
        "/api/v1/documents/not-a-valid-uuid", headers=user["headers"]
    )
    assert response.status_code == 422, response.text
