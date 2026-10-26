"""unique_event_per_season_type

Revision ID: b8e2f4a6c913
Revises: a4c9e1f7b302
Create Date: 2026-10-27 00:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = "b8e2f4a6c913"
down_revision: Union[str, Sequence[str], None] = "a4c9e1f7b302"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

INDEX = "uq_event_season_type"


def upgrade() -> None:
    """Upgrade schema."""
    bind = op.get_bind()
    if INDEX in {i["name"] for i in sa.inspect(bind).get_indexes("events")}:
        return
    duplicates = bind.execute(
        sa.text(
            "SELECT season, type, COUNT(*) FROM events "
            "GROUP BY season, type HAVING COUNT(*) > 1"
        )
    ).fetchall()
    if duplicates:
        listed = ", ".join(f"season {s} {t} x{n}" for s, t, n in duplicates)
        raise RuntimeError(
            f"Duplicate events found ({listed}). Back up the database, merge or "
            "delete the extra events, then rerun the migration."
        )
    op.create_index(INDEX, "events", ["season", "type"], unique=True)


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_index(INDEX, table_name="events")
