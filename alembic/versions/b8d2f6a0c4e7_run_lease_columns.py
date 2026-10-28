# Run execution lease columns (durable claim + crash recovery)

"""add workflow_runs lease columns (lease_token, lease_issued_at, lease_expires_at)

Revision ID: b8d2f6a0c4e7
Revises: a3d9f2c4e8b1
Create Date: 2026-10-29

"""
from typing import Sequence, Union
from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'b8d2f6a0c4e7'
down_revision: Union[str, None] = 'a3d9f2c4e8b1'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column('workflow_runs', sa.Column('lease_token', sa.String(), nullable=True))
    op.add_column('workflow_runs', sa.Column('lease_issued_at', sa.DateTime(), nullable=True))
    op.add_column('workflow_runs', sa.Column('lease_expires_at', sa.DateTime(), nullable=True))


def downgrade() -> None:
    op.drop_column('workflow_runs', 'lease_expires_at')
    op.drop_column('workflow_runs', 'lease_issued_at')
    op.drop_column('workflow_runs', 'lease_token')
