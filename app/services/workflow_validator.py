"""
Approval-time validation for workflow definitions.

AI-drafted workflows can be structurally broken in ways that only surface on
first execution: zero steps, duplicate ``order_index`` values silently
overwriting branch wiring, steps that reference rules missing from the rule
library. This module validates a definition *before* the approve endpoint
flips its status, so a broken workflow can never become approvable.

The structural checks (branch targets, condition shapes, branch ambiguity)
are delegated to the compiler, which already rejects them at compile time;
here the compiler is invoked purely as a validator — no graph is executed.
"""

import logging

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from typing import Any, Dict

from app.models.rule import Rule, RuleVersion
from app.models.workflow import WorkflowDefinition
from app.services.compiler import WorkflowCompiler

logger = logging.getLogger(__name__)


async def validate_workflow_for_approval(
    workflow: WorkflowDefinition, db: AsyncSession
) -> None:
    """Validate a workflow definition before it can be approved.

    Raises ``ValueError`` with a precise message for the first problem found:

    - no steps at all;
    - duplicate ``order_index`` values or duplicate step names;
    - a DOMAIN_RULE step whose ``config_json.rule_id`` resolves to no rule in
      the rule library (or to a rule with no active version), mirroring the
      semantics of ``WorkflowExecutor._resolve_rule_logics``;
    - a compile failure (bad CONDITIONAL targets/shapes, branch ambiguity,
      ...), re-raised with the compiler's step-level context attached.
    """
    steps = sorted(workflow.steps, key=lambda s: s.order_index)

    # (a) A workflow with zero steps approves fine today and fails the moment
    # it is executed. Refuse it at gate time.
    if not steps:
        raise ValueError(
            "Workflow has no steps to execute; at least one step is required"
        )

    # (b) Duplicate order_index values make step sequencing ambiguous and
    # silently overwrite branch wiring; duplicate names break CONDITIONAL
    # targeting, which routes by step name.
    indexes = [step.order_index for step in steps]
    duplicate_indexes = sorted({i for i in indexes if indexes.count(i) > 1})
    if duplicate_indexes:
        raise ValueError(
            f"Workflow order_index values must be unique; duplicates: {duplicate_indexes}"
        )

    names = [step.name for step in steps]
    duplicate_names = sorted({name for name in names if names.count(name) > 1})
    if duplicate_names:
        raise ValueError(
            f"Workflow step names must be unique; duplicates: {duplicate_names}"
        )

    # (d) Resolve every DOMAIN_RULE rule_id exactly the way the executor does
    # (Rule matched by name, plus its latest active version). An omitted
    # rule_id stays allowed — the compiler treats such steps as pass-through.
    rule_logics: Dict[str, Dict[str, Any]] = {}
    for step in steps:
        rule_id = (step.config_json or {}).get("rule_id")
        if not rule_id or rule_id in rule_logics:
            continue

        result = await db.execute(select(Rule).where(Rule.name == rule_id))
        rule = result.scalar_one_or_none()
        if not rule:
            raise ValueError(
                f"Rule '{rule_id}' referenced by step '{step.name}' not found "
                f"in the rule library"
            )

        version_result = await db.execute(
            select(RuleVersion)
            .where(RuleVersion.rule_id == rule.id, RuleVersion.is_active == True)  # noqa: E712
            .order_by(RuleVersion.version_number.desc())
        )
        version = version_result.scalars().first()
        if not version:
            raise ValueError(
                f"Rule '{rule_id}' referenced by step '{step.name}' exists but "
                f"has no active version"
            )

        rule_logics[rule_id] = version.logic_json

    # (c) Delegate the structural checks to the compiler. The pre-resolved
    # rule map satisfies the compiler's own rule resolution check, so a
    # compile ValueError here is a genuine structural problem (conditional
    # targets, branch bodies, ambiguity) — re-raise with its step context.
    try:
        WorkflowCompiler().compile(workflow, rule_logics=rule_logics)
    except ValueError as exc:
        raise ValueError(f"Workflow step wiring is invalid: {exc}") from exc
