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


PACK_MENU = [
    ("weekly",    "еженедельный",        "5 карточек (65-74: 55%, 75-84: 35%, 85+: 10%)"),
    ("special",   "специальный",         "5 карточек (65-74: 30%, 75-84: 35%, 85+: 35%)"),
    ("russia",    "🇷🇺 Россия",          "2 русских игрока (1% Аршавин)"),
    ("brazil",    "🇧🇷 Бразилия",        "2 бразильца"),
    ("turkey",    "🇹🇷 Турция",          "2 турка (1% Арда Туран)"),
    ("saudi",     "🇸🇦 Саудовская лига", "2 игрока Saudi Pro League"),
    ("minirandom","мини-рандом",          "рандомно: russia/brazil/france/england/turkey/saudi"),
    ("morning",   "🌅 утренний",         "4 карточки (до 70: 20%, 70-75: 20%, 76-80: 50%, 81-85: 8%, 86+: 2%)"),
    ("record",      "🏆 Рекорд",           "3 карточки (65-74: 20%, 75-84: 75%, 85+: 5%)"),
    ("consolation",        "🤝 Утешающий",            "2 карточки (70-75: 20%, 76-80: 45%, 81-82: 30%, 83+: 5%)"),
    ("weekly_tournament",  "🏅 Еженедельный турнир",  "2 карточки (65-74: 15%, 75-81: 30%, 82-84: 50%, 85+: 5%)"),
    ("france",    "🇫🇷 Франция",         "2 французских игрока"),
    ("england",   "🏴󠁧󠁢󠁥󠁮󠁧󠁿 Англия",          "2 английских игрока"),
]

PACK_HELP_TEXT = "📦 <b>Типы паков:</b>\n\n" + "\n".join(
    f"<b>{i+1}.</b> {name} — {desc}"
    for i, (_, name, desc) in enumerate(PACK_MENU)
) + "\n\n<i>Выдать пак: /givepack @username &lt;номер&gt;</i>"


@router.message(Command("givepack"))
async def cmd_givepack(message: Message) -> None:
    """Без аргументов — показывает список паков. С аргументами (@username номер) — выдаёт пак."""
    if not is_admin(message.from_user.id):
        return
    if message.message_id in _processed_updates:
        return
    _processed_updates.add(message.message_id)
    if len(_processed_updates) > 1000:
        _processed_updates.clear()

    parts = message.text.split()

    # Без аргументов — показать меню
    if len(parts) == 1:
        await message.reply(PACK_HELP_TEXT, parse_mode="HTML")
        return

    # Нужно минимум: /givepack @username <номер>
    if len(parts) < 3:
        await message.reply(PACK_HELP_TEXT, parse_mode="HTML")
        return

    # Резолвим пользователя: @username или user_id
    user_arg = parts[1]
    target_id: int | None = None

    if user_arg.startswith("@"):
        username_clean = user_arg.lstrip("@")
        async with AsyncSessionLocal() as session:
            from sqlalchemy import select as _select
            from sqlalchemy import func as _func
            result = await session.execute(
                _select(Whitelist).where(_func.lower(Whitelist.username) == username_clean.lower())
            )
            wl = result.scalar_one_or_none()
            if not wl:
                await message.reply(f"❌ Пользователь @{username_clean} не найден в whitelist.")
                return
            target_id = wl.user_id
            display_name = f"@{username_clean}"
    else:
        try:
            target_id = int(user_arg)
            display_name = f"ID{target_id}"
        except ValueError:
            await message.reply("❌ Первый аргумент должен быть @username или user_id.")
            return

    # Резолвим номер пака
    try:
        pack_num = int(parts[2])
    except ValueError:
        await message.reply(PACK_HELP_TEXT, parse_mode="HTML")
        return

    if not (1 <= pack_num <= len(PACK_MENU)):
        await message.reply(f"❌ Номер пака должен быть от 1 до {len(PACK_MENU)}.\n\n" + PACK_HELP_TEXT, parse_mode="HTML")
        return

    pack_type, pack_name, _ = PACK_MENU[pack_num - 1]

    if pack_type == "minirandom":
        import random as _random
        pack_type = _random.choice(["russia", "brazil", "france", "england", "turkey", "saudi"])
        pack_name = f"мини-рандом → {pack_type}"

    async with AsyncSessionLocal() as session:
        await give_pending_pack(session, target_id, pack_type)

    try:
        await message.bot.send_message(
            target_id,
            f"🎴 Тебе выдан пак «{pack_name}»!\n\nОткрой его командой /openpack",
        )
    except Exception:
        pass

    await message.reply(f"✅ Пак «{pack_name}» добавлен в очередь {display_name}.")


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
