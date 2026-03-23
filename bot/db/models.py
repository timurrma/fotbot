import json
from datetime import datetime
from typing import Optional

from sqlalchemy import (
    BigInteger, Boolean, DateTime, Float, ForeignKey,
    Integer, String, Text, UniqueConstraint, func,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship


class Base(DeclarativeBase):
    pass


class Player(Base):
    """Справочник футболистов (заполняется скриптом fetch_players.py)."""
    __tablename__ = "players"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)  # API-Football player id
    name: Mapped[str] = mapped_column(String(200), nullable=False)
    club: Mapped[Optional[str]] = mapped_column(String(200))
    nationality: Mapped[Optional[str]] = mapped_column(String(100))
    # Основная позиция: GK, CB, LB, RB, CDM, CM, LM, RM, CAM, LW, RW, CF, ST
    position: Mapped[str] = mapped_column(String(10), nullable=False)
    # JSON-список всех позиций игрока: ["ST", "CF"]
    positions_json: Mapped[str] = mapped_column(Text, default="[]")
    overall_rating: Mapped[int] = mapped_column(Integer, nullable=False)
    photo_url: Mapped[Optional[str]] = mapped_column(Text)
    league_id: Mapped[Optional[int]] = mapped_column(Integer)
    is_national_team: Mapped[bool] = mapped_column(Boolean, default=False)

    @property
    def positions(self) -> list[str]:
        return json.loads(self.positions_json)

    @positions.setter
    def positions(self, value: list[str]) -> None:
        self.positions_json = json.dumps(value)


class Whitelist(Base):
    """Разрешённые пользователи."""
    __tablename__ = "whitelist"

    user_id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    username: Mapped[Optional[str]] = mapped_column(String(100))
    added_at: Mapped[datetime] = mapped_column(DateTime, default=func.now())


class UserCard(Base):
    """Карточка в коллекции пользователя."""
    __tablename__ = "user_cards"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    user_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    player_id: Mapped[int] = mapped_column(Integer, ForeignKey("players.id"), nullable=False)
    acquired_at: Mapped[datetime] = mapped_column(DateTime, default=func.now())

    player: Mapped["Player"] = relationship("Player", lazy="joined")


class UserSquad(Base):
    """Сохранённый состав пользователя."""
    __tablename__ = "user_squads"

    user_id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    formation: Mapped[str] = mapped_column(String(10), default="4-4-2")
    # JSON: {"GK": user_card_id, "CB1": user_card_id, ...}
    slot_assignments_json: Mapped[str] = mapped_column(Text, default="{}")

    @property
    def slot_assignments(self) -> dict:
        return json.loads(self.slot_assignments_json)

    @slot_assignments.setter
    def slot_assignments(self, value: dict) -> None:
        self.slot_assignments_json = json.dumps(value)


class PackHistory(Base):
    """История открытых паков."""
    __tablename__ = "pack_history"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    user_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    opened_at: Mapped[datetime] = mapped_column(DateTime, default=func.now())
    pack_type: Mapped[str] = mapped_column(String(20), default="weekly")  # weekly / starter / special
    player_ids_json: Mapped[str] = mapped_column(Text, default="[]")

    @property
    def player_ids(self) -> list[int]:
        return json.loads(self.player_ids_json)

    @player_ids.setter
    def player_ids(self, value: list[int]) -> None:
        self.player_ids_json = json.dumps(value)


class Tournament(Base):
    """Еженедельный турнир."""
    __tablename__ = "tournaments"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    week_number: Mapped[int] = mapped_column(Integer, nullable=False)
    year: Mapped[int] = mapped_column(Integer, nullable=False)
    status: Mapped[str] = mapped_column(String(20), default="pending")  # pending / running / finished
    created_at: Mapped[datetime] = mapped_column(DateTime, default=func.now())

    matches: Mapped[list["Match"]] = relationship("Match", back_populates="tournament")

    __table_args__ = (UniqueConstraint("week_number", "year", name="uq_tournament_week"),)


class Match(Base):
    """Матч в рамках турнира."""
    __tablename__ = "matches"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    tournament_id: Mapped[int] = mapped_column(Integer, ForeignKey("tournaments.id"), nullable=False)
    home_user_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    away_user_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    home_goals: Mapped[Optional[int]] = mapped_column(Integer)
    away_goals: Mapped[Optional[int]] = mapped_column(Integer)
    # JSON: список событий матча для LLM
    events_json: Mapped[str] = mapped_column(Text, default="[]")
    played_at: Mapped[Optional[datetime]] = mapped_column(DateTime)

    tournament: Mapped["Tournament"] = relationship("Tournament", back_populates="matches")
    stats: Mapped[list["MatchStat"]] = relationship("MatchStat", back_populates="match")

    @property
    def events(self) -> list[dict]:
        return json.loads(self.events_json)

    @events.setter
    def events(self, value: list[dict]) -> None:
        self.events_json = json.dumps(value, ensure_ascii=False)


class MatchStat(Base):
    """Статистика голов/ассистов за матч."""
    __tablename__ = "match_stats"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    match_id: Mapped[int] = mapped_column(Integer, ForeignKey("matches.id"), nullable=False)
    user_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    user_card_id: Mapped[int] = mapped_column(Integer, ForeignKey("user_cards.id"), nullable=False)
    player_id: Mapped[int] = mapped_column(Integer, ForeignKey("players.id"), nullable=False)
    goals: Mapped[int] = mapped_column(Integer, default=0)
    assists: Mapped[int] = mapped_column(Integer, default=0)

    match: Mapped["Match"] = relationship("Match", back_populates="stats")
    player: Mapped["Player"] = relationship("Player", lazy="joined")


class TransferOffer(Base):
    """Предложение обмена карточками."""
    __tablename__ = "transfer_offers"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    from_user_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    to_user_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    offer_card_id: Mapped[int] = mapped_column(Integer, ForeignKey("user_cards.id"), nullable=False)
    want_card_id: Mapped[int] = mapped_column(Integer, ForeignKey("user_cards.id"), nullable=False)
    status: Mapped[str] = mapped_column(String(20), default="pending")  # pending / accepted / declined / cancelled
    created_at: Mapped[datetime] = mapped_column(DateTime, default=func.now())

    offer_card: Mapped["UserCard"] = relationship(
        "UserCard", foreign_keys=[offer_card_id], lazy="joined"
    )
    want_card: Mapped["UserCard"] = relationship(
        "UserCard", foreign_keys=[want_card_id], lazy="joined"
    )


class TransferCount(Base):
    """Счётчик трансферов за текущую неделю."""
    __tablename__ = "transfer_counts"

    user_id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    week_number: Mapped[int] = mapped_column(Integer, nullable=False)
    year: Mapped[int] = mapped_column(Integer, nullable=False)
    count: Mapped[int] = mapped_column(Integer, default=0)
