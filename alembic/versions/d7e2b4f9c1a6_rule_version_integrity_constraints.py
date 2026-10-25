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
    is_sqlite = bind.dialect.name == "sqlite"
    row_ident = "rowid" if is_sqlite else "ctid"
    # 1) drop duplicate (rule_id, version_number) rows, keeping one per key...
    bind.execute(sa.text(f"""
        DELETE FROM rule_versions
        WHERE {row_ident} NOT IN (
            SELECT MIN({row_ident}) FROM rule_versions
            GROUP BY rule_id, version_number
        )
    """))
    # 2) keep only the highest-numbered active version per rule
    bind.execute(sa.text(f"""
        UPDATE rule_versions
        SET is_active = FALSE
        WHERE is_active
          AND {row_ident} NOT IN (
              SELECT keep.{row_ident} FROM rule_versions AS keep
              WHERE keep.rule_id = rule_versions.rule_id AND keep.is_active
              ORDER BY keep.version_number DESC
              LIMIT 1
          )
    """))

    with op.batch_alter_table('rule_versions') as batch_op:
        batch_op.create_unique_constraint(
            'uq_rule_version_number', ['rule_id', 'version_number']
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
    with op.batch_alter_table('rule_versions') as batch_op:
        batch_op.drop_constraint('uq_rule_version_number', type_='unique')
