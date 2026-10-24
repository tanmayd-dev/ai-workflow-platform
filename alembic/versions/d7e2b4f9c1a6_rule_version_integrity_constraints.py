# Rule version integrity constraints

"""rule_versions uniqueness: (rule_id, version_number) unique + one ACTIVE version per rule

Revision ID: d7e2b4f9c1a6
Revises: c3f1a9e5b7d4
Create Date: 2026-10-24

"""
from typing import Sequence, Union
from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'd7e2b4f9c1a6'
down_revision: Union[str, None] = 'c3f1a9e5b7d4'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_unique_constraint(
        'uq_rule_version_number', 'rule_versions', ['rule_id', 'version_number']
    )
    op.create_index(
        'uq_rule_active_version',
        'rule_versions',
        ['rule_id'],
        unique=True,
        postgresql_where=sa.text('is_active'),
    )


def downgrade() -> None:
    op.drop_index('uq_rule_active_version', table_name='rule_versions')
    op.drop_constraint('uq_rule_version_number', 'rule_versions', type_='unique')
