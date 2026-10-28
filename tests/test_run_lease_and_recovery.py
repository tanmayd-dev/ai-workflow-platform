"""
Lease claim + startup crash-recovery tests (T2.1).

The background executor must durably claim a run (single CAS UPDATE on a
NULL/expired lease) before executing, and the app lifespan sweeps stale
RUNNING rows to FAILED on startup so a process kill cannot wedge an account
behind a forever-RUNNING run.
"""
from datetime import datetime, timedelta
from uuid import UUID, uuid4

import pytest

from app.core.database import get_session_maker
from app.models.execution import RunStatus, WorkflowRun
from app.services.executor import (
    extend_run_lease,
    sweep_stale_running_runs,
    try_claim_run_lease,
)

SWEEP_MESSAGE = "interrupted by server restart (no live lease)"


async def _insert_run(status: RunStatus, **overrides) -> UUID:
    """Insert a bare WorkflowRun row directly (SQLite does not enforce FKs)."""
    session_maker = get_session_maker()
    async with session_maker() as session:
        run = WorkflowRun(
            id=uuid4(),
            workflow_id=uuid4(),
            account_id=f"ACC_LEASE_{uuid4().hex[:8]}",
            status=status,
            current_step_index=0,
            triggered_by=uuid4(),
            started_at=datetime.utcnow(),
            **overrides,
        )
        session.add(run)
        await session.commit()
    return run.id


async def _reload_run(run_id: UUID) -> WorkflowRun:
    session_maker = get_session_maker()
    async with session_maker() as session:
        run = await session.get(WorkflowRun, run_id)
        assert run is not None
        session.expunge(run)
    return run


async def test_exactly_one_of_two_simultaneous_claims_wins():
    """Two fresh claim attempts with distinct tokens: the first wins, the second aborts."""
    run_id = await _insert_run(RunStatus.RUNNING)

    async with get_session_maker()() as db_a, get_session_maker()() as db_b:
        token_a = await try_claim_run_lease(db_a, run_id, token="token-a")
        token_b = await try_claim_run_lease(db_b, run_id, token="token-b")

    assert token_a == "token-a"
    assert token_b is None, "a second claim must not steal a live lease"

    run = await _reload_run(run_id)
    assert run.lease_token == "token-a"
    assert run.lease_expires_at is not None
    assert run.lease_expires_at > datetime.utcnow()


async def test_claim_requires_running_status_and_free_or_expired_lease():
    now = datetime.utcnow()

    # PAUSED is resumable via approve/reject, not claimable by executors.
    paused_id = await _insert_run(RunStatus.PAUSED)
    async with get_session_maker()() as db:
        assert await try_claim_run_lease(db, paused_id, token="t") is None

    # A run held under a live lease cannot be claimed.
    live_id = await _insert_run(
        RunStatus.RUNNING,
        lease_token="live-owner",
        lease_issued_at=now,
        lease_expires_at=now + timedelta(seconds=300),
    )
    async with get_session_maker()() as db:
        assert await try_claim_run_lease(db, live_id, token="t") is None

    # An expired lease may be taken over (crashed previous owner).
    stale_id = await _insert_run(
        RunStatus.RUNNING,
        lease_token="dead-owner",
        lease_issued_at=now - timedelta(seconds=600),
        lease_expires_at=now - timedelta(seconds=300),
    )
    async with get_session_maker()() as db:
        assert await try_claim_run_lease(db, stale_id, token="new-owner") == "new-owner"


async def test_extend_run_lease_only_succeeds_for_the_owner():
    run_id = await _insert_run(RunStatus.RUNNING)
    async with get_session_maker()() as db:
        token = await try_claim_run_lease(db, run_id, token="owner")
        assert token == "owner"
        assert await extend_run_lease(db, run_id, "owner") is True
        assert await extend_run_lease(db, run_id, "someone-else") is False

    run = await _reload_run(run_id)
    assert run.lease_token == "owner"
    # The successful extension pushed the expiry beyond the original claim TTL.
    assert run.lease_expires_at > datetime.utcnow()


async def test_sweep_fails_only_stale_running_runs():
    now = datetime.utcnow()
    stale_id = await _insert_run(RunStatus.RUNNING)  # never claimed: NULL lease
    abandoned_id = await _insert_run(
        RunStatus.RUNNING,
        lease_token="dead",
        lease_issued_at=now - timedelta(seconds=3600),
        lease_expires_at=now - timedelta(seconds=3600),
    )
    paused_id = await _insert_run(RunStatus.PAUSED)
    live_id = await _insert_run(
        RunStatus.RUNNING,
        lease_token="live",
        lease_issued_at=now,
        lease_expires_at=now + timedelta(seconds=300),
    )

    async with get_session_maker()() as db:
        swept = await sweep_stale_running_runs(db)
    assert swept == 2, "only the never-claimed and expired-lease RUNNING rows"

    stale = await _reload_run(stale_id)
    assert stale.status == RunStatus.FAILED
    assert stale.error_message == SWEEP_MESSAGE
    assert stale.completed_at is not None

    abandoned = await _reload_run(abandoned_id)
    assert abandoned.status == RunStatus.FAILED
    assert abandoned.error_message == SWEEP_MESSAGE

    # PAUSED is resumable (approval state is durable) and must not be swept.
    paused = await _reload_run(paused_id)
    assert paused.status == RunStatus.PAUSED

    # A live lease (another live worker) must not be reaped by the sweep.
    live = await _reload_run(live_id)
    assert live.status == RunStatus.RUNNING


async def test_second_sweep_is_a_noop():
    """Sweeping twice must not double-fail or resurrect anything."""
    run_id = await _insert_run(RunStatus.RUNNING)
    async with get_session_maker()() as db:
        assert await sweep_stale_running_runs(db) == 1
        assert await sweep_stale_running_runs(db) == 0

    run = await _reload_run(run_id)
    assert run.status == RunStatus.FAILED
