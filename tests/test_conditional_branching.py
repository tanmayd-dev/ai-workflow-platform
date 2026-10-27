"""Focused tests for conditional branching (issue #4) and audit lineage (#7)."""
from uuid import uuid4

import pytest
import pytest_asyncio
from sqlalchemy import select

from app.core.database import get_session_maker
from app.models.execution import AuditLog, NodeExecution, NodeStatus, RunStatus
from app.models.rule import Rule, RuleVersion
from app.models.user import User
from app.models.workflow import (
    PromptTemplate,
    StepType,
    WorkflowDefinition,
    WorkflowStatus,
    WorkflowStep,
)
from app.services.compiler import WorkflowCompiler
from app.services.executor import WorkflowExecutor


@pytest_asyncio.fixture
async def db_session():
    async_session_maker = get_session_maker()
    async with async_session_maker() as session:
        yield session


@pytest_asyncio.fixture
async def operator(db_session):
    user = User(
        id=uuid4(),
        email=f"conditional_{uuid4().hex[:8]}@example.com",
        hashed_password="not-used",
        full_name="Conditional Operator",
        is_active=True,
    )
    db_session.add(user)
    await db_session.commit()
    await db_session.refresh(user)
    return user


@pytest_asyncio.fixture
async def escalate_rule(db_session, operator):
    rule = Rule(
        id=uuid4(),
        name=f"escalate_{uuid4().hex[:8]}",
        description="Escalate when risk score is high",
        domain="fraud",
    )
    db_session.add(rule)
    await db_session.flush()

    version = RuleVersion(
        id=uuid4(),
        rule_id=rule.id,
        version_number=1,
        logic_json={
            "condition": {"field": "risk_score", "op": ">", "value": 0.8},
            "result": "ESCALATE",
        },
        is_active=True,
        created_by=operator.id,
    )
    db_session.add(version)
    await db_session.commit()
    return rule, version


async def _make_workflow(db, operator, name, steps):
    workflow = WorkflowDefinition(
        id=uuid4(),
        name=name,
        description="conditional branching test workflow",
        domain="fraud",
        version=1,
        status=WorkflowStatus.APPROVED,
        is_active=True,
        created_by=operator.id,
        approved_by=operator.id,
    )
    db.add(workflow)
    await db.flush()

    for order_index, step_type, step_name, config_json in steps:
        db.add(
            WorkflowStep(
                id=uuid4(),
                workflow_id=workflow.id,
                order_index=order_index,
                step_type=step_type,
                name=step_name,
                description=step_name,
                config_json=config_json,
            )
        )
    await db.commit()
    await db.refresh(workflow)
    return workflow


async def _node_rows(db, run_id):
    result = await db.execute(
        select(WorkflowStep.name, NodeExecution)
        .join(NodeExecution, NodeExecution.step_id == WorkflowStep.id)
        .where(NodeExecution.run_id == run_id)
        .order_by(WorkflowStep.order_index)
    )
    return result.all()


async def _branch_workflow(db, operator, rule_name):
    return await _make_workflow(
        db,
        operator,
        "Branch Workflow",
        [
            (
                0,
                StepType.DOMAIN_RULE,
                "Risk Check",
                {"rule_id": rule_name},
            ),
            (
                1,
                StepType.CONDITIONAL,
                "Risk Branch",
                {
                    "condition": {
                        "field": "current_output",
                        "op": "==",
                        "value": "ESCALATE",
                    },
                    "on_true": "High Path",
                    "on_false": "Low Path",
                },
            ),
            (2, StepType.AI_ANALYSIS, "High Path", {"prompt_template": "high risk"}),
            (3, StepType.AI_ANALYSIS, "Low Path", {"prompt_template": "low risk"}),
            (4, StepType.AI_ANALYSIS, "Final Step", {"prompt_template": "final"}),
        ],
    )


@pytest.mark.asyncio
async def test_conditional_true_branch_skips_false_branch(
    db_session, operator, escalate_rule, mock_llm
):
    rule, _ = escalate_rule
    workflow = await _branch_workflow(db_session, operator, rule.name)

    run = await WorkflowExecutor().execute(
        workflow_id=workflow.id,
        input_data={"risk_score": 0.95},
        account_id="ACC_COND_TRUE",
        db=db_session,
        triggered_by=operator.id,
    )
    assert run.status == RunStatus.COMPLETED, run.error_message

    rows = {name: node for name, node in await _node_rows(db_session, run.id)}
    assert rows["Risk Check"].status == NodeStatus.COMPLETED
    assert rows["Risk Branch"].status == NodeStatus.COMPLETED
    assert rows["Risk Branch"].output_snapshot == {
        "condition_met": True,
        "next": "High Path",
    }
    assert rows["High Path"].status == NodeStatus.COMPLETED
    assert rows["Low Path"].status == NodeStatus.SKIPPED
    assert rows["Low Path"].output_snapshot is None
    assert rows["Final Step"].status == NodeStatus.COMPLETED

    snapshot = run.state_snapshot or {}
    assert "High Path" in snapshot["ai_outputs"]
    assert "Low Path" not in snapshot["ai_outputs"]


@pytest.mark.asyncio
async def test_conditional_false_branch_skips_true_branch(
    db_session, operator, escalate_rule, mock_llm
):
    rule, _ = escalate_rule
    workflow = await _branch_workflow(db_session, operator, rule.name)

    run = await WorkflowExecutor().execute(
        workflow_id=workflow.id,
        input_data={"risk_score": 0.1},
        account_id="ACC_COND_FALSE",
        db=db_session,
        triggered_by=operator.id,
    )
    assert run.status == RunStatus.COMPLETED, run.error_message

    rows = {name: node for name, node in await _node_rows(db_session, run.id)}
    assert rows["Risk Branch"].output_snapshot == {
        "condition_met": False,
        "next": "Low Path",
    }
    assert rows["High Path"].status == NodeStatus.SKIPPED
    assert rows["Low Path"].status == NodeStatus.COMPLETED
    assert rows["Final Step"].status == NodeStatus.COMPLETED

    snapshot = run.state_snapshot or {}
    assert "Low Path" in snapshot["ai_outputs"]
    assert "High Path" not in snapshot["ai_outputs"]


@pytest.mark.asyncio
async def test_conditional_omitted_target_falls_through(
    db_session, operator, mock_llm
):
    workflow = await _make_workflow(
        db_session,
        operator,
        "Fallthrough Workflow",
        [
            (
                0,
                StepType.CONDITIONAL,
                "Amount Gate",
                {
                    "condition": {"field": "amount", "op": ">", "value": 1000},
                    "on_true": "Escalate",
                },
            ),
            (1, StepType.AI_ANALYSIS, "Manual Review", {"prompt_template": "manual"}),
            (2, StepType.AI_ANALYSIS, "Escalate", {"prompt_template": "escalate"}),
            (3, StepType.AI_ANALYSIS, "Done", {"prompt_template": "done"}),
        ],
    )
    executor = WorkflowExecutor()

    high = await executor.execute(
        workflow_id=workflow.id,
        input_data={"amount": 5000},
        account_id="ACC_FALL_HIGH",
        db=db_session,
        triggered_by=operator.id,
    )
    assert high.status == RunStatus.COMPLETED, high.error_message
    high_rows = {name: node for name, node in await _node_rows(db_session, high.id)}
    assert high_rows["Amount Gate"].output_snapshot == {
        "condition_met": True,
        "next": "Escalate",
    }
    assert high_rows["Manual Review"].status == NodeStatus.SKIPPED
    assert high_rows["Escalate"].status == NodeStatus.COMPLETED
    assert high_rows["Done"].status == NodeStatus.COMPLETED

    low = await executor.execute(
        workflow_id=workflow.id,
        input_data={"amount": 10},
        account_id="ACC_FALL_LOW",
        db=db_session,
        triggered_by=operator.id,
    )
    assert low.status == RunStatus.COMPLETED, low.error_message
    low_rows = {name: node for name, node in await _node_rows(db_session, low.id)}
    assert low_rows["Amount Gate"].output_snapshot == {
        "condition_met": False,
        "next": None,
    }
    # Omitted target: the next linear step runs, and the then-branch is skipped.
    assert low_rows["Manual Review"].status == NodeStatus.COMPLETED
    assert low_rows["Escalate"].status == NodeStatus.SKIPPED
    assert low_rows["Done"].status == NodeStatus.COMPLETED


def test_conditional_unknown_target_raises_at_compile_time(operator):
    workflow = WorkflowDefinition(
        id=uuid4(),
        name="Broken Conditional Workflow",
        domain="fraud",
        version=1,
        status=WorkflowStatus.DRAFT,
        created_by=operator.id,
    )
    workflow.steps = [
        WorkflowStep(
            id=uuid4(),
            order_index=0,
            step_type=StepType.CONDITIONAL,
            name="Gate",
            config_json={
                "condition": {"field": "amount", "op": ">", "value": 100},
                "on_true": "Does Not Exist",
            },
        )
    ]

    with pytest.raises(ValueError, match="unknown target 'Does Not Exist'"):
        WorkflowCompiler().compile(workflow)


@pytest.mark.asyncio
async def test_nested_conditional_branches(db_session, operator, escalate_rule, mock_llm):
    rule, _ = escalate_rule
    workflow = await _make_workflow(
        db_session,
        operator,
        "Nested Branch Workflow",
        [
            (0, StepType.DOMAIN_RULE, "Risk Check", {"rule_id": rule.name}),
            (
                1,
                StepType.CONDITIONAL,
                "Outer Gate",
                {
                    "condition": {
                        "field": "current_output",
                        "op": "==",
                        "value": "ESCALATE",
                    },
                    "on_true": "Inner Gate",
                    "on_false": "Low Path",
                },
            ),
            (
                2,
                StepType.CONDITIONAL,
                "Inner Gate",
                {
                    "condition": {"field": "amount", "op": ">", "value": 1000},
                    "on_true": "High Amount",
                    "on_false": "Low Amount",
                },
            ),
            (3, StepType.AI_ANALYSIS, "High Amount", {"prompt_template": "high"}),
            (4, StepType.AI_ANALYSIS, "Low Amount", {"prompt_template": "low"}),
            (5, StepType.AI_ANALYSIS, "Low Path", {"prompt_template": "low path"}),
            (6, StepType.AI_ANALYSIS, "Final Step", {"prompt_template": "final"}),
        ],
    )

    run = await WorkflowExecutor().execute(
        workflow_id=workflow.id,
        input_data={"risk_score": 0.95, "amount": 5000},
        account_id="ACC_NESTED",
        db=db_session,
        triggered_by=operator.id,
    )
    assert run.status == RunStatus.COMPLETED, run.error_message

    rows = {name: node for name, node in await _node_rows(db_session, run.id)}
    assert rows["Outer Gate"].output_snapshot["next"] == "Inner Gate"
    assert rows["Inner Gate"].output_snapshot["next"] == "High Amount"
    assert rows["High Amount"].status == NodeStatus.COMPLETED
    assert rows["Low Amount"].status == NodeStatus.SKIPPED
    # The outer false branch must not leak into a true outer branch.
    assert rows["Low Path"].status == NodeStatus.SKIPPED
    assert rows["Final Step"].status == NodeStatus.COMPLETED


@pytest.mark.asyncio
async def test_audit_write_failure_does_not_fail_run(
    db_session, operator, mock_llm, monkeypatch
):
    """An audit write failure is logged but must not change the run outcome."""
    import app.services.executor as executor_module

    class BrokenAuditLog:
        def __init__(self, **kwargs):
            raise RuntimeError("audit storage unavailable")

    monkeypatch.setattr(executor_module, "AuditLog", BrokenAuditLog)

    workflow = await _make_workflow(
        db_session,
        operator,
        "Audit Failure Workflow",
        [(0, StepType.AI_ANALYSIS, "Risk Analysis", {"prompt_template": "risk"})],
    )

    run = await WorkflowExecutor().execute(
        workflow_id=workflow.id,
        input_data={"risk_score": 0.5},
        account_id="ACC_AUDIT_FAIL",
        db=db_session,
        triggered_by=operator.id,
    )
    assert run.status == RunStatus.COMPLETED, run.error_message

    audits = (
        await db_session.execute(select(AuditLog).where(AuditLog.entity_id == run.id))
    ).scalars().all()
    assert audits == []


@pytest.mark.asyncio
async def test_human_approval_and_rejection_are_audited(
    db_session, operator, mock_llm
):
    workflow = await _make_workflow(
        db_session,
        operator,
        "Audited Approval Workflow",
        [
            (0, StepType.AI_ANALYSIS, "Analysis", {"prompt_template": "analysis"}),
            (1, StepType.HUMAN_APPROVAL, "Approval", {}),
            (2, StepType.AI_ANALYSIS, "After", {"prompt_template": "after"}),
        ],
    )
    executor = WorkflowExecutor()

    paused = await executor.execute(
        workflow_id=workflow.id,
        input_data={"risk_score": 0.9},
        account_id="ACC_AUDIT_APPROVE",
        db=db_session,
        triggered_by=operator.id,
    )
    assert paused.status == RunStatus.PAUSED

    paused_actions = {
        audit.action
        for audit in (
            await db_session.execute(
                select(AuditLog).where(AuditLog.entity_id == paused.id)
            )
        ).scalars().all()
    }
    assert "paused" in paused_actions

    approved = await executor.resume(
        paused.id,
        {"approved": True, "approver": operator.email, "notes": "ok"},
        db_session,
    )
    assert approved.status == RunStatus.COMPLETED

    all_audits = (await db_session.execute(select(AuditLog))).scalars().all()
    actions = {
        audit.action
        for audit in all_audits
        if audit.entity_id == paused.id
        or (audit.details or {}).get("run_id") == str(paused.id)
    }
    assert {"resumed", "approved", "completed"} <= actions

    rejected_run = await executor.execute(
        workflow_id=workflow.id,
        input_data={"risk_score": 0.9},
        account_id="ACC_AUDIT_REJECT",
        db=db_session,
        triggered_by=operator.id,
    )
    assert rejected_run.status == RunStatus.PAUSED

    await executor.reject(
        rejected_run.id, "policy violation", db_session, user=operator
    )

    reject_audits = (await db_session.execute(select(AuditLog))).scalars().all()
    rejected_actions = {
        audit.action
        for audit in reject_audits
        if audit.entity_id == rejected_run.id
        or (audit.details or {}).get("run_id") == str(rejected_run.id)
    }
    assert "rejected" in rejected_actions
    assert "failed" in rejected_actions
    assert all(
        audit.actor_id == operator.id
        for audit in reject_audits
        if (audit.details or {}).get("run_id") == str(rejected_run.id)
    )


@pytest.mark.asyncio
async def test_audit_lineage_persisted_after_run(db_session, operator, mock_llm):
    rule = Rule(
        id=uuid4(),
        name=f"lineage_rule_{uuid4().hex[:8]}",
        description="lineage test rule",
        domain="fraud",
    )
    db_session.add(rule)
    await db_session.flush()
    version = RuleVersion(
        id=uuid4(),
        rule_id=rule.id,
        version_number=1,
        logic_json={
            "condition": {"field": "risk_score", "op": ">", "value": 0.8},
            "result": "ESCALATE",
        },
        is_active=True,
        created_by=operator.id,
    )
    db_session.add(version)

    template = PromptTemplate(
        id=uuid4(),
        name=f"lineage_prompt_{uuid4().hex[:8]}",
        version=1,
        template_text="Analyze this transaction for fraud risk",
    )
    db_session.add(template)
    await db_session.commit()

    workflow = await _make_workflow(
        db_session,
        operator,
        "Lineage Workflow",
        [
            (
                0,
                StepType.AI_ANALYSIS,
                "Risk Analysis",
                {
                    "prompt_template": "Analyze this transaction for fraud risk",
                    "prompt_template_id": str(template.id),
                },
            ),
            (1, StepType.DOMAIN_RULE, "Threshold Check", {"rule_id": rule.name}),
        ],
    )

    run = await WorkflowExecutor().execute(
        workflow_id=workflow.id,
        input_data={"risk_score": 0.92},
        account_id="ACC_LINEAGE",
        db=db_session,
        triggered_by=operator.id,
    )
    assert run.status == RunStatus.COMPLETED, run.error_message

    rows = {name: node for name, node in await _node_rows(db_session, run.id)}

    ai_node = rows["Risk Analysis"]
    assert ai_node.prompt_template_id == template.id
    assert ai_node.prompt_snapshot and "Analyze this transaction" in ai_node.prompt_snapshot
    assert ai_node.model_name == "mock-model"

    rule_node = rows["Threshold Check"]
    assert rule_node.rule_version_id == version.id
    assert rule_node.rule_evaluation_result == "ESCALATE"

    # Audit trail: run transitions and per-node transitions are persisted.
    run_audits = (
        await db_session.execute(
            select(AuditLog).where(
                AuditLog.entity_type == "run", AuditLog.entity_id == run.id
            )
        )
    ).scalars().all()
    run_actions = {audit.action for audit in run_audits}
    assert {"started", "completed"} <= run_actions
    assert all(audit.actor_id == operator.id for audit in run_audits)

    node_audits = (
        await db_session.execute(
            select(AuditLog).where(AuditLog.entity_type == "node")
        )
    ).scalars().all()
    audited_node_ids = {audit.details["node_execution_id"] for audit in node_audits}
    assert {str(node.id) for node in rows.values()} <= audited_node_ids
    node_actions = {audit.action for audit in node_audits}
    assert {"started", "completed"} <= node_actions
    assert all(audit.details["run_id"] == str(run.id) for audit in node_audits)
    # Audit details carry ids, never the rendered prompt.
    assert all("SYSTEM:" not in str(audit.details) for audit in node_audits)
