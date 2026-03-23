"""Команды в групповом чате."""
from aiogram import Router
from aiogram.filters import Command
from aiogram.types import Message
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from bot.db.models import Tournament, UserCard, UserSquad
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
    """Лучшие бомбардиры за всё время."""
    async with AsyncSessionLocal() as session:
        rows = await get_top_scorers(session, limit=10)
    await message.reply(format_scorers(rows, "⚽ Лучшие бомбардиры всех времён"), parse_mode="Markdown")


@router.message(Command("topweek"))
async def cmd_topweek(message: Message) -> None:
    """Лучшие бомбардиры текущего турнира."""
    async with AsyncSessionLocal() as session:
        tournament = await get_or_create_tournament(session)
        rows = await get_top_scorers(session, limit=10, tournament_id=tournament.id)
    await message.reply(format_scorers(rows, "⚽ Лучшие бомбардиры этой недели"), parse_mode="Markdown")


@router.message(Command("topassists"))
async def cmd_topassists(message: Message) -> None:
    """Лучшие ассистенты за всё время."""
    async with AsyncSessionLocal() as session:
        rows = await get_top_assisters(session, limit=10)
    await message.reply(format_scorers(rows, "🎯 Лучшие ассистенты всех времён"), parse_mode="Markdown")


@router.message(Command("myteam"))
async def cmd_myteam(message: Message) -> None:
    """Показать текущий состав (свой или @упомянутого игрока)."""
    user_id = message.from_user.id

    # Если есть упомянутый пользователь в тексте — его команда
    if message.entities:
        for entity in message.entities:
            if entity.type == "mention":
                username = message.text[entity.offset + 1: entity.offset + entity.length]
                # Ищем по username в whitelist — упрощённо просто берём своё
                break

    async with AsyncSessionLocal() as session:
        squad = await session.get(UserSquad, user_id)
        formation = squad.formation if squad else "4-4-2"
        slots = squad.slot_assignments if squad else {}

        if not slots:
            await message.reply("Состав не настроен. Используй /squad в личных сообщениях с ботом.")
            return

        lines = [f"🏟 Схема: {formation}\n"]
        for slot, card_id in sorted(slots.items()):
            card = await session.get(UserCard, card_id)
            if card:
                lines.append(f"  {slot}: {card.player.name} — {card.player.overall_rating} ⭐")

    await message.reply("\n".join(lines))
