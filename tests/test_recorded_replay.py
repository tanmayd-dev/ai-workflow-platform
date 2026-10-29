"""Recorded-mode deterministic replay tests.

``POST /runs/{id}/replay`` accepts an optional body
``{"mode": "recorded" | "re-execute"}`` (default ``re-execute``, the
historical behavior). ``recorded`` replays the ORIGINAL run's recorded node
outputs verbatim:

- AI nodes return the recorded output as-is — the LLM is never called
  (asserted via a counting mock);
- DOMAIN_RULE nodes reuse the recorded output (no re-evaluation) while still
  recording the pinned rule_version_id (a rule bumped to v2 between run and
  replay must not leak v2 logic into the replay leg);
- CONDITIONAL nodes route exactly along the original run's recorded gate
  outcome ({"condition_met": ..., "next": ...}) so a re-executed AI output
  could never flip a branch;
- every executed AI/rule/gate node of the original must have a recorded
  output: a tampered (deleted) node-execution row fails the replay with 409;
- only COMPLETED source runs are recordable (409 for PAUSED / FAILED).

The default re-execute mode is unchanged (existing replay tests cover it).
"""
import time
from uuid import UUID, uuid4

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from app.core.database import get_session_maker
from app.main import app
from app.models.execution import (
    AuditLog,
    NodeExecution,
    NodeStatus,
    RunStatus,
    WorkflowRun,
)
from app.models.rule import RuleVersion
from app.models.workflow import WorkflowStep

from conftest import grant_role

TERMINAL_STATUSES = {"COMPLETED", "FAILED", "PAUSED"}


# ============= Shared fixtures / helpers =============

@pytest.fixture
def client():
    """In-process HTTP test client (startup initializes the test database)."""
    with TestClient(app) as test_client:
        yield test_client


@pytest.fixture
def counted_llm(monkeypatch):
    """mock_llm variant that also counts generate_completion calls.

    Mechanism: the shared ``mock_llm`` fixture does not record call counts,
    so this fixture monkeypatches LLMService.generate_completion with a
    counting closure and hands the test a {"count": N} registry. The count
    covers background executions too (the patch is global).
    """
    calls = {"count": 0}

    async def fake_generate_completion(
        self,
        system_prompt: str,
        user_prompt: str,
        context=None,
        temperature: float = 0.7,
        max_tokens: int = 2000,
    ):
        calls["count"] += 1
        return {
            "content": "MOCK-LLM ANALYSIS: transaction risk assessed",
            "prompt_snapshot": f"SYSTEM:{system_prompt} USER:{user_prompt} CONTEXT:{context}",
            "model_name": "mock-model",
            "temperature": temperature,
            "usage": {"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15},
        }

    monkeypatch.setattr(
        "app.services.llm_service.LLMService.generate_completion",
        fake_generate_completion,
    )
    return calls


def _register(client: TestClient, roles=("admin",)) -> dict:
    email = f"recr_{uuid4().hex[:8]}@example.com"
    register = client.post("/api/v1/auth/register", json={
        "email": email,
        "password": "testpass123",
        "full_name": "Recorded Replay Test User",
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


def _draft_and_approve(client: TestClient, headers: dict, *, name: str, steps: list) -> str:
    created = client.post("/api/v1/workflows/draft", headers=headers, json={
        "name": f"{name} {uuid4().hex[:8]}",
        "description": "Recorded replay test workflow",
        "domain": "fraud",
        "steps": steps,
    })
    assert created.status_code == 201, created.text
    workflow_id = created.json()["id"]
    approved = client.post(f"/api/v1/workflows/{workflow_id}/approve", headers=headers)
    assert approved.status_code == 200, approved.text
    return workflow_id


def _create_rule(client: TestClient, headers: dict, *, result: str) -> tuple[str, str]:
    rule_name = f"RecordedReplay_{uuid4().hex[:8]}"
    created = client.post("/api/v1/rules/", headers=headers, json={
        "name": rule_name,
        "description": "Recorded replay test rule",
        "domain": "fraud",
        "logic_json": {"result": result},
    })
    assert created.status_code == 201, created.text
    return created.json()["id"], rule_name


def _start_run(client: TestClient, headers: dict, workflow_id: str) -> str:
    started = client.post("/api/v1/runs/", headers=headers, json={
        "workflow_id": workflow_id,
        "account_id": f"ACC_RECORDED_{uuid4().hex[:8]}",
        "input_data": {"risk_score": 0.95},
    })
    assert started.status_code == 202, started.text
    return started.json()["id"]


def _poll_until_terminal(client: TestClient, run_id: str, headers: dict) -> dict:
    deadline = time.monotonic() + 5.0
    last_body = None
    while time.monotonic() < deadline:
        response = client.get(f"/api/v1/runs/{run_id}", headers=headers)
        assert response.status_code == 200, response.text
        last_body = response.json()
        if last_body["status"] in TERMINAL_STATUSES:
            return last_body
        time.sleep(0.05)
    raise AssertionError(f"Run {run_id} never reached terminal status: {last_body}")


def _branch_steps(rule_name: str) -> list:
    """Rule → gate → two one-step AI branches → shared AI tail.

    risk_score 0.95 evaluates the rule to ESCALATE, so the gate routes the
    TRUE branch ("High Path"); "Low Path" is recorded as SKIPPED.
    """
    return [
        {
            "order_index": 0,
            "step_type": "DOMAIN_RULE",
            "name": "Risk Check",
            "description": "Deterministic rule check",
            "config_json": {"rule_id": rule_name},
        },
        {
            "order_index": 1,
            "step_type": "CONDITIONAL",
            "name": "Risk Branch",
            "description": "Branch on the rule result",
            "config_json": {
                "condition": {
                    "field": "current_output",
                    "op": "==",
                    "value": "ESCALATE",
                },
                "on_true": "High Path",
                "on_false": "Low Path",
            },
        },
        {
            "order_index": 2,
            "step_type": "AI_ANALYSIS",
            "name": "High Path",
            "description": "AI handles high-risk path",
            "config_json": {"prompt_template": "high risk"},
        },
        {
            "order_index": 3,
            "step_type": "AI_ANALYSIS",
            "name": "Low Path",
            "description": "AI handles low-risk path",
            "config_json": {"prompt_template": "low risk"},
        },
        {
            "order_index": 4,
            "step_type": "AI_ANALYSIS",
            "name": "Final Step",
            "description": "Shared AI tail",
            "config_json": {"prompt_template": "final"},
        },
    ]


async def _node_rows(run_id: str) -> dict:
    """Node executions of a run keyed by step name."""
    session_maker = get_session_maker()
    async with session_maker() as session:
        result = await session.execute(
            select(NodeExecution, WorkflowStep.name)
            .join(WorkflowStep, WorkflowStep.id == NodeExecution.step_id)
            .where(NodeExecution.run_id == UUID(run_id))
            .order_by(WorkflowStep.order_index)
        )
        return {name: node for node, name in result.all()}


async def _latest_run_audit(run_id: UUID, action: str):
    session_maker = get_session_maker()
    async with session_maker() as session:
        return (await session.execute(
            select(AuditLog)
            .where(
                AuditLog.entity_type == "run",
                AuditLog.entity_id == run_id,
                AuditLog.action == action,
            )
            .order_by(AuditLog.timestamp.desc(), AuditLog.id.desc())
        )).scalars().first()


async def _fetch_run(run_id: str) -> WorkflowRun:
    session_maker = get_session_maker()
    async with session_maker() as session:
        return await session.get(WorkflowRun, UUID(run_id))


# ============= Recorded replay: determinism =============

async def test_recorded_replay_is_deterministic_and_never_calls_llm(client, counted_llm):
    """A recorded replay of a completed branching run produces identical
    outputs and identical per-step rows (routing included) — with zero
    additional LLM calls."""
    admin = _register(client)
    rule_id, rule_name = _create_rule(client, admin["headers"], result="ESCALATE")
    workflow_id = _draft_and_approve(
        client, admin["headers"], name="Recorded Branch", steps=_branch_steps(rule_name)
    )

    run_id = _start_run(client, admin["headers"], workflow_id)
    original = _poll_until_terminal(client, run_id, admin["headers"])
    assert original["status"] == "COMPLETED", original

    # The original leg ran 2 AI nodes (High Path + Final Step; Low Path is
    # skipped by the gate).
    assert counted_llm["count"] == 2, counted_llm
    calls_after_original = counted_llm["count"]

    original_rows = await _node_rows(run_id)
    assert original_rows["Risk Check"].status == NodeStatus.COMPLETED
    assert original_rows["Risk Branch"].output_snapshot == {
        "condition_met": True,
        "next": "High Path",
    }
    assert original_rows["Low Path"].status == NodeStatus.SKIPPED

    replay = client.post(
        f"/api/v1/runs/{run_id}/replay", headers=admin["headers"], json={"mode": "recorded"}
    )
    assert replay.status_code == 201, replay.text
    replay_run = replay.json()
    assert replay_run["status"] == "COMPLETED", replay_run
    assert replay_run["id"] != run_id
    assert UUID(replay_run["replayed_from_run_id"]) == UUID(run_id)

    # Zero additional LLM calls: the recorded leg never called the model.
    assert counted_llm["count"] == calls_after_original, counted_llm

    # Identical final output to the original.
    assert replay_run["output_data"] == original["output_data"], (
        replay_run["output_data"],
        original["output_data"],
    )

    # Per-step rows mirror the original execution — routing follows the
    # recorded gate outcome, AI/rule outputs are exact copies.
    replay_rows = await _node_rows(replay_run["id"])
    for step_name in ("Risk Check", "Risk Branch", "High Path", "Low Path", "Final Step"):
        assert step_name in replay_rows
        assert replay_rows[step_name].status == original_rows[step_name].status
        assert replay_rows[step_name].output_snapshot == (
            original_rows[step_name].output_snapshot
        )
    assert replay_rows["High Path"].model_name == "recorded-replay"
    assert replay_rows["Final Step"].model_name == "recorded-replay"
    assert replay_rows["Low Path"].status == NodeStatus.SKIPPED
    assert replay_rows["Low Path"].output_snapshot is None

    # The started audit of the replay leg marks the recorded mode.
    started_audit = await _latest_run_audit(UUID(replay_run["id"]), "started")
    assert started_audit.details is not None
    assert started_audit.details.get("replay_mode") == "recorded", started_audit.details


async def test_recorded_replay_without_body_reexecutes(client, counted_llm):
    """Default (no body) stays re-execute: the LLM IS called again and the
    audit does not mark the leg recorded."""
    admin = _register(client)
    workflow_id = _draft_and_approve(
        client, admin["headers"], name="Default Replay", steps=[
            {
                "order_index": 0,
                "step_type": "AI_ANALYSIS",
                "name": "Only Step",
                "description": "AI step",
                "config_json": {"prompt_template": "analyze"},
            },
        ],
    )
    run_id = _start_run(client, admin["headers"], workflow_id)
    original = _poll_until_terminal(client, run_id, admin["headers"])
    assert original["status"] == "COMPLETED", original
    calls_after_original = counted_llm["count"]
    assert calls_after_original == 1

    replay = client.post(f"/api/v1/runs/{run_id}/replay", headers=admin["headers"])
    assert replay.status_code == 201, replay.text
    replay_run = replay.json()
    assert replay_run["status"] == "COMPLETED"
    # Re-execute really re-ran the LLM.
    assert counted_llm["count"] == calls_after_original + 1

    audit = await _latest_run_audit(UUID(replay_run["id"]), "started")
    assert "replay_mode" not in (audit.details or {})


# ============= Recorded replay: rule pinning =============

async def test_recorded_replay_records_pinned_rule_version_despite_bump(client, mock_llm):
    """A rule bumped to v2 between the original run and the recorded replay
    must not leak v2 logic: the replay leg reuses v1's recorded output and
    still records v1's rule_version_id."""
    admin = _register(client)
    rule_id, rule_name = _create_rule(client, admin["headers"], result="RECORDED_V1_RESULT")
    v1_id = await _rule_version_id(rule_id, 1)

    workflow_id = _draft_and_approve(
        client, admin["headers"], name="Recorded Pinning", steps=[
            {
                "order_index": 0,
                "step_type": "DOMAIN_RULE",
                "name": "Single Rule",
                "description": "Deterministic rule check",
                "config_json": {"rule_id": rule_name},
            },
        ],
    )
    run_id = _start_run(client, admin["headers"], workflow_id)
    original = _poll_until_terminal(client, run_id, admin["headers"])
    assert original["status"] == "COMPLETED", original
    assert original["rule_versions_pinned"] == {rule_name: 1}

    # Bump to v2: the recorded replay must not re-evaluate with v2 logic.
    bumped = client.post(
        f"/api/v1/rules/{rule_id}/versions",
        headers=admin["headers"],
        json={"logic_json": {"result": "RECORDED_V2_LEAK"}},
    )
    assert bumped.status_code == 201, bumped.text
    assert bumped.json()["version_number"] == 2

    replay = client.post(
        f"/api/v1/runs/{run_id}/replay", headers=admin["headers"], json={"mode": "recorded"}
    )
    assert replay.status_code == 201, replay.text
    replay_run = replay.json()
    assert replay_run["status"] == "COMPLETED", replay_run
    assert replay_run["rule_versions_pinned"] == {rule_name: 1}

    rows = [node for node in (await _node_rows(replay_run["id"])).values()]
    completed = [node for node in rows if node.status == NodeStatus.COMPLETED]
    assert len(completed) == 1
    assert completed[0].rule_version_id == v1_id, (
        "the recorded replay leg must record the pinned v1 rule_version_id"
    )
    assert completed[0].output_snapshot == "RECORDED_V1_RESULT", (
        "the recorded replay must reuse v1's recorded output, not re-evaluate"
    )


async def _rule_version_id(rule_id: str, version_number: int) -> UUID:
    session_maker = get_session_maker()
    async with session_maker() as session:
        version = (await session.execute(
            select(RuleVersion).where(
                RuleVersion.rule_id == UUID(rule_id),
                RuleVersion.version_number == version_number,
            )
        )).scalar_one()
        return version.id


# ============= Recorded replay: tamper detection =============

async def test_recorded_replay_missing_recorded_output_conflicts(client, counted_llm):
    """Deleting one completed AI node-execution row of the source run makes
    the replay unverifiable: 409, and the graph must not have run (no silent
    LLM call masks the gap)."""
    admin = _register(client)
    rule_id, rule_name = _create_rule(client, admin["headers"], result="ESCALATE")
    workflow_id = _draft_and_approve(
        client, admin["headers"], name="Tampered Source", steps=_branch_steps(rule_name)
    )
    run_id = _start_run(client, admin["headers"], workflow_id)
    original = _poll_until_terminal(client, run_id, admin["headers"])
    assert original["status"] == "COMPLETED", original
    calls_before = counted_llm["count"]

    session_maker = get_session_maker()
    async with session_maker() as session:
        step = (await session.execute(
            select(WorkflowStep).where(
                WorkflowStep.workflow_id == UUID(workflow_id),
                WorkflowStep.name == "Final Step",
            )
        )).scalar_one()
        row = (await session.execute(
            select(NodeExecution).where(
                NodeExecution.run_id == UUID(run_id),
                NodeExecution.step_id == step.id,
                NodeExecution.status == NodeStatus.COMPLETED,
            )
        )).scalar_one()
        await session.delete(row)
        await session.commit()

    replay = client.post(
        f"/api/v1/runs/{run_id}/replay", headers=admin["headers"], json={"mode": "recorded"}
    )
    assert replay.status_code == 409, replay.text
    assert "recorded" in replay.json()["detail"].lower()
    assert "Final Step" in replay.json()["detail"]

    # The rejection happened at validation time: the graph never ran.
    assert counted_llm["count"] == calls_before


# ============= Recorded replay: source status gate =============

async def test_recorded_replay_requires_completed_source(client, mock_llm):
    """Recorded mode is only defined for COMPLETED source runs: a PAUSED and
    a FAILED source each give 409 (not the generic re-execute 400)."""
    admin = _register(client)
    workflow_id = _draft_and_approve(
        client, admin["headers"], name="Recorded Source Gate", steps=[
            {
                "order_index": 0,
                "step_type": "AI_ANALYSIS",
                "name": "Risk Analysis",
                "description": "AI analyzes",
                "config_json": {"prompt_template": "analyze"},
            },
            {
                "order_index": 1,
                "step_type": "HUMAN_APPROVAL",
                "name": "Manager Approval",
                "description": "Human gate",
                "config_json": {},
            },
        ],
    )
    run_id = _start_run(client, admin["headers"], workflow_id)
    paused = _poll_until_terminal(client, run_id, admin["headers"])
    assert paused["status"] == "PAUSED"

    replay_paused = client.post(
        f"/api/v1/runs/{run_id}/replay", headers=admin["headers"], json={"mode": "recorded"}
    )
    assert replay_paused.status_code == 409, replay_paused.text
    assert "COMPLETED" in replay_paused.json()["detail"]

    # Reject → FAILED source.
    rejected = client.post(
        f"/api/v1/runs/{run_id}/reject", headers=admin["headers"], json={"reason": "no go"}
    )
    assert rejected.status_code == 200, rejected.text
    assert rejected.json()["status"] == "FAILED"

    replay_failed = client.post(
        f"/api/v1/runs/{run_id}/replay", headers=admin["headers"], json={"mode": "recorded"}
    )
    assert replay_failed.status_code == 409, replay_failed.text
    assert "COMPLETED" in replay_failed.json()["detail"]


async def test_replay_mode_rejects_unknown_value(client, mock_llm):
    """Any mode other than recorded / re-execute fails body validation with
    422."""
    admin = _register(client)
    workflow_id = _draft_and_approve(
        client, admin["headers"], name="Mode Validation", steps=[
            {
                "order_index": 0,
                "step_type": "AI_ANALYSIS",
                "name": "Only Step",
                "description": "AI step",
                "config_json": {"prompt_template": "analyze"},
            },
        ],
    )
    run_id = _start_run(client, admin["headers"], workflow_id)
    completed = _poll_until_terminal(client, run_id, admin["headers"])
    assert completed["status"] == "COMPLETED"

    response = client.post(
        f"/api/v1/runs/{run_id}/replay", headers=admin["headers"], json={"mode": "bogus"}
    )
    assert response.status_code == 422, response.text
