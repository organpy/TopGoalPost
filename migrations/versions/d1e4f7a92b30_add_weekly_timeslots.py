"""add_weekly_timeslots

Revision ID: d1e4f7a92b30
Revises: c5b77a0458d9
Create Date: 2026-10-21 00:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = "d1e4f7a92b30"
down_revision: Union[str, Sequence[str], None] = "c5b77a0458d9"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    op.create_table(
        "weekly_timeslots",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("discord_id", sa.Integer(), nullable=False),
        sa.Column("weekday", sa.Integer(), nullable=False),
        sa.Column("start_minute", sa.Integer(), nullable=False),
        sa.Column("finish_minute", sa.Integer(), nullable=False),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_weekly_timeslots_discord_id",
        "weekly_timeslots",
        ["discord_id"],
    )


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_index("ix_weekly_timeslots_discord_id", table_name="weekly_timeslots")
    op.drop_table("weekly_timeslots")
