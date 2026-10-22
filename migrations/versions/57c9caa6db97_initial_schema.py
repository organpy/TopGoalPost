"""initial_schema

Revision ID: 57c9caa6db97
Revises:
Create Date: 2026-05-21 02:18:28.228112

"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "57c9caa6db97"
down_revision: Union[str, Sequence[str], None] = None
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "players",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("discord_id", sa.Integer(), nullable=False),
        sa.Column("name", sa.String(length=255), nullable=False),
        sa.Column("country", sa.String(length=2), nullable=True),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("discord_id"),
    )
    op.create_index("ix_players_discord_id", "players", ["discord_id"])

    op.create_table(
        "seasons",
        sa.Column("number", sa.Integer(), nullable=False),
        sa.Column("status", sa.String(length=8), nullable=False),
        sa.Column("champion_id", sa.Integer(), nullable=True),
        sa.Column("started_at", sa.DateTime(), nullable=False),
        sa.Column("ended_at", sa.DateTime(), nullable=True),
        sa.PrimaryKeyConstraint("number"),
        sa.ForeignKeyConstraint(
            ["champion_id"], ["players.id"], name="fk_seasons_champion"
        ),
    )

    op.create_table(
        "settings",
        sa.Column("key", sa.String(length=255), nullable=False),
        sa.Column("value", sa.Text(), nullable=False),
        sa.PrimaryKeyConstraint("key"),
    )

    op.create_table(
        "timezones",
        sa.Column("discord_id", sa.Integer(), nullable=False),
        sa.Column("timezone", sa.String(length=255), nullable=False),
        sa.PrimaryKeyConstraint("discord_id"),
    )

    op.create_table(
        "images",
        sa.Column("hash", sa.String(length=255), nullable=False),
        sa.Column("link", sa.Text(), nullable=False),
        sa.PrimaryKeyConstraint("hash"),
    )

    op.create_table(
        "timeslots",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("discord_id", sa.Integer(), nullable=False),
        sa.Column("start", sa.Integer(), nullable=False),
        sa.Column("finish", sa.Integer(), nullable=False),
        sa.Column("prio", sa.Integer(), nullable=False),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_timeslots_discord_id", "timeslots", ["discord_id"])

    op.create_table(
        "player_seasons",
        sa.Column("player_id", sa.Integer(), nullable=False),
        sa.Column("season_number", sa.Integer(), nullable=False),
        sa.Column("open_wins", sa.Integer(), server_default="0", nullable=False),
        sa.Column("open_losses", sa.Integer(), server_default="0", nullable=False),
        sa.Column("open_gd", sa.Integer(), server_default="0", nullable=False),
        sa.Column("swiss_seed", sa.Integer(), nullable=True),
        sa.Column("pool", sa.String(length=255), nullable=True),
        sa.Column("playin_eliminated", sa.Boolean(), server_default="0", nullable=False),
        sa.Column("playin_wildcard", sa.Boolean(), server_default="0", nullable=False),
        sa.Column("playin_rank", sa.Integer(), nullable=True),
        sa.Column("hp", sa.Integer(), server_default="4", nullable=False),
        sa.PrimaryKeyConstraint("player_id", "season_number"),
        sa.ForeignKeyConstraint(
            ["player_id"], ["players.id"], name="fk_ps_player"
        ),
        sa.ForeignKeyConstraint(
            ["season_number"], ["seasons.number"], name="fk_ps_season"
        ),
    )
    op.create_index("ix_ps_season_open", "player_seasons", ["season_number", "open_wins"])
    op.create_index("ix_ps_season_pool", "player_seasons", ["season_number", "pool"])

    op.create_table(
        "events",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("name", sa.String(length=255), nullable=False),
        sa.Column("type", sa.String(length=6), nullable=False),
        sa.Column("season", sa.Integer(), nullable=False),
        sa.Column("status", sa.String(length=9), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("idx_event_season_type", "events", ["season", "type"])

    op.create_table(
        "matches",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("event_id", sa.Integer(), nullable=False),
        sa.Column("p1_prereq_id", sa.Integer(), nullable=True),
        sa.Column("p2_prereq_id", sa.Integer(), nullable=True),
        sa.Column("p1_prereq_type", sa.String(length=10), server_default="winner", nullable=False),
        sa.Column("p2_prereq_type", sa.String(length=10), server_default="winner", nullable=False),
        sa.Column("bracket_slot", sa.String(length=1), nullable=True),
        sa.Column("series_game", sa.Integer(), server_default="1", nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("completed_at", sa.DateTime(), nullable=True),
        sa.PrimaryKeyConstraint("id"),
        sa.ForeignKeyConstraint(
            ["event_id"], ["events.id"], name="fk_match_event"
        ),
        sa.ForeignKeyConstraint(
            ["p1_prereq_id"], ["matches.id"], name="fk_match_p1_prereq"
        ),
        sa.ForeignKeyConstraint(
            ["p2_prereq_id"], ["matches.id"], name="fk_match_p2_prereq"
        ),
    )
    op.create_index("idx_match_event", "matches", ["event_id"])
    op.create_index("idx_match_completed", "matches", ["completed_at"])

    op.create_table(
        "match_players",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("match_id", sa.Integer(), nullable=False),
        sa.Column("player_id", sa.Integer(), nullable=True),
        sa.Column("goals", sa.Integer(), server_default="0", nullable=False),
        sa.Column("is_winner", sa.Boolean(), server_default="0", nullable=False),
        sa.Column("side", sa.Integer(), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.PrimaryKeyConstraint("id"),
        sa.ForeignKeyConstraint(
            ["match_id"], ["matches.id"], name="fk_mp_match"
        ),
        sa.ForeignKeyConstraint(
            ["player_id"], ["players.id"], name="fk_mp_player"
        ),
        sa.UniqueConstraint("match_id", "side", name="uq_match_side"),
    )
    op.create_index("idx_matchplayer_match", "match_players", ["match_id"])
    op.create_index("idx_matchplayer_player", "match_players", ["player_id"])


def downgrade() -> None:
    op.drop_table("match_players")
    op.drop_table("matches")
    op.drop_table("events")
    op.drop_table("player_seasons")
    op.drop_table("timeslots")
    op.drop_table("images")
    op.drop_table("timezones")
    op.drop_table("settings")
    op.drop_table("seasons")
    op.drop_table("players")
