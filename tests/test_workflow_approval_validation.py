"""
P1b tests: approve-time workflow validation and DRAFT-stage editing.

The approve endpoint must refuse structurally broken workflows (zero steps,
rule references that resolve to nothing) with HTTP 422 while leaving the
definition in DRAFT. A PATCH endpoint lets authors edit definitions while
DRAFT, applying the same validator as a pre-check.
"""
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient

from app.main import app


@pytest.fixture
def client():
    """In-process HTTP test client (startup initializes the test database)."""
    with TestClient(app) as test_client:
        yield test_client


def _register_with_roles(client: TestClient, roles: list) -> dict:
    """Register, grant roles, login; return the auth headers (admin harness pattern)."""
    email = f"workflow_editor_{uuid4().hex}@example.com"
    register = client.post("/api/v1/auth/register", json={
        "email": email,
        "password": "testpass123",
        "full_name": "Workflow Validation User",
    })
    assert register.status_code == 201, register.text

    # grant_role is the conftest helper used across the suite; it writes the
    # role link through its own session, so this endpoint can be used without
    # a user-management API.
    from conftest import grant_role
    for role in roles:
        assert "granted" in grant_role(email, role)

    login = client.post("/api/v1/auth/login", data={
        "username": email,
        "password": "testpass123",
    })
    assert login.status_code == 200, login.text
    return {"Authorization": f"Bearer {login.json()['access_token']}"}


def _create_workflow(client: TestClient, headers: dict, steps: list):
    body = {
        "name": f"Validation Workflow {uuid4().hex[:8]}",
        "description": "Approve-time validation test workflow",
        "domain": "fraud",
        "steps": steps,
    }
    created = client.post("/api/v1/workflows/draft", headers=headers, json=body)
    assert created.status_code == 201, created.text
    return created.json()


def _rule_step(rule_name: str, order: int = 0, name: str = "Threshold Check") -> dict:
    return {
        "order_index": order,
        "step_type": "DOMAIN_RULE",
        "name": name,
        "description": "Rule evaluation",
        "config_json": {"rule_id": rule_name},
    }


def _approval_step(order: int = 1, name: str = "Manager Approval") -> dict:
    return {
        "order_index": order,
        "step_type": "HUMAN_APPROVAL",
        "name": name,
        "description": "Human review before proceeding",
        "config_json": {},
    }


def _create_rule(client: TestClient, headers: dict) -> str:
    """Create a rule through the API; returns its (unique) name."""
    rule_name = f"approval_gate_rule_{uuid4().hex[:8]}"
    response = client.post("/api/v1/rules", headers=headers, json={
        "name": rule_name,
        "domain": "fraud",
        "logic_json": {
            "condition": {"field": "amount", "op": ">", "value": 100},
            "result": "FLAG",
        },
    })
    assert response.status_code == 201, response.text
    return rule_name


def _get_workflow(client: TestClient, headers: dict, workflow_id: str) -> dict:
    response = client.get(f"/api/v1/workflows/{workflow_id}", headers=headers)
    assert response.status_code == 200, response.text
    return response.json()


def test_approving_zero_step_workflow_returns_422_and_stays_draft(client):
    """A zero-step definition can be approved today and fails on execution;
    the gate must now refuse it and leave it in DRAFT."""
    headers = _register_with_roles(client, ["author", "approver"])
    workflow = _create_workflow(client, headers, [])

    approved = client.post(f"/api/v1/workflows/{workflow['id']}/approve", headers=headers)
    assert approved.status_code == 422, approved.text
    assert "Workflow does not pass approval validation" in approved.json()["detail"]

    fetched = _get_workflow(client, headers, workflow["id"])
    assert fetched["status"] == "DRAFT"
    assert fetched["approved_by"] is None
    assert fetched["approved_at"] is None


def test_approving_workflow_with_nonexistent_rule_returns_422_detail_mentions_rule(client):
    """config_json.rule_id is matched against Rule.name at execution time; a
    reference that resolves to nothing must fail the gate now."""
    headers = _register_with_roles(client, ["author", "approver"])
    missing_rule = f"missing_rule_{uuid4().hex[:8]}"
    workflow = _create_workflow(
        client, headers, [_rule_step(missing_rule), _approval_step()]
    )

    approved = client.post(f"/api/v1/workflows/{workflow['id']}/approve", headers=headers)
    assert approved.status_code == 422, approved.text
    assert missing_rule in approved.json()["detail"]
    assert "rule" in approved.json()["detail"]

    fetched = _get_workflow(client, headers, workflow["id"])
    assert fetched["status"] == "DRAFT"


def test_approving_valid_rule_and_approval_workflow_still_200(client, mock_llm):
    """A well-formed workflow (real rule reference + human gate) approves as before."""
    headers = _register_with_roles(client, ["author", "approver"])
    rule_name = _create_rule(client, headers)
    workflow = _create_workflow(
        client, headers, [_rule_step(rule_name), _approval_step()]
    )

    approved = client.post(f"/api/v1/workflows/{workflow['id']}/approve", headers=headers)
    assert approved.status_code == 200, approved.text
    assert approved.json()["status"] == "APPROVED"


def test_patch_editable_while_draft_then_approve_service_sees_edited_steps(client, mock_llm):
    """PATCH replaces steps while DRAFT; the approve endpoint then validates and
    approves the *edited* definition, and GET exposes the replacement steps."""
    headers = _register_with_roles(client, ["author", "approver"])
    workflow = _create_workflow(
        client, headers, [{
            "order_index": 0,
            "step_type": "AI_ANALYSIS",
            "name": "Risk Analysis",
            "description": "AI analyzes transaction risk",
            "config_json": {"prompt_template": "Analyze this transaction"},
        }]
    )

    rule_name = _create_rule(client, headers)
    patched = client.patch(
        f"/api/v1/workflows/{workflow['id']}",
        headers=headers,
        json={
            "name": "Edited Validation Workflow",
            "steps": [_rule_step(rule_name), _approval_step()],
        },
    )
    assert patched.status_code == 200, patched.text
    assert patched.json()["name"] == "Edited Validation Workflow"
    patched_names = [step["name"] for step in patched.json()["steps"]]
    assert patched_names == ["Threshold Check", "Manager Approval"]

    fetched_before = _get_workflow(client, headers, workflow["id"])
    assert [step["name"] for step in fetched_before["steps"]] == patched_names

    # The approve gate sees the edited (rule + approval) steps.
    approved = client.post(f"/api/v1/workflows/{workflow['id']}/approve", headers=headers)
    assert approved.status_code == 200, approved.text

    fetched_after = _get_workflow(client, headers, workflow["id"])
    assert fetched_after["status"] == "APPROVED"
    assert [step["name"] for step in fetched_after["steps"]] == patched_names


def test_patch_after_approval_returns_409(client):
    """Editing is a DRAFT-only affordance: an APPROVED workflow cannot be edited."""
    headers = _register_with_roles(client, ["author", "approver"])
    workflow = _create_workflow(
        client, headers, [{
            "order_index": 0,
            "step_type": "HUMAN_APPROVAL",
            "name": "Gate",
            "description": "Simple valid workflow",
            "config_json": {},
        }]
    )
    approved = client.post(f"/api/v1/workflows/{workflow['id']}/approve", headers=headers)
    assert approved.status_code == 200, approved.text

    patched = client.patch(
        f"/api/v1/workflows/{workflow['id']}",
        headers=headers,
        json={"name": "Should Not Apply"},
    )
    assert patched.status_code == 409, patched.text
    assert patched.json()["detail"] == "Only DRAFT workflows can be edited"

    fetched = _get_workflow(client, headers, workflow["id"])
    assert fetched["name"] == workflow["name"]


def test_patch_with_duplicate_order_index_returns_422_and_keeps_original_steps(client):
    """RULING (P1b): the PATCH path reuses the approval validator as a pre-check,
    so an edit producing duplicate order_index values is rejected with 422 and
    the previously stored steps survive the request."""
    headers = _register_with_roles(client, ["author", "approver"])
    rule_name = _create_rule(client, headers)
    original_steps = [_rule_step(rule_name, order=0), _approval_step(order=1)]
    workflow = _create_workflow(client, headers, original_steps)

    patched = client.patch(
        f"/api/v1/workflows/{workflow['id']}",
        headers=headers,
        json={
            "steps": [
                _rule_step("some_rule_a", order=0, name="First Check"),
                _rule_step("some_rule_b", order=0, name="Second Check"),
            ]
        },
    )
    assert patched.status_code == 422, patched.text
    assert "Workflow does not pass edit validation" in patched.json()["detail"]

    fetched = _get_workflow(client, headers, workflow["id"])
    assert [step["name"] for step in fetched["steps"]], fetched
    original_names = [step["name"] for step in original_steps]
    assert [step["name"] for step in fetched["steps"]] == original_names
