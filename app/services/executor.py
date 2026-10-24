"""
Workflow Executor Service
Executes compiled workflows and manages execution state.
"""
import asyncio
from uuid import UUID, uuid4
from datetime import datetime
from typing import Dict, Any, Optional
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, update
from sqlalchemy.orm import selectinload

from app.models.rule import Rule, RuleVersion
from app.models.workflow import WorkflowDefinition, WorkflowStep, StepType
from app.models.execution import WorkflowRun, NodeExecution, RunStatus, NodeStatus
from app.models.user import User
from app.services.compiler import WorkflowCompiler, WorkflowState, WorkflowPaused


class WorkflowExecutor:
    """Executes compiled workflows and persists execution state."""

    def __init__(self):
        self.compiler = WorkflowCompiler()

    async def _resolve_rule_logics(self, db: AsyncSession, workflow: WorkflowDefinition) -> Dict[str, Dict[str, Any]]:
        """Load the active latest rule version for every rule_id a step references."""
        rule_logics: Dict[str, Dict[str, Any]] = {}
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
        return rule_logics

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

        try:
            # Resolve rule logic from the rule library, then compile to a graph.
            # Inside the try: resolution/compile failures must mark the run FAILED
            # like every other execution failure.
            rule_logics = await self._resolve_rule_logics(db, workflow)
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

            # Save node executions (one per step, snapshots keyed by step name)
            for i, step in enumerate(sorted_steps):
                step_type = step.step_type
                if step_type == StepType.AI_ANALYSIS:
                    output_snapshot = final_state.ai_outputs.get(step.name)
                elif step_type == StepType.DOMAIN_RULE:
                    output_snapshot = final_state.rule_outputs.get(step.name)
                elif step_type == StepType.HUMAN_APPROVAL:
                    output_snapshot = final_state.human_approvals.get(step.name)
                else:
                    output_snapshot = final_state.context.get("condition_result")

                node_exec = NodeExecution(
                    id=uuid4(),
                    run_id=run.id,
                    step_id=step.id,
                    node_type=step.step_type.value,
                    status=NodeStatus.COMPLETED,
                    input_snapshot=input_data,
                    output_snapshot=output_snapshot,
                    started_at=datetime.utcnow(),
                    completed_at=datetime.utcnow(),
                    retrieved_context=final_state.context.get("retrieved_context"),
                    prompt_snapshot=final_state.context.get("last_prompt"),
                    model_name=(final_state.ai_outputs.get(step.name) or {}).get("model_name")
                        if step.step_type == StepType.AI_ANALYSIS else None,
                    rule_evaluation_result=final_state.rule_outputs.get(step.name)
                        if step.step_type == StepType.DOMAIN_RULE else None,
                )
                db.add(node_exec)
            
            # Update run status
            run.status = RunStatus.COMPLETED
            run.completed_at = datetime.utcnow()
            run.output_data = final_state.current_output
            run.state_snapshot = final_state.model_dump()
            run.current_step_index = sorted_steps[-1].order_index if sorted_steps else 0
            
            await db.commit()
            await db.refresh(run)
            
            return run

        except WorkflowPaused as pause_exc:
            paused_step = pause_exc.step
            pause_state = pause_exc.state

            sorted_steps = sorted(workflow.steps, key=lambda s: s.order_index)
            for step in sorted_steps:
                if step.order_index < paused_step.order_index:
                    step_type = step.step_type
                    if step_type == StepType.AI_ANALYSIS:
                        output_snapshot = pause_state.ai_outputs.get(step.name)
                    elif step_type == StepType.DOMAIN_RULE:
                        output_snapshot = pause_state.rule_outputs.get(step.name)
                    elif step_type == StepType.HUMAN_APPROVAL:
                        output_snapshot = pause_state.human_approvals.get(step.name)
                    else:
                        output_snapshot = pause_state.context.get("condition_result")

                    node_exec = NodeExecution(
                        id=uuid4(),
                        run_id=run.id,
                        step_id=step.id,
                        node_type=step.step_type.value,
                        status=NodeStatus.COMPLETED,
                        input_snapshot=input_data,
                        output_snapshot=output_snapshot,
                        started_at=datetime.utcnow(),
                        completed_at=datetime.utcnow(),
                        retrieved_context=pause_state.context.get("retrieved_context"),
                        prompt_snapshot=pause_state.context.get("last_prompt"),
                        model_name=(pause_state.ai_outputs.get(step.name) or {}).get("model_name")
                            if step.step_type == StepType.AI_ANALYSIS else None,
                        rule_evaluation_result=pause_state.rule_outputs.get(step.name)
                            if step.step_type == StepType.DOMAIN_RULE else None,
                    )
                    db.add(node_exec)
                elif step.order_index == paused_step.order_index:
                    node_exec = NodeExecution(
                        id=uuid4(),
                        run_id=run.id,
                        step_id=step.id,
                        node_type=step.step_type.value,
                        status=NodeStatus.PENDING,
                        input_snapshot=input_data,
                        output_snapshot=None,
                        started_at=datetime.utcnow(),
                        completed_at=None,
                    )
                    db.add(node_exec)
                    break

            run.status = RunStatus.PAUSED
            run.current_step_index = paused_step.order_index
            run.state_snapshot = pause_state.model_dump()
            run.output_data = pause_state.current_output

            await db.commit()
            await db.refresh(run)
            return run

        except Exception as e:
            # Mark run as failed. These failures are Python-level (bad input,
            # missing rule, node error) so the session is still usable; if the
            # session itself is poisoned by a DB-level error, re-attach by the
            # snapshotted PK and retry once.
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

    async def resume(
        self,
        run_id: UUID,
        verdict: Dict[str, Any],
        db: AsyncSession
    ) -> WorkflowRun:
        """
        Resume execution from the paused approval node using the stored snapshot,
        then continue all remaining steps to terminal status.

        Args:
            run_id: ID of the paused run
            verdict: Human verdict dict (e.g. {"approved": True, "approver": ...})
            db: Database session

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
            rule_logics = await self._resolve_rule_logics(db, workflow)
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
                step_type = step.step_type
                if step_type == StepType.AI_ANALYSIS:
                    output_snapshot = final_state.ai_outputs.get(step.name)
                elif step_type == StepType.DOMAIN_RULE:
                    output_snapshot = final_state.rule_outputs.get(step.name)
                elif step_type == StepType.HUMAN_APPROVAL:
                    output_snapshot = final_state.human_approvals.get(step.name)
                else:
                    output_snapshot = final_state.context.get("condition_result")

                if step.order_index == paused_step.order_index:
                    if pending_node_exec:
                        pending_node_exec.status = NodeStatus.COMPLETED
                        pending_node_exec.output_snapshot = output_snapshot
                        pending_node_exec.completed_at = datetime.utcnow()
                    else:
                        node_exec = NodeExecution(
                            id=uuid4(),
                            run_id=run.id,
                            step_id=step.id,
                            node_type=step.step_type.value,
                            status=NodeStatus.COMPLETED,
                            input_snapshot=run.input_data,
                            output_snapshot=output_snapshot,
                            started_at=datetime.utcnow(),
                            completed_at=datetime.utcnow(),
                        )
                        db.add(node_exec)
                else:
                    node_exec = NodeExecution(
                        id=uuid4(),
                        run_id=run.id,
                        step_id=step.id,
                        node_type=step.step_type.value,
                        status=NodeStatus.COMPLETED,
                        input_snapshot=run.input_data,
                        output_snapshot=output_snapshot,
                        started_at=datetime.utcnow(),
                        completed_at=datetime.utcnow(),
                        retrieved_context=final_state.context.get("retrieved_context"),
                        prompt_snapshot=final_state.context.get("last_prompt"),
                        model_name=(final_state.ai_outputs.get(step.name) or {}).get("model_name")
                            if step.step_type == StepType.AI_ANALYSIS else None,
                        rule_evaluation_result=final_state.rule_outputs.get(step.name)
                            if step.step_type == StepType.DOMAIN_RULE else None,
                    )
                    db.add(node_exec)

            run.status = RunStatus.COMPLETED
            run.completed_at = datetime.utcnow()
            run.output_data = final_state.current_output
            run.state_snapshot = final_state.model_dump()
            run.current_step_index = sorted_steps[-1].order_index if sorted_steps else paused_step.order_index

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

            for step in sorted_steps:
                if paused_step.order_index < step.order_index < new_paused_step.order_index:
                    step_type = step.step_type
                    if step_type == StepType.AI_ANALYSIS:
                        output_snapshot = new_pause_state.ai_outputs.get(step.name)
                    elif step_type == StepType.DOMAIN_RULE:
                        output_snapshot = new_pause_state.rule_outputs.get(step.name)
                    elif step_type == StepType.HUMAN_APPROVAL:
                        output_snapshot = new_pause_state.human_approvals.get(step.name)
                    else:
                        output_snapshot = new_pause_state.context.get("condition_result")

                    node_exec = NodeExecution(
                        id=uuid4(),
                        run_id=run.id,
                        step_id=step.id,
                        node_type=step.step_type.value,
                        status=NodeStatus.COMPLETED,
                        input_snapshot=run.input_data,
                        output_snapshot=output_snapshot,
                        started_at=datetime.utcnow(),
                        completed_at=datetime.utcnow(),
                    )
                    db.add(node_exec)
                elif step.order_index == new_paused_step.order_index:
                    node_exec = NodeExecution(
                        id=uuid4(),
                        run_id=run.id,
                        step_id=step.id,
                        node_type=step.step_type.value,
                        status=NodeStatus.PENDING,
                        input_snapshot=run.input_data,
                        output_snapshot=None,
                        started_at=datetime.utcnow(),
                        completed_at=None,
                    )
                    db.add(node_exec)
                    break

            run.status = RunStatus.PAUSED
            run.current_step_index = new_paused_step.order_index
            run.state_snapshot = new_pause_state.model_dump()
            run.output_data = new_pause_state.current_output

            await db.commit()
            await db.refresh(run)
            return run

        except Exception as e:
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

            run.status = RunStatus.FAILED
            run.error_message = reason
            run.completed_at = datetime.utcnow()
            snapshot = dict(run.state_snapshot or {})
            snapshot["rejection_reason"] = reason
            run.state_snapshot = snapshot

            await db.commit()
            await db.refresh(run)
            return run
        except Exception as e:
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
