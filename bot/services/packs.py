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

from bot.db.models import PackHistory, PendingPack, Player, UserCard, UserSquad

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
        "ranges": [(65, 74), (75, 82), (85, 99)],
        "weights": [90, 10, 0],
    },
    "russia": {
        "ranges": [(65, 74), (75, 78), (79, 99)],
        "weights": [68, 25, 7],
    },
    "brazil": {
        "ranges": [(65, 74), (75, 79), (80, 84), (85, 99)],
        "weights": [68, 23, 7, 2],
    },
    "turkey": {
        "ranges": [(65, 74), (75, 78), (79, 99)],
        "weights": [68, 25, 7],
    },
    "morning": {
        "ranges": [(50, 69), (70, 75), (76, 80), (81, 99)],
        "weights": [85, 10, 4.5, 0.5],
    },
    "saudi": {
        "ranges": [(65, 74), (75, 78), (79, 99)],
        "weights": [68, 25, 7],
    },
    "record": {
        "ranges": [(65, 74), (75, 84), (85, 99)],
        "weights": [20, 75, 5],
    },
    "consolation": {
        "ranges": [(70, 75), (76, 80), (81, 82), (83, 99)],
        "weights": [20, 45, 30, 5],
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
        return (80, 82)
    ranges, weights = cfg["ranges"], cfg["weights"]
    chosen = random.choices(ranges, weights=weights, k=1)[0]
    return chosen


async def _pick_player(
    session: AsyncSession,
    rating_min: int,
    rating_max: int,
    position: str | None = None,
    exclude_ids: set[int] | None = None,
    weighted: bool = False,
) -> Player | None:
    """Выбирает случайного игрока из БД в заданном диапазоне рейтинга.
    weighted=True — чем выше рейтинг, тем ниже вероятность выбора."""
    query = select(Player).where(
        Player.overall_rating >= rating_min,
        Player.overall_rating <= rating_max,
    )
    if position:
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
    if not players:
        return None
    if weighted:
        # Вес обратно пропорционален рейтингу: 85→15, 90→10, 95→5, 99→1
        weights = [max(1, 100 - p.overall_rating) for p in players]
        return random.choices(players, weights=weights, k=1)[0]
    return random.choice(players)


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
    elif pack_type == "russia":
        players_out = await _open_russia_pack(session, used_player_ids)
    elif pack_type == "brazil":
        players_out = await _open_brazil_pack(session, used_player_ids)
    elif pack_type == "turkey":
        players_out = await _open_turkey_pack(session, used_player_ids)
    elif pack_type == "morning":
        players_out = await _open_morning_pack(session, used_player_ids)
    elif pack_type == "saudi":
        players_out = await _open_saudi_pack(session, used_player_ids)
    elif pack_type == "record":
        players_out = await _open_record_pack(session, used_player_ids)
    elif pack_type == "consolation":
        players_out = await _open_consolation_pack(session, used_player_ids)
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
        player = await _pick_player(session, r_min, r_max, position=pos, exclude_ids=used_ids, weighted=force_high)
        if player is None:
            # Фоллбэк без позиции
            player = await _pick_player(session, r_min, r_max, exclude_ids=used_ids, weighted=force_high)
        if player:
            players_out.append(player)
            used_ids.add(player.id)
            if player.overall_rating >= 80:
                has_high = True

    return players_out


ARSHAVIN_ID = 2000032


async def _open_russia_pack(
    session: AsyncSession,
    used_ids: set[int],
) -> list[Player]:
    """Россия-пак: 1 игрок сборной России с взвешенной вероятностью по диапазонам.
    1% шанс выбить А. Аршавина (id=2000032, 90 рейтинг).
    """
    # 1% шанс на Аршавина
    if ARSHAVIN_ID not in used_ids and random.random() < 0.01:
        arshavin = await session.get(Player, ARSHAVIN_ID)
        if arshavin:
            return [arshavin]

    # Диапазоны: 65-74 (68%), 75-78 (25%), 79+ (7%), исключая Аршавина
    ranges = [(65, 74), (75, 78), (79, 99)]
    weights = [68, 25, 7]
    r_min, r_max = random.choices(ranges, weights=weights, k=1)[0]

    exclude = used_ids | {ARSHAVIN_ID}
    query = select(Player).where(
        Player.nationality == "Russia",
        Player.overall_rating >= r_min,
        Player.overall_rating <= r_max,
        Player.id.notin_(exclude),
    )
    result = await session.execute(query)
    players = result.scalars().all()

    if not players:
        # Фоллбэк — любой русский кроме Аршавина
        query2 = select(Player).where(
            Player.nationality == "Russia",
            Player.id.notin_(exclude),
        )
        result2 = await session.execute(query2)
        players = result2.scalars().all()

    if not players:
        return []
    return [random.choice(players)]


async def _open_brazil_pack(
    session: AsyncSession,
    used_ids: set[int],
) -> list[Player]:
    """Бразилия-пак: 2 игрока сборной Бразилии."""
    players_out: list[Player] = []
    for _ in range(2):
        cfg = PACK_WEIGHTS["brazil"]
        r_min, r_max = random.choices(cfg["ranges"], weights=cfg["weights"], k=1)[0]
        query = select(Player).where(
            Player.nationality == "Brazil",
            Player.overall_rating >= r_min,
            Player.overall_rating <= r_max,
            Player.id.notin_(used_ids) if used_ids else True,
        )
        result = await session.execute(query)
        players = result.scalars().all()
        if not players:
            query2 = select(Player).where(
                Player.nationality == "Brazil",
                Player.id.notin_(used_ids) if used_ids else True,
            )
            result2 = await session.execute(query2)
            players = result2.scalars().all()
        if players:
            p = random.choice(players)
            players_out.append(p)
            used_ids.add(p.id)
    return players_out


async def _open_turkey_pack(
    session: AsyncSession,
    used_ids: set[int],
) -> list[Player]:
    """Турция-пак: 1 игрок сборной Турции."""
    cfg = PACK_WEIGHTS["turkey"]
    r_min, r_max = random.choices(cfg["ranges"], weights=cfg["weights"], k=1)[0]
    query = select(Player).where(
        Player.nationality == "Türkiye",
        Player.overall_rating >= r_min,
        Player.overall_rating <= r_max,
        Player.id.notin_(used_ids) if used_ids else True,
    )
    result = await session.execute(query)
    players = result.scalars().all()
    if not players:
        query2 = select(Player).where(
            Player.nationality == "Türkiye",
            Player.id.notin_(used_ids) if used_ids else True,
        )
        result2 = await session.execute(query2)
        players = result2.scalars().all()
    if not players:
        return []
    return [random.choice(players)]


async def _open_morning_pack(
    session: AsyncSession,
    used_ids: set[int],
) -> list[Player]:
    """Утренний пак: 2 любых игрока. Вероятности: <70 (85%), 70-75 (10%), 76-80 (4.5%), 80+ (0.5%)."""
    players_out: list[Player] = []
    cfg = PACK_WEIGHTS["morning"]
    for _ in range(2):
        r_min, r_max = random.choices(cfg["ranges"], weights=cfg["weights"], k=1)[0]
        player = await _pick_player(session, r_min, r_max, exclude_ids=used_ids)
        if player:
            players_out.append(player)
            used_ids.add(player.id)
    return players_out


async def _open_record_pack(
    session: AsyncSession,
    used_ids: set[int],
) -> list[Player]:
    """Рекорд-пак: 3 карточки (65-74: 75%, 75-84: 20%, 85+: 5%)."""
    players_out: list[Player] = []
    cfg = PACK_WEIGHTS["record"]
    for _ in range(3):
        r_min, r_max = random.choices(cfg["ranges"], weights=cfg["weights"], k=1)[0]
        player = await _pick_player(session, r_min, r_max, exclude_ids=used_ids)
        if player:
            players_out.append(player)
            used_ids.add(player.id)
    return players_out


async def _open_consolation_pack(
    session: AsyncSession,
    used_ids: set[int],
) -> list[Player]:
    """Утешающий пак: 2 карточки (70-75: 20%, 76-80: 45%, 81-82: 30%, 83+: 5%)."""
    players_out: list[Player] = []
    cfg = PACK_WEIGHTS["consolation"]
    for _ in range(2):
        r_min, r_max = random.choices(cfg["ranges"], weights=cfg["weights"], k=1)[0]
        player = await _pick_player(session, r_min, r_max, exclude_ids=used_ids)
        if player:
            players_out.append(player)
            used_ids.add(player.id)
    return players_out


SAUDI_CLUBS = {'Al Nassr', 'Al Hilal', 'Al Ittihad'}


async def _open_saudi_pack(
    session: AsyncSession,
    used_ids: set[int],
) -> list[Player]:
    """Саудовская лига: 1 игрок из клубов Saudi Pro League."""
    cfg = PACK_WEIGHTS["saudi"]
    r_min, r_max = random.choices(cfg["ranges"], weights=cfg["weights"], k=1)[0]
    query = select(Player).where(
        Player.club.in_(SAUDI_CLUBS),
        Player.overall_rating >= r_min,
        Player.overall_rating <= r_max,
        Player.id.notin_(used_ids) if used_ids else True,
    )
    result = await session.execute(query)
    players = result.scalars().all()
    if not players:
        query2 = select(Player).where(
            Player.club.in_(SAUDI_CLUBS),
            Player.id.notin_(used_ids) if used_ids else True,
        )
        result2 = await session.execute(query2)
        players = result2.scalars().all()
    if not players:
        return []
    return [random.choice(players)]


async def give_pending_pack(session: AsyncSession, user_id: int, pack_type: str = "weekly") -> None:
    """Добавляет неоткрытый пак в очередь игрока."""
    pack = PendingPack(user_id=user_id, pack_type=pack_type)
    session.add(pack)
    await session.commit()


async def get_pending_packs(session: AsyncSession, user_id: int) -> list[PendingPack]:
    """Возвращает список неоткрытых паков игрока."""
    result = await session.execute(
        select(PendingPack).where(PendingPack.user_id == user_id).order_by(PendingPack.created_at)
    )
    return result.scalars().all()


async def open_pending_pack(session: AsyncSession, user_id: int) -> list[Player] | None:
    """Открывает первый неоткрытый пак. Возвращает игроков или None если паков нет."""
    result = await session.execute(
        select(PendingPack).where(PendingPack.user_id == user_id).order_by(PendingPack.created_at).limit(1)
    )
    pending = result.scalar_one_or_none()
    if not pending:
        return None

    pack_type = pending.pack_type
    await session.delete(pending)
    await session.flush()

    players = await open_pack(session, user_id, pack_type)
    return players


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
    stars = {"starter": "🌟 Стартовый", "weekly": "🎴", "special": "💎 Спец", "russia": "🇷🇺 Россия", "brazil": "🇧🇷 Бразилия", "turkey": "🇹🇷 Турция", "morning": "🌅 Утренний", "saudi": "🇸🇦 Саудовская лига", "record": "🏆 Рекорд", "consolation": "🤝 Утешающий"}
    header = stars.get(pack_type, "🎴")

    lines = [f"{header} @{username} открыл пак!\n"]
    for i, player in enumerate(players):
        prefix = "└" if i == len(players) - 1 else "├"
        rating_star = "⭐" if player.overall_rating < 85 else "🌟" if player.overall_rating < 90 else "👑"
        club = f" ({player.club})" if player.club else ""
        pos = f" [{player.position}]" if player.position else ""
        lines.append(f"{prefix} {player.name}{club}{pos} — {player.overall_rating} {rating_star}")

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

    stars = {"starter": "🌟 Стартовый", "weekly": "🎴", "special": "💎 Спец", "russia": "🇷🇺 Россия", "brazil": "🇧🇷 Бразилия", "turkey": "🇹🇷 Турция", "morning": "🌅 Утренний", "saudi": "🇸🇦 Саудовская лига", "record": "🏆 Рекорд", "consolation": "🤝 Утешающий"}
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
