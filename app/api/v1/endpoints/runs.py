"""Workflow execution endpoints."""
import asyncio
import logging
from datetime import datetime
from fastapi import APIRouter, BackgroundTasks, Depends, Header, HTTPException, Query, Response
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, and_, update, or_
from sqlalchemy.orm import selectinload
from typing import Any, Dict, List, Optional
from uuid import uuid4, UUID

from app.core.database import get_db, get_session_maker
from app.core.rbac import ADMIN_ROLE, require_roles
from app.api.v1.endpoints.auth import get_current_active_user
from app.models.user import User, UserRole
from app.models.workflow import WorkflowDefinition, WorkflowStatus
from app.models.execution import WorkflowRun, NodeExecution, RunStatus
from app.schemas import (
    RunCreate,
    RunResponse,
    NodeExecutionResponse,
    RunApprovalRequest,
    RunRejectRequest,
    ReplayRequest,
)
from app.services.executor import (
    LEASE_TTL_SECONDS,
    ReplayValidationError,
    WorkflowExecutor,
    extend_run_lease,
    try_claim_run_lease,
)

router = APIRouter()

logger = logging.getLogger(__name__)

# Only these statuses count as "active" against the one-active-run-per-account
# constraint and the cancel transition guard.
ACTIVE_RUN_STATUSES = [RunStatus.RUNNING, RunStatus.PAUSED]

IDEMPOTENCY_KEY_MAX_LENGTH = 200


def _is_terminal(status: RunStatus) -> bool:
    """A run in one of these states will not progress without an API call."""
    return status in (
        RunStatus.COMPLETED,
        RunStatus.FAILED,
        RunStatus.PAUSED,
        RunStatus.CANCELLED,
    )


async def _load_user_with_roles(db: AsyncSession, user: Optional[User]) -> Optional[User]:
    """Re-load the user with ``user_roles`` → ``role`` eagerly (async-safe)."""
    if user is None:
        return None
    result = await db.execute(
        select(User)
        .options(selectinload(User.user_roles).selectinload(UserRole.role))
        .where(User.id == user.id)
    )
    return result.scalar_one_or_none() or user


def _has_admin_role(user: Optional[User]) -> bool:
    if user is None:
        return False
    return any(
        (link.role is not None and link.role.name or "").strip().lower() == ADMIN_ROLE
        for link in (user.user_roles or [])
    )


async def _is_admin(db: AsyncSession, user: Optional[User]) -> bool:
    """True when the user holds the ``admin`` superuser role.

    Kept in this module (not app.core.rbac, which belongs to another change
    area) so the cancel/approve maker-checker checks can load the role links
    with a session they already own, even when the dependency didn't preload
    ``user_roles``.
    """
    return _has_admin_role(await _load_user_with_roles(db, user))


async def _deny_maker_checker(
    db: AsyncSession,
    user: Optional[User],
    maker_id: UUID,
    *,
    action: str,
    entity_label: str,
    entity_id: UUID,
) -> None:
    """409 when the acting user made the artefact they are trying to judge.

    Maker-checker (separation of duties): the user who triggered a run (or
    created a workflow) is the "maker"; approving/rejecting it is the
    "checker" and must be a second person. The ``admin`` superuser role is
    exempt: it already confers every role and exists for break-glass
    administration.
    """
    if user is None or user.id != maker_id:
        return
    if await _is_admin(db, user):
        return
    raise HTTPException(
        status_code=409,
        detail=(
            f"User {user.id} cannot {action} {entity_label} {entity_id}: they are "
            f"its maker; maker-checker requires a second, different user (admin superusers exempt)"
        ),
    )


def _replay_response(response: Response, existing_run: WorkflowRun) -> WorkflowRun:
    """Mark a 200 replay of an idempotent request on the response object."""
    response.status_code = 200
    response.headers["Idempotency-Key-Replayed"] = "true"
    return existing_run


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
    response: Response,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(require_roles("executor")),
    idempotency_key: Optional[str] = Header(None, alias="Idempotency-Key"),
):
    """
    Queue a workflow execution run and return immediately (202 Accepted).

    Requires the executor role (admin bypasses).

    The run row is committed with status RUNNING before this response, so the
    client can poll ``GET /runs/{run_id}`` until the status reaches a terminal
    value: COMPLETED, FAILED, PAUSED, or CANCELLED.

    Execution happens after the response is sent, in a FastAPI background task
    that opens its own database session; the request-scoped session is never
    reused. Approve / reject / replay remain synchronous because they resume a
    single already-prepared run and their results are needed to answer the
    request.

    Constraints:
    - Workflow must be APPROVED
    - Only one active run per account: enforced by a partial unique index
      (account_id WHERE status IN (RUNNING, PAUSED)); a duplicate request
      fails at commit and returns 409.
    - Optional ``Idempotency-Key`` header (<= 200 chars): repeated requests
      with the same key return the original run (200 + ``Idempotency-Key-
      Replayed: true``) instead of creating a duplicate decision.
    """
    if idempotency_key and len(idempotency_key) > IDEMPOTENCY_KEY_MAX_LENGTH:
        raise HTTPException(
            status_code=400,
            detail=f"Idempotency-Key must be at most {IDEMPOTENCY_KEY_MAX_LENGTH} characters",
        )

    # Verify workflow exists and is approved
    result = await db.execute(
        select(WorkflowDefinition).where(WorkflowDefinition.id == run_data.workflow_id)
    )
    workflow = result.scalar_one_or_none()

    if not workflow:
        raise HTTPException(status_code=404, detail="Workflow not found")

    if workflow.status != WorkflowStatus.APPROVED:
        raise HTTPException(status_code=400, detail="Only APPROVED workflows can be executed")

    # Idempotent replay: a run already stored under this key is returned as-is.
    if idempotency_key:
        replay_result = await db.execute(
            select(WorkflowRun)
            .where(WorkflowRun.idempotency_key == idempotency_key)
            .order_by(WorkflowRun.started_at.desc(), WorkflowRun.id.desc())
        )
        existing = replay_result.scalars().first()
        if existing:
            return _replay_response(response, existing)

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
        idempotency_key=idempotency_key,
    )

    db.add(run)
    try:
        await db.commit()
    except IntegrityError:
        # The one-active-run-per-account partial unique index (and, under true
        # concurrency, the idempotency-key index) fires at commit. Identify the
        # collateral run and give a definite answer instead of a raw 500.
        await db.rollback()

        if idempotency_key:
            replay_result = await db.execute(
                select(WorkflowRun)
                .where(WorkflowRun.idempotency_key == idempotency_key)
                .order_by(WorkflowRun.started_at.desc(), WorkflowRun.id.desc())
            )
            existing = replay_result.scalars().first()
            if existing:
                return _replay_response(response, existing)

        active_result = await db.execute(
            select(WorkflowRun)
            .where(
                and_(
                    WorkflowRun.account_id == run_data.account_id,
                    WorkflowRun.status.in_(ACTIVE_RUN_STATUSES),
                )
            )
            .order_by(WorkflowRun.started_at.desc(), WorkflowRun.id.desc())
        )
        active_run = active_result.scalars().first()
        if active_run:
            raise HTTPException(
                status_code=409,
                detail=f"Account {run_data.account_id} already has an active run: {active_run.id}",
            )
        raise HTTPException(
            status_code=409,
            detail="run already active for this account",
        )
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
    - Run triggerer cannot approve their own run (409); admin superusers exempt
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

    await _deny_maker_checker(
        db,
        current_user,
        run.triggered_by,
        action="approve",
        entity_label="run",
        entity_id=run_id,
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
    - Run triggerer cannot reject their own run (409); admin superusers exempt
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

    await _deny_maker_checker(
        db,
        current_user,
        run.triggered_by,
        action="reject",
        entity_label="run",
        entity_id=run_id,
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


@router.post("/{run_id}/cancel", response_model=RunResponse)
async def cancel_run(
    run_id: UUID,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_active_user)
):
    """
    Cancel a RUNNING or PAUSED run (409 on terminal status, 404 unknown).

    Authorization: the same user who triggered the run, or an admin
    (superuser). Other users get 403 regardless of run state.

    The status transition is a CAS UPDATE on the current status, so a double
    cancel (or a race with approve/reject on a PAUSED run) cannot double-fire.
    Cancel never mutates the lease columns: for a RUNNING run the status flip
    is what the executing task notices — its terminal write is guarded by the
    same CAS pattern (only commits COMPLETED/PAUSED/FAILED while the row is
    still RUNNING), so a cancelled run stays CANCELLED and the executor drops
    its results. For a PAUSED run, cancellation is immediate (there is no live
    lease).

    Ruling (T2.2): cancel does not require an expired lease — the WHERE clause
    matches on status only, and no lease columns are touched, so a live
    execution's lease is never yanked.
    """
    result = await db.execute(
        select(WorkflowRun).where(WorkflowRun.id == run_id)
    )
    run = result.scalar_one_or_none()

    if not run:
        raise HTTPException(status_code=404, detail="Run not found")

    # Re-load with roles eagerly once: needed both for the admin check and the
    # audit row's actor role (accessing user lazily would hit MissingGreenlet).
    role_user = await _load_user_with_roles(db, current_user)
    if run.triggered_by != current_user.id and not _has_admin_role(role_user):
        raise HTTPException(
            status_code=403,
            detail="Only the user who triggered this run (or an admin) can cancel it",
        )

    if run.status not in (RunStatus.RUNNING, RunStatus.PAUSED):
        raise HTTPException(
            status_code=409,
            detail=(
                f"Run {run_id} is not cancellable "
                f"(current: {run.status}; only RUNNING or PAUSED runs can be cancelled)"
            ),
        )

    cancellable_status = run.status
    claim = await db.execute(
        update(WorkflowRun)
        .where(WorkflowRun.id == run_id, WorkflowRun.status == cancellable_status)
        .values(
            status=RunStatus.CANCELLED,
            error_message=(
                f"Cancelled by {getattr(current_user, 'email', None) or str(current_user.id)}"
            ),
            completed_at=datetime.utcnow(),
        )
        .execution_options(synchronize_session=False)
    )
    if claim.rowcount == 0:
        # Lost the race with approve/reject/another cancel: report the fresh state.
        await db.refresh(run)
        raise HTTPException(
            status_code=409,
            detail=f"Run {run_id} is not cancellable (current: {run.status})",
        )

    # Audit trail: who cancelled (mirrors the executor's audit-row style).
    executor = WorkflowExecutor()
    await executor._audit_run(
        db,
        run,
        "cancelled",
        actor_id=current_user.id,
        actor_role=executor._actor_role(role_user),
        details={"previous_status": cancellable_status.value},
    )

    await db.commit()
    await db.refresh(run)
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
    replay_data: Optional[ReplayRequest] = None,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(require_roles("executor"))
):
    """
    Replay a completed run for verification.

    Requires the executor role (admin bypasses).

    Stays synchronous (like approve/reject) so the new run's outcome is
    returned directly.

    Optional body ``{"mode": "recorded" | "re-execute"}`` (default
    ``re-execute``, unchanged behavior):

    - ``re-execute``: re-runs all nodes with the original inputs (AI nodes
      call the LLM again; rules re-evaluate against the pinned versions)
    - ``recorded``: deterministic replay — the original run's recorded node
      outputs are reused verbatim (never calls the LLM); only allowed for
      COMPLETED source runs with an intact node-execution trail (otherwise
      409)

    In both modes the new run is linked via `replayed_from_run_id`.

    - Any other ``mode`` value fails validation (422)
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

    mode = replay_data.mode if replay_data else "re-execute"

    # Existing rule kept for the default mode; recorded mode carries its own
    # (conflict-shaped, 409) validation for non-COMPLETED sources.
    if original_run.status != RunStatus.COMPLETED and mode != "recorded":
        raise HTTPException(status_code=400, detail="Can only replay completed runs")

    executor = WorkflowExecutor()
    try:
        replayed = await executor.replay(
            original_run_id=run_id,
            db=db,
            triggered_by=current_user.id,
            mode=mode,
        )
    except ReplayValidationError as ve:
        # Recorded mode cannot replay this run deterministically (wrong
        # source status, or the recorded node outputs are incomplete).
        raise HTTPException(status_code=409, detail=str(ve))
    return replayed
