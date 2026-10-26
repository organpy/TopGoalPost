from datetime import datetime
from enum import Enum as PyEnum
from typing import Optional

from sqlalchemy import (
    Boolean,
    BigInteger,
    Enum,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    DateTime,
    UniqueConstraint,
    CheckConstraint,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship


class Base(DeclarativeBase):
    pass


class EventType(PyEnum):
    OPEN = "open"
    PLAYIN = "playin"
    SWISS = "swiss"
    PLAYOFF = "playoff"


class EventStatus(PyEnum):
    PENDING = "pending"
    ACTIVE = "active"
    COMPLETED = "completed"


class ReportStatus(PyEnum):
    PENDING = "pending"
    CONFIRMED = "confirmed"
    REJECTED = "rejected"
    DISPUTED = "disputed"


OPEN_REPORT_STATUSES = (ReportStatus.PENDING, ReportStatus.DISPUTED)


class SeasonStatus(PyEnum):
    ACTIVE = "active"
    ARCHIVED = "archived"


class Season(Base):
    __tablename__ = "seasons"

    number: Mapped[int] = mapped_column(primary_key=True)
    status: Mapped[SeasonStatus] = mapped_column(Enum(SeasonStatus), default=SeasonStatus.ACTIVE)
    champion_id: Mapped[Optional[int]] = mapped_column(ForeignKey("players.id"), nullable=True)
    started_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    ended_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)

    champion: Mapped[Optional["Player"]] = relationship("Player", foreign_keys=[champion_id])
    player_seasons: Mapped[list["PlayerSeason"]] = relationship(back_populates="season")

    def __repr__(self):
        return f"<Season(number={self.number}, status={self.status.value})>"


class Player(Base):
    __tablename__ = "players"

    id: Mapped[int] = mapped_column(primary_key=True)
    discord_id: Mapped[int] = mapped_column(Integer, unique=True, index=True)
    name: Mapped[str] = mapped_column(String(255))
    country: Mapped[Optional[str]] = mapped_column(String(2), nullable=True)

    match_players: Mapped[list["MatchPlayer"]] = relationship(
        back_populates="player", foreign_keys="MatchPlayer.player_id"
    )
    player_seasons: Mapped[list["PlayerSeason"]] = relationship(back_populates="player")

    def __repr__(self):
        return f"<Player(id={self.id}, discord_id={self.discord_id}, name={self.name})>"


class PlayerSeason(Base):
    __tablename__ = "player_seasons"

    player_id: Mapped[int] = mapped_column(ForeignKey("players.id"), primary_key=True)
    season_number: Mapped[int] = mapped_column(ForeignKey("seasons.number"), primary_key=True)
    open_wins: Mapped[int] = mapped_column(Integer, default=0)
    open_losses: Mapped[int] = mapped_column(Integer, default=0)
    open_gd: Mapped[int] = mapped_column(Integer, default=0)
    swiss_seed: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    pool: Mapped[Optional[str]] = mapped_column(String(255), nullable=True)
    playin_eliminated: Mapped[bool] = mapped_column(Boolean, default=False)
    playin_wildcard: Mapped[bool] = mapped_column(Boolean, default=False)
    playin_rank: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    hp: Mapped[int] = mapped_column(Integer, default=4)

    player: Mapped["Player"] = relationship(back_populates="player_seasons", foreign_keys=[player_id])
    season: Mapped["Season"] = relationship(back_populates="player_seasons", foreign_keys=[season_number])

    __table_args__ = (
        Index("ix_ps_season_open", "season_number", "open_wins"),
        Index("ix_ps_season_pool", "season_number", "pool"),
    )

    def __repr__(self):
        return f"<PlayerSeason(player_id={self.player_id}, season={self.season_number})>"


class Setting(Base):
    __tablename__ = "settings"

    key: Mapped[str] = mapped_column(String(255), primary_key=True)
    value: Mapped[str] = mapped_column(Text)

    def __repr__(self):
        return f"<Setting(key={self.key})>"


class Event(Base):
    __tablename__ = "events"

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(255))
    type: Mapped[EventType] = mapped_column(Enum(EventType))
    season: Mapped[int] = mapped_column(Integer)
    status: Mapped[EventStatus] = mapped_column(
        Enum(EventStatus), default=EventStatus.PENDING
    )
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)

    matches: Mapped[list["Match"]] = relationship(
        back_populates="event", cascade="all, delete-orphan"
    )

    __table_args__ = (Index("idx_event_season_type", "season", "type"),)

    def __repr__(self):
        return f"<Event(id={self.id}, name={self.name}, type={self.type.value})>"


class Match(Base):
    __tablename__ = "matches"

    id: Mapped[int] = mapped_column(primary_key=True)
    event_id: Mapped[int] = mapped_column(ForeignKey("events.id"), index=True)
    p1_prereq_id: Mapped[Optional[int]] = mapped_column(
        ForeignKey("matches.id"), nullable=True
    )
    p2_prereq_id: Mapped[Optional[int]] = mapped_column(
        ForeignKey("matches.id"), nullable=True
    )
    p1_prereq_type: Mapped[str] = mapped_column(String(10), default="winner")
    p2_prereq_type: Mapped[str] = mapped_column(String(10), default="winner")
    bracket_slot: Mapped[Optional[str]] = mapped_column(String(10), nullable=True)
    series_game: Mapped[int] = mapped_column(Integer, default=1)
    thread_id: Mapped[Optional[int]] = mapped_column(BigInteger, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    completed_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)

    __table_args__ = (
        Index("idx_match_event", "event_id"),
        Index("idx_match_completed", "completed_at"),
        CheckConstraint("p1_prereq_type IN ('winner', 'loser')", name="ck_p1_prereq_type"),
        CheckConstraint("p2_prereq_type IN ('winner', 'loser')", name="ck_p2_prereq_type"),
    )

    event: Mapped["Event"] = relationship(back_populates="matches")
    match_players: Mapped[list["MatchPlayer"]] = relationship(
        back_populates="match", cascade="all, delete-orphan", foreign_keys="MatchPlayer.match_id"
    )
    p1_prereq: Mapped[Optional["Match"]] = relationship(
        "Match", remote_side=[id], foreign_keys=[p1_prereq_id], backref="p1_dependent"
    )
    p2_prereq: Mapped[Optional["Match"]] = relationship(
        "Match", remote_side=[id], foreign_keys=[p2_prereq_id], backref="p2_dependent"
    )

    def __repr__(self):
        return f"<Match(id={self.id}, event_id={self.event_id})>"

    @property
    def is_completed(self) -> bool:
        return self.completed_at is not None

    def get_winner(self) -> Optional["MatchPlayer"]:
        for mp in self.match_players:
            if mp.is_winner:
                return mp
        return None

    def get_loser(self) -> Optional["MatchPlayer"]:
        for mp in self.match_players:
            if not mp.is_winner and mp.player_id is not None:
                return mp
        return None


class MatchPlayer(Base):
    __tablename__ = "match_players"

    id: Mapped[int] = mapped_column(primary_key=True)
    match_id: Mapped[int] = mapped_column(ForeignKey("matches.id"), index=True)
    player_id: Mapped[Optional[int]] = mapped_column(
        ForeignKey("players.id"), nullable=True, index=True
    )
    goals: Mapped[int] = mapped_column(Integer, default=0)
    is_winner: Mapped[bool] = mapped_column(Boolean, default=False)
    side: Mapped[int] = mapped_column(Integer)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)

    match: Mapped["Match"] = relationship(back_populates="match_players", foreign_keys=[match_id])
    player: Mapped[Optional["Player"]] = relationship(
        back_populates="match_players", foreign_keys=[player_id]
    )

    __table_args__ = (
        UniqueConstraint("match_id", "side", name="uq_match_side"),
        Index("idx_matchplayer_player", "player_id"),
    )

    def __repr__(self):
        return f"<MatchPlayer(id={self.id}, match_id={self.match_id}, player_id={self.player_id}, side={self.side})>"


class MatchReport(Base):
    """A player-submitted score waiting for the opponent (or a mod) to resolve it."""

    __tablename__ = "match_reports"

    id: Mapped[int] = mapped_column(primary_key=True)
    match_id: Mapped[int] = mapped_column(
        ForeignKey("matches.id", ondelete="CASCADE"), index=True
    )
    reporter_discord_id: Mapped[int] = mapped_column(BigInteger)
    reporter_player_id: Mapped[Optional[int]] = mapped_column(
        ForeignKey("players.id"), nullable=True
    )
    p1_goals: Mapped[int] = mapped_column(Integer)
    p2_goals: Mapped[int] = mapped_column(Integer)
    winner_side: Mapped[int] = mapped_column(Integer)
    status: Mapped[ReportStatus] = mapped_column(
        Enum(ReportStatus), default=ReportStatus.PENDING, index=True
    )
    reason: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    channel_id: Mapped[Optional[int]] = mapped_column(BigInteger, nullable=True)
    message_id: Mapped[Optional[int]] = mapped_column(BigInteger, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    resolved_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)
    resolved_by: Mapped[Optional[int]] = mapped_column(BigInteger, nullable=True)

    match: Mapped["Match"] = relationship()

    def __repr__(self):
        return f"<MatchReport(id={self.id}, match_id={self.match_id}, status={self.status.value})>"


class ImageCache(Base):
    __tablename__ = "images"

    hash: Mapped[str] = mapped_column(String(255), primary_key=True)
    link: Mapped[str] = mapped_column(Text)

    def __repr__(self):
        return f"<ImageCache(hash={self.hash})>"


class Timezone(Base):
    __tablename__ = "timezones"

    discord_id: Mapped[int] = mapped_column(Integer, primary_key=True)
    timezone: Mapped[str] = mapped_column(String(255))

    def __repr__(self):
        return f"<Timezone(discord_id={self.discord_id}, timezone={self.timezone})>"


class Timeslot(Base):
    __tablename__ = "timeslots"

    id: Mapped[int] = mapped_column(primary_key=True)
    discord_id: Mapped[int] = mapped_column(Integer, index=True)
    start: Mapped[int] = mapped_column(Integer)
    finish: Mapped[int] = mapped_column(Integer)
    prio: Mapped[int] = mapped_column(Integer)

    def __repr__(self):
        return f"<Timeslot(discord_id={self.discord_id}, start={self.start}, finish={self.finish})>"


class WeeklyTimeslot(Base):
    __tablename__ = "weekly_timeslots"

    id: Mapped[int] = mapped_column(primary_key=True)
    discord_id: Mapped[int] = mapped_column(Integer, index=True)
    weekday: Mapped[int] = mapped_column(Integer)  # 0=Monday .. 6=Sunday
    start_minute: Mapped[int] = mapped_column(Integer)  # minutes from midnight
    finish_minute: Mapped[int] = mapped_column(Integer)

    def __repr__(self):
        return (
            f"<WeeklyTimeslot(discord_id={self.discord_id}, "
            f"weekday={self.weekday}, start={self.start_minute}, "
            f"finish={self.finish_minute})>"
        )
