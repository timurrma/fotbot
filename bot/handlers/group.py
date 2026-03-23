"""Команды в групповом чате."""
from aiogram import F, Router
from aiogram.filters import Command
from aiogram.types import Message

from bot.db.models import UserCard, UserSquad
from bot.db.session import AsyncSessionLocal
from bot.services.stats import (
    format_scorers,
    get_top_assisters,
    get_top_scorers,
)
from bot.services.tournament import build_standings_text, get_or_create_tournament

router = Router()


@router.message(Command("start"), F.chat.type.in_({"group", "supergroup"}))
async def cmd_start_group(message: Message) -> None:
    """Отвечаем на /start в группе — просим написать в ЛС."""
    await message.reply("👋 Привет! Напиши мне в личные сообщения, чтобы начать игру.")


@router.message(Command("standings"))
async def cmd_standings(message: Message) -> None:
    """Турнирная таблица текущей недели."""
    async with AsyncSessionLocal() as session:
        tournament = await get_or_create_tournament(session)
        text = await build_standings_text(session, tournament.id)
    await message.reply(text, parse_mode="HTML")


@router.message(Command("alltime"))
async def cmd_alltime(message: Message) -> None:
    """Таблица за всё время."""
    async with AsyncSessionLocal() as session:
        text = await build_standings_text(session, tournament_id=None)
    await message.reply(text, parse_mode="HTML")


@router.message(Command("top"))
async def cmd_top(message: Message) -> None:
    """Лучшие бомбардиры и ассистенты за всё время."""
    async with AsyncSessionLocal() as session:
        scorers = await get_top_scorers(session, limit=10)
        assisters = await get_top_assisters(session, limit=10)
    text = format_scorers(scorers, "⚽ Бомбардиры всех времён")
    text += "\n\n" + format_scorers(assisters, "🎯 Ассистенты всех времён")
    await message.reply(text, parse_mode="Markdown")


@router.message(Command("topweek"))
async def cmd_topweek(message: Message) -> None:
    """Лучшие бомбардиры и ассистенты текущего турнира."""
    async with AsyncSessionLocal() as session:
        tournament = await get_or_create_tournament(session)
        scorers = await get_top_scorers(session, limit=10, tournament_id=tournament.id)
        assisters = await get_top_assisters(session, limit=10, tournament_id=tournament.id)
    text = format_scorers(scorers, "⚽ Бомбардиры этой недели")
    text += "\n\n" + format_scorers(assisters, "🎯 Ассистенты этой недели")
    await message.reply(text, parse_mode="Markdown")


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
                for wl in result.scalars().all():
                    if wl.username and wl.username.lower() == target_username:
                        user_id = wl.user_id
                        display_name = f"Состав @{target_username}"
                        break

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
