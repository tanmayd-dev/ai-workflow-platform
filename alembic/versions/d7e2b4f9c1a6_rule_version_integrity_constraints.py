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
    # Pre-migration cleanup: older builds had no constraints, so raced writes
    # may have left duplicate (rule_id, version_number) rows and multiple
    # active versions per rule. Both would make the new constraints fail.
    bind = op.get_bind()
    # 1) drop duplicate (rule_id, version_number) rows, keeping one per key...
    bind.execute(sa.text("""
        DELETE FROM rule_versions
        WHERE ctid NOT IN (
            SELECT MIN(ctid) FROM rule_versions
            GROUP BY rule_id, version_number
        )
    """))
    # 2) keep only the highest-numbered active version per rule
    bind.execute(sa.text("""
        UPDATE rule_versions
        SET is_active = FALSE
        WHERE is_active
          AND ctid NOT IN (
              SELECT keep.ctid FROM rule_versions AS keep
              WHERE keep.rule_id = rule_versions.rule_id AND keep.is_active
              ORDER BY keep.version_number DESC
              LIMIT 1
          )
    """))

    op.create_unique_constraint(
        'uq_rule_version_number', 'rule_versions', ['rule_id', 'version_number']
    )
    op.create_index(
        'uq_rule_active_version',
        'rule_versions',
        ['rule_id'],
        unique=True,
        postgresql_where=sa.text('is_active'),
        sqlite_where=sa.text('is_active'),
    )


def downgrade() -> None:
    op.drop_index('uq_rule_active_version', table_name='rule_versions')
    op.drop_constraint('uq_rule_version_number', 'rule_versions', type_='unique')
