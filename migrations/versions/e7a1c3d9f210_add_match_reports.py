"""add_match_reports

Revision ID: e7a1c3d9f210
Revises: d1e4f7a92b30
Create Date: 2026-10-27 00:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = "e7a1c3d9f210"
down_revision: Union[str, Sequence[str], None] = "d1e4f7a92b30"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    # Startup `create_all` may already have created the table.
    if sa.inspect(op.get_bind()).has_table("match_reports"):
        return
    op.create_table(
        "match_reports",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("match_id", sa.Integer(), nullable=False),
        sa.Column("reporter_discord_id", sa.BigInteger(), nullable=False),
        sa.Column("reporter_player_id", sa.Integer(), nullable=True),
        sa.Column("p1_goals", sa.Integer(), nullable=False),
        sa.Column("p2_goals", sa.Integer(), nullable=False),
        sa.Column("winner_side", sa.Integer(), nullable=False),
        sa.Column("status", sa.String(length=9), nullable=False),
        sa.Column("reason", sa.Text(), nullable=True),
        sa.Column("channel_id", sa.BigInteger(), nullable=True),
        sa.Column("message_id", sa.BigInteger(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("resolved_at", sa.DateTime(), nullable=True),
        sa.Column("resolved_by", sa.BigInteger(), nullable=True),
        sa.ForeignKeyConstraint(["match_id"], ["matches.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["reporter_player_id"], ["players.id"]),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_match_reports_match_id", "match_reports", ["match_id"])
    op.create_index("ix_match_reports_status", "match_reports", ["status"])


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_index("ix_match_reports_status", table_name="match_reports")
    op.drop_index("ix_match_reports_match_id", table_name="match_reports")
    op.drop_table("match_reports")
