"""Команды администратора: управление whitelist и ручные паки."""
from aiogram import Router
from aiogram.filters import Command
from aiogram.types import Message
from sqlalchemy.ext.asyncio import AsyncSession

from bot.config import settings
from bot.db.models import Whitelist
from bot.db.session import AsyncSessionLocal
from bot.services.packs import give_pending_pack

router = Router()

_match_running = False  # защита от двойного запуска
_processed_updates: set[int] = set()  # дедупликация апдейтов


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
            if username:
                existing.username = username
                await session.commit()
                await message.reply(f"✅ Username обновлён: ID {target_id} → @{username}.")
            else:
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
    if message.message_id in _processed_updates:
        return
    _processed_updates.add(message.message_id)
    if len(_processed_updates) > 1000:
        _processed_updates.clear()

    parts = message.text.split()
    if len(parts) < 2:
        await message.reply("Использование: /givepak <user_id> [special]")
        return

    try:
        target_id = int(parts[1])
    except ValueError:
        await message.reply("user_id должен быть числом.")
        return

    valid_types = {"weekly", "special", "russia", "brazil", "turkey", "minirandom", "morning", "saudi", "record"}
    pack_type = parts[2] if len(parts) > 2 and parts[2] in valid_types else "weekly"

    if pack_type == "minirandom":
        import random as _random
        pack_type = _random.choice(["russia", "brazil", "turkey", "saudi"])

    pack_names = {
        "weekly": "еженедельный", "special": "специальный",
        "russia": "🇷🇺 Россия", "brazil": "🇧🇷 Бразилия", "turkey": "🇹🇷 Турция",
        "morning": "🌅 утренний",
        "saudi": "🇸🇦 Саудовская лига",
        "record": "🏆 Рекорд",
    }

    async with AsyncSessionLocal() as session:
        await give_pending_pack(session, target_id, pack_type)

    try:
        await message.bot.send_message(
            target_id,
            f"🎴 Тебе выдан {pack_names[pack_type]} пак!\n\nОткрой его командой /openpack",
        )
    except Exception:
        pass

    await message.reply(f"✅ Пак «{pack_names[pack_type]}» добавлен в очередь игрока ID{target_id}.\n\nДоступные типы: weekly, special, russia, brazil, turkey, minirandom, morning")


@router.message(Command("starttournament"))
async def cmd_starttournament(message: Message) -> None:
    """Создать и запустить новый турнир (только для админа)."""
    if not is_admin(message.from_user.id):
        return

    from bot.db.models import Tournament
    from bot.services.tournament import ensure_matches_created, get_active_tournament

    async with AsyncSessionLocal() as session:
        active = await get_active_tournament(session)
        if active:
            await message.reply("⚠️ Турнир уже активен!")
            return

        t = Tournament(status="running")
        session.add(t)
        await session.commit()
        await session.refresh(t)
        await ensure_matches_created(session, t)

    await message.bot.send_message(
        settings.group_id,
        f"🏆 <b>Новый турнир начался!</b>\n\n"
        "Настройте состав в боте и ждите матчей.\n"
        "Матчи запускаются командой /nextmatch",
        parse_mode="HTML",
    )
    await message.reply(f"✅ Турнир #{t.id} запущен!")


@router.message(Command("nextmatch"))
async def cmd_nextmatch(message: Message) -> None:
    """Запустить следующий матч турнира с LLM-комментарием."""
    from bot.services.tournament import get_active_tournament, play_next_match

    async with AsyncSessionLocal() as session:
        active = await get_active_tournament(session)

    if not active:
        await message.reply("❌ Нет активного турнира. Запустите турнир командой /starttournament.")
        return

    global _match_running
    if _match_running:
        await message.reply("⏳ Матч уже запущен, подожди...")
        return

    _match_running = True
    await message.reply("⚽ Запускаю следующий матч...")
    import asyncio
    try:
        played = await asyncio.wait_for(play_next_match(message.bot, with_commentary=True), timeout=300)
    except asyncio.TimeoutError:
        await message.reply("⚠️ Матч завис (таймаут 5 мин). Попробуй ещё раз.")
        return
    except Exception as e:
        await message.reply(f"❌ Ошибка при симуляции: {e}")
        return
    finally:
        _match_running = False
    if not played:
        await message.reply("Все матчи этой недели уже сыграны!")
