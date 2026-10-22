"""increase_bracket_slot_length

Revision ID: c5b77a0458d9
Revises: a9f3d3111452
Create Date: 2026-05-21 04:30:00.000000

"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "c5b77a0458d9"
down_revision: Union[str, Sequence[str], None] = "a9f3d3111452"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    with op.batch_alter_table("matches") as batch_op:
        batch_op.alter_column(
            "bracket_slot",
            type_=sa.String(10),
            existing_type=sa.String(1),
            nullable=True,
        )


def downgrade() -> None:
    with op.batch_alter_table("matches") as batch_op:
        batch_op.alter_column(
            "bracket_slot",
            type_=sa.String(1),
            existing_type=sa.String(10),
            nullable=True,
        )
