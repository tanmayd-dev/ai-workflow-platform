# Persist and expose the final decision output (workflow_runs.output_data)

"""add workflow_runs.output_data (final decision output for terminal runs)

Revision ID: a3d9f2c4e8b1
Revises: f1b3c7d9e2a4
Create Date: 2026-10-28

"""
from typing import Sequence, Union
from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'a3d9f2c4e8b1'
down_revision: Union[str, None] = 'f1b3c7d9e2a4'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column('workflow_runs', sa.Column('output_data', sa.JSON(), nullable=True))


def downgrade() -> None:
    op.drop_column('workflow_runs', 'output_data')
