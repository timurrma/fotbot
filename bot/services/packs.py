from __future__ import annotations

"""
Пак-система: логика выдачи карточек игрокам.

Типы паков:
- starter  — стартовый, выдаётся 1 раз при /start (14 карточек, 1 гарантирован 85+)
- weekly   — обычный еженедельный (5 карточек)
- special  — крутой пак от админа (5 карточек, повышенный шанс 85+)
"""
import random
from datetime import datetime

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from bot.db.models import PackHistory, Player, UserCard, UserSquad

# Вероятности рейтингов для разных паков
PACK_WEIGHTS = {
    "weekly": {
        "ranges": [(65, 74), (75, 84), (85, 99)],
        "weights": [55, 35, 10],
    },
    "special": {
        "ranges": [(65, 74), (75, 84), (85, 99)],
        "weights": [30, 35, 35],
    },
    "starter": {
        "ranges": [(65, 74), (75, 84), (85, 99)],
        "weights": [65, 27, 8],
    },
}

# Позиции по схеме 4-4-2 для стартового пака
STARTER_POSITIONS_442 = ["GK", "CB", "CB", "LB", "RB", "CM", "CM", "LM", "RM", "ST", "ST"]
# 3 запасных на случайные позиции
STARTER_BENCH_POSITIONS = ["CM", "CB", "ST"]

# Маппинг "широкой" позиции API → слоты схемы
POSITION_GROUPS = {
    "GK": ["GK"],
    "CB": ["CB", "LB", "RB"],
    "LB": ["CB", "LB", "RB"],
    "RB": ["CB", "LB", "RB"],
    "CDM": ["CDM", "CM", "LM", "RM"],
    "CM": ["CM", "CDM", "LM", "RM", "CAM"],
    "LM": ["LM", "CM", "RM"],
    "RM": ["RM", "CM", "LM"],
    "CAM": ["CAM", "CM", "LM", "RM"],
    "LW": ["LW", "LM", "CAM"],
    "RW": ["RW", "RM", "CAM"],
    "CF": ["CF", "ST", "CAM"],
    "ST": ["ST", "CF", "CAM"],
}


def _pick_rating(pack_type: str, force_high: bool = False) -> tuple[int, int]:
    """Возвращает (min, max) диапазон рейтинга согласно весам пака."""
    cfg = PACK_WEIGHTS[pack_type]
    if force_high:
        return (85, 99)
    ranges, weights = cfg["ranges"], cfg["weights"]
    chosen = random.choices(ranges, weights=weights, k=1)[0]
    return chosen


async def _pick_player(
    session: AsyncSession,
    rating_min: int,
    rating_max: int,
    position: str | None = None,
    exclude_ids: set[int] | None = None,
) -> Player | None:
    """Выбирает случайного игрока из БД в заданном диапазоне рейтинга."""
    query = select(Player).where(
        Player.overall_rating >= rating_min,
        Player.overall_rating <= rating_max,
    )
    if position:
        # Ищем игроков с основной позицией из подходящей группы
        allowed = POSITION_GROUPS.get(position, [position])
        query = query.where(Player.position.in_(allowed))
    if exclude_ids:
        query = query.where(Player.id.notin_(exclude_ids))

    result = await session.execute(query)
    players = result.scalars().all()
    if not players:
        # Фоллбэк — без фильтра по позиции
        query2 = select(Player).where(
            Player.overall_rating >= rating_min,
            Player.overall_rating <= rating_max,
        )
        if exclude_ids:
            query2 = query2.where(Player.id.notin_(exclude_ids))
        result2 = await session.execute(query2)
        players = result2.scalars().all()
    return random.choice(players) if players else None


async def open_pack(
    session: AsyncSession,
    user_id: int,
    pack_type: str = "weekly",
) -> list[Player]:
    """
    Открывает пак для пользователя.
    Сохраняет карточки в user_cards и историю в pack_history.
    Возвращает список выпавших игроков.
    """
    used_player_ids: set[int] = set()
    players_out: list[Player] = []

    if pack_type == "starter":
        players_out = await _open_starter_pack(session, user_id, used_player_ids)
    else:
        count = 5
        # weekly / special
        for i in range(count):
            force_high = (pack_type == "special" and i == 0)
            r_min, r_max = _pick_rating(pack_type, force_high=force_high)
            player = await _pick_player(session, r_min, r_max, exclude_ids=used_player_ids)
            if player:
                players_out.append(player)
                used_player_ids.add(player.id)

    # Сохраняем карточки в коллекцию
    for player in players_out:
        card = UserCard(user_id=user_id, player_id=player.id, acquired_at=datetime.utcnow())
        session.add(card)

    # История
    history = PackHistory(
        user_id=user_id,
        opened_at=datetime.utcnow(),
        pack_type=pack_type,
    )
    history.player_ids = [p.id for p in players_out]
    session.add(history)

    await session.commit()
    return players_out


async def _open_starter_pack(
    session: AsyncSession,
    user_id: int,
    used_ids: set[int],
) -> list[Player]:
    """Стартовый пак: 11 карточек по позициям 4-4-2 + 3 запасных. Гарантирован 1 игрок 85+."""
    players_out: list[Player] = []
    has_high = False

    all_positions = STARTER_POSITIONS_442 + STARTER_BENCH_POSITIONS
    for i, pos in enumerate(all_positions):
        # Последний слот — гарантируем 85+ если ещё не выпал
        is_last = (i == len(all_positions) - 1)
        force_high = (is_last and not has_high)

        r_min, r_max = _pick_rating("starter", force_high=force_high)
        player = await _pick_player(session, r_min, r_max, position=pos, exclude_ids=used_ids)
        if player is None:
            # Фоллбэк без позиции
            player = await _pick_player(session, r_min, r_max, exclude_ids=used_ids)
        if player:
            players_out.append(player)
            used_ids.add(player.id)
            if player.overall_rating >= 85:
                has_high = True

    return players_out


async def has_starter_pack(session: AsyncSession, user_id: int) -> bool:
    """Проверяет, получал ли пользователь стартовый пак."""
    result = await session.execute(
        select(PackHistory).where(
            PackHistory.user_id == user_id,
            PackHistory.pack_type == "starter",
        )
    )
    return result.scalar_one_or_none() is not None


def format_pack_announcement(username: str, players: list[Player], pack_type: str = "weekly") -> str:
    """Формирует текстовый анонс открытия пака (без фото)."""
    stars = {"starter": "🌟 Стартовый", "weekly": "🎴", "special": "💎 Спец"}
    header = stars.get(pack_type, "🎴")

    lines = [f"{header} @{username} открыл пак!\n"]
    for i, player in enumerate(players):
        prefix = "└" if i == len(players) - 1 else "├"
        rating_star = "⭐" if player.overall_rating < 85 else "🌟" if player.overall_rating < 90 else "👑"
        lines.append(f"{prefix} {player.name} — {player.overall_rating} {rating_star}")

    return "\n".join(lines)


async def send_pack_with_photos(
    bot,
    chat_id: int,
    username: str,
    players: list[Player],
    pack_type: str = "weekly",
) -> None:
    """
    Отправляет пак в чат: медиагруппа с фото игроков (макс 10) + итоговый текст.
    Если у игрока нет фото — отправляет только текст.
    """
    from aiogram.types import InputMediaPhoto

    stars = {"starter": "🌟 Стартовый", "weekly": "🎴", "special": "💎 Спец"}
    header = stars.get(pack_type, "🎴")

    # Берём только игроков с фото (макс 10 для медиагруппы)
    players_with_photo = [p for p in players if p.photo_url][:10]

    if players_with_photo:
        # Первое фото — с заголовком пака
        media = []
        for i, player in enumerate(players_with_photo):
            rating_star = "⭐" if player.overall_rating < 85 else "🌟" if player.overall_rating < 90 else "👑"
            caption = f"{player.name} — {player.overall_rating} {rating_star}"
            if i == 0:
                caption = f"{header} @{username} открыл пак!\n\n{caption}"
            media.append(InputMediaPhoto(media=player.photo_url, caption=caption))

        try:
            await bot.send_media_group(chat_id=chat_id, media=media)
            # Отдельным сообщением — полный список всех игроков
            await bot.send_message(chat_id=chat_id, text=format_pack_announcement(username, players, pack_type))
            return
        except Exception:
            pass  # фоллбэк на текст если фото недоступны

    # Фоллбэк — текст
    text = format_pack_announcement(username, players, pack_type)
    await bot.send_message(chat_id=chat_id, text=text)
