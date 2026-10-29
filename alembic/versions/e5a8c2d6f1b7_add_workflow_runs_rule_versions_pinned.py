# Rule-version pinning on workflow_runs

"""Add workflow_runs.rule_versions_pinned ({rule_name: version_number})

Revision ID: e5a8c2d6f1b7
Revises: d1e5b9f3a7c8
Create Date: 2026-10-29

Notes:
- Recorded by the executor at execution start (persisted durably with the
  "started" audit). resume()/replay() re-resolve exactly these versions —
  ignoring is_active — instead of the latest-active ones, so later version
  bumps cannot alter a past or resumed leg. Nullable: legacy runs started
  before pinning keep NULL and fall back to latest-active resolution.
- A plain sa.Column add; SQLite has no native JSON type, but SQLAlchemy's
  sa.JSON renders as TEXT under aiosqlite both here and through the ORM.
"""
from typing import Sequence, Union
from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'e5a8c2d6f1b7'
down_revision: Union[str, None] = 'd1e5b9f3a7c8'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        'workflow_runs',
        sa.Column('rule_versions_pinned', sa.JSON(), nullable=True),
    )


def downgrade() -> None:
    op.drop_column('workflow_runs', 'rule_versions_pinned')
