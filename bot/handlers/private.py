"""Команды в личных сообщениях: старт, коллекция, статистика, трансферы, просмотр состава."""
import asyncio
from aiogram import F, Router
from aiogram.filters import Command
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup, Message, WebAppInfo
from sqlalchemy import select

from bot.config import settings
from bot.db.models import UserCard, UserSquad, Whitelist
from bot.db.session import AsyncSessionLocal
from bot.services.packs import has_starter_pack, open_pack, send_pack_with_photos
from bot.services.simulation import FORMATIONS_SLOTS
from bot.services.stats import format_scorers, get_tournament_record, get_user_stats
from bot.services.transfers import (
    accept_transfer,
    create_transfer_offer,
    decline_transfer,
    get_outgoing_offers,
    get_pending_offers,
    get_remaining_transfers,
)

router = Router()


# ─── /start ───────────────────────────────────────────────────────────────────

@router.message(Command("start"))
async def cmd_start(message: Message) -> None:
    user_id = message.from_user.id

    async with AsyncSessionLocal() as session:
        wl = await session.get(Whitelist, user_id)
        if not wl and user_id != settings.admin_id:
            await message.answer(
                "⛔ У тебя нет доступа к боту.\n"
                "Попроси администратора добавить тебя командой /adduser."
            )
            return

        already_has = await has_starter_pack(session, user_id)

        if not already_has:
            await message.answer("👋 Добро пожаловать! Открываю твой стартовый пак...")
            await asyncio.sleep(1)
            players = await open_pack(session, user_id, pack_type="starter")

    if not already_has:
        username = message.from_user.username or message.from_user.full_name
        await send_pack_with_photos(message.bot, message.chat.id, username, players, "starter")
        await send_pack_with_photos(message.bot, settings.group_id, username, players, "starter")
    else:
        await message.answer(
            "👋 С возвращением!\n\n"
            "📋 Команды:\n"
            "/setup — настроить состав (Mini App)\n"
            "/squad — посмотреть состав\n"
            "/mycards — моя коллекция\n"
            "/mystats — моя статистика\n"
            "/transfers — трансферы"
        )


# ─── /squad — просмотр состава ────────────────────────────────────────────────

@router.message(Command("squad"))
async def cmd_squad(message: Message) -> None:
    """Показывает текущий состав пользователя."""
    user_id = message.from_user.id
    async with AsyncSessionLocal() as session:
        squad = await session.get(UserSquad, user_id)
        if not squad or not squad.slot_assignments:
            await message.answer(
                "Состав не настроен.\n"
                "Используй /setup чтобы настроить состав через Mini App."
            )
            return

        formation = squad.formation
        assignments = squad.slot_assignments
        lines = [f"🏟 Схема: <b>{formation}</b>\n"]
        for slot, card_id in sorted(assignments.items()):
            card = await session.get(UserCard, card_id)
            if card:
                r = card.player.overall_rating
                icon = "👑" if r >= 90 else "🌟" if r >= 85 else "⭐"
                lines.append(f"  {slot}: {card.player.name} — {r}{icon}")

    await message.answer("\n".join(lines), parse_mode="HTML")


# ─── /setup — кнопка для Mini App ─────────────────────────────────────────────

@router.message(Command("setup"))
async def cmd_setup(message: Message) -> None:
    """Открывает Mini App для настройки состава."""
    webapp_url = settings.miniapp_url
    keyboard = InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(
            text="⚽ Настроить состав",
            web_app=WebAppInfo(url=webapp_url),
        )
    ]])
    await message.answer(
        "Нажми кнопку ниже чтобы настроить состав и схему:",
        reply_markup=keyboard,
    )


# ─── /mycards ─────────────────────────────────────────────────────────────────

@router.message(Command("mycards"))
async def cmd_mycards(message: Message) -> None:
    user_id = message.from_user.id
    async with AsyncSessionLocal() as session:
        result = await session.execute(
            select(UserCard)
            .where(UserCard.user_id == user_id)
            .join(UserCard.player)
            .order_by(UserCard.player.property.mapper.c.overall_rating.desc())
            .limit(30)
        )
        cards = result.scalars().all()

    if not cards:
        await message.answer("У тебя пока нет карточек. Жди четверга — придёт пак!")
        return

    lines = [f"🃏 Твоя коллекция ({len(cards)} карточек):\n"]
    for c in cards:
        r = c.player.overall_rating
        icon = "👑" if r >= 90 else "🌟" if r >= 85 else "⭐"
        lines.append(f"  #{c.id} {c.player.name} — {r}{icon} ({c.player.position})")

    await message.answer("\n".join(lines))


# ─── /mystats ─────────────────────────────────────────────────────────────────

@router.message(Command("mystats"))
async def cmd_mystats(message: Message) -> None:
    user_id = message.from_user.id
    async with AsyncSessionLocal() as session:
        rows = await get_user_stats(session, user_id, limit=10)
        record = await get_tournament_record(session, user_id)

    record_text = (
        f"🏆 Рекорд: {record['wins']}В {record['draws']}Н {record['losses']}П "
        f"({record['played']} матчей)\n\n"
    )
    await message.answer(record_text + format_scorers(rows, "⚽ Твои бомбардиры"), parse_mode="Markdown")


# ─── /transfers ───────────────────────────────────────────────────────────────

@router.message(Command("transfers"))
async def cmd_transfers(message: Message) -> None:
    user_id = message.from_user.id
    async with AsyncSessionLocal() as session:
        incoming = await get_pending_offers(session, user_id)
        outgoing = await get_outgoing_offers(session, user_id)
        remaining = await get_remaining_transfers(session, user_id)

    lines = [f"🔄 Трансферы (осталось: {remaining}/3)\n"]

    if incoming:
        lines.append("📨 Входящие предложения:")
        for offer in incoming:
            lines.append(
                f"  #{offer.id} от ID{offer.from_user_id}: "
                f"{offer.offer_card.player.name} → {offer.want_card.player.name}"
            )
            lines.append(f"  /accept_{offer.id}  |  /decline_{offer.id}")

    if outgoing:
        lines.append("\n📤 Исходящие предложения:")
        for offer in outgoing:
            lines.append(
                f"  #{offer.id} для ID{offer.to_user_id}: "
                f"{offer.offer_card.player.name} → {offer.want_card.player.name}"
            )

    if not incoming and not outgoing:
        lines.append("Нет активных предложений.\n\nЧтобы предложить обмен: /transfer")

    await message.answer("\n".join(lines))


@router.message(F.text.regexp(r"^/accept_(\d+)$"))
async def cmd_accept(message: Message) -> None:
    offer_id = int(message.text.split("_")[1])
    async with AsyncSessionLocal() as session:
        ok, msg = await accept_transfer(session, offer_id, message.from_user.id)
    await message.answer(f"{'✅' if ok else '❌'} {msg}")


@router.message(F.text.regexp(r"^/decline_(\d+)$"))
async def cmd_decline(message: Message) -> None:
    offer_id = int(message.text.split("_")[1])
    async with AsyncSessionLocal() as session:
        ok, msg = await decline_transfer(session, offer_id, message.from_user.id)
    await message.answer(f"{'✅' if ok else '❌'} {msg}")


@router.message(Command("transfer"))
async def cmd_transfer(message: Message) -> None:
    await message.answer(
        "Чтобы предложить обмен:\n\n"
        "/maketransfer <to_user_id> <своя_card_id> <его_card_id>\n\n"
        "ID карточек смотри в /mycards\n"
        "Пример: /maketransfer 123456789 42 87"
    )


@router.message(Command("maketransfer"))
async def cmd_maketransfer(message: Message) -> None:
    parts = message.text.split()
    if len(parts) != 4:
        await message.answer("Использование: /maketransfer <to_user_id> <своя_card_id> <его_card_id>")
        return

    try:
        to_user_id = int(parts[1])
        offer_card_id = int(parts[2])
        want_card_id = int(parts[3])
    except ValueError:
        await message.answer("Все параметры должны быть числами.")
        return

    async with AsyncSessionLocal() as session:
        ok, result = await create_transfer_offer(
            session,
            from_user_id=message.from_user.id,
            to_user_id=to_user_id,
            offer_card_id=offer_card_id,
            want_card_id=want_card_id,
        )

    if ok:
        await message.answer(f"✅ Предложение #{result} отправлено!")
        try:
            await message.bot.send_message(
                to_user_id,
                f"📨 Новое предложение обмена от ID{message.from_user.id}!\n"
                f"Используй /transfers чтобы принять или отклонить."
            )
        except Exception:
            pass
    else:
        await message.answer(f"❌ {result}")
