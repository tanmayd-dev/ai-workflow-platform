# Add CONDITIONAL to the workflow step type enum

"""add CONDITIONAL to steptype

Revision ID: f1b3c7d9e2a4
Revises: d7e2b4f9c1a6
Create Date: 2026-10-27

PostgreSQL uses a native enum type, so a single ALTER TYPE ... ADD VALUE is
enough. SQLite has no enum type: SQLAlchemy stores it as VARCHAR plus a CHECK
constraint, and CHECK constraints can only be changed by rebuilding the table,
which is what batch mode does here.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'f1b3c7d9e2a4'
down_revision: Union[str, None] = 'd7e2b4f9c1a6'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


OLD_VALUES = ('AI_ANALYSIS', 'DOMAIN_RULE', 'HUMAN_APPROVAL')
NEW_VALUES = OLD_VALUES + ('CONDITIONAL',)


def _step_type_enum(values) -> sa.Enum:
    # create_constraint=True: on SQLite this renders the CHECK constraint that
    # must be rebuilt by the batch migration.
    return sa.Enum(*values, name='steptype', create_constraint=True)


def upgrade() -> None:
    bind = op.get_bind()
    if bind.dialect.name == 'postgresql':
        op.execute("ALTER TYPE steptype ADD VALUE IF NOT EXISTS 'CONDITIONAL'")
        return

    with op.batch_alter_table('workflow_steps', recreate='always') as batch_op:
        batch_op.alter_column(
            'step_type',
            existing_type=_step_type_enum(OLD_VALUES),
            type_=_step_type_enum(NEW_VALUES),
            existing_nullable=False,
        )


def downgrade() -> None:
    bind = op.get_bind()
    if bind.dialect.name == 'postgresql':
        # Removing an enum value means recreating the type. Refuse to guess
        # what CONDITIONAL rows should become; fail loudly instead.
        op.execute(
            """
            DO $$
            BEGIN
                IF EXISTS (
                    SELECT 1 FROM workflow_steps WHERE step_type = 'CONDITIONAL'
                ) THEN
                    RAISE EXCEPTION
                        'cannot downgrade: CONDITIONAL workflow steps still exist';
                END IF;
            END $$;
            """
        )
        op.execute("ALTER TYPE steptype RENAME TO steptype_old")
        op.execute(
            "CREATE TYPE steptype AS ENUM "
            "('AI_ANALYSIS', 'DOMAIN_RULE', 'HUMAN_APPROVAL')"
        )
        op.execute(
            "ALTER TABLE workflow_steps ALTER COLUMN step_type "
            "TYPE steptype USING step_type::text::steptype"
        )
        op.execute("DROP TYPE steptype_old")
        return

    # SQLite: rebuild the table/CHECK constraint without CONDITIONAL. If any
    # row still uses CONDITIONAL the copy fails, which is preferable to
    # silently rewriting audit data.
    with op.batch_alter_table('workflow_steps', recreate='always') as batch_op:
        batch_op.alter_column(
            'step_type',
            existing_type=_step_type_enum(NEW_VALUES),
            type_=_step_type_enum(OLD_VALUES),
            existing_nullable=False,
        )
