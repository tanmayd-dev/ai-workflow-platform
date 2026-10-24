# Add durable run failure/rejection reason (workflow_runs.error_message)

"""add workflow_runs.error_message (durable failure/rejection reason)

Revision ID: c3f1a9e5b7d4
Revises: b7e2f1c4a9d3
Create Date: 2026-10-24

"""
from typing import Sequence, Union
from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'c3f1a9e5b7d4'
down_revision: Union[str, None] = 'b7e2f1c4a9d3'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column('workflow_runs', sa.Column('error_message', sa.Text(), nullable=True))


def downgrade() -> None:
    op.drop_column('workflow_runs', 'error_message')
