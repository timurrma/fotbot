"""Команды администратора: управление whitelist и ручные паки."""
from aiogram import Router
from aiogram.filters import Command
from aiogram.types import Message
from sqlalchemy.ext.asyncio import AsyncSession

from bot.config import settings
from bot.db.models import Whitelist
from bot.db.session import AsyncSessionLocal
from bot.services.packs import open_pack, send_pack_with_photos

router = Router()


def is_admin(user_id: int) -> bool:
    return user_id == settings.admin_id


@router.message(Command("adduser"))
async def cmd_adduser(message: Message) -> None:
    if not is_admin(message.from_user.id):
        return

    parts = message.text.split()
    if len(parts) < 2:
        await message.reply("Использование: /adduser <user_id> [username]")
        return

    try:
        target_id = int(parts[1])
    except ValueError:
        await message.reply("user_id должен быть числом.")
        return

    username = parts[2] if len(parts) > 2 else None

    async with AsyncSessionLocal() as session:
        existing = await session.get(Whitelist, target_id)
        if existing:
            await message.reply(f"ID {target_id} уже в whitelist.")
            return
        entry = Whitelist(user_id=target_id, username=username)
        session.add(entry)
        await session.commit()

    await message.reply(f"✅ ID {target_id} добавлен в whitelist.")


@router.message(Command("removeuser"))
async def cmd_removeuser(message: Message) -> None:
    if not is_admin(message.from_user.id):
        return

    parts = message.text.split()
    if len(parts) < 2:
        await message.reply("Использование: /removeuser <user_id>")
        return

    try:
        target_id = int(parts[1])
    except ValueError:
        await message.reply("user_id должен быть числом.")
        return

    async with AsyncSessionLocal() as session:
        entry = await session.get(Whitelist, target_id)
        if not entry:
            await message.reply(f"ID {target_id} не в whitelist.")
            return
        await session.delete(entry)
        await session.commit()

    await message.reply(f"✅ ID {target_id} удалён из whitelist.")


@router.message(Command("givepak"))
async def cmd_givepak(message: Message) -> None:
    """Выдать специальный пак игроку вручную. /givepak @username или /givepak user_id [special]"""
    if not is_admin(message.from_user.id):
        return

    parts = message.text.split()
    if len(parts) < 2:
        await message.reply("Использование: /givepak <user_id> [special]")
        return

    try:
        target_id = int(parts[1])
    except ValueError:
        await message.reply("user_id должен быть числом.")
        return

    pack_type = "special" if len(parts) > 2 and parts[2] == "special" else "weekly"

    async with AsyncSessionLocal() as session:
        players = await open_pack(session, target_id, pack_type)

    try:
        chat = await message.bot.get_chat(target_id)
        username = chat.username or chat.full_name or f"ID{target_id}"
    except Exception:
        username = f"ID{target_id}"

    await send_pack_with_photos(message.bot, settings.group_id, username, players, pack_type)
    await message.reply(f"✅ Пак выдан игроку {username}.")


@router.message(Command("starttournament"))
async def cmd_starttournament(message: Message) -> None:
    """Создать и запустить новый турнир (только для админа)."""
    if not is_admin(message.from_user.id):
        return

    from datetime import datetime, timezone
    from bot.db.models import Tournament
    from bot.services.tournament import ensure_matches_created

    now = datetime.now(timezone.utc)
    week = now.isocalendar()[1]
    year = now.year

    async with AsyncSessionLocal() as session:
        from sqlalchemy import select as sa_select
        result = await session.execute(
            sa_select(Tournament).where(
                Tournament.week_number == week,
                Tournament.year == year,
            )
        )
        t = result.scalar_one_or_none()

        if t:
            if t.status == "running":
                await message.reply("⚠️ Турнир уже активен!")
                return
            # Перезапускаем завершённый или ожидающий
            t.status = "running"
        else:
            t = Tournament(week_number=week, year=year, status="running")
            session.add(t)

        await session.commit()
        await session.refresh(t)
        await ensure_matches_created(session, t)

    await message.bot.send_message(
        settings.group_id,
        f"🏆 <b>Турнир недели #{week} начался!</b>\n\n"
        "Настройте состав в боте и ждите матчей.\n"
        "Матчи запускаются командой /nextmatch",
        parse_mode="HTML",
    )
    await message.reply(f"✅ Турнир #{week} запущен!")


@router.message(Command("nextmatch"))
async def cmd_nextmatch(message: Message) -> None:
    """Запустить следующий матч турнира с LLM-комментарием."""
    from bot.services.tournament import get_active_tournament, play_next_match

    async with AsyncSessionLocal() as session:
        active = await get_active_tournament(session)

    if not active:
        await message.reply("❌ Нет активного турнира. Запустите турнир командой /starttournament.")
        return

    await message.reply("⚽ Запускаю следующий матч...")
    played = await play_next_match(message.bot, with_commentary=True)
    if not played:
        await message.reply("Все матчи этой недели уже сыграны!")
