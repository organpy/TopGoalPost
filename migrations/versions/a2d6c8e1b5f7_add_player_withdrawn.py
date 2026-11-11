"""add player_seasons.withdrawn

Revision ID: a2d6c8e1b5f7
Revises: b8e2f4a6c913
Create Date: 2026-11-11
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "a2d6c8e1b5f7"
down_revision: Union[str, Sequence[str], None] = "b8e2f4a6c913"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def _columns() -> set[str]:
    return {c["name"] for c in sa.inspect(op.get_bind()).get_columns("player_seasons")}


def upgrade() -> None:
    # Startup schema sync may already have added it.
    if "withdrawn" not in _columns():
        with op.batch_alter_table("player_seasons") as batch:
            batch.add_column(
                sa.Column("withdrawn", sa.Boolean(), nullable=True, server_default="0")
            )


def downgrade() -> None:
    with op.batch_alter_table("player_seasons") as batch:
        batch.drop_column("withdrawn")
