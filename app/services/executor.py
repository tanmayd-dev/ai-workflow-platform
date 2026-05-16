"""
Workflow Executor Service
Executes compiled workflows and manages execution state.
"""
import asyncio
from uuid import UUID, uuid4
from datetime import datetime
from typing import Dict, Any
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select
from sqlalchemy.orm import selectinload

from app.models.workflow import WorkflowDefinition
from app.models.execution import WorkflowRun, NodeExecution, RunStatus, NodeStatus
from app.services.compiler import WorkflowCompiler, WorkflowState


class WorkflowExecutor:
    """Executes compiled workflows and persists execution state."""
    
    def __init__(self):
        self.compiler = WorkflowCompiler()
    
    async def execute(
        self,
        workflow_id: UUID,
        input_data: Dict[str, Any],
        account_id: str,
        db: AsyncSession,
        run_id: UUID = None
    ) -> WorkflowRun:
        """
        Execute a workflow and persist all results.
        
        Args:
            workflow_id: ID of workflow to execute
            input_data: Input data for the workflow
            account_id: Account this execution is for
            db: Database session
            run_id: Optional existing run ID (for replay)
            
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
                triggered_by=uuid4(),  # TODO: Get from auth context
                started_at=datetime.utcnow()
            )
            db.add(run)
            await db.flush()
        else:
            result = await db.execute(
                select(WorkflowRun).where(WorkflowRun.id == run_id)
            )
            run = result.scalar_one()
        
        # Compile workflow to graph
        graph = self.compiler.compile(workflow)
        
        # Initialize state
        initial_state = WorkflowState(
            input_data=input_data,
            context={"account_id": account_id, "run_id": str(run.id)}
        )
        
        try:
            # Execute graph asynchronously
            final_state = await graph.ainvoke(initial_state)
            
            # Save node executions
            for i, step in enumerate(sorted(workflow.steps, key=lambda s: s.order_index)):
                node_exec = NodeExecution(
                    id=uuid4(),
                    run_id=run.id,
                    step_id=step.id,
                    node_type=step.step_type.value,
                    status=NodeStatus.COMPLETED,
                    input_snapshot=input_data,
                    output_snapshot=final_state.current_output,
                    started_at=datetime.utcnow(),
                    completed_at=datetime.utcnow(),
                    retrieved_context=final_state.context.get("retrieved_context"),
                    prompt_snapshot=final_state.context.get("last_prompt"),
                    rule_result=final_state.rule_outputs.get(step.name),
                    ai_response=final_state.ai_outputs.get(step.name)
                )
                db.add(node_exec)
            
            # Update run status
            run.status = RunStatus.COMPLETED
            run.completed_at = datetime.utcnow()
            run.output_data = final_state.current_output
            
            await db.commit()
            await db.refresh(run)
            
            return run
            
        except Exception as e:
            # Mark run as failed
            run.status = RunStatus.FAILED
            run.error_message = str(e)
            run.completed_at = datetime.utcnow()
            
            await db.commit()
            raise
    
    async def replay(
        self,
        original_run_id: UUID,
        db: AsyncSession
    ) -> WorkflowRun:
        """
        Replay a previous workflow execution for verification.
        
        Args:
            original_run_id: ID of the run to replay
            db: Database session
            
        Returns:
            New WorkflowRun with replay results
        """
        # Load original run
        result = await db.execute(
            select(WorkflowRun).where(WorkflowRun.id == original_run_id)
        )
        original_run = result.scalar_one_or_none()
        
        if not original_run:
            raise ValueError(f"Run {original_run_id} not found")
        
        # Execute with same input
        replay_run = await self.execute(
            workflow_id=original_run.workflow_id,
            input_data=original_run.input_data,
            account_id=original_run.account_id,
            db=db
        )
        
        # Mark as replay
        replay_run.replayed_from_run_id = original_run_id
        await db.commit()
        
        return replay_run
