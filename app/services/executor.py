"""
Workflow Executor Service
Executes compiled workflows and manages execution state.
"""
import asyncio
import logging
from uuid import UUID, uuid4
from datetime import datetime, timedelta
from typing import Dict, Any, Optional
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, update, or_
from sqlalchemy.orm import selectinload

from app.models.rule import Rule, RuleVersion
from app.models.workflow import WorkflowDefinition, WorkflowStep, StepType
from app.models.execution import (
    AuditLog,
    WorkflowRun,
    NodeExecution,
    RunStatus,
    NodeStatus,
)
from app.models.user import User
from app.services.compiler import WorkflowCompiler, WorkflowState, WorkflowPaused

logger = logging.getLogger(__name__)

# Durable lease TTL for background run execution. The background task claims the
# run before executing (CAS on a NULL/expired lease) and must re-extend the
# lease more often than this TTL while the run is alive.
LEASE_TTL_SECONDS = 300


async def try_claim_run_lease(
    db: AsyncSession,
    run_id: UUID,
    *,
    ttl_seconds: int = LEASE_TTL_SECONDS,
    token: Optional[str] = None,
) -> Optional[str]:
    """Atomically claim a RUNNING run for execution; return the lease token.

    Single compare-and-swap UPDATE: the claim only applies when the run is
    RUNNING and nobody holds a live lease (lease_token IS NULL, or the expiry
    has passed). Zero rows updated means someone else already owns the run
    and the caller must not execute it. ``token`` may be supplied for tests
    to force two distinguishable claim attempts.
    """
    lease_token = token or uuid4().hex
    now = datetime.utcnow()
    result = await db.execute(
        update(WorkflowRun)
        .where(
            WorkflowRun.id == run_id,
            WorkflowRun.status == RunStatus.RUNNING,
            or_(
                WorkflowRun.lease_token.is_(None),
                WorkflowRun.lease_expires_at < now,
            ),
        )
        .values(
            lease_token=lease_token,
            lease_issued_at=now,
            lease_expires_at=now + timedelta(seconds=ttl_seconds),
        )
        .execution_options(synchronize_session=False)
    )
    await db.commit()
    if result.rowcount == 0:
        return None
    return lease_token


async def extend_run_lease(
    db: AsyncSession,
    run_id: UUID,
    token: str,
    *,
    ttl_seconds: int = LEASE_TTL_SECONDS,
) -> bool:
    """Re-extend the lease iff the caller still owns it (heartbeat)."""
    now = datetime.utcnow()
    result = await db.execute(
        update(WorkflowRun)
        .where(
            WorkflowRun.id == run_id,
            WorkflowRun.lease_token == token,
            WorkflowRun.status == RunStatus.RUNNING,
        )
        .values(lease_expires_at=now + timedelta(seconds=ttl_seconds))
        .execution_options(synchronize_session=False)
    )
    await db.commit()
    return result.rowcount > 0


async def sweep_stale_running_runs(db: AsyncSession) -> int:
    """Startup crash recovery: fail RUNNING runs nobody holds a lease for.

    Called once from the app lifespan. A RUNNING run left behind by a process
    kill has either no lease at all (crashed before/without claiming) or an
    expired one (crashed mid-execution after the heartbeat lapsed, or a second
    process exited while still executing). Both are indistinguishable from
    "the execution will never come back", so they are marked FAILED durably
    and the account's active-run slot frees up.

    Runs whose lease is still fresh/live are deliberately left RUNNING: with a
    single process that cannot happen (the sweep only sees stale or never-held
    leases), and with multiple processes it protects a run owned by another
    live worker. PAUSED runs are never swept — the approval flow is durable
    (state snapshots persist) and resume is an explicit API call.
    """
    now = datetime.utcnow()
    result = await db.execute(
        update(WorkflowRun)
        .where(
            WorkflowRun.status == RunStatus.RUNNING,
            or_(
                WorkflowRun.lease_token.is_(None),
                WorkflowRun.lease_expires_at < now,
            ),
        )
        .values(
            status=RunStatus.FAILED,
            error_message="interrupted by server restart (no live lease)",
            completed_at=now,
        )
        .execution_options(synchronize_session=False)
    )
    await db.commit()
    return result.rowcount


class WorkflowExecutor:
    """Executes compiled workflows and persists execution state."""

    def __init__(self):
        self.compiler = WorkflowCompiler()

    async def _resolve_rule_logics(
        self, db: AsyncSession, workflow: WorkflowDefinition
    ) -> tuple[Dict[str, Dict[str, Any]], Dict[str, UUID]]:
        """Load the active latest rule version for every rule_id a step references.

        Returns the logic keyed by rule_id plus the resolved RuleVersion IDs, so
        NodeExecution rows can record exactly which version was evaluated.
        """
        rule_logics: Dict[str, Dict[str, Any]] = {}
        rule_version_ids: Dict[str, UUID] = {}
        for step in sorted(workflow.steps, key=lambda s: s.order_index):
            rule_id = (step.config_json or {}).get("rule_id")
            if not rule_id or rule_id in rule_logics:
                continue

            result = await db.execute(select(Rule).where(Rule.name == rule_id))
            rule = result.scalar_one_or_none()
            if not rule:
                raise ValueError(
                    f"Rule '{rule_id}' referenced by step '{step.name}' not found in the rule library"
                )

            version_result = await db.execute(
                select(RuleVersion)
                .where(RuleVersion.rule_id == rule.id, RuleVersion.is_active == True)  # noqa: E712
                .order_by(RuleVersion.version_number.desc())
            )
            version = version_result.scalars().first()
            if not version:
                raise ValueError(f"Rule '{rule_id}' exists but has no active version")

            rule_logics[rule_id] = version.logic_json
            rule_version_ids[rule_id] = version.id
        return rule_logics, rule_version_ids

    async def _audit(
        self,
        db: AsyncSession,
        *,
        entity_type: str,
        entity_id: UUID,
        action: str,
        actor_id: Optional[UUID],
        details: Optional[Dict[str, Any]] = None,
        actor_role: Optional[str] = None,
    ) -> None:
        """Write one audit row in a savepoint.

        An audit failure must never roll back or mask the run outcome: it is
        logged and the savepoint is discarded so the surrounding transaction
        (run + node executions) still commits.
        """
        try:
            async with db.begin_nested():
                db.add(
                    AuditLog(
                        id=uuid4(),
                        entity_type=entity_type,
                        entity_id=entity_id,
                        action=action,
                        actor_id=actor_id,
                        actor_role=actor_role,
                        details=details or {},
                    )
                )
        except Exception:
            logger.exception(
                "Audit write failed (entity_type=%s entity_id=%s action=%s)",
                entity_type,
                entity_id,
                action,
            )

    async def _audit_run(
        self,
        db: AsyncSession,
        run: WorkflowRun,
        action: str,
        details: Optional[Dict[str, Any]] = None,
        actor_id: Optional[UUID] = None,
        actor_role: Optional[str] = None,
    ) -> None:
        payload = {"run_id": str(run.id), "workflow_id": str(run.workflow_id)}
        payload.update(details or {})
        await self._audit(
            db,
            entity_type="run",
            entity_id=run.id,
            action=action,
            actor_id=actor_id or run.triggered_by,
            actor_role=actor_role,
            details=payload,
        )

    async def _audit_node(
        self,
        db: AsyncSession,
        run: WorkflowRun,
        node_exec: NodeExecution,
        step: WorkflowStep,
        action: str,
        details: Optional[Dict[str, Any]] = None,
        actor_id: Optional[UUID] = None,
    ) -> None:
        payload = {
            "run_id": str(run.id),
            "node_execution_id": str(node_exec.id),
            "step_id": str(step.id),
            "step_name": step.name,
        }
        payload.update(details or {})
        await self._audit(
            db,
            entity_type="node",
            entity_id=node_exec.id,
            action=action,
            actor_id=actor_id or run.triggered_by,
            details=payload,
        )

    async def _discard_cancelled_run(
        self, db: AsyncSession, run_pk: UUID
    ) -> Optional[WorkflowRun]:
        """Roll back the current execution transaction for a cancelled run.

        A concurrent ``POST /runs/{id}/cancel`` flips the run row to CANCELLED
        without touching the lease. The terminal status writes before each
        commit are guarded by a CAS on ``status = RUNNING``; when that CAS
        matches zero rows the caller must NOT commit a terminal status over
        the cancellation, so the pending node executions / audit rows are
        dropped here and the CANCELLED row is returned.
        """
        await db.rollback()
        logger.warning(
            "Run %s was cancelled mid-execution; dropping execution results", run_pk
        )
        return await db.get(WorkflowRun, run_pk)

    async def _terminal_cas_claim(
        self,
        db: AsyncSession,
        run_pk: UUID,
        *,
        status: RunStatus,
    ) -> bool:
        """Atomically claim the RUNNING -> ``status`` terminal transition.

        CAS on the committed row state: the UPDATE only matches while the run
        is still RUNNING, i.e. no concurrent cancel (which itself changed the
        status in a separate transaction) has reached the row. Zero rows means
        the caller must drop its results instead of committing.
        """
        guard = await db.execute(
            update(WorkflowRun)
            .where(
                WorkflowRun.id == run_pk,
                WorkflowRun.status == RunStatus.RUNNING,
            )
            .values(status=status)
            .execution_options(synchronize_session=False)
        )
        return guard.rowcount > 0

    @staticmethod
    def _coerce_uuid(value: Any) -> Optional[UUID]:
        if isinstance(value, UUID):
            return value
        if value is None:
            return None
        try:
            return UUID(str(value))
        except (ValueError, TypeError, AttributeError):
            return None

    @staticmethod
    def _step_output(step: WorkflowStep, state: WorkflowState) -> Any:
        if step.step_type == StepType.AI_ANALYSIS:
            return state.ai_outputs.get(step.name)
        if step.step_type == StepType.DOMAIN_RULE:
            return state.rule_outputs.get(step.name)
        if step.step_type == StepType.HUMAN_APPROVAL:
            return state.human_approvals.get(step.name)
        if step.step_type == StepType.CONDITIONAL:
            return state.conditional_outputs.get(step.name)
        return None

    def _build_node_execution(
        self,
        step: WorkflowStep,
        state: WorkflowState,
        run: WorkflowRun,
        input_data: Dict[str, Any],
        rule_version_ids: Dict[str, UUID],
        status: Optional[NodeStatus] = None,
    ) -> NodeExecution:
        """Build the audit row for one step, including lineage fields.

        Steps that were never visited (because a conditional routed around
        them) are recorded as SKIPPED instead of COMPLETED with stale output.
        """
        executed = step.name in (state.executed_steps or [])
        if status is None:
            status = NodeStatus.COMPLETED if executed else NodeStatus.SKIPPED

        config = step.config_json or {}
        prompt_template_id = None
        prompt_snapshot = None
        model_name = None
        retrieved_context = None
        rule_version_id = None
        rule_evaluation_result = None

        if status != NodeStatus.SKIPPED:
            if step.step_type == StepType.AI_ANALYSIS:
                prompt_template_id = self._coerce_uuid(config.get("prompt_template_id"))
                prompt_snapshot = state.ai_prompts.get(step.name)
                model_name = state.ai_models.get(step.name)
                retrieved_context = state.retrieved_contexts.get(step.name)
            elif step.step_type == StepType.DOMAIN_RULE:
                rule_version_id = rule_version_ids.get(config.get("rule_id"))
                rule_evaluation_result = state.rule_outputs.get(step.name)

        now = datetime.utcnow()
        return NodeExecution(
            id=uuid4(),
            run_id=run.id,
            step_id=step.id,
            node_type=step.step_type.value,
            status=status,
            input_snapshot=input_data,
            output_snapshot=self._step_output(step, state)
            if status == NodeStatus.COMPLETED
            else None,
            started_at=now,
            completed_at=now if status != NodeStatus.PENDING else None,
            retrieved_context=retrieved_context,
            prompt_template_id=prompt_template_id,
            prompt_snapshot=prompt_snapshot,
            model_name=model_name,
            rule_version_id=rule_version_id,
            rule_evaluation_result=rule_evaluation_result,
        )

    async def _persist_step(
        self,
        db: AsyncSession,
        run: WorkflowRun,
        step: WorkflowStep,
        state: WorkflowState,
        input_data: Dict[str, Any],
        rule_version_ids: Dict[str, UUID],
        status: Optional[NodeStatus] = None,
    ) -> NodeExecution:
        """Persist one step's NodeExecution row and its node audit transitions."""
        node_exec = self._build_node_execution(
            step, state, run, input_data, rule_version_ids, status=status
        )
        db.add(node_exec)

        if node_exec.status == NodeStatus.SKIPPED:
            await self._audit_node(db, run, node_exec, step, "skipped")
        elif node_exec.status == NodeStatus.PENDING:
            await self._audit_node(db, run, node_exec, step, "awaiting_approval")
        else:
            await self._audit_node(db, run, node_exec, step, "started")
            await self._audit_node(db, run, node_exec, step, "completed")
        return node_exec

    async def execute(
        self,
        workflow_id: UUID,
        input_data: Dict[str, Any],
        account_id: str,
        db: AsyncSession,
        run_id: UUID = None,
        triggered_by: UUID = None,
        human_verdicts: Optional[Dict[str, Dict[str, Any]]] = None
    ) -> WorkflowRun:
        """
        Execute a workflow and persist all results.

        Args:
            workflow_id: ID of workflow to execute
            input_data: Input data for the workflow
            account_id: Account this execution is for
            db: Database session
            run_id: Optional existing run ID (created by the API)
            triggered_by: Authenticated user initiating the run
                (falls back to the workflow's creator)
            human_verdicts: Optional pre-supplied verdicts for human approval steps
                (used during deterministic replay)

        Returns:
            WorkflowRun with execution results
        """
        # Load workflow with steps
        result = await db.execute(
            select(WorkflowDefinition)
            .where(WorkflowDefinition.id == workflow_id)
            .options(selectinload(WorkflowDefinition.steps))
        )
        workflow = result.scalar_one_or_none()
        
        if not workflow:
            raise ValueError(f"Workflow {workflow_id} not found")
        
        # Create or load run
        if run_id is None:
            run = WorkflowRun(
                id=uuid4(),
                workflow_id=workflow_id,
                account_id=account_id,
                status=RunStatus.RUNNING,
                current_step_index=0,
                triggered_by=triggered_by or workflow.created_by,
                started_at=datetime.utcnow()
            )
            db.add(run)
            await db.flush()
        else:
            result = await db.execute(
                select(WorkflowRun).where(WorkflowRun.id == run_id)
            )
            run = result.scalar_one()
        run.input_data = input_data  # Persist inputs so `replay()` can re-execute
        run_pk = run.id  # Plain-Python PK snapshot, safe to use after any session state change
        await self._audit_run(db, run, "started", details={"account_id": account_id})
        # Durability checkpoint: the run row, its inputs, and the "started"
        # audit commit before execution so a crash mid-execution still leaves a
        # discoverable RUNNING row (reaped by the startup lease sweep) instead
        # of vanishing entirely.
        await db.commit()

        try:
            # Resolve rule logic from the rule library, then compile to a graph.
            # Inside the try: resolution/compile failures must mark the run FAILED
            # like every other execution failure.
            rule_logics, rule_version_ids = await self._resolve_rule_logics(db, workflow)
            graph = self.compiler.compile(workflow, rule_logics, human_verdicts=human_verdicts)

            # Initialize state
            initial_state = WorkflowState(
                input_data=input_data,
                context={"account_id": account_id, "run_id": str(run.id)}
            )

            # Execute graph asynchronously
            final_state = await graph.ainvoke(initial_state)
            # Rehydrate into the pydantic WorkflowState: ainvoke returns LangGraph's
            # dict-like state wrapper, not typed attribute access.
            final_state = WorkflowState(**final_state)
            
            sorted_steps = sorted(workflow.steps, key=lambda s: s.order_index)

            # Save node executions (one per step; skipped branches become SKIPPED)
            for step in sorted_steps:
                await self._persist_step(
                    db, run, step, final_state, input_data, rule_version_ids
                )
            
            # Update run status
            run.status = RunStatus.COMPLETED
            run.completed_at = datetime.utcnow()
            run.output_data = final_state.current_output
            run.state_snapshot = final_state.model_dump()
            # Terminal CAS FIRST: status-affecting ORM mutations are deferred
            # until the claim is won, so no mid-transaction flush (e.g. from
            # the audit savepoints) can commit a terminal status over a
            # concurrent cancel.
            if not await self._terminal_cas_claim(db, run_pk, status=RunStatus.COMPLETED):
                return await self._discard_cancelled_run(db, run_pk)

            run.status = RunStatus.COMPLETED
            run.completed_at = datetime.utcnow()
            run.output_data = final_state.current_output
            run.state_snapshot = final_state.model_dump()
            run.current_step_index = sorted_steps[-1].order_index if sorted_steps else 0
            await self._audit_run(db, run, "completed")

            await db.commit()
            await db.refresh(run)
            
            return run

        except WorkflowPaused as pause_exc:
            paused_step = pause_exc.step
            pause_state = pause_exc.state

            sorted_steps = sorted(workflow.steps, key=lambda s: s.order_index)
            for step in sorted_steps:
                if step.order_index < paused_step.order_index:
                    await self._persist_step(
                        db, run, step, pause_state, input_data, rule_version_ids
                    )
                elif step.order_index == paused_step.order_index:
                    await self._persist_step(
                        db,
                        run,
                        step,
                        pause_state,
                        input_data,
                        rule_version_ids,
                        status=NodeStatus.PENDING,
                    )
                    break

            # Sink CAS from the resume() claim: the PAUSED transition only
            # commits if the run is still RUNNING (a cancel that landed
            # mid-execution wins and the paused snapshot is dropped).
            if not await self._terminal_cas_claim(db, run_pk, status=RunStatus.PAUSED):
                return await self._discard_cancelled_run(db, run_pk)

            run.status = RunStatus.PAUSED
            run.current_step_index = paused_step.order_index
            run.state_snapshot = pause_state.model_dump()
            run.output_data = pause_state.current_output
            await self._audit_run(
                db,
                run,
                "paused",
                details={"step_id": str(paused_step.id), "step_name": paused_step.name},
            )

            await db.commit()
            await db.refresh(run)
            return run

        except Exception as e:
            # Mark run as failed. These failures are Python-level (bad input,
            # missing rule, node error) so the session is still usable; if the
            # session itself is poisoned by a DB-level error, re-attach by the
            # snapshotted PK and retry once.
            failed_step_name = getattr(e, "workflow_step", None)
            failed_step = next(
                (s for s in workflow.steps if s.name == failed_step_name), None
            )
            if failed_step is not None:
                await self._audit(
                    db,
                    entity_type="node",
                    entity_id=failed_step.id,
                    action="failed",
                    actor_id=run.triggered_by,
                    details={
                        "run_id": str(run.id),
                        "step_id": str(failed_step.id),
                        "step_name": failed_step.name,
                        "error_type": type(e).__name__,
                    },
                )
            await self._audit_run(
                db,
                run,
                "failed",
                details={"error_type": type(e).__name__},
            )
            # Terminal CAS: if the run was cancelled mid-flight, the
            # cancellation wins and the failure is dropped (no status-affecting
            # ORM mutation happened before this check, so no flush could have
            # already overwritten a cancellation). Re-raise so the caller's own
            # handling sees the hard failure.
            if not await self._terminal_cas_claim(db, run_pk, status=RunStatus.FAILED):
                await self._discard_cancelled_run(db, run_pk)
                raise
            run.status = RunStatus.FAILED
            run.error_message = str(e)
            run.completed_at = datetime.utcnow()
            try:
                await db.commit()
            except Exception:
                await db.rollback()
                run = await db.get(WorkflowRun, run_pk)
                if run is not None:
                    run.status = RunStatus.FAILED
                    run.error_message = str(e)
                    run.completed_at = datetime.utcnow()
                    await db.commit()
            raise

    @staticmethod
    def _actor_role(user: Optional[User]) -> Optional[str]:
        """Formatted role string for audit rows (comma-joined, lowercase).

        Mirrors how ``require_roles`` compares role names. ``None`` when the
        actor's roles are unknown so the audit row simply omits the claim.
        """
        if user is None:
            return None
        names = sorted({
            link.role.name.strip().lower()
            for link in user.user_roles or []
            if link is not None and link.role is not None and link.role.name
        })
        return ",".join(names) or None

    async def resume(
        self,
        run_id: UUID,
        verdict: Dict[str, Any],
        db: AsyncSession,
        user: Optional[User] = None
    ) -> WorkflowRun:
        """
        Resume execution from the paused approval node using the stored snapshot,
        then continue all remaining steps to terminal status.

        Args:
            run_id: ID of the paused run
            verdict: Human verdict dict (e.g. {"approved": True, "approver": ...})
            db: Database session
            user: Authenticated user approving the run; the "resumed"/"approved"
                audit rows carry this actor instead of run.triggered_by

        Returns:
            WorkflowRun with execution results
        """
        # Atomic claim via compare-and-swap: only a caller that moves the run
        # PAUSED -> RUNNING wins. Losers get rowcount 0 and fail closed.
        claim = await db.execute(
            update(WorkflowRun)
            .where(WorkflowRun.id == run_id, WorkflowRun.status == RunStatus.PAUSED)
            .values(status=RunStatus.RUNNING)
        )
        result = await db.execute(
            select(WorkflowRun)
            .where(WorkflowRun.id == run_id)
            .execution_options(populate_existing=True)  # fresh attributes even for a stale identity map
        )
        run = result.scalar_one_or_none()
        if claim.rowcount == 0 or not run:
            if not run:
                raise ValueError(f"Run {run_id} not found")
            raise ValueError(f"Run {run_id} is not in PAUSED status (current: {run.status})")
        # Sync the ORM identity map with the CAS write so later explicit status
        # transitions (COMPLETED / PAUSED / FAILED) always flush.
        await db.refresh(run)

        run_pk = run.id

        # The real approver, not the run triggerer, is the actor of the
        # resume-side audit rows (mirrors how reject() takes the rejecting user).
        actor_id = getattr(user, "id", None) or run.triggered_by
        actor_role = self._actor_role(user)

        result = await db.execute(
            select(WorkflowDefinition)
            .where(WorkflowDefinition.id == run.workflow_id)
            .options(selectinload(WorkflowDefinition.steps))
        )
        workflow = result.scalar_one_or_none()
        if not workflow:
            raise ValueError(f"Workflow {run.workflow_id} not found")

        sorted_steps = sorted(workflow.steps, key=lambda s: s.order_index)
        paused_step = next((s for s in sorted_steps if s.order_index == run.current_step_index), None)
        if not paused_step:
            raise ValueError(f"Paused step with index {run.current_step_index} not found")

        # Find existing pending NodeExecution
        result = await db.execute(
            select(NodeExecution).where(
                NodeExecution.run_id == run.id,
                NodeExecution.step_id == paused_step.id,
                NodeExecution.status == NodeStatus.PENDING
            )
        )
        pending_node_exec = result.scalar_one_or_none()

        try:
            await self._audit_run(
                db,
                run,
                "resumed",
                actor_id=actor_id,
                actor_role=actor_role,
            )
            rule_logics, rule_version_ids = await self._resolve_rule_logics(db, workflow)
            human_verdicts = {paused_step.name: verdict}
            graph = self.compiler.compile(
                workflow,
                rule_logics=rule_logics,
                start_step_index=paused_step.order_index,
                human_verdicts=human_verdicts
            )

            # Rebuild state from snapshot
            snapshot = run.state_snapshot or {}
            state = WorkflowState(**snapshot)

            run.status = RunStatus.RUNNING
            final_state = await graph.ainvoke(state)
            final_state = WorkflowState(**final_state)

            steps_resumed = [s for s in sorted_steps if s.order_index >= paused_step.order_index]
            for step in steps_resumed:
                if step.order_index == paused_step.order_index:
                    if pending_node_exec:
                        pending_node_exec.status = NodeStatus.COMPLETED
                        pending_node_exec.output_snapshot = self._step_output(step, final_state)
                        pending_node_exec.completed_at = datetime.utcnow()
                        await self._audit_node(
                            db,
                            run,
                            pending_node_exec,
                            step,
                            "approved",
                            actor_id=actor_id,
                        )
                    else:
                        await self._persist_step(
                            db, run, step, final_state, run.input_data, rule_version_ids
                        )
                else:
                    await self._persist_step(
                        db, run, step, final_state, run.input_data, rule_version_ids
                    )

            run.status = RunStatus.COMPLETED
            run.completed_at = datetime.utcnow()
            run.output_data = final_state.current_output
            run.state_snapshot = final_state.model_dump()
            run.current_step_index = sorted_steps[-1].order_index if sorted_steps else paused_step.order_index
            await self._audit_run(
                db,
                run,
                "approved",
                actor_id=actor_id,
                actor_role=actor_role,
                details={
                    "step_id": str(paused_step.id),
                    "step_name": paused_step.name,
                    "approved": bool(verdict.get("approved", True)),
                },
            )
            await self._audit_run(db, run, "completed")

            await db.commit()
            await db.refresh(run)
            return run

        except WorkflowPaused as pause_exc:
            new_paused_step = pause_exc.step
            new_pause_state = pause_exc.state

            if pending_node_exec:
                pending_node_exec.status = NodeStatus.COMPLETED
                pending_node_exec.output_snapshot = verdict
                pending_node_exec.completed_at = datetime.utcnow()
                await self._audit_node(
                    db,
                    run,
                    pending_node_exec,
                    paused_step,
                    "approved",
                    actor_id=actor_id,
                )

            for step in sorted_steps:
                if paused_step.order_index < step.order_index < new_paused_step.order_index:
                    await self._persist_step(
                        db, run, step, new_pause_state, run.input_data, rule_version_ids
                    )
                elif step.order_index == new_paused_step.order_index:
                    await self._persist_step(
                        db,
                        run,
                        step,
                        new_pause_state,
                        run.input_data,
                        rule_version_ids,
                        status=NodeStatus.PENDING,
                    )
                    break

            run.status = RunStatus.PAUSED
            run.current_step_index = new_paused_step.order_index
            run.state_snapshot = new_pause_state.model_dump()
            run.output_data = new_pause_state.current_output
            await self._audit_run(
                db,
                run,
                "paused",
                details={
                    "step_id": str(new_paused_step.id),
                    "step_name": new_paused_step.name,
                },
            )

            await db.commit()
            await db.refresh(run)
            return run

        except Exception as e:
            run.status = RunStatus.FAILED
            run.error_message = str(e)
            run.completed_at = datetime.utcnow()
            failed_step_name = getattr(e, "workflow_step", None)
            failed_step = next(
                (s for s in workflow.steps if s.name == failed_step_name), None
            )
            if failed_step is not None:
                await self._audit(
                    db,
                    entity_type="node",
                    entity_id=failed_step.id,
                    action="failed",
                    actor_id=run.triggered_by,
                    details={
                        "run_id": str(run.id),
                        "step_id": str(failed_step.id),
                        "step_name": failed_step.name,
                        "error_type": type(e).__name__,
                    },
                )
            await self._audit_run(
                db,
                run,
                "failed",
                details={"error_type": type(e).__name__},
            )
            try:
                await db.commit()
            except Exception:
                await db.rollback()
                run = await db.get(WorkflowRun, run_pk)
                if run is not None:
                    run.status = RunStatus.FAILED
                    run.error_message = str(e)
                    run.completed_at = datetime.utcnow()
                    await db.commit()
            raise

    async def reject(
        self,
        run_id: UUID,
        reason: str,
        db: AsyncSession,
        user: Optional[User] = None
    ) -> WorkflowRun:
        """
        Reject a paused workflow run, marking it FAILED with a rejection reason.

        Args:
            run_id: ID of the paused run
            reason: Reason for rejection
            db: Database session
            user: Authenticated user rejecting the run

        Returns:
            WorkflowRun with status FAILED
        """
        # Atomic claim via compare-and-swap: PAUSED -> FAILED happens iff the
        # run is still PAUSED, so concurrent approve/reject cannot double-fire.
        claim = await db.execute(
            update(WorkflowRun)
            .where(WorkflowRun.id == run_id, WorkflowRun.status == RunStatus.PAUSED)
            .values(status=RunStatus.FAILED)
        )
        result = await db.execute(
            select(WorkflowRun)
            .where(WorkflowRun.id == run_id)
            .execution_options(populate_existing=True)  # fresh attributes even for a stale identity map
        )
        run = result.scalar_one_or_none()
        if claim.rowcount == 0 or not run:
            if not run:
                raise ValueError(f"Run {run_id} not found")
            raise ValueError(f"Run {run_id} is not in PAUSED status (current: {run.status})")
        # Sync the ORM identity map with the CAS write.
        await db.refresh(run)

        run_pk = run.id
        try:
            result = await db.execute(
                select(NodeExecution).where(
                    NodeExecution.run_id == run.id,
                    NodeExecution.status == NodeStatus.PENDING
                )
            )
            pending_node = result.scalar_one_or_none()
            approver = getattr(user, "email", str(getattr(user, "id", "human"))) if user else "human"
            actor_id = getattr(user, "id", None) or run.triggered_by
            if pending_node:
                pending_node.status = NodeStatus.FAILED
                pending_node.error_message = reason
                pending_node.output_snapshot = {
                    "approved": False,
                    "approver": approver,
                    "reason": reason,
                    "notes": reason,
                    "timestamp": datetime.utcnow().isoformat()
                }
                pending_node.completed_at = datetime.utcnow()

                step_result = await db.execute(
                    select(WorkflowStep).where(WorkflowStep.id == pending_node.step_id)
                )
                rejected_step = step_result.scalar_one_or_none()
                if rejected_step is not None:
                    await self._audit_node(
                        db,
                        run,
                        pending_node,
                        rejected_step,
                        "failed",
                        details={"error_type": "Rejected", "reason": reason[:500]},
                        actor_id=actor_id,
                    )

            run.status = RunStatus.FAILED
            run.error_message = reason
            run.completed_at = datetime.utcnow()
            snapshot = dict(run.state_snapshot or {})
            snapshot["rejection_reason"] = reason
            run.state_snapshot = snapshot
            await self._audit_run(
                db,
                run,
                "rejected",
                actor_id=actor_id,
                details={
                    "node_execution_id": str(pending_node.id) if pending_node else None,
                    "reason": reason[:500],
                },
            )

            await db.commit()
            await db.refresh(run)
            return run
        except Exception as e:
            run.status = RunStatus.FAILED
            run.error_message = str(e)
            run.completed_at = datetime.utcnow()
            await self._audit_run(
                db,
                run,
                "failed",
                details={"error_type": type(e).__name__},
            )
            try:
                await db.commit()
            except Exception:
                await db.rollback()
                run = await db.get(WorkflowRun, run_pk)
                if run is not None:
                    run.status = RunStatus.FAILED
                    run.error_message = str(e)
                    run.completed_at = datetime.utcnow()
                    await db.commit()
            raise

    async def replay(
        self,
        original_run_id: UUID,
        db: AsyncSession,
        triggered_by: UUID = None
    ) -> WorkflowRun:
        """
        Replay a previous workflow execution for verification.

        Args:
            original_run_id: ID of the run to replay
            db: Database session
            triggered_by: Optional authenticated user initiating the replay

        Returns:
            New WorkflowRun with replay results
        """
        # Load original run
        result = await db.execute(
            select(WorkflowRun)
            .options(selectinload(WorkflowRun.node_executions))
            .where(WorkflowRun.id == original_run_id)
        )
        original_run = result.scalar_one_or_none()

        if not original_run:
            raise ValueError(f"Run {original_run_id} not found")

        # Collect human approval verdicts from original run
        human_verdicts = {}
        if original_run.state_snapshot and isinstance(original_run.state_snapshot, dict):
            human_verdicts = dict(original_run.state_snapshot.get("human_approvals", {}))

        if not human_verdicts and original_run.node_executions:
            for ne in original_run.node_executions:
                if ne.node_type == StepType.HUMAN_APPROVAL.value and ne.output_snapshot:
                    step_res = await db.execute(
                        select(WorkflowStep).where(WorkflowStep.id == ne.step_id)
                    )
                    step = step_res.scalar_one_or_none()
                    if step:
                        human_verdicts[step.name] = ne.output_snapshot

        # Execute with same input and recorded human verdicts
        replay_run = await self.execute(
            workflow_id=original_run.workflow_id,
            input_data=original_run.input_data,
            account_id=original_run.account_id,
            db=db,
            triggered_by=triggered_by,
            human_verdicts=human_verdicts
        )
        
        # Mark as replay
        replay_run.replayed_from_run_id = original_run_id
        await db.commit()
        
        return replay_run
