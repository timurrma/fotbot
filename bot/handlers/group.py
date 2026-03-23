"""Команды в групповом чате."""
from aiogram import F, Router
from aiogram.filters import Command
from aiogram.types import Message

from bot.db.models import UserCard, UserSquad
from bot.db.session import AsyncSessionLocal
from bot.services.stats import (
    format_scorers,
    get_top_assisters,
    get_top_combined,
    get_top_mvp,
    get_top_scorers,
)
from bot.services.tournament import build_standings_text, get_or_create_tournament, get_active_tournament

router = Router()


@router.message(Command("start"), F.chat.type.in_({"group", "supergroup"}))
async def cmd_start_group(message: Message) -> None:
    """Отвечаем на /start в группе — просим написать в ЛС."""
    await message.reply("👋 Привет! Напиши мне в личные сообщения, чтобы начать игру.")


@router.message(Command("standings"))
async def cmd_standings(message: Message) -> None:
    """Турнирная таблица текущей недели + последние матчи + топ-3."""
    from sqlalchemy import select as sa_select
    from bot.db.models import Match

    async with AsyncSessionLocal() as session:
        tournament = await get_active_tournament(session)
        if not tournament:
            tournament = await get_or_create_tournament(session)

        standings = await build_standings_text(session, tournament.id)

        # Последние сыгранные матчи
        result = await session.execute(
            sa_select(Match).where(
                Match.tournament_id == tournament.id,
                Match.home_goals.isnot(None),
            ).order_by(Match.played_at.desc()).limit(5)
        )
        played_matches = result.scalars().all()

        # Никнеймы из whitelist
        from bot.db.models import Whitelist
        from sqlalchemy import select as wl_select
        wl_result = await session.execute(wl_select(Whitelist))
        wl_map = {w.user_id: (w.username or f"ID{w.user_id}") for w in wl_result.scalars().all()}

        # Топ-3 бомбардиры, ассистенты и MVP турнира
        scorers = await get_top_scorers(session, limit=3, tournament_id=tournament.id)
        assisters = await get_top_assisters(session, limit=3, tournament_id=tournament.id)
        mvp_week = await get_top_mvp(session, limit=3, tournament_id=tournament.id)

    text = standings

    if played_matches:
        text += "\n\n📋 <b>Результаты матчей</b>\n"
        for m in reversed(played_matches):
            h = wl_map.get(m.home_user_id, f"ID{m.home_user_id}")
            a = wl_map.get(m.away_user_id, f"ID{m.away_user_id}")
            text += f"  @{h} {m.home_goals}:{m.away_goals} @{a}\n"

    if scorers:
        text += "\n⚽ <b>Бомбардиры</b>\n"
        for i, r in enumerate(scorers, 1):
            owner = wl_map.get(r['user_id'], f"ID{r['user_id']}")
            text += f"  {i}. {r['player_name']} (@{owner}) — {r['goals']} гол.\n"

    if assisters:
        text += "\n🎯 <b>Ассистенты</b>\n"
        for i, r in enumerate(assisters, 1):
            owner = wl_map.get(r['user_id'], f"ID{r['user_id']}")
            text += f"  {i}. {r['player_name']} (@{owner}) — {r['assists']} acc.\n"

    if mvp_week:
        text += "\n🏅 <b>MVP</b>\n"
        for i, r in enumerate(mvp_week, 1):
            owner = wl_map.get(r['user_id'], f"ID{r['user_id']}")
            text += f"  {i}. {r['player_name']} (@{owner}) — {r['mvp_count']} 🏅\n"

    await message.reply(text, parse_mode="HTML")


@router.message(Command("alltime"))
async def cmd_alltime(message: Message) -> None:
    """Таблица за всё время."""
    async with AsyncSessionLocal() as session:
        text = await build_standings_text(session, tournament_id=None)
    await message.reply(text, parse_mode="HTML")


def _format_with_owners(rows: list[dict], title: str, key: str, wl_map: dict) -> str:
    lines = [f"<b>{title}</b>\n"]
    if not rows:
        lines.append("Пока нет данных.")
    for i, r in enumerate(rows, 1):
        owner = wl_map.get(r['user_id'], f"ID{r['user_id']}")
        apps = r.get('appearances', 0)
        lines.append(f"  {i}. {r['player_name']} (@{owner}) — {r[key]} ({apps} матч.)")
    return "\n".join(lines)


def _format_mvp(rows: list[dict], title: str, wl_map: dict) -> str:
    lines = [f"<b>{title}</b>\n"]
    if not rows:
        lines.append("Пока нет данных.")
    for i, r in enumerate(rows, 1):
        owner = wl_map.get(r['user_id'], f"ID{r['user_id']}")
        lines.append(f"  {i}. {r['player_name']} (@{owner}) — {r['mvp_count']} 🏅")
    return "\n".join(lines)


def _format_combined(rows: list[dict], title: str, wl_map: dict) -> str:
    lines = [f"<b>{title}</b>\n"]
    if not rows:
        lines.append("Пока нет данных.")
    for i, r in enumerate(rows, 1):
        owner = wl_map.get(r['user_id'], f"ID{r['user_id']}")
        apps = r.get('appearances', 0)
        total = r['goals'] + r['assists']
        lines.append(f"  {i}. {r['player_name']} (@{owner}) — {total} (⚽{r['goals']} 🎯{r['assists']}, {apps} матч.)")
    return "\n".join(lines)


@router.message(Command("top"))
async def cmd_top(message: Message) -> None:
    """Лучшие бомбардиры, ассистенты и г+п за всё время."""
    from sqlalchemy import select as wl_select
    from bot.db.models import Whitelist
    async with AsyncSessionLocal() as session:
        scorers = await get_top_scorers(session, limit=5)
        assisters = await get_top_assisters(session, limit=5)
        combined = await get_top_combined(session, limit=5)
        mvp = await get_top_mvp(session, limit=5)
        wl_result = await session.execute(wl_select(Whitelist))
        wl_map = {w.user_id: (w.username or f"ID{w.user_id}") for w in wl_result.scalars().all()}
    text = _format_with_owners(scorers, "⚽ Бомбардиры всех времён", "goals", wl_map)
    text += "\n\n" + _format_with_owners(assisters, "🎯 Ассистенты всех времён", "assists", wl_map)
    text += "\n\n" + _format_combined(combined, "🏆 Гол+пас всех времён", wl_map)
    text += "\n\n" + _format_mvp(mvp, "🏅 MVP всех времён", wl_map)
    await message.reply(text, parse_mode="HTML")


@router.message(Command("topweek"))
async def cmd_topweek(message: Message) -> None:
    """Лучшие бомбардиры и ассистенты текущего турнира."""
    from sqlalchemy import select as wl_select
    from bot.db.models import Whitelist
    async with AsyncSessionLocal() as session:
        tournament = await get_or_create_tournament(session)
        scorers = await get_top_scorers(session, limit=5, tournament_id=tournament.id)
        assisters = await get_top_assisters(session, limit=5, tournament_id=tournament.id)
        mvp = await get_top_mvp(session, limit=5, tournament_id=tournament.id)
        wl_result = await session.execute(wl_select(Whitelist))
        wl_map = {w.user_id: (w.username or f"ID{w.user_id}") for w in wl_result.scalars().all()}
    text = _format_with_owners(scorers, "⚽ Бомбардиры этой недели", "goals", wl_map)
    text += "\n\n" + _format_with_owners(assisters, "🎯 Ассистенты этой недели", "assists", wl_map)
    text += "\n\n" + _format_mvp(mvp, "🏅 MVP этой недели", wl_map)
    await message.reply(text, parse_mode="HTML")


@router.message(Command("schedule"))
async def cmd_schedule(message: Message) -> None:
    """Расписание событий недели."""
    from datetime import datetime, timezone, timedelta

    now = datetime.now(timezone(timedelta(hours=3)))  # Moscow time
    weekday = now.weekday()  # 0=пн, 2=ср, 3=чт

    # Ближайшая среда и четверг
    days_to_wed = (2 - weekday) % 7
    days_to_thu = (3 - weekday) % 7

    wed = now + timedelta(days=days_to_wed)
    thu = now + timedelta(days=days_to_thu)

    wed_str = wed.strftime("%d.%m")
    thu_str = thu.strftime("%d.%m")

    await message.reply(
        "📅 <b>Расписание недели</b>\n\n"
        f"⚽ <b>Среда {wed_str}, 20:00</b> — анонс турнира\n"
        f"🎮 <b>Среда {wed_str}, 20:00–21:00</b> — настройка состава\n"
        f"🏟 <b>Среда {wed_str}, 21:00+</b> — запуск матчей (/nextmatch)\n\n"
        f"📊 <b>Четверг {thu_str}, 09:00</b> — авто-итоги несыгранных матчей\n"
        f"🎴 <b>Четверг {thu_str}, 10:00</b> — раздача еженедельных паков\n\n"
        "Время московское 🕐",
        parse_mode="HTML",
    )


@router.message(Command("myteam"))
async def cmd_myteam(message: Message) -> None:
    """Показать состав — свой или по @username."""
    # Определяем чей состав показывать
    parts = message.text.split()
    target_username = None
    if len(parts) > 1 and parts[1].startswith("@"):
        target_username = parts[1][1:].lower()

    user_id = message.from_user.id
    display_name = "Твой состав"

    if target_username:
        # Ищем по упомянутому пользователю
        if message.entities:
            for entity in message.entities:
                if entity.type == "mention" and entity.user:
                    user_id = entity.user.id
                    display_name = f"Состав @{target_username}"
                    break
        # Если mention без user (публичный юзернейм) — ищем в whitelist
        if user_id == message.from_user.id:
            from sqlalchemy import select
            from bot.db.models import Whitelist
            async with AsyncSessionLocal() as session:
                result = await session.execute(select(Whitelist))
                found = False
                for wl in result.scalars().all():
                    if wl.username and wl.username.lower() == target_username:
                        user_id = wl.user_id
                        display_name = f"Состав @{target_username}"
                        found = True
                        break
            if not found:
                await message.reply(f"❌ Игрок @{target_username} не найден. Проверь username в /adduser.")
                return

    async with AsyncSessionLocal() as session:
        squad = await session.get(UserSquad, user_id)
        if not squad or not squad.slot_assignments:
            await message.reply("Состав не настроен.")
            return

        lines = [f"🏟 {display_name} | Схема: {squad.formation}\n"]
        for slot, card_id in sorted(squad.slot_assignments.items()):
            card = await session.get(UserCard, card_id)
            if card:
                r = card.player.overall_rating
                icon = "👑" if r >= 90 else "🌟" if r >= 85 else "⭐"
                club = f" ({card.player.club})" if card.player.club else ""
                lines.append(f"  {slot}: {card.player.name}{club} — {r}{icon}")

    await message.reply("\n".join(lines))
