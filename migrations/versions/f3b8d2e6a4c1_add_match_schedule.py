"""add_match_schedule

Revision ID: f3b8d2e6a4c1
Revises: e7a1c3d9f210
Create Date: 2026-10-27 00:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = "f3b8d2e6a4c1"
down_revision: Union[str, Sequence[str], None] = "e7a1c3d9f210"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def _columns() -> set[str]:
    return {c["name"] for c in sa.inspect(op.get_bind()).get_columns("matches")}


def upgrade() -> None:
    """Upgrade schema."""
    # Startup schema sync may already have added these.
    existing = _columns()
    with op.batch_alter_table("matches") as batch:
        if "scheduled_at" not in existing:
            batch.add_column(sa.Column("scheduled_at", sa.DateTime(), nullable=True))
        if "scheduled_by" not in existing:
            batch.add_column(sa.Column("scheduled_by", sa.BigInteger(), nullable=True))


def downgrade() -> None:
    """Downgrade schema."""
    with op.batch_alter_table("matches") as batch:
        batch.drop_column("scheduled_by")
        batch.drop_column("scheduled_at")
