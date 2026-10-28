"""Workflow execution endpoints."""
import asyncio
import logging
from datetime import datetime
from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, Query
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, and_
from sqlalchemy.orm import selectinload
from typing import Any, Dict, List, Optional
from uuid import uuid4, UUID

from app.core.database import get_db, get_session_maker
from app.core.rbac import require_roles
from app.api.v1.endpoints.auth import get_current_active_user
from app.models.user import User
from app.models.workflow import WorkflowDefinition, WorkflowStatus
from app.models.execution import WorkflowRun, NodeExecution, RunStatus
from app.schemas import (
    RunCreate,
    RunResponse,
    NodeExecutionResponse,
    RunApprovalRequest,
    RunRejectRequest,
)
from app.services.executor import (
    LEASE_TTL_SECONDS,
    WorkflowExecutor,
    extend_run_lease,
    try_claim_run_lease,
)

router = APIRouter()

logger = logging.getLogger(__name__)


def _is_terminal(status: RunStatus) -> bool:
    """A run in one of these states will not progress without an API call."""
    return status in (RunStatus.COMPLETED, RunStatus.FAILED, RunStatus.PAUSED)


async def _execute_run_in_background(
    *,
    run_id: UUID,
    workflow_id: UUID,
    input_data: Dict[str, Any],
    account_id: str,
    triggered_by: UUID,
) -> None:
    """
    Execute a queued run in a fresh, task-owned database session.

    The request-scoped ``AsyncSession`` is bound to the HTTP request and may be
    closed (or torn down) once the response has been sent, so it must never be
    captured here. All values passed across the boundary are plain data.

    Before executing, the task durably claims the run via a lease CAS (single
    UPDATE matching a NULL/expired lease). Zero rows claimed means another
    worker owns the run already, so this task aborts silently. While the run
    executes, a lightweight heartbeat task keeps re-extending the lease so a
    startup sweep cannot reap a live run; the run typically finishes in-process
    long before the first heartbeat fires.

    The executor persists terminal state itself (COMPLETED / FAILED / PAUSED)
    before re-raising; the fallback below only repairs a row that would
    otherwise be stuck in RUNNING forever (e.g. failure before the executor's
    own error handling).
    """
    session_maker = get_session_maker()
    # The async context manager always closes the session, even on failure.
    async with session_maker() as db:
        lease_token = await try_claim_run_lease(db, run_id)
        if lease_token is None:
            logger.info(
                "Background execution skipped for run %s: lease not claimable "
                "(already owned, restarted, or no longer RUNNING)",
                run_id,
            )
            return

        stop_heartbeat = asyncio.Event()

        async def _heartbeat() -> None:
            """Keep the lease alive while the executor works."""
            interval = LEASE_TTL_SECONDS / 3
            while True:
                try:
                    # Returns as soon as the executor finishes; TimeoutError
                    # means the interval elapsed and the lease needs extending.
                    await asyncio.wait_for(stop_heartbeat.wait(), timeout=interval)
                except asyncio.TimeoutError:
                    pass
                else:
                    return
                # Heartbeat uses its own short-lived session so it can commit
                # while the executor's transaction is still open.
                async with session_maker() as hb_db:
                    try:
                        await extend_run_lease(hb_db, run_id, lease_token)
                    except Exception:
                        logger.exception("Lease heartbeat failed for run %s", run_id)

        heartbeat_task = asyncio.create_task(_heartbeat())
        try:
            await WorkflowExecutor().execute(
                workflow_id=workflow_id,
                input_data=input_data,
                account_id=account_id,
                db=db,
                run_id=run_id,
                triggered_by=triggered_by,
            )
        except Exception as exc:
            logger.exception("Background execution failed for run %s", run_id)
            try:
                await db.rollback()
                run = await db.get(WorkflowRun, run_id)
                if run is not None and not _is_terminal(run.status):
                    run.status = RunStatus.FAILED
                    run.error_message = str(exc) or "Background run failed"
                    run.completed_at = datetime.utcnow()
                    await db.commit()
                    logger.error(
                        "Run %s was not marked FAILED by the executor; repaired the row",
                        run_id,
                    )
                elif run is None:
                    logger.error("Run %s disappeared while executing in the background", run_id)
            except Exception:
                logger.exception("Could not persist FAILED state for run %s", run_id)
        finally:
            stop_heartbeat.set()
            try:
                await heartbeat_task
            except Exception:
                logger.exception("Lease heartbeat crashed for run %s", run_id)


@router.post("/", response_model=RunResponse, status_code=202)
async def create_run(
    run_data: RunCreate,
    background_tasks: BackgroundTasks,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(require_roles("executor"))
):
    """
    Queue a workflow execution run and return immediately (202 Accepted).

    Requires the executor role (admin bypasses).

    The run row is committed with status RUNNING before this response, so the
    client can poll ``GET /runs/{run_id}`` until the status reaches a terminal
    value: COMPLETED, FAILED, or PAUSED (waiting for human approval).

    Execution happens after the response is sent, in a FastAPI background task
    that opens its own database session; the request-scoped session is never
    reused. Approve / reject / replay remain synchronous because they resume a
    single already-prepared run and their results are needed to answer the
    request.

    Constraints:
    - Workflow must be APPROVED
    - Only one active run per account (a second request returns 409)
    """
    # Verify workflow exists and is approved
    result = await db.execute(
        select(WorkflowDefinition).where(WorkflowDefinition.id == run_data.workflow_id)
    )
    workflow = result.scalar_one_or_none()

    if not workflow:
        raise HTTPException(status_code=404, detail="Workflow not found")

    if workflow.status != WorkflowStatus.APPROVED:
        raise HTTPException(status_code=400, detail="Only APPROVED workflows can be executed")

    # Check for existing active run for this account. The run row below is
    # committed as RUNNING before the response, keeping this guard effective
    # even though execution itself is deferred.
    existing_run_result = await db.execute(
        select(WorkflowRun).where(
            and_(
                WorkflowRun.account_id == run_data.account_id,
                WorkflowRun.status.in_([RunStatus.RUNNING, RunStatus.PAUSED])
            )
        )
    )
    existing_run = existing_run_result.scalar_one_or_none()

    if existing_run:
        raise HTTPException(
            status_code=409,
            detail=f"Account {run_data.account_id} already has an active run: {existing_run.id}"
        )

    run = WorkflowRun(
        id=uuid4(),
        workflow_id=run_data.workflow_id,
        account_id=run_data.account_id,
        status=RunStatus.RUNNING,
        current_step_index=0,
        triggered_by=current_user.id,
        trigger_reason=run_data.trigger_reason,
        # Persist inputs up front (not just inside the executor) so the row is
        # complete and replayable even if the process dies before the task runs.
        input_data=run_data.input_data,
    )

    db.add(run)
    await db.commit()
    await db.refresh(run)

    # Defer execution until after the 202 response is sent. Only plain values
    # cross the boundary: the background task opens its own session.
    background_tasks.add_task(
        _execute_run_in_background,
        run_id=run.id,
        workflow_id=run_data.workflow_id,
        input_data=run_data.input_data,
        account_id=run_data.account_id,
        triggered_by=current_user.id,
    )

    return run


@router.get("", response_model=List[RunResponse])
async def list_runs(
    status: Optional[str] = None,
    workflow_id: Optional[UUID] = None,
    account_id: Optional[str] = None,
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0),
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_active_user)
):
    """
    List runs newest-first with optional filters (any authenticated user).

    This is how an approver discovers the runs waiting on them. Filters:
    - ``status``: comma-separated (e.g. ``PAUSED`` or ``RUNNING,PAUSED``)
    - ``workflow_id``: runs of one workflow definition
    - ``account_id``: runs for one account

    ``limit`` caps the page (default 50, max 200) and ``offset`` pages through
    the newest-first ordering (started_at desc, id desc as tiebreak).
    """
    query = select(WorkflowRun)

    if status:
        parsed_statuses = []
        for token in status.split(","):
            token = token.strip()
            if not token:
                continue
            try:
                parsed_statuses.append(RunStatus[token.upper()])
            except KeyError:
                raise HTTPException(
                    status_code=400,
                    detail=f"Unknown run status: {token}",
                )
        if parsed_statuses:
            query = query.where(WorkflowRun.status.in_(parsed_statuses))

    if workflow_id:
        query = query.where(WorkflowRun.workflow_id == workflow_id)
    if account_id:
        query = query.where(WorkflowRun.account_id == account_id)

    query = query.order_by(
        WorkflowRun.started_at.desc(), WorkflowRun.id.desc()
    ).limit(limit).offset(offset)

    result = await db.execute(query)
    return result.scalars().all()


@router.get("/{run_id}", response_model=RunResponse)
async def get_run(
    run_id: UUID,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_active_user)
):
    """Get run details (any authenticated user)."""
    result = await db.execute(
        select(WorkflowRun).where(WorkflowRun.id == run_id)
    )
    run = result.scalar_one_or_none()

    if not run:
        raise HTTPException(status_code=404, detail="Run not found")

    return run


@router.post("/{run_id}/approve", response_model=RunResponse)
async def approve_run(
    run_id: UUID,
    approval_data: Optional[RunApprovalRequest] = None,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(require_roles("approver"))
):
    """
    Approve a paused workflow run and resume execution from the approval node.

    Requires the approver role (admin bypasses).

    This endpoint stays synchronous: the caller needs the resumed run's new
    status (COMPLETED or the next PAUSED step) in the response.

    Constraints:
    - Run must exist (404)
    - Run must be in PAUSED status (400)
    """
    result = await db.execute(
        select(WorkflowRun).where(WorkflowRun.id == run_id)
    )
    run = result.scalar_one_or_none()

    if not run:
        raise HTTPException(status_code=404, detail="Run not found")

    if run.status != RunStatus.PAUSED:
        raise HTTPException(
            status_code=400,
            detail=f"Run {run_id} is not in PAUSED status (current: {run.status})"
        )

    notes = approval_data.notes if approval_data and approval_data.notes else "Approved by human reviewer"
    approver = getattr(current_user, "email", str(current_user.id))
    verdict = {
        "approved": True,
        "approver": approver,
        "notes": notes,
        "timestamp": datetime.utcnow().isoformat()
    }

    executor = WorkflowExecutor()
    try:
        run = await executor.resume(
            run_id=run_id,
            verdict=verdict,
            db=db,
            user=current_user
        )
    except ValueError as ve:
        # Claim lost in a race (or raced past the pre-check): this request's
        # verdict did not apply — say so instead of pretending success.
        await db.refresh(run)
        raise HTTPException(status_code=409, detail=str(ve))
    except Exception:
        # The executor marks the run FAILED in its own error handling; the
        # client gets a hard failure instead of a 200 wearing a FAILED body.
        logger.exception("Resume execution failed for run %s; raising 500", run_id)
        raise HTTPException(
            status_code=500,
            detail=f"Executor failed while resuming run {run_id}; run is marked FAILED"
        )

    return run


@router.post("/{run_id}/reject", response_model=RunResponse)
async def reject_run(
    run_id: UUID,
    reject_data: Optional[RunRejectRequest] = None,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(require_roles("approver"))
):
    """
    Reject a paused workflow run, marking it FAILED with a rejection reason.

    Requires the approver role (admin bypasses).

    Synchronous by design: this only transitions an existing paused run, so
    the verdict outcome is returned in the response.

    Constraints:
    - Run must exist (404)
    - Run must be in PAUSED status (400)
    """
    result = await db.execute(
        select(WorkflowRun).where(WorkflowRun.id == run_id)
    )
    run = result.scalar_one_or_none()

    if not run:
        raise HTTPException(status_code=404, detail="Run not found")

    if run.status != RunStatus.PAUSED:
        raise HTTPException(
            status_code=400,
            detail=f"Run {run_id} is not in PAUSED status (current: {run.status})"
        )

    reason = reject_data.get_reason() if reject_data else "Rejected by user"

    executor = WorkflowExecutor()
    try:
        run = await executor.reject(
            run_id=run_id,
            reason=reason,
            db=db,
            user=current_user
        )
    except ValueError as ve:
        # Claim lost in a race (or raced past the pre-check): this request's
        # rejection did not apply — say so instead of pretending success.
        await db.refresh(run)
        raise HTTPException(status_code=409, detail=str(ve))
    except Exception:
        # The run row carries the outcome; a mid-flight executor failure lands
        # here and the client gets a hard failure, not a 200 with a FAILED body.
        logger.exception("Rejection execution failed for run %s; raising 500", run_id)
        raise HTTPException(
            status_code=500,
            detail=f"Executor failed while rejecting run {run_id}; run is marked FAILED"
        )

    return run


@router.get("/{run_id}/executions", response_model=List[NodeExecutionResponse])
async def get_run_executions(
    run_id: UUID,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_active_user)
):
    """Get all node executions for a run (audit trail; any authenticated user)."""
    result = await db.execute(
        select(NodeExecution)
        .where(NodeExecution.run_id == run_id)
        .order_by(NodeExecution.started_at)
    )
    executions = result.scalars().all()

    return executions


@router.post("/{run_id}/replay", response_model=RunResponse, status_code=201)
async def replay_run(
    run_id: UUID,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(require_roles("executor"))
):
    """
    Replay a completed run for verification.

    Requires the executor role (admin bypasses).

    Stays synchronous (like approve/reject) so the new run's outcome is
    returned directly.

    - Re-executes all nodes with the original inputs
    - Returns a new run linked via `replayed_from_run_id`
    """
    # Get original run
    result = await db.execute(
        select(WorkflowRun)
        .options(selectinload(WorkflowRun.node_executions))
        .where(WorkflowRun.id == run_id)
    )
    original_run = result.scalar_one_or_none()

    if not original_run:
        raise HTTPException(status_code=404, detail="Run not found")

    if original_run.status != RunStatus.COMPLETED:
        raise HTTPException(status_code=400, detail="Can only replay completed runs")

    executor = WorkflowExecutor()
    replayed = await executor.replay(
        original_run_id=run_id,
        db=db,
        triggered_by=current_user.id
    )
    return replayed
