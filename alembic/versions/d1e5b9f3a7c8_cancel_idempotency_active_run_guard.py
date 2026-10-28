# Run cancellation + idempotency + DB-enforced one-active-run-per-account

"""RunStatus.CANCELLED, idempotency_key column, and partial unique indexes

Revision ID: d1e5b9f3a7c8
Revises: b8d2f6a0c4e7
Create Date: 2026-10-29

Notes:
- The runstatus CHECK constraint (SQLite renders sa.Enum as VARCHAR + CHECK)
  must be recreated to accept the new CANCELLED value, hence batch mode.
- The two partial unique indexes use plain CREATE INDEX, which SQLite supports
  with a WHERE clause directly (no table rebuild needed).
- The downgrade drops rows first only for the enum re-tightening, not the rows
  themselves: CANCELLED runs would violate the downgraded CHECK, so a live
  deployment should replay/confirm those runs before downgrading.
"""
from typing import Sequence, Union
from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'd1e5b9f3a7c8'
down_revision: Union[str, None] = 'b8d2f6a0c4e7'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

OLD_STATUS_ENUM = lambda: sa.Enum('RUNNING', 'PAUSED', 'COMPLETED', 'FAILED', name='runstatus')
NEW_STATUS_ENUM = lambda: sa.Enum('RUNNING', 'PAUSED', 'COMPLETED', 'FAILED', 'CANCELLED', name='runstatus')


def upgrade() -> None:
    # The enum definitions live inside the functions so the SQLite batch
    # recreation renders the CHECK constraint including CANCELLED precisely.
    with op.batch_alter_table('workflow_runs') as batch_op:
        batch_op.alter_column(
            'status',
            existing_type=OLD_STATUS_ENUM(),
            type_=NEW_STATUS_ENUM(),
            existing_nullable=True,
        )

    # DB-enforced replacement for the old check-then-insert active-run guard:
    # a second RUNNING/PAUSED run for one account violates this index at commit.
    op.create_index(
        'uq_workflow_runs_active_per_account',
        'workflow_runs',
        ['account_id'],
        unique=True,
        postgresql_where=sa.text("status IN ('RUNNING', 'PAUSED')"),
        sqlite_where=sa.text("status IN ('RUNNING', 'PAUSED')"),
    )

    op.add_column('workflow_runs', sa.Column('idempotency_key', sa.String(length=200), nullable=True))
    op.create_index(
        'uq_workflow_runs_idempotency_key',
        'workflow_runs',
        ['idempotency_key'],
        unique=True,
        postgresql_where=sa.text('idempotency_key IS NOT NULL'),
        sqlite_where=sa.text('idempotency_key IS NOT NULL'),
    )


def downgrade() -> None:
    op.drop_index('uq_workflow_runs_idempotency_key', table_name='workflow_runs')
    op.drop_column('workflow_runs', 'idempotency_key')
    op.drop_index('uq_workflow_runs_active_per_account', table_name='workflow_runs')
    with op.batch_alter_table('workflow_runs') as batch_op:
        batch_op.alter_column(
            'status',
            existing_type=NEW_STATUS_ENUM(),
            type_=OLD_STATUS_ENUM(),
            existing_nullable=True,
        )
