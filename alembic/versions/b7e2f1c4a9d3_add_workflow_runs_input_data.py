# Add input_data column to workflow_runs

"""add workflow_runs.input_data + workflow_runs.replayed_from_run_id (replay support)

Revision ID: b7e2f1c4a9d3
Revises: a9e54758a839
Create Date: 2026-10-24

"""
from typing import Sequence, Union
from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'b7e2f1c4a9d3'
down_revision: Union[str, None] = 'a9e54758a839'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column('workflow_runs', sa.Column('input_data', sa.JSON(), nullable=True))
    op.add_column('workflow_runs', sa.Column('replayed_from_run_id', sa.UUID(), nullable=True))


def downgrade() -> None:
    op.drop_column('workflow_runs', 'replayed_from_run_id')
    op.drop_column('workflow_runs', 'input_data')
