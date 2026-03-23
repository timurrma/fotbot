"""Команды в групповом чате."""
from aiogram import Router
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


@router.message(Command("myteam"))
async def cmd_myteam(message: Message) -> None:
    """Показать текущий состав."""
    user_id = message.from_user.id

    async with AsyncSessionLocal() as session:
        squad = await session.get(UserSquad, user_id)
        if not squad or not squad.slot_assignments:
            await message.reply("Состав не настроен. Настрой через кнопку Menu в личке с ботом.")
            return

        lines = [f"🏟 Схема: {squad.formation}\n"]
        for slot, card_id in sorted(squad.slot_assignments.items()):
            card = await session.get(UserCard, card_id)
            if card:
                r = card.player.overall_rating
                icon = "👑" if r >= 90 else "🌟" if r >= 85 else "⭐"
                lines.append(f"  {slot}: {card.player.name} — {r}{icon}")

    await message.reply("\n".join(lines))
